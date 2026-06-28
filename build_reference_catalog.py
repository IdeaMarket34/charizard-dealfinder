"""
build_reference_catalog.py
==========================

Rebuilds the Pokemon reference catalog (pokemon_sets + pokemon_cards) from the
free Pokemon TCG API (https://pokemontcg.io), in a shape that MATCHES your parser
(charizard_ingest_rewrite.py) so that listings can actually be matched to cards.

By default this now pulls ALL Pokemon (not just Charizard) -- the parser's name
extraction reads its list of recognizable Pokemon names directly from this
catalog, so a bigger catalog means the parser recognizes more Pokemon
automatically, with no code change. Trainer and Energy cards are skipped; only
actual Pokemon cards are loaded. Use --name to limit a run to one species (handy
for testing), the same way the original Charizard-only version worked.

WHY THIS EXISTS
---------------
Your old loader (load_charizard_reference.py) had two bugs that broke matching:
  1. It jammed the card number and set total into one field ("4_102") instead of
     storing the number ("4") and the total ("102") separately. Your parser
     produces them separately, so they could never line up.
  2. It set promo_prefix = None for every card. ~1,356 of your listings are promos
     (SWSH261, SM211, ...) and had nothing to match against.

This script fixes both, for every Pokemon:
  - card_number holds JUST the number ("4", "199", "TG20")
  - total_in_set holds the printed total ("102", "165") as its own field
  - promo cards get a real promo_prefix ("SWSH", "SM", "XY", "BW", "SVP") and the
    digits go in card_number ("261"), exactly how your matcher's promo path
    looks them up.
  - duplicate rows are avoided (idempotent upsert on card_key).
  - the base species is derived from the card's full name, stripping variant
    suffixes (ex/GX/V/VMAX/VSTAR/BREAK/Prime/LEGEND/Star) and prefixes
    (Mega/Dark/Light/Shining), and taking the named partner for tag-team cards
    ("Reshiram & Charizard-GX" -> "charizard").

SAFETY
------
Default mode is --dry-run: it fetches from the API and shows you EXACTLY what it
would write, as a preview file + summary. It does NOT touch your database.
Run that first, paste the summary back, and only then run --apply.
A full pull is a few thousand cards across many pages -- a free API key from
pokemontcg.io raises the rate limit and makes this faster, but isn't required.

USAGE
-----
  # 1) Safe preview of the FULL catalog (no database writes):
  python build_reference_catalog.py --dry-run

  # 2) For real, once the preview looks right:
  python build_reference_catalog.py --apply

  # Limit to one species (e.g. for a quick regression check against Charizard):
  python build_reference_catalog.py --dry-run --name Charizard

ENV VARS
--------
  SUPABASE_URL         (required only for --apply)
  SUPABASE_SERVICE_KEY (required only for --apply)
  POKEMONTCG_API_KEY   (optional; raises rate limits. Get a free key at pokemontcg.io)
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

try:
    # Load SUPABASE_URL / SUPABASE_SERVICE_KEY / POKEMONTCG_API_KEY from a local .env.
    # Search several places so it works no matter which directory you run from:
    #   1) walking up from the current working directory
    #   2) right next to this script
    #   3) walking up from the script's directory
    from pathlib import Path as _Path
    from dotenv import load_dotenv, find_dotenv

    _loaded_from = None
    _candidates = [
        find_dotenv(usecwd=True),
        str(_Path(__file__).resolve().parent / ".env"),
        find_dotenv(str(_Path(__file__).resolve().parent / ".env")),
    ]
    for _cand in _candidates:
        if _cand and _Path(_cand).is_file():
            load_dotenv(_cand, override=False)
            _loaded_from = _cand
            break
    if _loaded_from:
        print(f"Loaded environment from {_loaded_from}")
    else:
        print("Note: no .env file found; relying on variables already set in the shell.")
except ImportError:
    pass  # dotenv is optional; env vars can also be set directly in the shell

POKEMON_TCG_API_URL = "https://api.pokemontcg.io/v2/cards"

# Promo prefixes your PARSER recognizes (extract_promo_code). The catalog must use
# the same set, or the matcher's promo path can't line up.
PROMO_PREFIXES = ("SWSH", "SVP", "SM", "XY", "BW")
PROMO_NUMBER_RE = re.compile(r"^(SWSH|SVP|SM|XY|BW)\s*[-#]?\s*(\d{1,4})$", re.I)


def log(msg: str) -> None:
    print(msg, flush=True)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Normalization — these mirror charizard_ingest_rewrite.py so both sides agree.
# ---------------------------------------------------------------------------

def normalize_set_key(name: str, ptcgo_code: Optional[str]) -> str:
    """Prefer the official PTCGO/PTCGL code (stable, e.g. 'BS', 'OBF', 'PR-SW').
    Fall back to a slug of the set name."""
    if ptcgo_code:
        return ptcgo_code.lower().replace("-", "").replace("_", "").strip()
    slug = (
        (name or "")
        .lower()
        .replace("&", "and")
        .replace("'", "")
        .replace(":", "")
        .replace("-", " ")
        .replace("/", " ")
        .strip()
    )
    slug = re.sub(r"\s+", "_", slug)
    return slug


def clean_single_pokemon_name(name: str) -> str:
    """Strip variant prefixes/suffixes from ONE Pokemon name (no '&' handling --
    see get_pokemon_names_for_card for tag-team cards)."""
    name = name or ""

    # Trainer-owned Pokemon naming convention (Gym Heroes/Gym Challenge era):
    # "Blaine's Charizard", "Team Rocket's Charizard". Strip ANY leading
    # possessive phrase -- the trainer name varies endlessly but the pattern
    # ("<name>'s ") is consistent, and Pokemon species names never contain it.
    possessive_pattern = re.compile(r"^\s*[A-Za-z_][A-Za-z_.\u2019'\-\s]*[\u2019']s\s+")

    suffix_pattern = re.compile(
        r"[\s\-]+(VSTAR|VMAX|V-UNION|GX|EX|BREAK|PRIME|LEGEND|STAR|TAG TEAM|"
        r"LV\.?\s?X|GL|SP|X|Y|"   # Lv.X, GL/SP (Platinum), Mega X/Y forms
        r"V)\s*$",
        re.IGNORECASE,
    )
    # Single-letter "G" Pokemon (HeartGold & SoulSilver mechanic, e.g. "Absol G")
    # kept separate from the main pattern since a bare letter needs a tighter,
    # explicit word-boundary match to avoid ever touching a real name.
    g_suffix_pattern = re.compile(r"\s+G\s*$")
    # Gold Star / ancient-star and delta-species markers are symbols, not words.
    symbol_suffix_pattern = re.compile(r"[\u2605\u2606\u03b4]\s*$")
    prefix_pattern = re.compile(
        r"^\s*(M|MEGA|DARK|LIGHT|SHINING|CRYSTAL|RADIANT|SPECIAL DELIVERY)\s+",
        re.IGNORECASE,
    )

    prev = None
    while prev != name:
        prev = name
        name = possessive_pattern.sub("", name).strip()
        name = suffix_pattern.sub("", name).strip()
        name = g_suffix_pattern.sub("", name).strip()
        name = symbol_suffix_pattern.sub("", name).strip()
        name = prefix_pattern.sub("", name).strip()

    return name.strip().lower()


def get_pokemon_names_for_card(card_name: str) -> List[str]:
    """Get every species a card should be filed under. Almost always one name.
    Tag-team cards ("Charizard & Braixen-GX") name TWO Pokemon, and which one a
    buyer's listing title mentions varies by listing -- so the card gets a
    catalog row under EACH partner's name, rather than guessing one. This
    duplicates a small number of physical-card rows (same set/number/total,
    different pokemon_name) which is exactly what lets the matcher find the
    card regardless of which name a given listing happened to use."""
    raw = card_name or ""
    parts = raw.split("&") if "&" in raw else [raw]
    names = []
    for part in parts:
        cleaned = clean_single_pokemon_name(part)
        if cleaned and cleaned not in names:
            names.append(cleaned)
    return names


def derive_pokemon_name(card_name: str) -> str:
    """Convenience wrapper for single-name use (e.g. tests): for a tag-team
    card, returns its first partner. Real catalog building uses
    get_pokemon_names_for_card so BOTH partners get a row -- see build_rows."""
    names = get_pokemon_names_for_card(card_name)
    return names[0] if names else ""


def normalize_card_part(part: str) -> str:
    """Mirror of the parser's _normalize_card_part: '004' -> '4', 'TG20' -> 'TG20'."""
    part = (part or "").strip().upper()
    m = re.match(r"^([A-Z]{0,3})(\d{1,4})$", part)
    if not m:
        return part.lower()
    prefix = m.group(1)
    number = str(int(m.group(2)))
    return f"{prefix}{number}" if prefix else number


