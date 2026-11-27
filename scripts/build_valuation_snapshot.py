#!/usr/bin/env python3
"""Build a point-in-time A-share broad-market PE/PB valuation snapshot.

Only provider-supplied historical quantiles are accepted. This module does not
manufacture theme-level valuation scores from broad-market valuation data.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

try:
    from scripts.research_provider_utils import call_with_retry
except ModuleNotFoundError:
    from research_provider_utils import call_with_retry

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
MARKET = DATA / "latest_market.csv"
OUT = DATA / "valuation_crowding.json"

PE_FIELDS = {
    "median_pe_ttm": "middlePETTM",
    "median_pe_ttm_quantile_all_history": "quantileInAllHistoryMiddlePeTtm",
    "median_pe_ttm_quantile_10y": "quantileInRecent10YearsMiddlePeTtm",
    "median_pe_lyr": "middlePELYR",
    "median_pe_lyr_quantile_all_history": "quantileInAllHistoryMiddlePeLyr",
    "median_pe_lyr_quantile_10y": "quantileInRecent10YearsMiddlePeLyr",
}
PB_FIELDS = {
    "median_pb": "middlePB",
    "median_pb_quantile_all_history": "quantileInAllHistoryMiddlePB",
    "median_pb_quantile_10y": "quantileInRecent10YearsMiddlePB",
}


def _day(value):
    raw = str(value or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.strftime("%Y%m%d")


def _finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _latest_record(frame, required_fields, target_day):
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError("valuation provider returned an empty table")
    if "date" not in frame.columns:
        raise ValueError("valuation provider date column missing")
    missing = [field for field in required_fields.values() if field not in frame.columns]
    if missing:
        raise ValueError("valuation provider schema missing fields: " + ", ".join(missing))
    work = frame.copy()
    work["_day"] = work["date"].map(_day)
    work = work[work["_day"].notna() & (work["_day"] <= target_day)].sort_values("_day")
    if work.empty:
        raise ValueError("valuation provider has no observations on or before market trade date")
    row = work.iloc[-1]
    values = {key: _finite(row[field]) for key, field in required_fields.items()}
    if any(value is None for value in values.values()):
        raise ValueError("latest valuation observation contains missing/non-finite required metrics")
    quantiles = [value for key, value in values.items() if "quantile" in key]
    if any(value < 0 or value > 1 for value in quantiles):
        raise ValueError("provider historical quantiles must be in [0, 1]")
    return str(row["_day"]), values


def build_snapshot(target_trade_date, pe_frame, pb_frame, captured_at=None):
    target_day = _day(target_trade_date)
    if not target_day:
        raise ValueError("invalid market trade date")
    pe_day, pe = _latest_record(pe_frame, PE_FIELDS, target_day)
    pb_day, pb = _latest_record(pb_frame, PB_FIELDS, target_day)
    all_values = {**pe, **pb}
    expected_count = len(PE_FIELDS) + len(PB_FIELDS)
    available_count = sum(value is not None for value in all_values.values())
    exact_date = pe_day == target_day and pb_day == target_day
    iso_day = lambda value: f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    return {
        "schema_version": 1,
        "scope": "broad_market",
        "status": "validated" if exact_date and available_count == expected_count else "partial",
        "trade_date": iso_day(target_day),
        "provider_dates": {"pe": iso_day(pe_day), "pb": iso_day(pb_day)},
        "source": "AkShare/Legulegu A-share historical valuation series",
        "provider": "AkShare",
        "metric_definition": (
            "Broad A-share median PE(TTM/LYR) and median PB with provider-supplied "
            "all-history and recent-10-year historical quantiles; not theme-level valuation"
        ),
        "coverage_basis": "required_broad_market_valuation_metrics",
        "records": 2,
        "coverage_pct": round(available_count / expected_count * 100, 1),
        "required_metrics": expected_count,
        "available_metrics": available_count,
        "metrics": all_values,
        "captured_at": captured_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": (
            "仅代表全A股市场整体估值，不代表任何行业或主题的估值分位；"
            "只有PE、PB历史序列均与行情交易日一致才标记validated。"
        ),
    }


def main():
    if not MARKET.exists():
        raise SystemExit("latest_market.csv missing")
    market = pd.read_csv(MARKET, encoding="utf-8-sig", dtype={"trade_date": str})
    if market.empty or "trade_date" not in market.columns:
        raise SystemExit("latest_market.csv has no valid trade_date")
    target_day = _day(market.iloc[0]["trade_date"])
    if not target_day:
        raise SystemExit("latest_market.csv trade_date is invalid")
    import akshare as ak
    pe_frame = call_with_retry(ak.stock_a_ttm_lyr, operation="A-share historical PE valuation")
    pb_frame = call_with_retry(ak.stock_a_all_pb, operation="A-share historical PB valuation")
    report = build_snapshot(target_day, pe_frame, pb_frame)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    if report["status"] != "validated":
        raise SystemExit("valuation data is stale/partial; snapshot retained as explicitly partial")


if __name__ == "__main__":
    main()
