#!/usr/bin/env python3
"""Collect public industry, sector-flow and exchange ETF-share snapshots.

This script only runs after the live market collector has validated today's
full-market close. It never reads private holdings, accounts or notes.
"""
from __future__ import annotations

import csv
import json
import logging
import os
import time
import requests
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
try:
    from research_provider_utils import call_with_retry
except ModuleNotFoundError:
    from scripts.research_provider_utils import call_with_retry

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TZ = ZoneInfo("Asia/Shanghai")
LOG = logging.getLogger("research-collect")
DATA.mkdir(exist_ok=True)


def number(value):
    try:
        value = pd.to_numeric(str(value).replace(",", "").replace("%", "").replace("亿元", "").strip(), errors="coerce")
        return None if pd.isna(value) else float(value)
    except Exception:
        return None


def is_eastmoney_rate_limit(exc):
    message = str(exc).lower()
    return any(token in message for token in (
        "push2ratelimiterror", "rate limit", "too many requests",
        "短时间内请求过多", "429", "频繁访问",
    ))


def ths_industry_universe_names(ak):
    """Return exact THS industry taxonomy names; None means coverage is unknown."""
    fn = getattr(ak, "stock_board_industry_name_ths", None)
    if fn is None:
        return None
    frame = call_with_retry(fn, operation="THS industry universe")
    if frame is None or frame.empty:
        return None
    name_col = next((x for x in ("name", "行业", "行业名称", "板块名称")
                     if x in frame.columns), None)
    if not name_col:
        return None
    names = {str(x).strip() for x in frame[name_col].tolist()
             if x is not None and str(x).strip() and str(x).lower() != "nan"}
    return names or None


def ths_industry_universe_count(ak):
    names = ths_industry_universe_names(ak)
    return len(names) if names else None


def ths_industry_flow_rows(frame, indicator, trade_day, captured_at,
                           expected_industry_names=None, return_diagnostics=False):
    """Normalize THS's provider-defined net amount; source unit is CNY 100m."""
    if frame is None or frame.empty:
        raise RuntimeError("THS industry flow returned no rows")
    name_col = next((x for x in ("行业", "行业名称", "板块") if x in frame.columns), None)
    net_col = next((x for x in ("净额", "净流入", "资金净流入") if x in frame.columns), None)
    change_col = next((x for x in ("行业-涨跌幅", "阶段涨跌幅", "涨跌幅") if x in frame.columns), None)
    if not name_col or not net_col:
        raise RuntimeError("THS industry flow schema changed: " + str(list(frame.columns)))
    by_industry = {}
    source_rows = {}
    conflict_rows = {}
    raw_records = 0
    identical_duplicates = 0
    for _, item in frame.iterrows():
        name = str(item.get(name_col, "")).strip()
        net_yi = number(item.get(net_col))
        change = number(item.get(change_col)) if change_col else None
        if not name or name.lower() == "nan" or net_yi is None:
            continue
        raw_records += 1
        if name in conflict_rows:
            conflict_rows[name].append({str(key): str(value) for key, value in item.to_dict().items()})
            continue
        normalized = {
            "trade_date": trade_day.isoformat(),
            "indicator": indicator,
            "industry": name,
            "change_pct": change,
            "net_inflow_cny": net_yi * 1e8,
            "main_net_inflow_cny": None,
            "main_net_ratio_pct": None,
            "provider": "THS",
            "source": "AKShare/THS industry fund-flow ranking",
            "source_field": net_col,
            "metric_definition": f"THS provider-defined industry net amount from {net_col}; source unit 亿元 converted to CNY; not relabelled as EastMoney main-net flow",
            "captured_at": captured_at,
        }
        previous = by_industry.get(name)
        current_source_row = {str(key): str(value) for key, value in item.to_dict().items()}
        if previous:
            if (previous["net_inflow_cny"] != normalized["net_inflow_cny"]
                    or previous["change_pct"] != normalized["change_pct"]):
                conflict_rows[name] = [source_rows.get(name), current_source_row]
                by_industry.pop(name, None)
                source_rows.pop(name, None)
                continue
            identical_duplicates += 1
            continue
        by_industry[name] = normalized
        source_rows[name] = current_source_row
    rows = list(by_industry.values())
    if not rows:
        raise RuntimeError("THS industry flow contains no unambiguous finite net-amount rows")
    observed_names = set(by_industry)
    expected_names = set(expected_industry_names or [])
    unexpected_names = sorted(observed_names - expected_names) if expected_names else []
    coverage_pct = (
        round(len(observed_names & expected_names) / len(expected_names) * 100, 2)
        if expected_names and not unexpected_names else None
    )
    diagnostics = {
        "raw_records": raw_records,
        "records": len(rows),
        "unique_industries": len(rows),
        "duplicate_rows_removed": identical_duplicates,
        "expected_industries": len(expected_names) if expected_names else None,
        "coverage_pct": coverage_pct,
        "unexpected_industries": unexpected_names,
        "conflicting_duplicates": [
            {"industry": name, "rows": source_rows_for_conflict}
            for name, source_rows_for_conflict in conflict_rows.items()
        ],
    }
    if conflict_rows and not return_diagnostics:
        details = "; ".join(
            f"{name}: {rows[0]} vs {rows[1]}"
            for name, rows in conflict_rows.items()
        )
        raise RuntimeError("THS industry flow has conflicting duplicate rows: " + details)
    return (rows, diagnostics) if return_diagnostics else rows