def classify_number(raw_number: str, set_name: str) -> Tuple[Optional[str], str, bool]:
    """
    Decide how a card's number should be stored.
    Returns (promo_prefix, card_number, is_promo).

    Promo  -> promo_prefix='SWSH', card_number='261'  (digits only)
    Normal -> promo_prefix=None,   card_number='4' or 'TG20'
    """
    raw = (raw_number or "").strip()
    m = PROMO_NUMBER_RE.match(raw)
    if m:
        prefix = m.group(1).upper()
        digits = str(int(m.group(2)))
        return prefix, digits, True
    # Not a recognized promo number -> store as a normal (possibly prefixed) number
    return None, normalize_card_part(raw), False


# ---------------------------------------------------------------------------
# Fetch from the Pokemon TCG API
# ---------------------------------------------------------------------------

def fetch_cards(api_key: Optional[str], limit: Optional[int], name_filter: Optional[str] = None) -> List[dict]:
    headers = {"X-Api-Key": api_key} if api_key else {}
    all_cards: List[dict] = []
    page = 1
    page_size = 250

    # supertype:Pokémon excludes Trainer and Energy cards, which don't have a
    # species name and aren't part of what we're matching listings against.
    query = 'supertype:Pokémon'
    if name_filter:
        query += f' name:"{name_filter}"'

    while True:
        params = {"q": query, "page": page, "pageSize": page_size}
        for attempt in range(1, 5):
            try:
                resp = requests.get(POKEMON_TCG_API_URL, params=params, headers=headers, timeout=60)
                if resp.status_code == 429:
                    wait = min(2 ** attempt, 30)
                    log(f"  rate limited; waiting {wait}s...")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                break
            except requests.RequestException as e:
                if attempt == 4:
                    raise
                log(f"  request error ({e}); retrying...")
                time.sleep(2 * attempt)

        payload = resp.json()
        cards = payload.get("data", [])
        if not cards:
            break
        all_cards.extend(cards)
        log(f"  fetched page {page}: {len(cards)} cards (running total {len(all_cards)})")

        if limit and len(all_cards) >= limit:
            all_cards = all_cards[:limit]
            break

        total_count = payload.get("totalCount", 0)
        if len(all_cards) >= total_count:
            break
        page += 1

    return all_cards


