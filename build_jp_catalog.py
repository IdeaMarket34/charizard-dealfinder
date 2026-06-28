"""
build_jp_catalog.py
===================

Seeds the Pokemon reference catalog with Japanese Charizard sets and cards.
pokemontcg.io is English-only, so JP cards are not covered by build_reference_catalog.py.
This script hard-codes the confirmed JP Charizard card data sourced from TCGplayer,
Serebii, and Bulbapedia, and upserts it to pokemon_sets + pokemon_cards.

The card_key format used here is: {pokemon_name}|{set_key}|{card_number}|ja
which mirrors the existing English cards' format (pokemon_name|set_key|card_number|en).

SETS COVERED (confirmed card numbers from public sources)
---------------------------------------------------------
  sv3   — Ruler of the Black Flame (JP, Jul 2023) — printed total: 108
  sv4a  — Shiny Treasure ex       (JP, Dec 2023) — printed total: 190
  s12a  — VSTAR Universe          (JP, Dec 2022) — printed total: 172
  s4a   — Shiny Star V            (JP, Nov 2020) — printed total: 190
  s9    — Star Birth               (JP, Jan 2022) — printed total: 100
  s8b   — VMAX Climax             (JP, Dec 2021) — printed total: 184
  m2    — Inferno X               (JP, Sep 2025) — printed total: 80
  m2a   — MEGA Dream ex           (JP, Nov 2025) — printed total: 193
  cll   — Classic: Charizard & Ho-Oh ex Deck (JP, Oct 2023) — 32 cards

KNOWN GAPS (not yet seeded — add in a follow-up pass)
------------------------------------------------------
  - Japanese vintage sets: Base, Neo, e-Series, EX era (hard to catalog accurately)
  - CLK (Classic Venusaur & Mewtwo deck) — no confirmed Charizard cards
  - sv3a Raging Surf, sv5 Crimson Haze, sv6 Mask of Change, sv7 Stellar Miracle —
    unlikely to have Charizard but should be confirmed

USAGE
-----
  # Safe preview (no DB writes):
  python3 build_jp_catalog.py --dry-run

  # Write to Supabase:
  python3 build_jp_catalog.py --apply
"""

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv, find_dotenv

_loaded_from = None
for _cand in [find_dotenv(usecwd=True), str(Path(__file__).resolve().parent / ".env"),
              find_dotenv(str(Path(__file__).resolve().parent / ".env"))]:
    if _cand and Path(_cand).is_file():
        load_dotenv(_cand, override=False)
        _loaded_from = _cand
        break
print(f"Loaded environment from {_loaded_from}" if _loaded_from
      else "Note: no .env file found; relying on shell environment variables.")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# JP set definitions
# ---------------------------------------------------------------------------

JP_SETS = [
    {
        "set_key": "sv3",
        "set_name": "Ruler of the Black Flame",
        "series_name": "Scarlet & Violet",
        "set_code": "SV3",
        "language": "ja",
        "release_date": "2023-07-28",
        "aliases": "Ruler of the Black Flame,SV3,sv3",
    },
    {
        "set_key": "sv4a",
        "set_name": "Shiny Treasure ex",
        "series_name": "Scarlet & Violet",
        "set_code": "SV4a",
        "language": "ja",
        "release_date": "2023-12-01",
        "aliases": "Shiny Treasure ex,SV4a,sv4a",
    },
    {
        "set_key": "s12a",
        "set_name": "VSTAR Universe",
        "series_name": "Sword & Shield",
        "set_code": "S12a",
        "language": "ja",
        "release_date": "2022-12-02",
        "aliases": "VSTAR Universe,S12a,s12a",
    },
    {
        "set_key": "s4a",
        "set_name": "Shiny Star V",
        "series_name": "Sword & Shield",
        "set_code": "S4a",
        "language": "ja",
        "release_date": "2020-11-20",
        "aliases": "Shiny Star V,S4a,s4a",
    },
    {
        "set_key": "s9",
        "set_name": "Star Birth",
        "series_name": "Sword & Shield",
        "set_code": "S9",
        "language": "ja",
        "release_date": "2022-01-14",
        "aliases": "Star Birth,S9,s9",
    },
    {
        "set_key": "s8b",
        "set_name": "VMAX Climax",
        "series_name": "Sword & Shield",
        "set_code": "S8b",
        "language": "ja",
        "release_date": "2021-12-03",
        "aliases": "VMAX Climax,S8b,s8b,vmax climax",
    },
    {
        "set_key": "m2",
        "set_name": "Inferno X",
        "series_name": "MEGA",
        "set_code": "M2",
        "language": "ja",
        "release_date": "2025-09-26",
        "aliases": "Inferno X,M2,m2",
    },
    {
        "set_key": "m2a",
        "set_name": "MEGA Dream ex",
        "series_name": "MEGA",
        "set_code": "M2a",
        "language": "ja",
        "release_date": "2025-11-28",
        "aliases": "MEGA Dream ex,Mega Dream ex,M2a,m2a",
    },
    {
        "set_key": "cll",
        "set_name": "Classic: Charizard & Ho-Oh ex Deck",
        "series_name": "Classic",
        "set_code": "CLL",
        "language": "ja",
        "release_date": "2023-10-13",
        "aliases": "Classic Charizard and Ho-Oh ex Deck,CLL,cll,Charizard and Ho-Oh ex Deck",
    },
]

