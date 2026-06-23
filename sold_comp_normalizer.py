import importlib.util
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from dotenv import load_dotenv
from supabase import create_client

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
SOLD_NORMALIZER_BATCH_SIZE = int(os.environ.get("SOLD_NORMALIZER_BATCH_SIZE", "200"))
CHARIZARD_REWRITE_PATH = BASE_DIR / "charizard_ingest_rewrite.py"


supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_rewrite_module(module_path: Path):
    if not module_path.exists():
        raise FileNotFoundError(f"rewrite module not found: {module_path}")
    spec = importlib.util.spec_from_file_location("charizard_ingest_rewrite_runtime", str(module_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load module spec from {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rewrite = load_rewrite_module(CHARIZARD_REWRITE_PATH)
parse_listing_title = rewrite.parse_listing_title
normalize_charizard_key_from_parsed = rewrite.normalize_charizard_key_from_parsed
normalize_text = rewrite.normalize_text
extract_fraction_fields = rewrite.extract_fraction_fields


LOT_PATTERNS = [
    r"\bset of \d+\b",
    r"\b\d+\s*card set\b",
    r"\b3 card set\b",
    r"\bcomplete set\b",
    r"\bmaster set\b",
    r"\bchoose your card\b",
    r"\bcharmander\b.*\bcharmeleon\b",
    r"\bcharizard\b.*\bcharmeleon\b",
    r"\bcharizard\b.*\bcharmander\b",
    r"\blot\b",
    r"\bbundle\b",
]

STRONG_PACKAGED_PRODUCT_PATTERNS = [
    r"\bultra premium collection\b",
    r"\bupc\b",
    r"\bbooster box\b",
    r"\bbooster pack\b",
    r"\bblister pack\b",
    r"\bcollection box\b",
    r"\bbox set\b",
    r"\bsealed etb\b",
    r"\bsealed elite trainer box\b",
    r"\bunopened etb\b",
    r"\bunopened elite trainer box\b",
    r"\bsealed tin\b",
    r"\bunopened tin\b",
]

WEAK_PACKAGED_ORIGIN_PATTERNS = [
    r"\betb\b",
    r"\belite trainer box\b",
]

SEALED_CARD_PATTERNS = [
    r"\bsealed\b",
    r"\bfactory sealed\b",
    r"\bstill sealed\b",
    r"\bcello\b",
    r"\bcello pack\b",
]


def load_targets() -> Dict[str, dict]:
    result = (
        supabase.table("sold_search_targets")
        .select("id,query_text,pokemon_card_id,normalized_item_key")
        .eq("enabled", True)
        .execute()
    )
    rows = result.data or []
    return {row["query_text"]: row for row in rows if row.get("query_text")}



def load_raw_rows(limit: int = SOLD_NORMALIZER_BATCH_SIZE) -> List[dict]:
    result = (
        supabase.table("sold_comps_raw")
        .select("id,provider,provider_record_id,search_query,title,item_web_url,sold_at,sold_price_value,sold_price_currency,shipping_value,condition_text,listing_format,seller_name,quantity_sold,raw_json,ingested_at")
        .order("ingested_at", desc=False)
        .limit(limit)
        .execute()
    )
    return result.data or []



def extract_target_identity(normalized_item_key: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    if not normalized_item_key:
        return None, None, None
    m = re.match(r"^charizard_([a-z0-9_]+)_([a-z0-9]+_[a-z0-9]+|[a-z]+\d+|\d+_\d+|\d+)_", normalized_item_key)
    if not m:
        return None, None, None
    set_key = m.group(1)
    ident = m.group(2)
    if re.match(r"^[a-z]+\d+$", ident):
        return set_key, ident, None
    if re.match(r"^\d+_\d+$", ident):
        left, right = ident.split("_", 1)
        return set_key, None, f"{left}/{right}"
    return set_key, None, None



def compute_total_price(sold_price, shipping_price):
    try:
        sold = float(sold_price) if sold_price is not None else None
    except (TypeError, ValueError):
        sold = None
    try:
        shipping = float(shipping_price) if shipping_price is not None else None
    except (TypeError, ValueError):
        shipping = None
    if sold is None:
        return None
    return sold + (shipping or 0.0)



def detect_exclusion(title_norm: str, parsed, target: Optional[dict]) -> Tuple[bool, Optional[str], str]:
    if parsed.pokemon_name != "charizard":
        return False, "not_charizard", "C"

    if parsed.is_junk:
        return False, parsed.junk_reason or "junk", "C"

    for pat in LOT_PATTERNS:
        if re.search(pat, title_norm):
            return False, "multi_card_lot", "C"

    for pat in STRONG_PACKAGED_PRODUCT_PATTERNS:
        if re.search(pat, title_norm):
            return False, "sealed_product", "C"

    if any(re.search(pat, title_norm) for pat in WEAK_PACKAGED_ORIGIN_PATTERNS):
        if not has_strong_single_card_signal(parsed, title_norm):
            return False, "sealed_product", "C"

    target_set, target_promo, target_fraction = extract_target_identity((target or {}).get("normalized_item_key"))

    if target_promo and parsed.promo_code_guess and parsed.promo_code_guess != target_promo:
        return False, "wrong_card_code", "C"

    if target_fraction and parsed.card_fraction_norm and parsed.card_fraction_norm != target_fraction:
        return False, "wrong_card_number", "C"

    if target_set and parsed.set_guess and parsed.set_guess != target_set and not (target_promo and parsed.promo_code_guess == target_promo):
        return False, "wrong_set", "C"

    if target_promo and not parsed.promo_code_guess:
        return True, None, "B"

    if target_fraction and not parsed.card_fraction_norm:
        return True, None, "B"

    return True, None, "A"



def build_comp_row(raw_row: dict, target: Optional[dict]) -> dict:
    title = raw_row.get("title") or ""
    parsed = parse_listing_title(title)
    title_norm = normalize_text(title)
    is_valid, exclusion_reason, confidence_grade = detect_exclusion(title_norm, parsed, target)
    normalized_item_key = (target or {}).get("normalized_item_key") or normalize_charizard_key_from_parsed(parsed)
    matched_card_id = (target or {}).get("pokemon_card_id")
    now = utc_now()
    sold_price = raw_row.get("sold_price_value")
    shipping_price = raw_row.get("shipping_value")
    total_price = compute_total_price(sold_price, shipping_price)

    source = raw_row.get("provider") or "soldcomps"
    provider = raw_row.get("provider") or "soldcomps"
    provider_record_id = str(raw_row.get("provider_record_id")) if raw_row.get("provider_record_id") is not None else None
    external_comp_id = provider_record_id

    return {
        "id": str(uuid4()),
        "normalized_item_key": normalized_item_key,
        "source": source,
        "title": title,
        "sold_price_value": sold_price,
        "sold_price_currency": raw_row.get("sold_price_currency") or "USD",
        "shipping_value": shipping_price,
        "condition_text": raw_row.get("condition_text"),
        "sold_at": raw_row.get("sold_at"),
        "item_web_url": raw_row.get("item_web_url"),
        "raw_json": raw_row.get("raw_json") or {},
        "created_at": now,
        "source_tier": 1,
        "source_run_id": f"sold_comp_normalizer:{provider}",
        "external_comp_id": external_comp_id,
        "search_query": raw_row.get("search_query"),
        "sold_price": sold_price,
        "shipping_price": shipping_price,
        "currency": raw_row.get("sold_price_currency") or "USD",
        "comp_window_label": None,
        "grade_company": parsed.grade_company,
        "grade_value": parsed.grade_value,
        "listing_type": raw_row.get("listing_format"),
        "confidence_grade": confidence_grade,
        "is_valid_comp": is_valid,
        "exclusion_reason": exclusion_reason,
        "updated_at": now,
        "provider": provider,
        "provider_record_id": provider_record_id,
        "matched_card_id": matched_card_id,
    }



def upsert_comp_row(row: dict) -> None:
    result = (
        supabase.table("sold_comps")
        .select("id")
        .eq("provider", row["provider"])
        .eq("provider_record_id", row["provider_record_id"])
        .limit(1)
        .execute()
    )
    existing = result.data or []
    if existing:
        row = dict(row)
        row["id"] = existing[0]["id"]
    supabase.table("sold_comps").upsert(row).execute()



def main() -> None:
    targets = load_targets()
    raw_rows = load_raw_rows()
    processed = 0
    valid = 0
    excluded = 0
    failures: List[str] = []

    for raw_row in raw_rows:
        try:
            target = targets.get(raw_row.get("search_query"))
            comp_row = build_comp_row(raw_row, target)
            upsert_comp_row(comp_row)
            processed += 1
            if comp_row["is_valid_comp"]:
                valid += 1
            else:
                excluded += 1
        except Exception as e:
            failures.append(f"raw_id={raw_row.get('id')}: {e}")

    print({
        "raw_rows_seen": len(raw_rows),
        "processed": processed,
        "valid": valid,
        "excluded": excluded,
        "failed": len(failures),
        "failures": failures[:10],
    })


if __name__ == "__main__":
    main()