# ---------------------------------------------------------------------------
# Build rows in your schema's shape
# ---------------------------------------------------------------------------

def build_rows(cards: List[dict]) -> Tuple[Dict[str, dict], List[dict]]:
    """Returns (sets_by_key, card_rows). Sets are de-duplicated by set_key."""
    sets_by_key: Dict[str, dict] = {}
    card_rows: List[dict] = []
    seen_card_keys = set()

    for card in cards:
        if (card.get("supertype") or "").strip().lower() != "pokémon":
            continue  # skip Trainer/Energy cards even if the API query lets one through

        cset = card.get("set") or {}
        set_name = cset.get("name") or ""
        ptcgo = cset.get("ptcgoCode")
        set_key = normalize_set_key(set_name, ptcgo)

        if set_key not in sets_by_key:
            aliases = sorted({x for x in [set_name, ptcgo, set_key] if x})
            sets_by_key[set_key] = {
                "set_key": set_key,
                "set_name": set_name or set_key,
                "series_name": cset.get("series"),
                "set_code": ptcgo,
                "language": "en",
                "release_date": cset.get("releaseDate"),
                "aliases": ",".join(aliases),
            }

        raw_number = str(card.get("number") or "").strip()
        printed_total = cset.get("printedTotal")
        promo_prefix, card_number, is_promo = classify_number(raw_number, set_name)

        # printed total only meaningful for non-promo cards (titles read "4/102")
        total_in_set = None if is_promo else (str(printed_total) if printed_total else None)

        subtypes = card.get("subtypes") or []
        pokemon_names = get_pokemon_names_for_card(card.get("name") or "")
        if not pokemon_names:
            continue  # couldn't derive a species name; skip rather than store junk

        # Normally one name. For tag-team cards this is two -- emit a row per
        # partner so the card is findable under either species name, since a
        # buyer's listing might mention either one.
        for pokemon_name in pokemon_names:
            card_key = f"{pokemon_name}|{set_key}|{card_number}|en"
            # guard against rare dupes within the API response
            if card_key in seen_card_keys:
                continue
            seen_card_keys.add(card_key)

            card_rows.append({
                "card_key": card_key,
                "pokemon_name": pokemon_name,          # base species; matcher uses this
                "_set_key": set_key,                   # internal join helper, stripped before insert
                "card_number_raw": raw_number,
                "card_number": card_number,
                "total_in_set": total_in_set,
                "promo_prefix": promo_prefix,
                "rarity": card.get("rarity"),
                "supertype": card.get("supertype"),
                "subtype": ",".join(subtypes) if subtypes else None,
                "variant_family": "tag_team" if len(pokemon_names) > 1 else None,
                "language": "en",
                "image_url": (card.get("images") or {}).get("small"),
                "metadata": {
                    "api_card_id": card.get("id"),
                    "full_name": card.get("name"),
                    "set_name": set_name,
                    "ptcgo_code": ptcgo,
                    "printed_total": printed_total,
                    "tcgplayer_url": (card.get("tcgplayer") or {}).get("url"),
                },
            })

    return sets_by_key, card_rows


