"""
archive_raw_events.py

Exports raw_market_events rows older than ARCHIVE_THRESHOLD_DAYS to
Backblaze B2 (one gzipped NDJSON file per calendar day), then deletes
those rows from Postgres -- but ONLY after the upload is confirmed
written, and ONLY for days that aren't already in the archive log.

Safety:
- DRY_RUN=true (default) exports and logs but never deletes from Postgres.
  Set DRY_RUN=false only after confirming:
    1. 0_verify_payload_duplication.sql showed 0 (unrelated table, but
       same "don't delete until verified" principle applies here)
    2. 2_check_fk_references.sql showed no rows referencing
       raw_market_events.id, OR you've added handling for any that exist
    3. You've run this once in dry-run mode and spot-checked the
       uploaded files in B2 against the source rows

Resumable: re-running is always safe. Already-archived days are skipped
(tracked in raw_market_events_archive_log). If the script crashes
mid-run, just run it again.

Required environment variables:
    DATABASE_URL        Postgres connection string (same one the other
                         worker scripts use)
    B2_ENDPOINT          e.g. https://s3.us-west-004.backblazeb2.com
    B2_KEY_ID             Backblaze application key ID
    B2_APPLICATION_KEY    Backblaze application key secret
    B2_BUCKET             bucket name
    ARCHIVE_THRESHOLD_DAYS  optional, default 90
    DRY_RUN               optional, default "true"
"""

import os
import sys
import json
import gzip
import io
import logging
from datetime import date, timedelta

import boto3
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("archive_raw_events")

DATABASE_URL = os.environ["DATABASE_URL"]
B2_ENDPOINT = os.environ["B2_ENDPOINT"]
B2_KEY_ID = os.environ["B2_KEY_ID"]
B2_APPLICATION_KEY = os.environ["B2_APPLICATION_KEY"]
B2_BUCKET = os.environ["B2_BUCKET"]
THRESHOLD_DAYS = int(os.environ.get("ARCHIVE_THRESHOLD_DAYS", "90"))
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"

BATCH_TABLE_COLUMNS = [
    "id", "source", "source_listing_id", "event_type", "observed_at",
    "search_plan_id", "search_run_id", "payload_hash", "payload_json",
    "created_at",
]


def get_b2_client():
    return boto3.client(
        "s3",
        endpoint_url=B2_ENDPOINT,
        aws_access_key_id=B2_KEY_ID,
        aws_secret_access_key=B2_APPLICATION_KEY,
    )


def get_days_needing_export(conn, cutoff_date):
    """Days older than cutoff that have never been uploaded to B2 yet."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT created_at::date AS d
            FROM raw_market_events
            WHERE created_at::date < %s
              AND created_at::date NOT IN (SELECT archive_date FROM raw_market_events_archive_log)
            ORDER BY d
            """,
            (cutoff_date,),
        )
        return [row[0] for row in cur.fetchall()]


def get_days_needing_delete(conn, cutoff_date):
    """Days older than cutoff that are ALREADY archived in B2 (per the log)
    but still have rows sitting in raw_market_events -- e.g. because a
    previous run happened during DRY_RUN. These need to be revisited so
    the delete actually happens once DRY_RUN is off, instead of being
    silently skipped forever just because they have a log entry."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT rme.created_at::date AS d
            FROM raw_market_events rme
            JOIN raw_market_events_archive_log log
              ON log.archive_date = rme.created_at::date
            WHERE rme.created_at::date < %s
            ORDER BY d
            """,
            (cutoff_date,),
        )
        return [row[0] for row in cur.fetchall()]


def export_day(conn, day):
    """Fetch all rows for a given day, return list of dict rows."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT {", ".join(BATCH_TABLE_COLUMNS)}
            FROM raw_market_events
            WHERE created_at::date = %s
            ORDER BY created_at
            """,
            (day,),
        )
        return cur.fetchall()


def rows_to_gzipped_ndjson(rows):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        for row in rows:
            # default=str handles datetimes, UUIDs, etc.
            gz.write((json.dumps(dict(row), default=str) + "\n").encode("utf-8"))
    buf.seek(0)
    return buf


def b2_key_for_day(day):
    return f"raw_market_events/{day.year:04d}/{day.month:02d}/{day.isoformat()}.jsonl.gz"


def archive_one_day(conn, b2, day):
    rows = export_day(conn, day)
    row_count = len(rows)
    if row_count == 0:
        log.info("No rows for %s, marking as archived with 0 rows.", day)
        mark_archived(conn, day, 0, None)
        return

    key = b2_key_for_day(day)
    body = rows_to_gzipped_ndjson(rows)

    log.info("Uploading %d rows for %s to b2://%s/%s", row_count, day, B2_BUCKET, key)
    b2.upload_fileobj(body, B2_BUCKET, key)

    # Confirm the object actually landed before we trust it
    head = b2.head_object(Bucket=B2_BUCKET, Key=key)
    if head["ContentLength"] == 0:
        raise RuntimeError(f"Uploaded object {key} is empty, refusing to mark as archived")

    mark_archived(conn, day, row_count, key)
    delete_day_if_live(conn, day, row_count)
    conn.commit()


def delete_day_if_live(conn, day, row_count):
    """Delete a day's rows from Postgres, but only if DRY_RUN is off.
    Safe to call for a day that's already archived but not yet deleted
    (e.g. left over from an earlier dry run)."""
    if DRY_RUN:
        log.info("DRY_RUN=true: skipping delete for %s (%d rows would be deleted)", day, row_count)
    else:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM raw_market_events WHERE created_at::date = %s",
                (day,),
            )
        log.info("Deleted %d rows for %s from raw_market_events", row_count, day)


def mark_archived(conn, day, row_count, key):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO raw_market_events_archive_log (archive_date, row_count, b2_key)
            VALUES (%s, %s, %s)
            ON CONFLICT (archive_date) DO NOTHING
            """,
            (day, row_count, key or "no-rows"),
        )


def main():
    cutoff_date = date.today() - timedelta(days=THRESHOLD_DAYS)
    log.info(
        "Starting archive run. Cutoff date: %s. DRY_RUN=%s",
        cutoff_date, DRY_RUN,
    )

    conn = psycopg2.connect(DATABASE_URL)
    b2 = get_b2_client()

    try:
        export_days = get_days_needing_export(conn, cutoff_date)
        delete_days = get_days_needing_delete(conn, cutoff_date)

        if not export_days and not delete_days:
            log.info("Nothing to archive or delete. All eligible days already processed.")
            return

        if export_days:
            log.info(
                "Found %d day(s) needing export: %s ... %s",
                len(export_days), export_days[0], export_days[-1],
            )
            for day in export_days:
                archive_one_day(conn, b2, day)

        if delete_days:
            log.info(
                "Found %d day(s) already archived but still present in Postgres "
                "(likely from a prior dry run): %s ... %s",
                len(delete_days), delete_days[0], delete_days[-1],
            )
            for day in delete_days:
                rows = export_day(conn, day)
                delete_day_if_live(conn, day, len(rows))
                conn.commit()

        log.info("Archive run complete.")
    except Exception:
        conn.rollback()
        log.exception("Archive run failed, rolled back current transaction.")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()