# ---------------------------------------------------------------------------
# JP Charizard card definitions
# All numbers verified against TCGplayer, Serebii, and/or Bulbapedia.
#
# Fields:
#   set_key         — must match a set in JP_SETS above
#   full_name       — card's full printed name (used only for metadata)
#   card_number_raw — as printed on card ("066/108", "331/190", etc.)
#   card_number     — normalized number, leading zeros stripped ("66", "331", "13")
#   total_in_set    — printed set total ("108", "190", "172"); None for promo-style
#   rarity          — informational only, not used by matcher
#   variant_family  — "tag_team" if applicable, else None
# ---------------------------------------------------------------------------

JP_CHARIZARD_CARDS = [

    # ── sv3: Ruler of the Black Flame ────────────────────────────────────────
    # Printed total: 108. All numbers confirmed via TCGplayer/Serebii.
    # 066 = regular Double Rare (in-set); 125, 134, 139 = secret rares above 108.
    {
        "set_key": "sv3",
        "full_name": "Charizard ex",
        "card_number_raw": "066/108",
        "card_number": "66",
        "total_in_set": "108",
        "rarity": "Double Rare",
        "variant_family": None,
    },
    {
        "set_key": "sv3",
        "full_name": "Charizard ex",
        "card_number_raw": "125/108",
        "card_number": "125",
        "total_in_set": "108",
        "rarity": "Special Art Rare",
        "variant_family": None,
    },
    {
        "set_key": "sv3",
        "full_name": "Charizard ex",
        "card_number_raw": "134/108",
        "card_number": "134",
        "total_in_set": "108",
        "rarity": "Super Rare",
        "variant_family": None,
    },
    {
        "set_key": "sv3",
        "full_name": "Charizard ex",
        "card_number_raw": "139/108",
        "card_number": "139",
        "total_in_set": "108",
        "rarity": "Art Rare",
        "variant_family": None,
    },

    # ── sv4a: Shiny Treasure ex ───────────────────────────────────────────────
    # Printed total: 190.
    # 115 = in-set Double Rare; 331 = Shiny Secret Rare; 349 = Special Art Rare.
    {
        "set_key": "sv4a",
        "full_name": "Charizard ex",
        "card_number_raw": "115/190",
        "card_number": "115",
        "total_in_set": "190",
        "rarity": "Double Rare",
        "variant_family": None,
    },
    {
        "set_key": "sv4a",
        "full_name": "Charizard ex",
        "card_number_raw": "331/190",
        "card_number": "331",
        "total_in_set": "190",
        "rarity": "Shiny Secret Rare",
        "variant_family": None,
    },
    {
        "set_key": "sv4a",
        "full_name": "Charizard ex",
        "card_number_raw": "349/190",
        "card_number": "349",
        "total_in_set": "190",
        "rarity": "Special Art Rare",
        "variant_family": None,
    },

    # ── s12a: VSTAR Universe ──────────────────────────────────────────────────
    # Printed total: 172.
    # 013 = Charizard V (Triple Rare), 014 = Charizard VSTAR (Triple Rare),
    # 015 = Radiant Charizard, 211 = Charizard V SAR, 212 = Charizard VSTAR SAR.
    {
        "set_key": "s12a",
        "full_name": "Charizard V",
        "card_number_raw": "013/172",
        "card_number": "13",
        "total_in_set": "172",
        "rarity": "Triple Rare",
        "variant_family": None,
    },
    {
        "set_key": "s12a",
        "full_name": "Charizard VSTAR",
        "card_number_raw": "014/172",
        "card_number": "14",
        "total_in_set": "172",
        "rarity": "Triple Rare",
        "variant_family": None,
    },
    {
        "set_key": "s12a",
        "full_name": "Radiant Charizard",
        "card_number_raw": "015/172",
        "card_number": "15",
        "total_in_set": "172",
        "rarity": "Radiant Rare",
        "variant_family": None,
    },
    {
        "set_key": "s12a",
        "full_name": "Charizard V",
        "card_number_raw": "211/172",
        "card_number": "211",
        "total_in_set": "172",
        "rarity": "Special Art Rare",
        "variant_family": None,
    },
    {
        "set_key": "s12a",
        "full_name": "Charizard VSTAR",
        "card_number_raw": "212/172",
        "card_number": "212",
        "total_in_set": "172",
        "rarity": "Special Art Rare",
        "variant_family": None,
    },

    # ── s4a: Shiny Star V ─────────────────────────────────────────────────────
    # Printed total: 190 (High Class Pack).
    # 307 = Charizard V SSR (Shiny Super Rare), 308 = Charizard VMAX SSR.
    {
        "set_key": "s4a",
        "full_name": "Charizard V",
        "card_number_raw": "307/190",
        "card_number": "307",
        "total_in_set": "190",
        "rarity": "Shiny Super Rare",
        "variant_family": None,
    },
    {
        "set_key": "s4a",
        "full_name": "Charizard VMAX",
        "card_number_raw": "308/190",
        "card_number": "308",
        "total_in_set": "190",
        "rarity": "Shiny Super Rare",
        "variant_family": None,
    },

    # ── s9: Star Birth ────────────────────────────────────────────────────────
    # Printed total: 100 (JP equivalent of Brilliant Stars).
    # 102 = Charizard V SR, 103 = Charizard V SAR.
    {
        "set_key": "s9",
        "full_name": "Charizard V",
        "card_number_raw": "102/100",
        "card_number": "102",
        "total_in_set": "100",
        "rarity": "Super Rare",
        "variant_family": None,
    },
    {
        "set_key": "s9",
        "full_name": "Charizard V",
        "card_number_raw": "103/100",
        "card_number": "103",
        "total_in_set": "100",
        "rarity": "Special Art Rare",
        "variant_family": None,
    },

    # ── m2: Inferno X ─────────────────────────────────────────────────────────
    # Printed total: 80. Released Sept 26, 2025. EN equivalent: Phantasmal Flames.
    # 013 = RR (Double Rare, in-set), 094 = SR, 110 = SAR, 116 = MUR (Mega Ultra Rare).
    {
        "set_key": "m2",
        "full_name": "Mega Charizard X ex",
        "card_number_raw": "013/080",
        "card_number": "13",
        "total_in_set": "80",
        "rarity": "Double Rare",
        "variant_family": None,
    },
    {
        "set_key": "m2",
        "full_name": "Mega Charizard X ex",
        "card_number_raw": "094/080",
        "card_number": "94",
        "total_in_set": "80",
        "rarity": "Super Rare",
        "variant_family": None,
    },
    {
        "set_key": "m2",
        "full_name": "Mega Charizard X ex",
        "card_number_raw": "110/080",
        "card_number": "110",
        "total_in_set": "80",
        "rarity": "Special Art Rare",
        "variant_family": None,
    },
    {
        "set_key": "m2",
        "full_name": "Mega Charizard X ex",
        "card_number_raw": "116/080",
        "card_number": "116",
        "total_in_set": "80",
        "rarity": "Mega Ultra Rare",
        "variant_family": None,
    },

    # ── m2a: MEGA Dream ex ────────────────────────────────────────────────────
    # Printed total: 193 (High Class Pack). Released Nov 28, 2025.
    # 223 = Mega Charizard X ex MA (Mega Attack Rare).
    {
        "set_key": "m2a",
        "full_name": "Mega Charizard X ex",
        "card_number_raw": "223/193",
        "card_number": "223",
        "total_in_set": "193",
        "rarity": "Mega Attack Rare",
        "variant_family": None,
    },

    # ── cll: Classic Charizard & Ho-Oh ex Deck ────────────────────────────────
    # 32-card JP Classic deck (Oct 2023). The deck's signature card is the
    # Charizard & Ho-Oh ex tag team. Individual card numbers need further
    # verification — adding the tag team card as a charizard entry.
    # NOTE: The card number for Charizard & Ho-Oh ex within this /032 deck
    # is not yet confirmed from a primary source. Skipping cards for now;
    # the set itself is seeded so parser set_code resolution works.
    # Add cards here once numbers are confirmed.
]

