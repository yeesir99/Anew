#!/usr/bin/env python3
"""Read-only live smoke checks for industry capital-flow provider contracts.

This script does not write market data or update research snapshots. It is
intended for an explicitly triggered network check, separate from offline CI.
"""
from __future__ import annotations

import json
import sys

import pandas as pd


PERIOD_SYMBOLS = {
    "今日": "即时",
    "5日": "5日排行",
    "10日": "10日排行",
}
INDUSTRY_COLUMNS = ("行业", "行业名称", "板块")
NET_COLUMNS = ("净额", "净流入", "资金净流入")


def validate_ths_flow_frame(frame):
    """Return normalized contract facts or raise when the THS schema is unusable."""
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError("provider returned an empty or non-DataFrame result")
    industry_col = next((name for name in INDUSTRY_COLUMNS if name in frame.columns), None)
    net_col = next((name for name in NET_COLUMNS if name in frame.columns), None)
    if not industry_col or not net_col:
        raise ValueError("schema mismatch; columns=" + repr(list(frame.columns)))
    names = frame[industry_col].astype(str).str.strip()
    amounts = pd.to_numeric(
        frame[net_col].astype(str).str.replace(",", "", regex=False).str.replace("亿元", "", regex=False),
        errors="coerce",
    )
    valid = names.ne("") & names.str.lower().ne("nan") & amounts.notna()
    count = int(valid.sum())
    if count == 0:
        raise ValueError("no rows with both industry name and finite net amount")
    return {
        "records": count,
        "unique_industries": int(names[valid].nunique()),
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
