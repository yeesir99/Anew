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
        and mapping.get("provider")
        and mapping.get("source_url")
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


def star_next_new_eligible(code, listing_date, as_of, max_days=None):
    """Project rule: STAR-listed and listed less than one calendar year ago."""
    code = str(code or "").strip()
    if not code.startswith(("688", "689")) or len(code) != 6 or not code.isdigit():
        return False
    try:
        listed = listing_date if isinstance(listing_date, date) else date.fromisoformat(str(listing_date)[:10])
        current = as_of if isinstance(as_of, date) else date.fromisoformat(str(as_of)[:10])
    except (TypeError, ValueError):
        return False
    if max_days is not None:
        try:
            limit = int(max_days)
            if limit <= 0:
                return False
            return 0 <= (current - listed).days < limit
        except (TypeError, ValueError):
            return False
    try:
        one_year_ago = current.replace(year=current.year - 1)
    except ValueError:
        # 29 February has no counterpart in the preceding non-leap year.
        one_year_ago = current.replace(year=current.year - 1, day=28)
    return one_year_ago < listed <= current


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
        "annual_report", "semiannual_report", "semi_annual_report", "quarterly_report",
        "periodic_report", "exchange_announcement", "cninfo_filing"
    }:
        return False
    return len(str(record.get("source_excerpt") or "").strip()) >= 30



PRIVATE_BANK_MAX_DISCLOSURE_AGE_DAYS = 180
PRIVATE_BANK_MAX_REVIEW_AGE_DAYS = 180


def private_bank_records_for_trade_date(records, trade_date):
    """Carry forward the latest primary disclosure only within explicit freshness limits."""
    try:
        as_of = date.fromisoformat(str(trade_date)[:10])
    except (TypeError, ValueError):
        return []
    selected = {}
    for record in records or []:
        if not verified_private_bank_relationship(record):
            continue
        try:
            reviewed = date.fromisoformat(str(record.get("as_of_trade_date") or "")[:10])
            disclosed = date.fromisoformat(str(record.get("disclosure_date") or "")[:10])
        except ValueError:
            continue
        review_age = (as_of - reviewed).days
        disclosure_age = (as_of - disclosed).days
        if (reviewed > as_of or disclosed > as_of
                or review_age < 0 or review_age > PRIVATE_BANK_MAX_REVIEW_AGE_DAYS
                or disclosure_age < 0 or disclosure_age > PRIVATE_BANK_MAX_DISCLOSURE_AGE_DAYS):
            continue
        code = str(record["listed_company_code"]).strip()
        bank = str(record["investee_bank_name"]).strip()
        key = (code, bank)
        previous = selected.get(key)
        previous_date = date.min if previous is None else date.fromisoformat(
            str(previous["disclosure_date"])[:10])
        if previous is None or disclosed > previous_date:
            enriched = dict(record)
            enriched["evidence_age_days"] = disclosure_age
            enriched["review_age_days"] = review_age
            enriched["evidence_status"] = "latest_disclosure_within_freshness_window"
            selected[key] = enriched
    return [selected[key] for key in sorted(selected)]



def consistent_provider_flow_persistence(flow_5d, flow_10d):
    """Score flow direction only when both periods use the same named provider."""
    def unpack(value):
        if not isinstance(value, (tuple, list)) or len(value) != 2:
            return None, None
        provider, amount = value
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return None, None
        if not math.isfinite(amount) or not str(provider or "").strip():
            return None, None
        return str(provider).strip(), amount

    provider5, amount5 = unpack(flow_5d)
    provider10, amount10 = unpack(flow_10d)
    if provider5 is None or provider10 is None or provider5 != provider10:
        return None
    if amount5 > 0 and amount10 > 0:
        return 75.0
    if amount5 < 0 and amount10 < 0:
        return 25.0
    return 50.0
