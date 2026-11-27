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


def validate_ths_flow_frame(frame):
    """Return normalized contract facts or raise when the THS schema is unusable."""
    if frame is None or getattr(frame, "empty", True):
        raise ValueError("provider returned an empty result")
    columns = list(getattr(frame, "columns", []))
    industry_col = next((name for name in INDUSTRY_COLUMNS if name in columns), None)
    net_col = next((name for name in NET_COLUMNS if name in columns), None)
    if not industry_col or not net_col:
        raise ValueError("schema mismatch; columns=" + repr(columns))
    by_name = {}
    raw_count = 0
    for _, row in frame.iterrows():
        name = str(row.get(industry_col, "")).strip()
        amount = _finite_number(row.get(net_col))
        if name and name.lower() != "nan" and amount is not None:
            raw_count += 1
            previous = by_name.get(name)
            if previous is not None and previous != amount:
                raise ValueError(f"conflicting duplicate industry rows: {name}")
            by_name[name] = amount
    if not by_name:
        raise ValueError("no rows with both industry name and finite net amount")
    return {
        "records": len(by_name),
        "raw_records": raw_count,
        "unique_industries": len(by_name),
        "duplicate_rows_removed": raw_count - len(by_name),
        "industry_column": industry_col,
        "net_amount_column": net_col,
        "source_unit": "provider-returned value; expected 亿元 per AkShare THS endpoint contract",
    }


def main():
    import akshare as ak

    endpoint = getattr(ak, "stock_fund_flow_industry_ths", None)
    if endpoint is None:
        raise SystemExit("FAIL: installed AkShare has no stock_fund_flow_industry_ths endpoint")

    results = []
    for period, symbol in PERIOD_SYMBOLS.items():
        try:
            frame = endpoint(symbol=symbol)
            facts = validate_ths_flow_frame(frame)
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
    if any(item["status"] != "success" for item in results):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
