"""
One-off backfill: re-check every currently-"active" market_listings row
against eBay's item detail endpoint and flip sold/ended ones to
listing_status="ended" right away, instead of waiting for
lifecycle_refresh_worker.py to cycle through them naturally (which could
take a long time across ~10k+ rows at its normal stale-refresh pace).

Reuses the exact same fetch/parse/ended-detection logic from
detail_fetch_worker.py (including the session #39 404-handling fix) so the
backfill behaves identically to the live pipeline — this is just running
that logic against the whole active backlog in one pass instead of waiting
for the 12-hour staleness window.

Respects eBay's daily call quota: pass --max-calls to cap how many API
calls this run makes, and --start-after to resume from where a previous
run left off (rows are processed ordered by id, ascending). Designed to be
run multiple times across multiple days if the active backlog is large.

Usage:
    python3 backfill_sold_listings.py --max-calls 1500
    python3 backfill_sold_listings.py --max-calls 1500 --start-after 48213
"""

import argparse
import importlib.util
import time
from pathlib import Path
from typing import List, Optional

BASE_DIR = Path(__file__).resolve().parent
WORKER_PATH = BASE_DIR / "detail_fetch_worker.py"


def load_worker_module():
    spec = importlib.util.spec_from_file_location("detail_fetch_worker_runtime", WORKER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load module spec from {WORKER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


worker = load_worker_module()
supabase = worker.supabase
# Backfill reuses detail_fetch_worker's fetch_item_detail (and therefore its
# shared-budget logging) directly. Override the tag so calls made by this
# script show up correctly in ebay_api_call_log instead of being misattributed
# to "detail_fetch_worker".
worker.SCRIPT_NAME = "backfill_sold_listings"


def get_active_ebay_listings(start_after: int, batch_size: int) -> List[dict]:
    result = (
        supabase.table("market_listings")
        .select("id,source,source_listing_id")
        .eq("source", "ebay")
        .eq("listing_status", "active")
        .gt("id", start_after)
        .order("id", desc=False)
        .limit(batch_size)
        .execute()
    )
    return result.data or []


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-calls", type=int, default=1500, help="max eBay API calls this run")
    parser.add_argument("--start-after", type=int, default=0, help="resume after this market_listings.id")
    parser.add_argument("--batch-size", type=int, default=200, help="rows to pull from DB per page")
    parser.add_argument("--sleep-seconds", type=float, default=0.2, help="delay between calls to be polite to eBay")
    args = parser.parse_args()

    access_token = worker.get_ebay_access_token()

    calls_made = 0
    ended_count = 0
    updated_count = 0
    failed_count = 0
    last_id = args.start_after

    while calls_made < args.max_calls:
        remaining_budget = args.max_calls - calls_made
        page = get_active_ebay_listings(last_id, min(args.batch_size, remaining_budget))
        if not page:
            break

        for row in page:
            if calls_made >= args.max_calls:
                break

            source = row["source"]
            source_listing_id = row["source_listing_id"]
            last_id = row["id"]

            try:
                detail = worker.fetch_item_detail(access_token, source_listing_id)
                calls_made += 1
                patch = worker.build_market_listing_patch(detail)
                worker.update_market_listing(source, source_listing_id, patch)
                updated_count += 1
                if patch.get("listing_status") == "ended":
                    ended_count += 1
            except worker.ItemNotFoundError:
                calls_made += 1
                worker.mark_listing_ended(source, source_listing_id)
                ended_count += 1
            except worker.WorkerRateLimitError as exc:
                worker.log(f"Rate limited at id={last_id}; stopping run early. Resume with --start-after {last_id - 1}")
                print({
                    "stopped_early": True,
                    "reason": "rate_limited",
                    "resume_after_id": last_id - 1,
                    "calls_made": calls_made,
                    "ended_count": ended_count,
                    "updated_count": updated_count,
                    "failed_count": failed_count,
                })
                return
            except worker.SharedBudgetExceeded as exc:
                # Shared cross-script daily budget reached (see session #40)
                # — not an eBay-side 429, just stopping proactively before
                # making another real call. Resume tomorrow (UTC).
                worker.log(
                    f"Shared eBay daily budget reached ({exc.calls_today}/{worker.EBAY_SHARED_DAILY_CAP}) "
                    f"at id={last_id}; stopping run early. Resume with --start-after {last_id - 1}"
                )
                print({
                    "stopped_early": True,
                    "reason": "shared_budget_exceeded",
                    "calls_today_all_scripts": exc.calls_today,
                    "resume_after_id": last_id - 1,
                    "calls_made": calls_made,
                    "ended_count": ended_count,
                    "updated_count": updated_count,
                    "failed_count": failed_count,
                })
                return
            except Exception as exc:
                calls_made += 1
                failed_count += 1
                worker.log(f"backfill failed for {source_listing_id}: {exc}")

            if calls_made % 25 == 0:
                worker.log(
                    f"progress: {calls_made}/{args.max_calls} calls | "
                    f"ended={ended_count} updated={updated_count} failed={failed_count} | "
                    f"last_id={last_id}"
                )

            time.sleep(args.sleep_seconds)

    print({
        "calls_made": calls_made,
        "ended_count": ended_count,
        "updated_count": updated_count,
        "failed_count": failed_count,
        "last_id_processed": last_id,
        "note": (
            f"If there are more active rows left to check, resume with: "
            f"--start-after {last_id}"
        ),
    })


if __name__ == "__main__":
    main()