# ---------------------------------------------------------------------------
# Build + apply
# ---------------------------------------------------------------------------

def build_rows(sets: List[dict], cards: List[dict]):
    """
    Returns (set_rows, card_rows) ready for upsert.
    card_rows have _set_key stripped and replaced with set_id after DB lookup.
    """
    set_rows = [{**s, "auto_created": False, "needs_review": False} for s in sets]

    card_rows = []
    seen_keys = set()
    for c in cards:
        # card_key: charizard|sv4a|115|ja
        card_key = f"charizard|{c['set_key']}|{c['card_number']}|ja"
        if card_key in seen_keys:
            continue
        seen_keys.add(card_key)
        card_rows.append({
            "card_key": card_key,
            "pokemon_name": "charizard",
            "_set_key": c["set_key"],
            "card_number_raw": c["card_number_raw"],
            "card_number": c["card_number"],
            "total_in_set": c.get("total_in_set"),
            "promo_prefix": None,
            "rarity": c.get("rarity"),
            "supertype": "Pokémon",
            "subtype": None,
            "variant_family": c.get("variant_family"),
            "language": "ja",
            "image_url": None,
            "metadata": {
                "full_name": c.get("full_name", "Charizard"),
                "set_name": next(
                    (s["set_name"] for s in sets if s["set_key"] == c["set_key"]), None
                ),
                "source": "build_jp_catalog.py",
            },
        })
    return set_rows, card_rows


