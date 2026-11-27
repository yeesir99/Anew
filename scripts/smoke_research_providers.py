#!/usr/bin/env python3
"""Read-only live smoke checks for industry capital-flow provider contracts.

This script does not write market data or update research snapshots. It is
intended for an explicitly triggered network check, separate from offline CI.
"""
from __future__ import annotations

import json
import math
import sys


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