# ---------------------------------------------------------------------------
# Reporting + apply
# ---------------------------------------------------------------------------

def summarize(sets_by_key: Dict[str, dict], card_rows: List[dict]) -> None:
    promos = [c for c in card_rows if c["promo_prefix"]]
    with_total = [c for c in card_rows if c["total_in_set"]]
    species = sorted({c["pokemon_name"] for c in card_rows})
    log("\n================ DRY-RUN SUMMARY ================")
    log(f"Sets that would be written:        {len(sets_by_key)}")
    log(f"Cards that would be written:       {len(card_rows)}")
    log(f"Distinct Pokemon species:          {len(species)}")
    log(f"  ...of those, promo cards:        {len(promos)}")
    log(f"  ...of those, with printed total: {len(with_total)}")
    log("\nPromo prefixes present: " + ", ".join(sorted({c['promo_prefix'] for c in promos})))
    charizard_count = sum(1 for c in card_rows if c["pokemon_name"] == "charizard")
    log(f"\nCharizard cards in this pull: {charizard_count} "
        f"(regression check -- should be close to the Charizard-only run's 108)")
    log("\nSample species found (first 15 alphabetically):")
    log("  " + ", ".join(species[:15]))
    log("\nSample NORMAL cards (number + total, no set needed to match):")
    for c in [c for c in card_rows if not c["promo_prefix"]][:8]:
        log(f"  {c['card_key']:32}  num={c['card_number']:>5}  total={c['total_in_set']}")
    log("\nSample PROMO cards (prefix + number):")
    for c in promos[:8]:
        log(f"  {c['card_key']:32}  prefix={c['promo_prefix']:<5} num={c['card_number']}")
    log("\nSets sample:")
    for s in list(sets_by_key.values())[:10]:
        log(f"  set_key={s['set_key']:<14} name={s['set_name']}")
    log("================================================\n")


