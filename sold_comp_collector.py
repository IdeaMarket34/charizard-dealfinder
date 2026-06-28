import os
import requests
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
SOLD_PROVIDER = os.environ.get("SOLD_PROVIDER", "soldcomps")
SOLD_BATCH_SIZE = int(os.environ.get("SOLD_BATCH_SIZE", "10"))
# Primary call-budget guard: skip targets fetched successfully within this window.
# At 5 targets and 24h cooldown: ~150 calls/month (well within 2,000/month paid tier).
# Lower this value (e.g. 12) to increase freshness at the cost of more calls.
SOLD_MIN_HOURS_BETWEEN_RUNS = int(os.environ.get("SOLD_MIN_HOURS_BETWEEN_RUNS", "24"))

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_targets(limit: int = SOLD_BATCH_SIZE) -> List[dict]:
    """
    Load enabled targets that haven't been successfully fetched within the
    SOLD_MIN_HOURS_BETWEEN_RUNS window. Ordered by priority then staleness.
    This is the primary mechanism for staying within the monthly API call budget.
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(hours=SOLD_MIN_HOURS_BETWEEN_RUNS)
    ).isoformat()
    result = (
        supabase.table("sold_search_targets")
        .select("id,query_text,priority,pokemon_card_id,normalized_item_key,last_success_at")
        .eq("enabled", True)
        .or_(f"last_success_at.is.null,last_success_at.lt.{cutoff}")
        .order("priority", desc=False)
        .order("last_run_at", desc=False)
        .limit(limit)
        .execute()
    )
    return result.data or []


def count_enabled_targets() -> int:
    """Return total count of enabled targets (for burn rate logging)."""
    result = (
        supabase.table("sold_search_targets")
        .select("id", count="exact")
        .eq("enabled", True)
        .execute()
    )
    return result.count or 0


def fetch_sold_results(provider: str, query_text: str, limit: int = 100) -> List[Dict]:
    if provider == "soldcomps":
        return fetch_sold_results_soldcomps(query_text, limit=limit)
    raise ValueError(f"unsupported sold provider: {provider}")


def fetch_sold_results_soldcomps(query_text: str, limit: int = 100) -> List[Dict]:
    api_key = os.environ["SOLDCOMPS_API_KEY"]

    response = requests.get(
        "https://api.sold-comps.com/v1/scrape",
        headers={
            "Authorization": f"Bearer {api_key}",
        },
        params={
            "keyword": query_text,
            "page": 1,
            "count": min(limit, 240),
            "daysToScrape": 30,
            "ebaySite": "ebay.com",
            "sortOrder": "endedRecently",
        },
        timeout=60,
    )

    print({
        "status_code": response.status_code,
        "url": response.url,
    })

    response.raise_for_status()
    data = response.json()

    items = data.get("items", [])
    normalized: List[Dict] = []

    for item in items:
        record_id = item.get("itemId") or item.get("url")
        if not record_id:
            continue

        normalized.append({
            "provider": "soldcomps",
            "provider_record_id": str(record_id),
            "title": item.get("title") or "",
            "item_web_url": item.get("url"),
            "sold_at": item.get("endedAt"),
            "sold_price_value": item.get("soldPrice"),
            "sold_price_currency": item.get("soldCurrency") or "USD",
            "shipping_value": item.get("shippingPrice"),
            "condition_text": item.get("condition"),
            "listing_format": None,
            "seller_name": item.get("sellerUsername"),
            "quantity_sold": None,
            "search_query": query_text,
            "raw_json": item,
        })

    return normalized


def insert_raw_rows(rows: List[Dict]) -> int:
    """
    Batch upsert raw rows into sold_comps_raw.
    Uses chunked batches of 100 instead of one DB call per row.
    """
    if not rows:
        return 0

    payloads = [
        {
            "provider": row["provider"],
            "provider_record_id": row["provider_record_id"],
            "search_query": row["search_query"],
            "title": row["title"],
            "item_web_url": row.get("item_web_url"),
            "sold_at": row.get("sold_at"),
            "sold_price_value": row.get("sold_price_value"),
            "sold_price_currency": row.get("sold_price_currency"),
            "shipping_value": row.get("shipping_value"),
            "condition_text": row.get("condition_text"),
            "listing_format": row.get("listing_format"),
            "seller_name": row.get("seller_name"),
            "quantity_sold": row.get("quantity_sold"),
            "raw_json": row.get("raw_json") or {},
        }
        for row in rows
    ]

    inserted = 0
    chunk_size = 100
    for i in range(0, len(payloads), chunk_size):
        chunk = payloads[i : i + chunk_size]
        result = (
            supabase.table("sold_comps_raw")
            .upsert(chunk, on_conflict="provider,provider_record_id")
            .execute()
        )
        if result.data:
            inserted += len(result.data)

    return inserted


def mark_target_success(target_id: int, result_count: int) -> None:
    now = utc_now()
    supabase.table("sold_search_targets").update({
        "last_run_at": now,
        "last_success_at": now,
        "last_result_count": result_count,
        "last_error": None,
        "updated_at": now,
    }).eq("id", target_id).execute()


def mark_target_error(target_id: int, error_text: str) -> None:
    now = utc_now()
    supabase.table("sold_search_targets").update({
        "last_run_at": now,
        "last_error": error_text[:1000],
        "updated_at": now,
    }).eq("id", target_id).execute()


def main() -> None:
    total_enabled = count_enabled_targets()
    targets = load_targets()

    # Startup log: shows config and how many targets are eligible vs total.
    # Monthly burn estimate: eligible_this_run * (720h / min_hours_between_runs)
    estimated_monthly_calls = total_enabled * (720 // max(SOLD_MIN_HOURS_BETWEEN_RUNS, 1))
    print({
        "startup": True,
        "provider": SOLD_PROVIDER,
        "batch_size": SOLD_BATCH_SIZE,
        "min_hours_between_runs": SOLD_MIN_HOURS_BETWEEN_RUNS,
        "total_enabled_targets": total_enabled,
        "eligible_this_run": len(targets),
        "estimated_monthly_calls": estimated_monthly_calls,
    })

    calls_made = 0
    calls_skipped = total_enabled - len(targets)

    for target in targets:
        try:
            rows = fetch_sold_results(SOLD_PROVIDER, target["query_text"], limit=100)
            inserted = insert_raw_rows(rows)
            mark_target_success(target["id"], len(rows))
            calls_made += 1
            print({
                "target_id": target["id"],
                "query_text": target["query_text"],
                "results": len(rows),
                "inserted": inserted,
            })
        except Exception as e:
            mark_target_error(target["id"], str(e))
            print({
                "target_id": target["id"],
                "query_text": target["query_text"],
                "error": str(e),
            })

    print({
        "run_complete": True,
        "calls_made": calls_made,
        "calls_skipped_recent": calls_skipped,
        "estimated_monthly_calls": estimated_monthly_calls,
    })


if __name__ == "__main__":
    main()