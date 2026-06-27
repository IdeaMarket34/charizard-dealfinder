"""
archive_raw_events.py (v2)

CHANGED FROM v1: eligibility is no longer based on age alone. Rows are
exported/deleted once they've ACTUALLY been processed downstream:

  - 'detail' events: eligible once a matching listing_history row exists
    (i.e. parser_worker has consumed it). Every detail event becomes its
    own listing_history row on purpose (that's price history over time),
    so detail events are never "superseded" -- only "processed".

  - 'summary' events: eligible once a NEWER summary event exists for the
    same listing. discovery_collector.py's dedupe check
    (get_latest_event_hashes) only ever needs the latest summary event
    per listing, so once a listing has a newer summary row, the older
    one is just history nothing reads again.

  - FORCE_ARCHIVE_AFTER_DAYS (default 30): a backstop, not the primary
    mechanism. Rows older than this get archived/deleted regardless of
    the above, so permanently stuck rows (e.g. a job that exhausted all
    its retries and will never get parsed) don't sit forever. NOTE: this
    means a stuck row IS allowed to be deleted before it's ever
    processed, once it's this old -- that's an intentional tradeoff
    (better than holding space forever for something that's never going
    to complete), but worth knowing.

Still gzipped NDJSON to B2, still upload-confirmed (head_object) before
any delete, still defaults to DRY_RUN=true.

REQUIRES: run 9_add_archived_to_b2_column.sql first (adds the
archived_to_b2_at column this script reads/writes).

Required environment variables:
    DATABASE_URL
    B2_ENDPOINT, B2_KEY_ID, B2_APPLICATION_KEY, B2_BUCKET
    DRY_RUN                    optional, default "true"
    BATCH_SIZE                 optional, default 2000 (rows per B2 file)
    MAX_BATCHES_PER_RUN        optional, default 25 (per phase, per run)
    FORCE_ARCHIVE_AFTER_DAYS   optional, default 30 (replaces the old
                                ARCHIVE_THRESHOLD_DAYS=90 -- if you had
                                that set anywhere, it's no longer read)
"""

import os
import sys
import json
import gzip
import io
import logging
import uuid
from datetime import date, datetime, timezone, timedelta

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
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "2000"))
MAX_BATCHES_PER_RUN = int(os.environ.get("MAX_BATCHES_PER_RUN", "25"))
FORCE_ARCHIVE_AFTER_DAYS = int(os.environ.get("FORCE_ARCHIVE_AFTER_DAYS", "30"))

EXPORT_COLUMNS = [
    "id", "source", "source_listing_id", "event_type", "observed_at",
    "search_plan_id", "search_run_id", "payload_hash", "payload_json",
    "created_at",
]

RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


def get_b2_client():
    return boto3.client(
        "s3",
        endpoint_url=B2_ENDPOINT,
        aws_access_key_id=B2_KEY_ID,
        aws_secret_access_key=B2_APPLICATION_KEY,
    )


def force_cutoff():
    return date.today() - timedelta(days=FORCE_ARCHIVE_AFTER_DAYS)


def get_eligible_detail_batch(conn, limit):
    """'detail' events already parsed into listing_history (parser_worker
    has consumed them), OR older than the force cutoff regardless."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT {", ".join(EXPORT_COLUMNS)}
            FROM raw_market_events rme
            WHERE event_type = 'detail'
              AND archived_to_b2_at IS NULL
              AND (
                EXISTS (
                    SELECT 1 FROM listing_history lh
                    WHERE lh.source = rme.source
                      AND lh.source_listing_id = rme.source_listing_id
                      AND lh.observed_at = rme.observed_at
                )
                OR rme.created_at::date < %s
              )
            ORDER BY rme.created_at
            LIMIT %s
            """,
            (force_cutoff(), limit),
        )
        return cur.fetchall()


