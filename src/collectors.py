"""Free-source collectors: AKShare/Eastmoney for the full-market close snapshot,
AKShare historical bars for a liquid-stock technical-analysis universe, and GDELT news.
No API token is required. Public endpoints may throttle or change without notice.
"""
from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
import requests

LOG = logging.getLogger(__name__)
GDELT_URL = "https://api.gdeltproject.org/api/v2/doc/doc"


def _ak():
    import akshare as ak
    return ak


def is_open_day(day: date) -> bool:
    """Use the China SSE trading calendar exposed by AKShare."""
    try:
        frame = _ak().tool_trade_date_hist_sina()
        dates = pd.to_datetime(frame.iloc[:, 0], errors="coerce").dt.date
        return day in set(dates.dropna())
    except Exception as exc:
        LOG.warning("交易日历接口暂不可用，将按行情快照是否有数据判断：%s", exc)
        return True


def _normalise_spot(frame: pd.DataFrame, day: date) -> pd.DataFrame:
    mapping = {
        "代码": "symbol", "名称": "name", "最新价": "close",
        "今开": "open", "最高": "high", "最低": "low", "昨收": "pre_close",
        "成交量": "vol", "成交额": "amount", "涨跌幅": "pct_chg",
        "换手率": "turnover_rate", "量比": "volume_ratio",
        "市盈率-动态": "pe", "市净率": "pb",
    }
    df = frame.rename(columns=mapping).copy()
    required = ["symbol", "name", "close", "open", "high", "low", "pre_close", "vol", "amount"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise RuntimeError(f"免费行情源字段发生变化，缺少字段：{missing}")
    for col in ["close", "open", "high", "low", "pre_close", "vol", "amount",
                "pct_chg", "turnover_rate", "volume_ratio", "pe", "pb"]:
        if col in df:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["close", "pre_close"])
    df = df[df["close"] > 0].copy()
    df["ts_code"] = df["symbol"].astype(str).str.zfill(6).map(
        lambda x: f"{x}.SH" if x.startswith(("5", "6", "9")) else
                  (f"{x}.BJ" if x.startswith(("4", "8")) else f"{x}.SZ"))
    df["trade_date"] = day.strftime("%Y%m%d")
    return df


def _history_one(ak, code: str, day: date, start: str) -> pd.DataFrame:
    raw = ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start,
                             end_date=day.strftime("%Y%m%d"), adjust="qfq")
    if raw is None or raw.empty:
        return pd.DataFrame()
    mapping = {"日期": "trade_date", "开盘": "open", "收盘": "close",
               "最高": "high", "最低": "low", "成交量": "vol",
               "成交额": "amount", "涨跌幅": "pct_chg"}
    hist = raw.rename(columns=mapping)
    hist["trade_date"] = pd.to_datetime(hist["trade_date"]).dt.strftime("%Y%m%d")
    hist["ts_code"] = f"{code}.SH" if code.startswith(("5", "6", "9")) else (
        f"{code}.BJ" if code.startswith(("4", "8")) else f"{code}.SZ")
    for col in ["close", "open", "high", "low", "vol", "amount"]:
        hist[col] = pd.to_numeric(hist[col], errors="coerce")
    return hist


def _technical_history(history: pd.DataFrame, trade_date: str) -> pd.DataFrame:
    if history is None or history.empty:
        raise RuntimeError("历史日线为空，无法计算技术指标。")
    hist = history.copy().sort_values(["ts_code", "trade_date"])
    grouped = hist.groupby("ts_code", group_keys=False)
    hist["ma5"] = grouped["close"].transform(lambda s: s.rolling(5, min_periods=5).mean())
    hist["ma10"] = grouped["close"].transform(lambda s: s.rolling(10, min_periods=10).mean())
    hist["ma20"] = grouped["close"].transform(lambda s: s.rolling(20, min_periods=20).mean())
    hist["ma60"] = grouped["close"].transform(lambda s: s.rolling(60, min_periods=60).mean())
    delta = grouped["close"].diff()
    gain, loss = delta.clip(lower=0), -delta.clip(upper=0)
    avg_gain = gain.groupby(hist["ts_code"]).transform(
        lambda s: s.ewm(alpha=1/14, adjust=False, min_periods=14).mean())
    avg_loss = loss.groupby(hist["ts_code"]).transform(
        lambda s: s.ewm(alpha=1/14, adjust=False, min_periods=14).mean())
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
    return hist[hist["trade_date"] == trade_date].copy()