def append_rows(path: Path, rows: list[dict], keys: tuple[str, ...]):
    if not rows:
        return 0
    old = []
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as f:
            old = list(csv.DictReader(f))
    merged = {tuple(str(x.get(k, "")) for k in keys): x for x in old}
    for row in rows:
        merged[tuple(str(row.get(k, "")) for k in keys)] = row
    all_rows = list(merged.values())
    fields = list(dict.fromkeys(k for x in all_rows for k in x))
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(all_rows)
    tmp.replace(path)
    return len(rows)


def replace_flow_period_rows(path: Path, trade_date: str, indicator: str, rows: list[dict]):
    """Replace one current trade-date/period slice; never retain stale omitted rows."""
    old = []
    fieldnames = []
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or [])
            old = list(reader)
    kept = [
        row for row in old
        if not (str(row.get("trade_date") or "")[:10] == trade_date
                and str(row.get("indicator") or "") == indicator)
    ]
    combined = kept + list(rows or [])
    fields = list(dict.fromkeys(fieldnames + [key for row in combined for key in row]))
    if not fields:
        return 0
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(combined)
    tmp.replace(path)
    return len(rows or [])


def load_validated_trade_day() -> date:
    path = DATA / "latest_market.csv"
    if not path.exists():
        raise RuntimeError("缺少经过校验的 latest_market.csv")
    frame = pd.read_csv(path, encoding="utf-8-sig")
    if "trade_date" not in frame or len(frame) < 2500:
        raise RuntimeError("行情日期或覆盖率不满足研究采集条件")
    # The live collector stores dates as YYYYMMDD integers/strings.
    # pandas otherwise interprets values such as 20261120 as nanoseconds
    # after the Unix epoch and silently turns them into 1970-01-01.
    raw_dates = frame["trade_date"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    parsed = pd.to_datetime(raw_dates, format="%Y%m%d", errors="coerce")
    missing = parsed.isna()
    if missing.any():
        parsed.loc[missing] = pd.to_datetime(raw_dates.loc[missing], errors="coerce")
    day = parsed.dropna().max()
    if pd.isna(day):
        raise RuntimeError("无法识别行情交易日")
    trade_day = day.date()
    today = datetime.now(TZ).date()
    gap = sum(1 for i in range(1, max(0, (today - trade_day).days) + 1)
              if (trade_day + timedelta(days=i)).weekday() < 5)
    if trade_day > today or gap > 1:
        raise RuntimeError(f"行情日期 {trade_day} 非最新有效交易日（工作日差 {gap}），跳过行业/资金采集")
    return trade_day


def fetch_sse_etf_scale_direct(day: date) -> pd.DataFrame:
    """Retry the SSE public ETF-size endpoint directly if AkShare's wrapper gets HTML/empty output."""
    url = "https://query.sse.com.cn/commonQuery.do"
    stat_date = day.strftime("%Y-%m-%d")
    params = {
        "isPagination": "true", "pageHelp.pageSize": "10000",
        "pageHelp.pageNo": "1", "pageHelp.beginPage": "1",
        "pageHelp.cacheSize": "1", "pageHelp.endPage": "1",
        "sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L",
        "STAT_DATE": stat_date,
    }
    headers = {"Referer":"https://www.sse.com.cn/",
        "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
    last_error = None
    for attempt in range(3):
        try:
            response = requests.get(url, params=params, headers=headers, timeout=(10, 25))
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("result") if isinstance(payload, dict) else None
            if not isinstance(rows, list) or not rows:
                raise RuntimeError("SSE response has no result rows")
            frame = pd.DataFrame(rows).rename(columns={"NUM":"序号","SEC_CODE":"基金代码",
                "SEC_NAME":"基金简称","ETF_TYPE":"ETF类型","STAT_DATE":"统计日期","TOT_VOL":"基金份额"})
            required = ["基金代码","基金份额","统计日期"]
            if not set(required).issubset(frame.columns):
                raise RuntimeError("SSE response schema changed: " + str(list(frame.columns)))
            frame["基金份额"] = pd.to_numeric(frame["基金份额"], errors="coerce") * 10000
            frame["统计日期"] = pd.to_datetime(frame["统计日期"], errors="coerce")
            frame = frame.dropna(subset=["基金代码","基金份额","统计日期"])
            if frame.empty:
                raise RuntimeError("SSE result rows failed schema/value validation")
            return frame
        except Exception as exc:
            last_error = str(exc)
            if attempt < 2:
                time.sleep(1 + attempt * 2)
    raise RuntimeError("SSE direct endpoint retries exhausted: " + str(last_error)[:300])


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    trade_day = load_validated_trade_day()
    import akshare as ak

    checked = datetime.now(TZ).isoformat(timespec="seconds")
    statuses = []
    ths_universe_names = None
    # Once EastMoney explicitly rate-limits this run, stop calling its other
    # endpoints and switch to the independent THS provider.
    eastmoney_circuit_open = False
    # Industry snapshot: prefer EastMoney, but retain a THS live ranking as
    # an explicitly separate fallback so taxonomies are not mixed into the
    # six-month Shenwan rotation history.
    ths_snapshot_path = DATA / "industry_snapshot_ths.json"
    if ths_snapshot_path.exists():
        ths_snapshot_path.unlink()
    try:
        frame = ak.stock_board_industry_name_em()
        if frame is None or frame.empty:
            raise RuntimeError("行业接口返回空数据")
        name_col = next((x for x in ("板块名称", "行业名称", "名称") if x in frame.columns), None)
        change_col = next((x for x in ("涨跌幅", "涨跌幅(%)") if x in frame.columns), None)
        amount_col = next((x for x in ("总成交额", "成交额") if x in frame.columns), None)
        if not name_col or not change_col:
            raise RuntimeError("行业字段不匹配：" + str(list(frame.columns)))
        rows = []
        for _, item in frame.iterrows():
            name = str(item.get(name_col, "")).strip()
            change = number(item.get(change_col))
            if not name or name == "nan" or change is None:
                continue
            amount = number(item.get(amount_col)) if amount_col else None
            rows.append({"trade_date": trade_day.isoformat(), "industry": name,
                         "change_pct": change, "turnover_cny": amount,
                         "source": "AKShare/EastMoney public industry ranking",
                         "captured_at": checked})
        append_rows(DATA / "industry_history.csv", rows, ("trade_date", "industry"))
        statuses.append({"source": "industry_snapshot", "status": "success", "records": len(rows),
                         "provider": "EastMoney",
                         "detail": "行业涨跌幅及成交额快照；代理分不等同产业基本面评分"})
    except Exception as exc:
        eastmoney_circuit_open = is_eastmoney_rate_limit(exc)
        LOG.warning("EastMoney industry snapshot failed; trying THS fallback: %s", str(exc)[:240])
        statuses.append({"source": "industry_snapshot", "status": "failed", "detail": str(exc)[:500]})
        try:
            ths_fn = getattr(ak, "stock_board_industry_summary_ths", None)
            if ths_fn is None:
                raise RuntimeError("stock_board_industry_summary_ths unavailable")
            ths_frame = call_with_retry(ths_fn, operation="THS industry summary")
            if ths_frame is None or ths_frame.empty:
                raise RuntimeError("THS industry summary returned empty")
            name_col = next((x for x in ("板块", "行业", "板块名称") if x in ths_frame.columns), None)
            change_col = next((x for x in ("涨跌幅", "涨跌幅(%)") if x in ths_frame.columns), None)
            amount_col = next((x for x in ("总成交额", "成交额") if x in ths_frame.columns), None)
            net_col = next((x for x in ("净额", "净流入", "主力净流入") if x in ths_frame.columns), None)
            up_col = next((x for x in ("上涨家数", "上涨股票数") if x in ths_frame.columns), None)
            down_col = next((x for x in ("下跌家数", "下跌股票数") if x in ths_frame.columns), None)
            leader_col = next((x for x in ("领涨股", "领涨股票") if x in ths_frame.columns), None)
            leader_change_col = next((x for x in ("领涨股-涨跌幅", "领涨股涨跌幅") if x in ths_frame.columns), None)
            if not name_col or not change_col:
                raise RuntimeError("THS industry fields changed: " + str(list(ths_frame.columns)))
            ths_rows = []
            for _, item in ths_frame.iterrows():
                name = str(item.get(name_col, "")).strip()
                change = number(item.get(change_col))
                if not name or name == "nan" or change is None:
                    continue
                amount_yi = number(item.get(amount_col)) if amount_col else None
                net_yi = number(item.get(net_col)) if net_col else None
                amount_cny = amount_yi * 1e8 if amount_yi is not None else None
                net_cny = net_yi * 1e8 if net_yi is not None else None
                ratio = net_cny / amount_cny * 100 if net_cny is not None and amount_cny else None
                leader_value = item.get(leader_col) if leader_col else None
                leader_name = "" if leader_value is None or pd.isna(leader_value) else str(leader_value).strip()
                ths_rows.append({"trade_date":trade_day.isoformat(),"industry_name":name,
                    "change_pct":change,"turnover_cny":amount_cny,
                    "net_inflow_cny":net_cny,"net_inflow_ratio_pct":ratio,
                    "main_net_inflow_cny":None,"main_net_ratio_pct":None,
                    "source_field":net_col,
                    "metric_definition":f"THS industry-summary net amount from {net_col}; source unit 亿元 converted to CNY; not EastMoney main-net flow",
                    "up_count":number(item.get(up_col)) if up_col else None,
                    "down_count":number(item.get(down_col)) if down_col else None,
                    "leader_name":leader_name,
                    "leader_change_pct":number(item.get(leader_change_col)) if leader_change_col else None,
                    "source":"AKShare/THS live industry summary","captured_at":checked})
            if not ths_rows:
                raise RuntimeError("THS industry summary has no valid rows")
            ths_universe_names = ths_industry_universe_names(ak)
            ths_universe_count = len(ths_universe_names) if ths_universe_names else None
            ths_coverage = (len({x["industry_name"] for x in ths_rows}) / ths_universe_count * 100.0
                            if ths_universe_count else None)
            ths_snapshot = {"trade_date":trade_day.isoformat(),"status":"success",
                "provider":"THS","records":len(ths_rows),
                "coverage_pct":round(min(100.0, ths_coverage), 2) if ths_coverage is not None else None,
                "expected_industries":ths_universe_count,
                "metric_definition":"THS industry-summary net amount; not EastMoney main-net flow",
                "captured_at":checked,"items":ths_rows,
                "note":"同花顺实时行业榜备用源；独立展示，不混入申万历史轮动序列。"}
            ths_snapshot_path.write_text(json.dumps(ths_snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            statuses.append({"source":"industry_snapshot_ths","status":"success",
                "records":len(ths_rows),"provider":"THS",
                "coverage_pct":round(min(100.0, ths_coverage), 2) if ths_coverage is not None else None,
                "expected_industries":ths_universe_count,
                "detail":"EastMoney限流时使用同花顺行业实时榜；独立展示，金额单位已由亿元转换为元"})
        except Exception as ths_exc:
            LOG.exception("THS industry fallback failed")
            statuses.append({"source":"industry_snapshot_ths","status":"failed","detail":str(ths_exc)[:500]})

    # Sector capital-flow estimates. EastMoney is preferred; THS is an
    # independently labelled fallback for each failed period. Provider metrics
    # remain distinct: THS "净额" is never relabelled as EastMoney "主力净流入".
    flow_rows = []
    flow_period_outcomes = []
    if not ths_universe_names:
        try:
            ths_universe_names = ths_industry_universe_names(ak)
        except Exception as exc:
            ths_universe_names = None
            LOG.warning("THS industry taxonomy unavailable: %s", str(exc)[:180])

    for indicator in ("今日", "5日", "10日"):
        period_rows = []
        period_status = "failed"
        em_error = None
        try:
            if eastmoney_circuit_open:
                raise RuntimeError("EastMoney circuit breaker open after rate-limit response")
            fn = getattr(ak, "stock_fund_flow_sector_rank_em", None)
            if fn is None:
                raise RuntimeError("stock_fund_flow_sector_rank_em unavailable")
            frame = call_with_retry(
                lambda: fn(indicator=indicator, sector_type="行业资金流"),
                operation=f"EastMoney sector flow {indicator}",
                should_retry=lambda error: not is_eastmoney_rate_limit(error),
            )
            if frame is None or frame.empty:
                raise RuntimeError("EastMoney sector-flow endpoint returned empty data")
            name_col = next((x for x in ("名称", "板块名称", "行业", "板块") if x in frame.columns), None)
            flow_col = next((x for x in (indicator + "主力净流入-净额", "主力净流入-净额") if x in frame.columns), None)
            ratio_col = next((x for x in (indicator + "主力净流入-净占比", "主力净流入-净占比") if x in frame.columns), None)
            change_col = next((x for x in (indicator + "涨跌幅", "涨跌幅") if x in frame.columns), None)
            if not name_col or not flow_col:
                raise RuntimeError("EastMoney sector-flow schema changed: " + str(list(frame.columns)))
            for _, item in frame.iterrows():
                name = str(item.get(name_col, "")).strip()
                net = number(item.get(flow_col))
                if name and name.lower() != "nan" and net is not None:
                    period_rows.append({
                        "trade_date": trade_day.isoformat(), "indicator": indicator,
                        "industry": name, "change_pct": number(item.get(change_col)) if change_col else None,
                        "main_net_inflow_cny": net, "net_inflow_cny": None,
                        "main_net_ratio_pct": number(item.get(ratio_col)) if ratio_col else None,
                        "provider": "EastMoney", "source": "AKShare/EastMoney public estimate",
                        "metric_definition": "EastMoney sector main-net-inflow estimate",
                        "captured_at": checked,
                    })
            if not period_rows:
                raise RuntimeError("EastMoney sector-flow rows contain no finite net values")
            # THS taxonomy is not a valid denominator for EastMoney's sector universe.
            # Without an EastMoney-native expected-universe count, completeness is unknown.
            period_status = "success"
            statuses.append({
                "source": "sector_flow_" + indicator, "status": "success",
                "records": len(period_rows), "provider": "EastMoney",
                "coverage_pct": None,
                "expected_industries": None,
                "metric_definition": "EastMoney sector main-net-inflow estimate",
                "detail": "EastMoney 行业资金流；缺少同口径行业总数，覆盖率保持未知",
            })
        except Exception as exc:
            em_error = str(exc)[:300]
            if is_eastmoney_rate_limit(exc):
                eastmoney_circuit_open = True
            LOG.warning("EastMoney %s sector flow failed; trying THS: %s", indicator, em_error)

        if not period_rows:
            try:
                ths_fn = (getattr(ak, "stock_fund_flow_industry_ths", None)
                          or getattr(ak, "stock_fund_flow_industry", None))
                if ths_fn is None:
                    raise RuntimeError("stock_fund_flow_industry_ths unavailable")
                ths_symbol = {"今日": "即时", "5日": "5日排行", "10日": "10日排行"}[indicator]
                frame = call_with_retry(lambda: ths_fn(symbol=ths_symbol), operation=f"THS industry flow {indicator}")
                period_rows, ths_diagnostics = ths_industry_flow_rows(
                    frame, indicator, trade_day, checked,
                    expected_industry_names=ths_universe_names,
                    return_diagnostics=True,
                )
                # Ambiguous duplicate names are excluded, while unaffected
                # industries remain publishable with a partial source status.
                period_status = (
                    "partial" if (ths_diagnostics["conflicting_duplicates"]
                                  or ths_diagnostics["coverage_pct"] is None
                                  or ths_diagnostics["unexpected_industries"])
                    else "success"
                )
                statuses.append({
                    "source": "sector_flow_" + indicator, "status": period_status,
                    "records": len(period_rows), "raw_records": ths_diagnostics["raw_records"],
                    "provider": "THS",
                    "endpoint": "stock_fund_flow_industry_ths",
                    "coverage_pct": ths_diagnostics["coverage_pct"],
                    "expected_industries": ths_diagnostics["expected_industries"],
                    "unexpected_industries": ths_diagnostics["unexpected_industries"],
                    "duplicate_rows_removed": ths_diagnostics["duplicate_rows_removed"],
                    "conflicting_duplicates": ths_diagnostics["conflicting_duplicates"],
                    "metric_definition": "THS provider-defined industry net amount (净额), CNY",
                    "detail": "EastMoney unavailable; THS industry flow fallback "
                             + ("partial due to ambiguous duplicate industry names"
                                if ths_diagnostics["conflicting_duplicates"] else "succeeded")
                             + ("; EastMoney error: " + em_error if em_error else ""),
                })
            except Exception as ths_exc:
                statuses.append({
                    "source": "sector_flow_" + indicator, "status": "failed",
                    "records": 0, "provider": None,
                    "detail": ("EastMoney: " + str(em_error or "not attempted")
                               + "; THS: " + str(ths_exc))[:500],
                })

        flow_rows.extend(period_rows)
        flow_period_outcomes.append((indicator, period_rows))

    # Replace each current period as a complete slice. If a provider fails or
    # an ambiguous duplicate is excluded, old rows for that same period are
    # removed rather than left visible as if they were freshly collected.
    flow_path = DATA / "capital_flows.csv"
    for indicator, period_rows in flow_period_outcomes:
        replace_flow_period_rows(flow_path, trade_day.isoformat(), indicator, period_rows)

    # Exchange-published ETF share snapshots. Prefer the SZSE daily history
    # endpoint (up to six months) so historical share changes are available
    # immediately; keep SSE daily snapshots and persist both exchanges.
    etf_rows = []
    # Shanghai exchange: date-scoped official snapshot.
    try:
        fn = getattr(ak, "fund_etf_scale_sse", None)
        if fn is None:
            raise RuntimeError("fund_etf_scale_sse unavailable")
        frame = fn(date=trade_day.strftime("%Y%m%d"))
        if frame is None or frame.empty:
            raise RuntimeError("SSE ETF share snapshot is empty")
        code_col = next((x for x in ("基金代码", "代码", "证券代码") if x in frame.columns), None)
        name_col = next((x for x in ("基金简称", "名称", "证券简称") if x in frame.columns), None)
        shares_col = next((x for x in ("基金份额", "份额", "总份额") if x in frame.columns), None)
        if not code_col or not shares_col:
            raise RuntimeError("SSE ETF share fields changed: " + str(list(frame.columns)))
        for _, item in frame.iterrows():
            code = str(item.get(code_col, "")).strip()
            shares = number(item.get(shares_col))
            if code and code != "nan" and shares is not None:
                etf_rows.append({"trade_date": trade_day.isoformat(), "exchange": "SSE",
                    "code": code, "name": str(item.get(name_col, "")) if name_col else "",
                    "shares": shares, "nav": None, "source": "SSE official ETF share snapshot",
                    "captured_at": checked})
        statuses.append({"source":"etf_shares_sse","status":"success","records":len(frame),
            "detail":"上交所官方指定交易日份额快照"})
    except Exception as exc:
        LOG.warning("AkShare SSE ETF份额接口失败，改用交易所原始接口重试：%s", exc)
        try:
            frame = fetch_sse_etf_scale_direct(trade_day)
            code_col = "基金代码"
            name_col = "基金简称" if "基金简称" in frame.columns else None
            shares_col = "基金份额"
            for _, item in frame.iterrows():
                code = str(item.get(code_col, "")).strip()
                shares = number(item.get(shares_col))
                if code and code != "nan" and shares is not None:
                    etf_rows.append({"trade_date":trade_day.isoformat(),"exchange":"SSE",
                        "code":code,"name":str(item.get(name_col,"")) if name_col else "",
                        "shares":shares,"nav":None,"source":"SSE official ETF share snapshot (direct API retry)",
                        "captured_at":checked})
            statuses.append({"source":"etf_shares_sse","status":"success","records":len(frame),
                "detail":"AkShare失败后由上交所原始API重试成功"})
        except Exception as fallback_exc:
            LOG.warning("SSE ETF份额原始接口重试失败：%s", fallback_exc)
            statuses.append({"source":"etf_shares_sse","status":"failed",
                "detail":(str(exc)+"; direct retry: "+str(fallback_exc))[:500]})

    # SZSE serves an XLSX file. Split the six-month window into smaller
    # bounded ranges and retry transient resets; keep partial history if only
    # some ranges succeed instead of discarding all returned dates.
    try:
        fn = getattr(ak, "fund_scale_daily_szse", None)
        if fn is None:
            raise RuntimeError("fund_scale_daily_szse unavailable in installed AkShare")
        start_date = trade_day - timedelta(days=170)
        cursor = start_date
        frames = []
        failed_ranges = []
        while cursor <= trade_day:
            chunk_end = min(cursor + timedelta(days=59), trade_day)
            frame_part = None
            last_error = None
            for attempt in range(2):
                try:
                    candidate = fn(start_date=cursor.strftime("%Y%m%d"),
                                   end_date=chunk_end.strftime("%Y%m%d"), symbol="ETF")
                    if candidate is None or candidate.empty:
                        raise RuntimeError("empty SZSE daily ETF response")
                    frame_part = candidate
                    break
                except Exception as chunk_exc:
                    last_error = str(chunk_exc)
                    if attempt == 0:
                        time.sleep(2)
            if frame_part is not None:
                frames.append(frame_part)
            else:
                failed_ranges.append({"start":cursor.isoformat(),"end":chunk_end.isoformat(),
                                      "error":(last_error or "unknown")[:180]})
            cursor = chunk_end + timedelta(days=1)
        if not frames:
            raise RuntimeError("all SZSE history ranges failed: " + json.dumps(failed_ranges, ensure_ascii=False))
        frame = pd.concat(frames, ignore_index=True).drop_duplicates()
        code_col = next((x for x in ("基金代码", "代码", "证券代码") if x in frame.columns), None)
        name_col = next((x for x in ("基金简称", "名称", "证券简称") if x in frame.columns), None)
        shares_col = next((x for x in ("基金份额", "份额", "总份额") if x in frame.columns), None)
        date_col = next((x for x in ("日期", "统计日期", "交易日期") if x in frame.columns), None)
        if not code_col or not shares_col or not date_col:
            raise RuntimeError("SZSE daily ETF fields changed: " + str(list(frame.columns)))
        szse_added = 0
        for _, item in frame.iterrows():
            code = str(item.get(code_col, "")).strip()
            shares = number(item.get(shares_col))
            raw_day = pd.to_datetime(item.get(date_col), errors="coerce")
            if code and code != "nan" and shares is not None and not pd.isna(raw_day):
                etf_rows.append({"trade_date":raw_day.date().isoformat(),"exchange":"SZSE",
                    "code":code,"name":str(item.get(name_col,"")) if name_col else "",
                    "shares":shares,"nav":None,"source":"SZSE official daily ETF share history",
                    "captured_at":checked})
                szse_added += 1
        statuses.append({"source":"etf_shares_szse",
            "status":"partial" if failed_ranges else "success","records":szse_added,
            "detail":"SZSE official ETF daily share history; 60-day chunks; failed_ranges="
                     + json.dumps(failed_ranges, ensure_ascii=False)[:700]})
    except Exception as exc:
        LOG.warning("SZSE daily ETF份额历史失败：%s", exc)
        # Keep the official current-list endpoint as a last-resort snapshot.
        try:
            fn = getattr(ak, "fund_etf_scale_szse", None)
            if fn is None:
                raise RuntimeError("fund_etf_scale_szse unavailable")
            frame = fn()
            if frame is None or frame.empty:
                raise RuntimeError("SZSE ETF share list returned empty")
            code_col = next((x for x in ("基金代码","代码","证券代码") if x in frame.columns), None)
            name_col = next((x for x in ("基金简称","名称","证券简称") if x in frame.columns), None)
            shares_col = next((x for x in ("基金份额","份额","总份额") if x in frame.columns), None)
            if not code_col or not shares_col:
                raise RuntimeError("SZSE ETF share fields changed: " + str(list(frame.columns)))
            added = 0
            for _, item in frame.iterrows():
                code = str(item.get(code_col,"")).strip()
                shares = number(item.get(shares_col))
                if code and code != "nan" and shares is not None:
                    etf_rows.append({"trade_date":trade_day.isoformat(),"exchange":"SZSE",
                        "code":code,"name":str(item.get(name_col,"")) if name_col else "",
                        "shares":shares,"nav":number(item.get("净值")),"source":"SZSE official ETF share snapshot",
                        "captured_at":checked})
                    added += 1
            statuses.append({"source":"etf_shares_szse","status":"partial" if added else "failed",
                "records":added,"detail":"日频历史不可用；仅保留当日份额快照"})
        except Exception as fallback_exc:
            statuses.append({"source":"etf_shares_szse","status":"failed",
                "detail":(str(exc)+"; fallback: "+str(fallback_exc))[:500]})
    if etf_rows:
        append_rows(DATA / "etf_shares.csv", etf_rows, ("trade_date","exchange","code"))

    status_path = DATA / "research_sources.json"
    previous = {}
    if status_path.exists():
        try:
            previous = json.loads(status_path.read_text(encoding="utf-8"))
        except Exception:
            previous = {}
    history = list(previous.get("history") or [])
    if previous.get("sources"):
        history.append({
            "trade_date": previous.get("trade_date"),
            "checked_at": previous.get("checked_at"),
            "sources": previous.get("sources"),
        })
    # The top-level sources array is the current run only. Historical statuses
    # stay in history and cannot satisfy current-trade-date quality gates.
    status_path.write_text(json.dumps({
        "trade_date": trade_day.isoformat(),
        "checked_at": checked,
        "sources": statuses,
        "history": history[-30:],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"trade_date": trade_day.isoformat(), "industry_rows": len(rows) if "rows" in locals() else 0,
        "capital_flow_rows": len(flow_rows), "etf_rows": len(etf_rows),
        "source_statuses": statuses}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
