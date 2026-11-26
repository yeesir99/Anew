"""Strict, reusable evidence rules for special theme membership."""
from __future__ import annotations

from datetime import date, datetime
import math


def has_mapping_provenance(mapping):
    mapping = mapping or {}
    status_ok = mapping.get("status") in {"success", "cached"}
    date_value = mapping.get("mapping_date") or mapping.get("checked_at") or mapping.get("fetched_at")
    try:
        datetime.fromisoformat(str(date_value).replace("Z", "+00:00"))
        date_ok = True
    except (TypeError, ValueError):
        date_ok = False
    return bool(
        status_ok
        and (mapping.get("mapping_source") or mapping.get("source"))
        and (mapping.get("matched_symbol") or mapping.get("matched_name"))
        and date_ok
    )


def merge_exact_components(components, required=("烟草", "工业大麻")):
    """Merge only when every explicitly required component has verified members."""
    merged = {}
    for component in required:
        item = (components or {}).get(component) or {}
        if item.get("status") != "success" or not item.get("members"):
            return []
        for member in item["members"]:
            code = str(member.get("code") or "").strip()
            if len(code) != 6 or not code.isdigit():
                continue
            merged[code] = member
    return [merged[key] for key in sorted(merged)]


def star_next_new_eligible(code, listing_date, as_of, max_days):
    """Require STAR Market identity, a valid listing date, and an explicit age rule."""
    code = str(code or "").strip()
    if not code.startswith(("688", "689")) or len(code) != 6 or not code.isdigit():
        return False
    try:
        limit = int(max_days)
        if limit <= 0:
            return False
        listed = listing_date if isinstance(listing_date, date) else date.fromisoformat(str(listing_date)[:10])
        current = as_of if isinstance(as_of, date) else date.fromisoformat(str(as_of)[:10])
    except (TypeError, ValueError):
        return False
    age = (current - listed).days
    return 0 <= age <= limit


def verified_private_bank_relationship(record):
    """A concept tag is not ownership proof; require dated primary disclosure fields."""
    record = record or {}
    relation = str(record.get("relationship_type") or "").lower()
    if relation not in {"equity_investment", "shareholding", "股权投资", "持股"}:
        return False
    company_code = str(record.get("listed_company_code") or "").strip()
    if len(company_code) != 6 or not company_code.isdigit():
        return False
    if not str(record.get("investee_bank_name") or "").strip():
        return False
    try:
        pct = float(record.get("holding_pct"))
        if not math.isfinite(pct) or not 0 < pct <= 100:
            return False
        date.fromisoformat(str(record.get("disclosure_date") or "")[:10])
    except (TypeError, ValueError):
        return False
    url = str(record.get("source_url") or "")
    if not url.startswith(("https://", "http://")):
        return False
    if str(record.get("source_type") or "").lower() not in {
        "annual_report", "exchange_announcement", "cninfo_filing"
    }:
        return False
    return len(str(record.get("source_excerpt") or "").strip()) >= 30



def private_bank_records_for_trade_date(records, trade_date):
    """Accept latest primary-source equity disclosure per issuer/bank as of a report date."""
    try:
        as_of = date.fromisoformat(str(trade_date)[:10])
    except (TypeError, ValueError):
        return []
    selected = {}
    for record in records or []:
        if not verified_private_bank_relationship(record):
            continue
        if str(record.get("as_of_trade_date") or "")[:10] != as_of.isoformat():
            continue
        try:
            disclosed = date.fromisoformat(str(record.get("disclosure_date") or "")[:10])
        except ValueError:
            continue
        if disclosed > as_of:
            continue
        code = str(record["listed_company_code"]).strip()
        bank = str(record["investee_bank_name"]).strip()
        key = (code, bank)
        previous = selected.get(key)
        previous_date = date.min if previous is None else date.fromisoformat(
            str(previous["disclosure_date"])[:10])
        if previous is None or disclosed > previous_date:
            selected[key] = dict(record)
    return [selected[key] for key in sorted(selected)]