def upsert_with_retry(table, rows, on_conflict, attempts=5):
    """Upsert a batch, retrying transient connection drops with backoff. A run
    over thousands of rows WILL eventually hit an isolated network blip --
    that should cost a short pause and a retry, not the entire run."""
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            return table.upsert(rows, on_conflict=on_conflict).execute()
        except Exception as e:
            last_err = e
            if attempt == attempts:
                raise
            wait = min(2 ** attempt, 20)
            log(f"    transient error ({type(e).__name__}); retrying in {wait}s "
                f"(attempt {attempt}/{attempts})...")
            time.sleep(wait)
    raise last_err  # pragma: no cover


def apply_to_supabase(sets_by_key: Dict[str, dict], card_rows: List[dict]) -> None:
    from supabase import create_client  # imported lazily so dry-run needs no creds

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        log("ERROR: --apply needs SUPABASE_URL and SUPABASE_SERVICE_KEY in your environment.")
        sys.exit(1)
    supabase = create_client(url, key)

    log("Upserting sets...")
    set_key_to_id: Dict[str, int] = {}
    for s in sets_by_key.values():
        row = {**s, "created_at": utc_now()}
        res = upsert_with_retry(supabase.table("pokemon_sets"), row, "set_key")
        if res.data:
            set_key_to_id[s["set_key"]] = res.data[0]["id"]

    # fill any set ids we didn't get back from the upsert response
    for set_key in sets_by_key:
        if set_key not in set_key_to_id:
            got = supabase.table("pokemon_sets").select("id").eq("set_key", set_key).limit(1).execute()
            if got.data:
                set_key_to_id[set_key] = got.data[0]["id"]

    log(f"Sets ready: {len(set_key_to_id)}")

    log("Upserting cards...")
    written = 0
    skipped = 0
    chunk_size = 200
    pending: List[dict] = []

    def flush(pending_rows: List[dict]) -> int:
        if not pending_rows:
            return 0
        upsert_with_retry(supabase.table("pokemon_cards"), pending_rows, "card_key")
        return len(pending_rows)

    for c in card_rows:
        set_id = set_key_to_id.get(c["_set_key"])
        if set_id is None:
            log(f"  skip {c['card_key']}: set not found")
            skipped += 1
            continue
        row = {k: v for k, v in c.items() if k != "_set_key"}
        row["set_id"] = set_id
        row["created_at"] = utc_now()
        pending.append(row)

        if len(pending) >= chunk_size:
            written += flush(pending)
            pending = []
            if written % 2000 == 0:
                log(f"  ...upserted {written:,} cards so far")

    written += flush(pending)

    log(f"\nDONE. Upserted {len(set_key_to_id)} sets and {written} cards."
        + (f" ({skipped} skipped, set not found)" if skipped else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild the Pokemon reference catalog.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Preview only; no DB writes (default).")
    mode.add_argument("--apply", action="store_true", help="Write to Supabase.")
    parser.add_argument("--limit", type=int, default=None, help="Cap number of cards (for testing).")
    parser.add_argument("--name", default=None, help='Limit to one species, e.g. --name Charizard (for testing).')
    parser.add_argument("--out", default="catalog_preview.json", help="Preview file path for dry-run.")
    args = parser.parse_args()

    apply = args.apply
    if not apply:
        args.dry_run = True

    api_key = os.environ.get("POKEMONTCG_API_KEY")
    what = f'"{args.name}" cards' if args.name else "ALL Pokemon cards"
    log(f"Fetching {what} from the Pokemon TCG API..." + ("" if api_key else " (no API key set; lower rate limit)"))
    cards = fetch_cards(api_key, args.limit, name_filter=args.name)
    log(f"Fetched {len(cards)} cards total.")

    sets_by_key, card_rows = build_rows(cards)

    if apply:
        apply_to_supabase(sets_by_key, card_rows)
    else:
        preview = {
            "generated_at": utc_now(),
            "set_count": len(sets_by_key),
            "card_count": len(card_rows),
            "sets": list(sets_by_key.values()),
            "cards": [{k: v for k, v in c.items() if k != "_set_key"} for c in card_rows],
        }
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(preview, f, indent=2)
        summarize(sets_by_key, card_rows)
        log(f"Full preview written to: {args.out}")
        log("Nothing was written to your database. Review the summary above, then run with --apply.")


if __name__ == "__main__":
    main()