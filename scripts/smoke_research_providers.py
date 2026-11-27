#!/usr/bin/env python3
"""Read-only live smoke checks for industry capital-flow provider contracts.

This script does not write market data or update research snapshots. It is
intended for an explicitly triggered network check, separate from offline CI.
"""
from __future__ import annotations

import csv
import json
import math
import sys
from datetime import date
from pathlib import Path

from scripts.theme_mapping_rules import star_next_new_eligible


PERIOD_SYMBOLS = {
    "今日": "即时",
    "5日": "5日排行",
    "10日": "10日排行",
}
INDUSTRY_COLUMNS = ("行业", "行业名称", "板块")
NET_COLUMNS = ("净额", "净流入", "资金净流入")


def _finite_number(value):
    try:
        number = float(str(value).replace(",", "").replace("亿元", "").strip())
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def ths_industry_universe_names(ak):
    frame = ak.stock_board_industry_name_ths()
    if frame is None or frame.empty:
        raise ValueError("THS industry universe is empty")
    name_col = next((col for col in ("name", "行业", "行业名称", "板块名称")
                     if col in frame.columns), None)
    if not name_col:
        raise ValueError(f"THS industry universe name column missing: {list(frame.columns)}")
    names = {str(value).strip() for value in frame[name_col]
             if str(value).strip() and str(value).strip().lower() != "nan"}
    if not names:
        raise ValueError("THS industry universe has no valid names")
    return names


def validate_ths_flow_frame(frame, universe_names=None):
    """Return normalized contract facts or raise when the THS schema is unusable."""
    if frame is None or getattr(frame, "empty", True):
        raise ValueError("provider returned an empty result")
    columns = list(getattr(frame, "columns", []))
    industry_col = next((name for name in INDUSTRY_COLUMNS if name in columns), None)
    net_col = next((name for name in NET_COLUMNS if name in columns), None)
    if not industry_col or not net_col:
        raise ValueError("schema mismatch; columns=" + repr(columns))
    by_name = {}
    source_rows = {}
    conflicts = {}
    raw_count = 0
    identical_duplicates = 0
    for _, row in frame.iterrows():
        name = str(row.get(industry_col, "")).strip()
        amount = _finite_number(row.get(net_col))
        if name and name.lower() != "nan" and amount is not None:
            raw_count += 1
            if name in conflicts:
                row_data = row.to_dict() if hasattr(row, "to_dict") else dict(row)
                conflicts[name].append({str(key): str(value) for key, value in row_data.items()})
                continue
            previous = by_name.get(name)
            row_data = row.to_dict() if hasattr(row, "to_dict") else dict(row)
            current_row = {str(key): str(value) for key, value in row_data.items()}
            if previous is not None and previous != amount:
                conflicts[name] = [source_rows.get(name), current_row]
                by_name.pop(name, None)
                source_rows.pop(name, None)
                continue
            if previous is not None:
                identical_duplicates += 1
                continue
            by_name[name] = amount
            source_rows[name] = current_row
    if not by_name:
        raise ValueError("no unambiguous rows with both industry name and finite net amount")
    expected_names = set(universe_names or [])
    observed_names = set(by_name)
    unexpected_names = sorted(observed_names - expected_names) if expected_names else []
    coverage_pct = (
        round(len(observed_names & expected_names) / len(expected_names) * 100, 2)
        if expected_names and not unexpected_names else None
    )
    return {
        "status": "partial" if (conflicts or coverage_pct is None or unexpected_names) else "success",
        "records": len(by_name),
        "raw_records": raw_count,
        "unique_industries": len(by_name),
        "expected_industries": len(expected_names) if expected_names else None,
        "coverage_pct": coverage_pct,
        "unexpected_industries": unexpected_names,
        "duplicate_rows_removed": identical_duplicates,
        "conflicting_duplicates": [
            {"industry": name, "rows": rows} for name, rows in conflicts.items()
        ],
        "industry_column": industry_col,
        "net_amount_column": net_col,
        "source_unit": "provider-returned value; expected 亿元 per AkShare THS endpoint contract",
    }


