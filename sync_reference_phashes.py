#!/usr/bin/env python3
"""
sync_reference_phashes.py — Charizard DealFinder

One-off / re-runnable local script. Reads the local reference image manifest
(built by build_reference_image_library.py, session #29) and upserts the
phash + provenance fields into the `reference_phashes` Supabase table.

This is the ONLY place that needs to read the local manifest at
/Users/colton/Desktop/TSM eBay/reference_images/manifest.json — the actual
images never leave your Mac, and image_analysis_worker.py (running in
GitHub Actions) only ever reads from the Supabase table this script writes.

Run locally whenever the manifest changes (new cards added to the library):

    set -a && source .env && set +a
    python3 sync_reference_phashes.py

Safe to re-run — does an upsert keyed on pokemon_card_id.
"""

import json
import os
import sys

from supabase import create_client, Client

MANIFEST_PATH = "/Users/colton/Desktop/TSM eBay/reference_images/manifest.json"

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]


def main() -> None:
    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)

    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Manifest is keyed by card_key -> {path, source, source_url, phash, fetched_at}.
    # We need pokemon_card_id, so look up card_key -> id in one query.
    card_keys = list(manifest.keys())
    lookup_resp = (
        supabase.table("pokemon_cards")
        .select("id, card_key")
        .in_("card_key", card_keys)
        .execute()
    )
    card_key_to_id = {row["card_key"]: row["id"] for row in lookup_resp.data}

    rows = []
    skipped = []
    for card_key, entry in manifest.items():
        pokemon_card_id = card_key_to_id.get(card_key)
        if pokemon_card_id is None:
            skipped.append(card_key)
            continue
        rows.append({
            "pokemon_card_id": pokemon_card_id,
            "card_key": card_key,
            "phash": entry["phash"],
            "source": entry["source"],
            "source_url": entry.get("source_url"),
            "fetched_at": entry.get("fetched_at"),
        })

    if skipped:
        print(f"WARNING: {len(skipped)} manifest entries had no matching "
              f"pokemon_cards.card_key, skipped: {skipped}", file=sys.stderr)

    if not rows:
        print("No rows to sync.")
        return

    resp = (
        supabase.table("reference_phashes")
        .upsert(rows, on_conflict="pokemon_card_id")
        .execute()
    )
    print(f"Synced {len(resp.data)} reference phashes "
          f"({len(skipped)} skipped, {len(manifest)} total in manifest).")


if __name__ == "__main__":
    main()
