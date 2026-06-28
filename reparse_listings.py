"""
reparse_listings.py
===================

Re-runs your CURRENT parser + matcher over every listing you've already captured,
using the raw eBay data stored in market_listings.raw_payload. It makes NO eBay
API calls -- it only reads data you already have and re-derives the parsed/matched
results against your (now rebuilt) reference catalog.

WHY THIS EXISTS
---------------
Most of your existing parse rows are stale -- they came from an old import and an
older version of the parser. This tool brings every listing up to date with the
current parser and the new catalog, which is what actually turns the catalog fix
into matches. It's also permanent infrastructure: any time you improve the parser
or the catalog, you re-run this to re-derive everything from your raw data.

This embodies a core principle of the whole system:
  raw captured data is kept forever; everything downstream is recomputable.

SAFETY
------
Default is --dry-run: it computes what WOULD change and prints a summary, without
writing to listing_parses or listing_card_matches. (Note: resolving a card's set
can create a missing set row -- that's a harmless, idempotent part of normal
parsing -- but your parse and match tables are left untouched in dry-run.)

USAGE
-----
  # Safe preview over a small sample first:
  python3 reparse_listings.py --dry-run --limit 200

  # Safe preview over everything:
  python3 reparse_listings.py --dry-run

  # For real, once the preview looks right:
  python3 reparse_listings.py --apply
"""

import argparse
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv, find_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
CWD = Path.cwd()

# Load .env from wherever it actually is: the current directory, next to this
# script, or any parent of either. Prevents the "credentials not found" wall when
# you run from a different folder than the one holding .env.
_loaded_from = None
for _cand in [find_dotenv(usecwd=True), str(SCRIPT_DIR / ".env"),
              find_dotenv(str(SCRIPT_DIR / ".env"))]:
    if _cand and Path(_cand).is_file():
        load_dotenv(_cand, override=False)
        _loaded_from = _cand
        break
print(f"Loaded environment from {_loaded_from}" if _loaded_from
      else "Note: no .env file found; relying on shell environment variables.")

# Reuse your real parser/matcher and its configured Supabase client, so this tool
# always stays in sync with the logic that runs in production. Look next to this
# script first, then in the current directory.
REWRITE_PATH = SCRIPT_DIR / "charizard_ingest_rewrite.py"
if not REWRITE_PATH.is_file():
    REWRITE_PATH = CWD / "charizard_ingest_rewrite.py"
BASE_DIR = REWRITE_PATH.parent


def load_rewrite():
    spec = importlib.util.spec_from_file_location("charizard_ingest_rewrite_runtime", str(REWRITE_PATH))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {REWRITE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rewrite = load_rewrite()
supabase = rewrite.supabase
map_item_to_bundle = rewrite.map_item_to_bundle


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_listings_page(after_id: int, page_size: int) -> List[dict]:
    """Pull a page of listings ordered by id, so we can walk the whole table."""
    result = (
        supabase.table("market_listings")
        .select("id,source,source_listing_id,raw_payload,raw_title")
        .gt("id", after_id)
        .order("id", desc=False)
        .limit(page_size)
        .execute()
    )
    return result.data or []


def upsert_parse(market_listing_id: int, bundle: dict) -> None:
    parse_row = dict(bundle["listing_parse_row"])
    parse_row["market_listing_id"] = market_listing_id
    # carry the resolved set id (FK) through from the parser notes when available
    notes = parse_row.get("parser_notes") or {}
    if notes.get("resolved_set_id"):
        parse_row["set_id"] = notes["resolved_set_id"]
    supabase.table("listing_parses").upsert(parse_row, on_conflict="market_listing_id").execute()


def upsert_card_match(market_listing_id: int, bundle: dict) -> None:
    parse_row = bundle["listing_parse_row"]
    matched_card_id = parse_row.get("matched_card_id")
    if not matched_card_id:
        return
    evidence = {
        "normalized_item_key": parse_row.get("normalized_item_key"),
        "set_code": parse_row.get("set_code"),
        "card_number": parse_row.get("card_number"),
        "promo_code": parse_row.get("promo_code"),
    }
    supabase.table("listing_card_matches").upsert({
        "market_listing_id": market_listing_id,
        "pokemon_card_id": matched_card_id,
        "match_method": "reparse_listings",
        "match_confidence": parse_row.get("match_confidence"),
        "evidence_json": evidence,
        "updated_at": utc_now(),
    }, on_conflict="market_listing_id").execute()


def main() -> None:
    ap = argparse.ArgumentParser(description="Re-parse all stored listings against the current catalog.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Preview only; don't write parses/matches (default).")
    mode.add_argument("--apply", action="store_true", help="Write updated parses and matches.")
    ap.add_argument("--limit", type=int, default=None, help="Only process the first N listings (for testing).")
    ap.add_argument("--page-size", type=int, default=200, help="DB page size.")
    args = ap.parse_args()
    apply = args.apply
    if not apply:
        args.dry_run = True

    print(f"Mode: {'APPLY (writing)' if apply else 'DRY-RUN (no parse/match writes)'}")

    stats = {
        "processed": 0,
        "charizard": 0,
        "junk": 0,
        "matched": 0,
        "with_set": 0,
        "with_number_or_promo": 0,
        "errors": 0,
    }
    failures: List[str] = []
    after_id = 0

    while True:
        rows = fetch_listings_page(after_id, args.page_size)
        if not rows:
            break

        for row in rows:
            after_id = row["id"]
            if args.limit and stats["processed"] >= args.limit:
                rows = []
                break

            payload = row.get("raw_payload") or {}
            if not payload:
                continue

            try:
                bundle = map_item_to_bundle(payload, payload)
                parse = bundle["listing_parse_row"]
                stats["processed"] += 1
                if parse.get("pokemon_name") == "charizard":
                    stats["charizard"] += 1
                if parse.get("is_junk"):
                    stats["junk"] += 1
                if parse.get("set_code"):
                    stats["with_set"] += 1
                if parse.get("card_number") or parse.get("promo_code"):
                    stats["with_number_or_promo"] += 1
                if parse.get("matched_card_id"):
                    stats["matched"] += 1

                if apply:
                    upsert_parse(row["id"], bundle)
                    upsert_card_match(row["id"], bundle)

            except Exception as e:
                stats["errors"] += 1
                if len(failures) < 10:
                    failures.append(f"id={row['id']} ({row.get('raw_title','')[:40]}): {e}")

            if stats["processed"] % 250 == 0 and stats["processed"]:
                print(f"  ...processed {stats['processed']:,} "
                      f"(matched so far: {stats['matched']:,})")

        if args.limit and stats["processed"] >= args.limit:
            break
        if not rows:
            break

    print("\n================ RE-PARSE SUMMARY ================")
    print(f"Listings processed:           {stats['processed']:,}")
    print(f"  identified as Charizard:    {stats['charizard']:,}")
    print(f"  flagged as junk/non-single: {stats['junk']:,}")
    print(f"  with a set detected:        {stats['with_set']:,}")
    print(f"  with a number or promo:     {stats['with_number_or_promo']:,}")
    print(f"  >>> MATCHED to a card:      {stats['matched']:,}")
    print(f"  errors:                     {stats['errors']:,}")
    if failures:
        print("\nFirst few errors:")
        for f in failures:
            print("  ", f)
    if not apply:
        print("\nDRY-RUN: nothing was written to listing_parses or listing_card_matches.")
        print("If the matched count looks good, re-run with --apply.")
    print("=================================================")


if __name__ == "__main__":
    main()