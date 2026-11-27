#!/usr/bin/env python3
"""Read-only live smoke checks for flow, special-theme and valuation provider contracts.

This script does not write market data or update research snapshots. It is
intended for an explicitly triggered network check, separate from offline CI.
"""
from __future__ import annotations

import csv
import json
import math
import re
import sys
from io import StringIO

import pandas as pd
import requests
from datetime import date
from pathlib import Path

try:
    from scripts.build_valuation_snapshot import build_snapshot
    from scripts.research_provider_utils import call_with_retry, fetch_baidu_concept_tags, fetch_eastmoney_concept_members
except ModuleNotFoundError:
    from build_valuation_snapshot import build_snapshot
    from research_provider_utils import call_with_retry, fetch_baidu_concept_tags, fetch_eastmoney_concept_members

try:
    from scripts.theme_mapping_rules import star_next_new_eligible
except ModuleNotFoundError:
    from theme_mapping_rules import star_next_new_eligible



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
        "status": "partial" if (conflicts or coverage_pct is None or coverage_pct < 95.0 or unexpected_names) else "success",
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


def _fetch_ths_concept_members(board_code, http_get=None):
    """Read the THS concept constituent page used by the production collector."""
    http_get = http_get or requests.get
    cookie = ""
    try:
        import py_mini_racer
        from akshare.datasets import get_ths_js
        runtime = py_mini_racer.MiniRacer()
        with open(get_ths_js("ths.js"), "r", encoding="utf-8") as handle:
            runtime.eval(handle.read())
        cookie = str(runtime.call("v"))
    except Exception:
        pass
    url = (
        "https://q.10jqka.com.cn/gn/detail/field/199112/order/desc/"
        f"page/1/ajax/1/code/{board_code}"
    )
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Referer": "https://q.10jqka.com.cn/gn/",
    }
    if cookie:
        headers["Cookie"] = f"v={cookie}"
    response = http_get(url, headers=headers, timeout=15)
    response.raise_for_status()
    tables = pd.read_html(StringIO(response.text))
    if not tables:
        raise ValueError("THS concept constituent page has no HTML table")
    return tables[0], url


def _fetch_em_concept_members(ak, theme_name, http_get=None):
    """Use EastMoney mirror hosts after THS cloud blocking."""
    members, meta = fetch_eastmoney_concept_members(theme_name, get=http_get)
    return pd.DataFrame(members), meta["taxonomy_node_id"], meta["source_url"]