def summarize(set_rows: List[dict], card_rows: List[dict]) -> None:
    print("\n================ JP CATALOG DRY-RUN ================")
    print(f"Sets:  {len(set_rows)}")
    for s in set_rows:
        cnt = sum(1 for c in card_rows if c["_set_key"] == s["set_key"])
        print(f"  {s['set_key']:<8}  {s['set_name']:<40}  ({cnt} Charizard cards)")
    print(f"\nTotal Charizard cards: {len(card_rows)}")
    print("\nSample cards:")
    for c in card_rows[:10]:
        print(f"  {c['card_key']:<40}  rarity={c['rarity']}")
    print("=====================================================\n")


def upsert_with_retry(table, rows, on_conflict, attempts=5):
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            return table.upsert(rows, on_conflict=on_conflict).execute()
        except Exception as e:
            last_err = e
            if attempt == attempts:
                raise
            wait = min(2 ** attempt, 20)
            print(f"    transient error ({type(e).__name__}); retrying in {wait}s "
                  f"(attempt {attempt}/{attempts})...")
            time.sleep(wait)
    raise last_err


def apply_to_supabase(set_rows: List[dict], card_rows: List[dict]) -> None:
    from supabase import create_client

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("ERROR: --apply needs SUPABASE_URL and SUPABASE_SERVICE_KEY.")
        sys.exit(1)
    supabase = create_client(url, key)

    print("Upserting JP sets...")
    set_key_to_id: Dict[str, int] = {}
    for s in set_rows:
        row = {**s, "created_at": utc_now()}
        res = upsert_with_retry(supabase.table("pokemon_sets"), row, "set_key")
        if res.data:
            set_key_to_id[s["set_key"]] = res.data[0]["id"]

    # Fill any ids we didn't get back from upsert response
    for s in set_rows:
        if s["set_key"] not in set_key_to_id:
            got = supabase.table("pokemon_sets").select("id").eq("set_key", s["set_key"]).limit(1).execute()
            if got.data:
                set_key_to_id[s["set_key"]] = got.data[0]["id"]

    print(f"Sets ready: {len(set_key_to_id)} / {len(set_rows)}")

    if not card_rows:
        print("No cards to upsert (all sets have pending card number confirmation — see script comments).")
        return

    print("Upserting JP Charizard cards...")
    written = 0
    skipped = 0
    pending = []

    def flush(rows):
        if not rows:
            return 0
        upsert_with_retry(supabase.table("pokemon_cards"), rows, "card_key")
        return len(rows)

    for c in card_rows:
        set_id = set_key_to_id.get(c["_set_key"])
        if set_id is None:
            print(f"  skip {c['card_key']}: set not found")
            skipped += 1
            continue
        row = {k: v for k, v in c.items() if k != "_set_key"}
        row["set_id"] = set_id
        row["created_at"] = utc_now()
        pending.append(row)
        if len(pending) >= 100:
            written += flush(pending)
            pending = []

    written += flush(pending)
    print(f"\nDONE. Upserted {len(set_key_to_id)} sets and {written} cards."
          + (f" ({skipped} skipped)" if skipped else ""))


def main() -> None:
    ap = argparse.ArgumentParser(description="Seed JP Charizard catalog into Supabase.")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Preview only; no DB writes (default).")
    mode.add_argument("--apply", action="store_true", help="Write to Supabase.")
    args = ap.parse_args()

    set_rows, card_rows = build_rows(JP_SETS, JP_CHARIZARD_CARDS)

    if args.apply:
        apply_to_supabase(set_rows, card_rows)
    else:
        summarize(set_rows, card_rows)
        print("Nothing written. Run with --apply to seed the DB.")


if __name__ == "__main__":
    main()
