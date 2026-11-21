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
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TZ = ZoneInfo("Asia/Shanghai")
LOG = logging.getLogger("research-collect")
DATA.mkdir(exist_ok=True)


def number(value):
    try:
        value = pd.to_numeric(str(value).replace(",", "").replace("%", "").strip(), errors="coerce")
        return None if pd.isna(value) else float(value)
    except Exception:
        return None


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


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    trade_day = load_validated_trade_day()
    import akshare as ak

    checked = datetime.now(TZ).isoformat(timespec="seconds")
    statuses = []
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
        LOG.warning("EastMoney industry snapshot failed; trying THS fallback: %s", str(exc)[:240])
        statuses.append({"source": "industry_snapshot", "status": "failed", "detail": str(exc)[:500]})
        try:
            ths_fn = getattr(ak, "stock_board_industry_summary_ths", None)
            if ths_fn is None:
                raise RuntimeError("stock_board_industry_summary_ths unavailable")
            ths_frame = ths_fn()
            if ths_frame is None or ths_frame.empty:
                raise RuntimeError("THS industry summary returned empty")
            name_col = next((x for x in ("板块", "行业", "板块名称") if x in ths_frame.columns), None)
            change_col = next((x for x in ("涨跌幅", "涨跌幅(%)") if x in ths_frame.columns), None)
            amount_col = next((x for x in ("总成交额", "成交额") if x in ths_frame.columns), None)
            net_col = next((x for x in ("净流入", "主力净流入") if x in ths_frame.columns), None)
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
                    "change_pct":change,"turnover_cny":amount_cny,"main_net_inflow_cny":net_cny,
                    "main_net_ratio_pct":ratio,"up_count":number(item.get(up_col)) if up_col else None,
                    "down_count":number(item.get(down_col)) if down_col else None,
                    "leader_name":leader_name,
                    "leader_change_pct":number(item.get(leader_change_col)) if leader_change_col else None,
                    "source":"AKShare/THS live industry summary","captured_at":checked})
            if not ths_rows:
                raise RuntimeError("THS industry summary has no valid rows")
            ths_snapshot = {"trade_date":trade_day.isoformat(),"status":"success",
                "provider":"THS","records":len(ths_rows),"captured_at":checked,"items":ths_rows,
                "note":"同花顺实时行业榜备用源；独立展示，不混入申万历史轮动序列。"}
            ths_snapshot_path.write_text(json.dumps(ths_snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            statuses.append({"source":"industry_snapshot_ths","status":"success",
                "records":len(ths_rows),"provider":"THS",
                "detail":"EastMoney限流时使用同花顺行业实时榜；独立展示，金额单位已由亿元转换为元"})
        except Exception as ths_exc:
            LOG.exception("THS industry fallback failed")
            statuses.append({"source":"industry_snapshot_ths","status":"failed","detail":str(ths_exc)[:500]})

    # Sector capital-flow estimates. They are not institution-specific account flows.
    flow_rows = []
    try:
        fn = getattr(ak, "stock_fund_flow_sector_rank_em", None)
        if fn is None:
            raise RuntimeError("当前 AkShare 不含 stock_fund_flow_sector_rank_em")
        for indicator in ("今日", "5日", "10日"):
            try:
                frame = fn(indicator=indicator, sector_type="行业资金流")
                if frame is None or frame.empty:
                    raise RuntimeError("接口返回空数据")
                name_col = next((x for x in ("名称", "板块名称", "行业", "板块") if x in frame.columns), None)
                flow_col = next((x for x in (indicator + "主力净流入-净额", "主力净流入-净额") if x in frame.columns), None)
                ratio_col = next((x for x in (indicator + "主力净流入-净占比", "主力净流入-净占比") if x in frame.columns), None)
                change_col = next((x for x in (indicator + "涨跌幅", "涨跌幅") if x in frame.columns), None)
                if not name_col or not flow_col:
                    raise RuntimeError("资金流字段不匹配：" + str(list(frame.columns)))
                for _, item in frame.iterrows():
                    name = str(item.get(name_col, "")).strip()
                    if name and name != "nan":
                        flow_rows.append({"trade_date": trade_day.isoformat(), "indicator": indicator,
                            "industry": name, "change_pct": number(item.get(change_col)) if change_col else None,
                            "main_net_inflow_cny": number(item.get(flow_col)),
                            "main_net_ratio_pct": number(item.get(ratio_col)) if ratio_col else None,
                            "source": "AKShare/EastMoney public estimate", "captured_at": checked})
            except Exception as exc:
                statuses.append({"source": "sector_flow_" + indicator, "status": "failed", "detail": str(exc)[:400]})
        if flow_rows:
            append_rows(DATA / "capital_flows.csv", flow_rows, ("trade_date", "indicator", "industry"))
        for indicator in ("今日", "5日", "10日"):
            n = sum(x["indicator"] == indicator for x in flow_rows)
            if not any(x.get("source") == "sector_flow_" + indicator for x in statuses):
                statuses.append({"source": "sector_flow_" + indicator,
                                 "status": "success" if n else "unavailable", "records": n,
                                 "detail": "公开市场主力资金估算，不代表机构专属账户"})
    except Exception as exc:
        LOG.exception("行业资金流采集失败")
        statuses.append({"source": "sector_flows", "status": "failed", "detail": str(exc)[:500]})

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
        LOG.warning("SSE ETF份额采集失败：%s", exc)
        statuses.append({"source":"etf_shares_sse","status":"failed","detail":str(exc)[:400]})

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
    by_key = {x["source"]: x for x in previous.get("sources", [])}
    by_key.update({x["source"]: x for x in statuses})
    status_path.write_text(json.dumps({"trade_date": trade_day.isoformat(), "checked_at": checked,
        "sources": list(by_key.values())}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"trade_date": trade_day.isoformat(), "industry_rows": len(rows) if "rows" in locals() else 0,
        "capital_flow_rows": len(flow_rows), "etf_rows": len(etf_rows),
        "source_statuses": statuses}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