def validate_ths_theme_concept(ak, theme_name, as_of=None, max_ipo_checks=None, http_get=None):
    provider = "THS"
    fallback_detail = None
    try:
        names = ak.stock_board_concept_name_ths()
        if names is None or names.empty:
            raise ValueError("THS concept-name taxonomy is empty")
        name_col = next((col for col in ("name", "名称", "概念名称") if col in names.columns), None)
        code_col = next((col for col in ("code", "代码", "板块代码") if col in names.columns), None)
        if not name_col or not code_col:
            raise ValueError(f"THS concept taxonomy schema changed: {list(names.columns)}")
        matched = names[names[name_col].astype(str).str.strip() == theme_name]
        if matched.empty:
            raise LookupError(f"exact THS concept not found: {theme_name}")
        board_code = str(matched.iloc[0][code_col]).strip()
        member_fn = getattr(ak, "stock_board_concept_cons_ths", None)
        if member_fn is not None:
            members = member_fn(symbol=theme_name)
            source_url = f"https://q.10jqka.com.cn/gn/detail/field/199112/order/desc/code/{board_code}"
        else:
            members, source_url = _fetch_ths_concept_members(board_code, http_get=http_get)
        if members is None or members.empty:
            raise ValueError(f"THS concept constituent page empty for {theme_name}")
    except Exception as ths_exc:
        try:
            members, board_code, source_url = _fetch_em_concept_members(ak, theme_name, http_get=http_get)
            provider = "EastMoney"
            fallback_detail = f"THS exact taxonomy/constituents unavailable: {type(ths_exc).__name__}: {str(ths_exc)[:180]}"
        except LookupError as em_exc:
            return {"theme": theme_name, "status": "partial",
                    "provider": None,
                    "detail": f"THS unavailable ({str(ths_exc)[:120]}); {str(em_exc)[:180]}"}
        except RuntimeError as em_exc:
            return {"theme": theme_name, "status": "unavailable",
                    "provider": None,
                    "detail": f"THS unavailable ({str(ths_exc)[:120]}); EastMoney mirrors unavailable ({str(em_exc)[:180]})"}
    member_code_col = next((col for col in ("代码", "股票代码", "证券代码", "code")
                            if col in members.columns), None)
    if not member_code_col:
        raise ValueError(f"{provider} concept constituent schema changed: {list(members.columns)}")
    codes = sorted({str(value).strip().zfill(6) for value in members[member_code_col]
                    if str(value).strip() and str(value).strip().lower() != "nan"})
    result = {
        "theme": theme_name, "status": "success", "provider": provider,
        "board_code": board_code, "source_url": source_url,
        "fallback_detail": fallback_detail,
        "candidate_count": len(codes), "member_count": len(codes),
        "member_page_scope": "first page smoke only",
    }
    if theme_name != "科创次新股":
        return result
    if as_of is None:
        as_of = date.today()
    failures, expired, eligible = [], 0, 0
    ipo_fn = getattr(ak, "stock_ipo_info", None)
    if ipo_fn is None:
        return {**result, "status": "partial", "detail": "IPO listing-date endpoint unavailable"}
    checked_codes = codes if max_ipo_checks is None else codes[:max(0, int(max_ipo_checks))]
    untested_candidates = max(0, len(codes) - len(checked_codes))
    for code in checked_codes:
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
        "listing_checks_performed": len(checked_codes),
        "untested_candidates": untested_candidates,
        "listing_age_policy": "THS: STAR-listed and listed less than one calendar year ago",
    })
    if failures or expired or untested_candidates:
        result["status"] = "partial"
    return result

def is_transport_unavailable(exc):
    message = f"{type(exc).__name__}: {exc}".lower()
    return any(token in message for token in (
        "timeout", "timed out", "502", "503", "504", "403", "429",
        "connection", "remote end closed", "remote disconnected",
        "max retries", "network is unreachable", "proxyerror",
    ))


def smoke_exit_code(results):
    """Only schema/contract failures fail CI; provider outages remain visible."""
    return 1 if any(item.get("status") == "failed" for item in results) else 0