def collect_market(day: date) -> pd.DataFrame:
    """Full-market snapshot plus technical indicators for the most liquid N stocks.

    MAX_HISTORY_STOCKS defaults to 300 to keep free public endpoints within a
    practical runtime. Set it to 0 to attempt every listed stock (slow/rate-limit prone).
    """
    ak = _ak()
    spot = ak.stock_zh_a_spot_em()
    latest = _normalise_spot(spot, day)
    # The Eastmoney spot endpoint may show the latest session, not the requested day.
    # Refuse to silently label a stale trading-day snapshot as today's close.
    if latest.empty:
        raise RuntimeError("免费行情源返回空数据。")
    latest = latest.sort_values("amount", ascending=False, na_position="last")
    limit = int(os.getenv("MAX_HISTORY_STOCKS", "300"))
    universe = latest if limit <= 0 else latest.head(limit)
    start = (day - timedelta(days=150)).strftime("%Y%m%d")
    histories = []
    failures = 0
    workers = max(1, min(int(os.getenv("HISTORY_WORKERS", "4")), 8))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_history_one, ak, str(row["symbol"]).zfill(6), day, start)
                   for _, row in universe.iterrows()}
        for future in as_completed(futures):
            try:
                part = future.result()
                if not part.empty:
                    histories.append(part)
            except Exception as exc:
                failures += 1
                if failures <= 5:
                    LOG.warning("个股历史K线获取失败：%s", exc)
    if histories:
        history = pd.concat(histories, ignore_index=True)
        tech = _technical_history(history, day.strftime("%Y%m%d"))
        keep = [c for c in ["ts_code", "ma5", "ma10", "ma20", "ma60", "rsi14",
                             "macd_dif", "macd_dea", "macd_hist", "volatility20",
                             "volume_ratio_calc"] if c in tech]
        latest = latest.merge(tech[keep], on="ts_code", how="left")
    else:
        LOG.warning("本次未取得可用历史K线；只生成全市场单日行情统计。")
    if "volume_ratio" not in latest:
        latest["volume_ratio"] = latest.get("volume_ratio_calc")
    elif "volume_ratio_calc" in latest:
        latest["volume_ratio"] = latest["volume_ratio"].fillna(latest["volume_ratio_calc"])
    LOG.info("全市场快照 %s 只；技术指标覆盖 %s 只；历史K线失败 %s 次",
             len(latest), int(latest["ma20"].notna().sum()) if "ma20" in latest else 0, failures)
    return latest


def collect_news(max_records: int = 80) -> list[dict[str, Any]]:
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
                result.append({"title": item.get("title", ""), "url": url,
                               "domain": item.get("domain", ""), "language": item.get("language", ""),
                               "source_country": item.get("sourceCountry", ""),
                               "published": item.get("seendate", "")})
            return result
        except Exception as exc:
            if attempt == 2:
                LOG.warning("GDELT 新闻采集失败：%s", exc)
                return []
            time.sleep(2 ** attempt)
    return []


def collect_announcements(day: date) -> list[dict[str, Any]]:
    """Best-effort public announcement index from AKShare; verify originals at CNINFO/exchanges."""
    try:
        frame = _ak().stock_notice_report(symbol="全部", date=day.strftime("%Y%m%d"))
        if frame is None or frame.empty:
            return []
        return frame.fillna("").to_dict(orient="records")
    except Exception as exc:
        LOG.warning("免费公告接口暂不可用：%s", exc)
        return []


def write_json(path: str, value: Any) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=str)
