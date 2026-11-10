"""External data collectors for A-share market research."""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
import requests

LOG = logging.getLogger(__name__)
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


def tushare_pro():
    token = os.getenv("TUSHARE_TOKEN", "").strip()
    if not token:
        raise RuntimeError("缺少 TUSHARE_TOKEN：请在 GitHub Actions Secrets 中配置。")
    import tushare as ts
    ts.set_token(token)
    return ts.pro_api()


def is_open_day(pro, day: date) -> bool:
    cal = pro.trade_cal(exchange="SSE", start_date=day.strftime("%Y%m%d"),
                        end_date=day.strftime("%Y%m%d"), fields="cal_date,is_open")
    return not cal.empty and str(cal.iloc[0]["is_open"]) == "1"


def _technical_history(history: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    """Calculate rolling indicators from a 120-calendar-day daily-bar window."""
    if history is None or history.empty:
        raise RuntimeError("历史日线为空，无法计算技术指标。")
    hist = history.copy()
    hist["trade_date"] = hist["trade_date"].astype(str)
    hist = hist.sort_values(["ts_code", "trade_date"])
    for col in ["close", "vol", "amount"]:
        if col in hist:
            hist[col] = pd.to_numeric(hist[col], errors="coerce")
    grouped = hist.groupby("ts_code", group_keys=False)
    hist["ma5"] = grouped["close"].transform(lambda s: s.rolling(5, min_periods=5).mean())
    hist["ma10"] = grouped["close"].transform(lambda s: s.rolling(10, min_periods=10).mean())
    hist["ma20"] = grouped["close"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    hist["ma60"] = grouped["close"].transform(lambda s: s.rolling(60, min_periods=60).mean())
    delta = grouped["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.groupby(hist["ts_code"]).transform(lambda s: s.ewm(alpha=1/14, adjust=False, min_periods=14).mean())
    avg_loss = loss.groupby(hist["ts_code"]).transform(lambda s: s.ewm(alpha=1/14, adjust=False, min_periods=14).mean())
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    hist["rsi14"] = 100 - 100 / (1 + rs)
    ema12 = grouped["close"].transform(lambda s: s.ewm(span=12, adjust=False, min_periods=12).mean())
    ema26 = grouped["close"].transform(lambda s: s.ewm(span=26, adjust=False, min_periods=26).mean())
    hist["macd_dif"] = ema12 - ema26
    hist["macd_dea"] = hist.groupby("ts_code")["macd_dif"].transform(
        lambda s: s.ewm(span=9, adjust=False, min_periods=9).mean())
    hist["macd_hist"] = (hist["macd_dif"] - hist["macd_dea"]) * 2
    hist["vol_ma5"] = grouped["vol"].transform(lambda s: s.rolling(5, min_periods=5).mean())
    hist["volume_ratio_calc"] = hist["vol"] / hist["vol_ma5"].replace(0, float("nan"))
    hist["volatility20"] = grouped["close"].transform(
        lambda s: s.pct_change().rolling(20, min_periods=20).std() * (252 ** 0.5) * 100)
    latest = hist[hist["trade_date"] == trade_date].copy()
    if latest.empty:
        raise RuntimeError(f"历史日线窗口中没有 {trade_date} 的交易数据。")
    return latest


def collect_market(pro, day: date) -> pd.DataFrame:
    ds = day.strftime("%Y%m%d")
    start = (day - timedelta(days=130)).strftime("%Y%m%d")
    history = pro.daily(start_date=start, end_date=ds)
    latest = _technical_history(history, ds)
    basic = pro.stock_basic(exchange="", list_status="L",
                            fields="ts_code,symbol,name,area,industry,market,list_date")
    if basic is not None and not basic.empty:
        latest = latest.merge(basic, on="ts_code", how="left")
    try:
        extra = pro.daily_basic(trade_date=ds,
                                fields="ts_code,trade_date,turnover_rate,volume_ratio,pe,pb,total_mv,circ_mv")
        if extra is not None and not extra.empty:
            latest = latest.merge(extra.drop(columns=["trade_date"], errors="ignore"),
                                  on="ts_code", how="left")
    except Exception as exc:
        LOG.warning("daily_basic 获取失败（可能是权限/积分不足），使用自行计算的量比：%s", exc)
    if "volume_ratio" not in latest.columns:
        latest["volume_ratio"] = latest["volume_ratio_calc"]
    else:
        latest["volume_ratio"] = latest["volume_ratio"].fillna(latest["volume_ratio_calc"])
    return latest


def collect_news(max_records: int = 80) -> list[dict[str, Any]]:
    """Fetch a compact set of global finance/economy news from GDELT DOC."""
    end = datetime.utcnow()
    start = end - timedelta(hours=30)
    params = {
        "query": '(finance OR economy OR markets OR central bank OR stocks OR China) sourcelang:english',
        "mode": "ArtList", "format": "json", "sort": "HybridRel",
        "maxrecords": str(max_records),
        "startdatetime": start.strftime("%Y%m%d%H%M%S"),
        "enddatetime": end.strftime("%Y%m%d%H%M%S"),
    }
    for attempt in range(3):
        try:
            response = requests.get(GDELT_URL, params=params, timeout=35)
            response.raise_for_status()
            articles = response.json().get("articles", []) or []
            seen, result = set(), []
            for item in articles:
                url = item.get("url")
                if not url or url in seen:
                    continue
                seen.add(url)
                result.append({
                    "title": item.get("title", ""), "url": url,
                    "domain": item.get("domain", ""), "language": item.get("language", ""),
                    "source_country": item.get("sourceCountry", ""),
                    "published": item.get("seendate", ""),
                })
            return result
        except Exception as exc:
            if attempt == 2:
                LOG.warning("GDELT 新闻采集失败：%s", exc)
                return []
            time.sleep(2 ** attempt)
    return []


def collect_announcements(pro, day: date) -> list[dict[str, Any]]:
    """Fetch Tushare announcement metadata if the account is entitled to anns_d."""
    try:
        frame = pro.anns_d(ann_date=day.strftime("%Y%m%d"))
        if frame is None or frame.empty:
            return []
        cols = [c for c in ["ts_code", "ann_date", "name", "title", "url"] if c in frame.columns]
        return frame[cols].fillna("").to_dict(orient="records")
    except Exception as exc:
        LOG.warning("公告索引 anns_d 不可用（可能需要额外权限）：%s", exc)
        return []


def write_json(path: str, value: Any) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=str)