def main():
    import akshare as ak

    endpoint = getattr(ak, "stock_fund_flow_industry_ths", None)
    if endpoint is None:
        raise SystemExit("FAIL: installed AkShare has no stock_fund_flow_industry_ths endpoint")

    results = []
    market_trade_day = None
    market_target_day = None
    try:
        latest_path = Path(__file__).resolve().parents[1] / "data" / "latest_market.csv"
        with latest_path.open(encoding="utf-8-sig", newline="") as handle:
            market_row = next(csv.DictReader(handle))
        raw_day = str(market_row.get("trade_date") or "")
        digits = "".join(ch for ch in raw_day if ch.isdigit())
        if len(digits) < 8:
            raise ValueError(f"invalid market trade date: {raw_day!r}")
        market_target_day = digits[:8]
        market_trade_day = date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except Exception as exc:
        raise SystemExit(f"FAIL: cannot read market trade date: {type(exc).__name__}: {str(exc)[:300]}")

    try:
        latest_path = Path(__file__).resolve().parents[1] / "data" / "latest_market.csv"
        with latest_path.open(encoding="utf-8-sig", newline="") as handle:
            universe_codes = [str(row.get("symbol") or "").zfill(6)
                              for row in csv.DictReader(handle)]
        baidu_tags, baidu_meta = fetch_baidu_concept_tags(
            universe_codes, batch_size=20, max_workers=3, timeout_seconds=8.0
        )
        results.append({
            "period": "baidu_concept_tag_smoke",
            "status": baidu_meta["status"],
            "provider": baidu_meta["provider"],
            "coverage_pct": baidu_meta["coverage_pct"],
            "input_codes": baidu_meta["input_codes"],
            "returned_codes": baidu_meta["returned_codes"],
            "records": baidu_meta["records"],
            "failed_batches": baidu_meta["failed_batches"],
        })
        normalize_concept = lambda value: re.sub(r"[\s·（）()ⅢⅣ]", "", str(value or "")).lower()
        for theme_name in ("烟草", "工业大麻", "参股民营银行", "科创次新股"):
            matches = [
                code for code, concepts in baidu_tags.items()
                if any(normalize_concept(item.get("name")) == normalize_concept(theme_name)
                       for item in concepts)
            ]
            results.append({
                "period": "baidu_special_theme_" + theme_name,
                "theme": theme_name,
                "status": "success" if matches and baidu_meta["status"] == "success" else "partial",
                "provider": "Baidu Stock Market",
                "candidate_count": len(matches),
                "coverage_pct": baidu_meta["coverage_pct"],
                "source_url": baidu_meta["source_url"],
            })
    except Exception as exc:
        results.append({
            "period": "baidu_concept_tag_smoke",
            "status": "unavailable" if is_transport_unavailable(exc) else "failed",
            "error": f"{type(exc).__name__}: {str(exc)[:300]}",
        })

    try:
        universe_names = ths_industry_universe_names(ak)
    except Exception as exc:
        universe_names = None
        results.append({"period": "industry_universe", "status": "failed",
                        "error": f"{type(exc).__name__}: {str(exc)[:300]}"})

    for period, symbol in PERIOD_SYMBOLS.items():
        try:
            frame = endpoint(symbol=symbol)
            facts = validate_ths_flow_frame(frame, universe_names=universe_names)
            results.append({"period": period, "symbol": symbol, **facts})
        except Exception as exc:
            results.append({
                "period": period, "symbol": symbol,
                "status": "unavailable" if is_transport_unavailable(exc) else "failed",
                "error": f"{type(exc).__name__}: {str(exc)[:400]}",
            })

    # Exact THS taxonomy names are resolved separately; STAR listing checks
    # are bounded in smoke mode and report the untested remainder explicitly.
    for theme_name in ("烟草", "工业大麻", "科创次新股"):
        try:
            facts = validate_ths_theme_concept(
                ak, theme_name, market_trade_day, max_ipo_checks=1
            )
            results.append({"period": "special_theme_" + theme_name,
                            "symbol": theme_name, **facts})
        except Exception as exc:
            results.append({"period": "special_theme_" + theme_name,
                            "symbol": theme_name,
                            "status": "unavailable" if is_transport_unavailable(exc) else "failed",
                            "error": f"{type(exc).__name__}: {str(exc)[:400]}"})

    # Broad-market valuation is separate from theme-level valuation.
    try:
        pe_frame = call_with_retry(ak.stock_a_ttm_lyr,
                                   operation="A-share historical PE valuation smoke")
        pb_frame = call_with_retry(ak.stock_a_all_pb,
                                   operation="A-share historical PB valuation smoke")
        valuation = build_snapshot(market_target_day, pe_frame, pb_frame)
        results.append({
            "period": "broad_market_valuation",
            "symbol": "stock_a_ttm_lyr + stock_a_all_pb",
            "status": valuation["status"],
            "trade_date": valuation["trade_date"],
            "provider_dates": valuation["provider_dates"],
            "coverage_pct": valuation["coverage_pct"],
            "scope": valuation["scope"],
        })
    except Exception as exc:
        results.append({
            "period": "broad_market_valuation",
            "symbol": "stock_a_ttm_lyr + stock_a_all_pb",
            "status": "unavailable" if is_transport_unavailable(exc) else "failed",
            "error": f"{type(exc).__name__}: {str(exc)[:400]}",
        })

    report = {
        "providers": ["THS", "Baidu Stock Market", "AkShare/Legulegu"],
        "endpoint": "THS industry flow + Baidu/THS concept tags + broad-market PE/PB",
        "akshare_version": getattr(ak, "__version__", "unknown"),
        "read_only": True,
        "as_of_trade_date": market_trade_day.isoformat(),
        "results": results,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # Partial coverage is visible but does not turn a structurally valid
    # provider response into a false hard failure. Empty/schema-invalid
    # responses remain hard failures.
    return smoke_exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