def validate_ths_theme_concept(ak, theme_name, as_of=None):
    names = ak.stock_board_concept_name_ths()
    if names is None or names.empty:
        raise ValueError("THS concept-name taxonomy is empty")
    name_col = next((col for col in ("name", "名称", "概念名称") if col in names.columns), None)
    code_col = next((col for col in ("code", "代码", "板块代码") if col in names.columns), None)
    if not name_col or not code_col:
        raise ValueError(f"THS concept taxonomy schema changed: {list(names.columns)}")
    matched = names[names[name_col].astype(str).str.strip() == theme_name]
    if matched.empty:
        return {"theme": theme_name, "status": "partial", "detail": "exact THS concept not found"}
    board_code = str(matched.iloc[0][code_col]).strip()
    members = ak.stock_board_concept_cons_ths(symbol=theme_name)
    if members is None or members.empty:
        raise ValueError(f"THS concept constituent endpoint empty for {theme_name}")
    member_code_col = next((col for col in ("代码", "股票代码", "证券代码", "code")
                            if col in members.columns), None)
    if not member_code_col:
        raise ValueError(f"THS concept constituent schema changed: {list(members.columns)}")
    codes = sorted({str(value).strip().zfill(6) for value in members[member_code_col]
                    if str(value).strip() and str(value).strip().lower() != "nan"})
    result = {
        "theme": theme_name, "status": "success", "board_code": board_code,
        "source_url": f"https://q.10jqka.com.cn/gn/detail/field/199112/order/desc/code/{board_code}",
        "candidate_count": len(codes), "member_count": len(codes),
    }
    if theme_name != "科创次新股":
        return result
    if as_of is None:
        as_of = date.today()
    failures, expired, eligible = [], 0, 0
    ipo_fn = getattr(ak, "stock_ipo_info", None)
    if ipo_fn is None:
        return {**result, "status": "partial", "detail": "IPO listing-date endpoint unavailable"}
    for code in codes:
        if not code.startswith(("688", "689")):
            failures.append({"code": code, "error": "not a STAR Market code"})
            continue
        try:
            info = ipo_fn(symbol=code)
            item_col = next((col for col in ("item", "项目") if col in info.columns), None)
            value_col = next((col for col in ("value", "值") if col in info.columns), None)
            if not item_col or not value_col:
                raise ValueError("IPO info schema changed")
            values = info.loc[info[item_col].astype(str).str.strip() == "上市日期", value_col]
            if values.empty:
                raise ValueError("listing date absent")
            listed = date.fromisoformat(str(values.iloc[0])[:10])
            if star_next_new_eligible(code, listed, as_of):
                eligible += 1
            else:
                expired += 1
        except Exception as exc:
            failures.append({"code": code, "error": str(exc)[:120]})
    result.update({
        "eligible_within_one_calendar_year": eligible,
        "expired_or_outside_rule": expired,
        "listing_lookup_failures": failures,
        "listing_age_policy": "THS: STAR-listed and listed less than one calendar year ago",
    })
    if failures or expired:
        result["status"] = "partial"
    return result


def smoke_exit_code(results):
    """Partial source data is reported but does not fail the code contract."""
    return 1 if any(item.get("status") == "failed" for item in results) else 0


def main():
    import akshare as ak

    endpoint = getattr(ak, "stock_fund_flow_industry_ths", None)
    if endpoint is None:
        raise SystemExit("FAIL: installed AkShare has no stock_fund_flow_industry_ths endpoint")

    results = []
    try:
        universe_names = ths_industry_universe_names(ak)
    except Exception as exc:
        universe_names = None
        print(json.dumps({"industry_universe_status": "unavailable", "detail": str(exc)[:500]}, ensure_ascii=False))
    as_of = None
    try:
        latest_path = Path(__file__).resolve().parents[1] / "data" / "latest_market.csv"
        with latest_path.open(encoding="utf-8-sig", newline="") as handle:
            latest_row = next(csv.DictReader(handle))
        as_of = date.fromisoformat(str(latest_row.get("trade_date") or "")[:10])
    except Exception:
        as_of = date.today()
    for period, symbol in PERIOD_SYMBOLS.items():
        try:
            frame = endpoint(symbol=symbol)
            facts = validate_ths_flow_frame(frame, universe_names=universe_names)
            results.append({"period": period, "symbol": symbol, "status": "success", **facts})
        except Exception as exc:
            results.append({
                "period": period, "symbol": symbol, "status": "failed",
                "error": f"{type(exc).__name__}: {str(exc)[:400]}",
            })

    report = {
        "provider": "THS",
        "endpoint": "stock_fund_flow_industry_ths",
        "akshare_version": getattr(ak, "__version__", "unknown"),
        "read_only": True,
        "results": results,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # A provider response with ambiguous duplicate names is useful but
    # incomplete; keep the run green while exposing "partial" in the JSON.
    # Empty/schema-invalid endpoint responses remain hard failures.
    return smoke_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