def get_eligible_summary_batch(conn, limit):
    """'summary' events superseded by a newer summary event for the same
    listing (discovery_collector only ever needs the latest one per
    listing for its dedupe check), OR older than the force cutoff."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT {", ".join(EXPORT_COLUMNS)}
            FROM raw_market_events rme
            WHERE event_type = 'summary'
              AND archived_to_b2_at IS NULL
              AND (
                EXISTS (
                    SELECT 1 FROM raw_market_events newer
                    WHERE newer.event_type = 'summary'
                      AND newer.source = rme.source
                      AND newer.source_listing_id = rme.source_listing_id
                      AND newer.observed_at > rme.observed_at
                )
                OR rme.created_at::date < %s
              )
            ORDER BY rme.created_at
            LIMIT %s
            """,
            (force_cutoff(), limit),
        )
        return cur.fetchall()


def rows_to_gzipped_ndjson(rows):
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        for row in rows:
            gz.write((json.dumps(dict(row), default=str) + "\n").encode("utf-8"))
    buf.seek(0)
    return buf


def export_and_mark_batch(conn, b2, rows, label, batch_num):
    if not rows:
        return 0

    key = (
        f"raw_market_events/processed/{date.today().isoformat()}/"
        f"{label}-{RUN_ID}-batch{batch_num:04d}.jsonl.gz"
    )
    body = rows_to_gzipped_ndjson(rows)

    log.info("Uploading %d %s rows to b2://%s/%s", len(rows), label, B2_BUCKET, key)
    b2.upload_fileobj(body, B2_BUCKET, key)

    # Confirm the object actually landed before we trust it
    head = b2.head_object(Bucket=B2_BUCKET, Key=key)
    if head["ContentLength"] == 0:
        raise RuntimeError(f"Uploaded object {key} is empty, refusing to mark as archived")

    ids = [row["id"] for row in rows]
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE raw_market_events SET archived_to_b2_at = %s WHERE id = ANY(%s::uuid[])",
            (datetime.now(timezone.utc), ids),
        )
    conn.commit()
    log.info("Marked %d %s rows as archived_to_b2_at (key=%s)", len(rows), label, key)
    return len(rows)


def delete_archived_batch(conn, limit):
    """Delete rows that are already confirmed-uploaded (archived_to_b2_at
    set) but still sitting in Postgres -- covers both rows just marked
    this run AND any leftovers from a previous dry run."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM raw_market_events WHERE archived_to_b2_at IS NOT NULL LIMIT %s",
            (limit,),
        )
        ids = [row[0] for row in cur.fetchall()]

    if not ids:
        return 0

    if DRY_RUN:
        log.info("DRY_RUN=true: would delete %d already-archived rows (skipping)", len(ids))
        return 0

    with conn.cursor() as cur:
        cur.execute("DELETE FROM raw_market_events WHERE id = ANY(%s::uuid[])", (ids,))
    conn.commit()
    log.info("Deleted %d already-archived rows from raw_market_events", len(ids))
    return len(ids)


def run_export_phase(conn, b2, fetch_fn, label):
    total = 0
    for batch_num in range(MAX_BATCHES_PER_RUN):
        rows = fetch_fn(conn, BATCH_SIZE)
        if not rows:
            break
        total += export_and_mark_batch(conn, b2, rows, label, batch_num)
    return total


def main():
    log.info(
        "Starting archive run %s. DRY_RUN=%s, BATCH_SIZE=%d, FORCE_ARCHIVE_AFTER_DAYS=%d",
        RUN_ID, DRY_RUN, BATCH_SIZE, FORCE_ARCHIVE_AFTER_DAYS,
    )

    conn = psycopg2.connect(DATABASE_URL)
    b2 = get_b2_client()

    try:
        detail_exported = run_export_phase(conn, b2, get_eligible_detail_batch, "detail")
        summary_exported = run_export_phase(conn, b2, get_eligible_summary_batch, "summary")

        log.info(
            "Export phase complete. detail_exported=%d summary_exported=%d",
            detail_exported, summary_exported,
        )

        total_deleted = 0
        for _ in range(MAX_BATCHES_PER_RUN):
            deleted = delete_archived_batch(conn, BATCH_SIZE)
            total_deleted += deleted
            if deleted == 0:
                break

        log.info(
            "Archive run complete. detail_exported=%d summary_exported=%d deleted=%d dry_run=%s",
            detail_exported, summary_exported, total_deleted, DRY_RUN,
        )
    except Exception:
        conn.rollback()
        log.exception("Archive run failed, rolled back current transaction.")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()