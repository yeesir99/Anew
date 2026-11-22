"""Free-source collectors: AKShare/Eastmoney for the full-market close snapshot,
AKShare historical bars for a liquid-stock technical-analysis universe, and GDELT news.
No API token is required. Public endpoints may throttle or change without notice.
"""
from __future__ import annotations

import json
import logging
import os
import re
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
    ak = _ak()
    for name in ("calendar_trade_date_hist_sina", "tool_trade_date_hist_sina"):
        fn = getattr(ak, name, None)
        if fn is None:
            continue
        try:
            frame = fn()
            dates = pd.to_datetime(frame.iloc[:, 0], errors="coerce").dt.date
            return day in set(dates.dropna())
        except Exception as exc:
            LOG.warning("交易日历接口 %s 不可用：%s", name, exc)
    LOG.warning("所有公开交易日历接口均不可用，将按行情快照日期校验。")
    return True


def _normalise_spot(frame: pd.DataFrame, day: date) -> pd.DataFrame:
    mapping = {
        "代码": "symbol", "名称": "name", "最新价": "close",
        "今开": "open", "最高": "high", "最低": "low", "昨收": "pre_close",
        "成交量": "vol", "成交额": "amount", "涨跌幅": "pct_chg",
        "换手率": "turnover_rate", "量比": "volume_ratio",
        "市盈率-动态": "pe", "市净率": "pb",
    }
    aliases = {"代码": "symbol", "证券代码": "symbol", "symbol": "symbol",
               "名称": "name", "证券简称": "name", "name": "name",
               "最新价": "close", "现价": "close", "close": "close",
               "今开": "open", "open": "open", "最高": "high", "high": "high",
               "最低": "low", "low": "low", "昨收": "pre_close", "pre_close": "pre_close",
               "成交量": "vol", "volume": "vol", "vol": "vol",
               "成交额": "amount", "amount": "amount",
               "涨跌幅": "pct_chg", "涨跌幅(%)": "pct_chg", "change_percent": "pct_chg",
               "换手率": "turnover_rate", "turnover": "turnover_rate",
               "量比": "volume_ratio", "volume_ratio": "volume_ratio",
               "市盈率-动态": "pe", "pe": "pe", "市净率": "pb", "pb": "pb"}
    df = frame.rename(columns={**aliases, **mapping}).copy()
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
    # Sina may return exchange-prefixed codes (sh600000/sz000001); normalize to six digits.
    df["symbol"] = df["symbol"].astype(str).str.extract(r"(\d{6})", expand=False)
    df = df.dropna(subset=["symbol"])
    df["symbol"] = df["symbol"].astype(str).str.zfill(6)
    # Exchange mapping must be based on the full A-share code family.
    # Beijing's post-reform 920xxx listings are not Shenzhen securities.
    df["ts_code"] = df["symbol"].map(
        lambda x: f"{x}.BJ" if x.startswith(("4", "8", "920")) else
                  (f"{x}.SH" if x.startswith(("5", "6", "9")) else f"{x}.SZ"))
    df["trade_date"] = day.strftime("%Y%m%d")
    return df


def _history_one(ak, code: str, day: date, start: str) -> pd.DataFrame:
    """Try Sina/Tencent daily bars before EastMoney to avoid push2 IP bans."""
    import inspect
    errors = []
    candidates = ("stock_zh_a_hist_sina", "stock_zh_a_hist_tx",
                  "stock_zh_a_hist_em", "stock_zh_a_hist")
    for name in candidates:
        fn = getattr(ak, name, None)
        if fn is None:
            continue
        try:
            params = inspect.signature(fn).parameters
            prefix = "bj" if code.startswith(("4", "8", "920")) else ("sh" if code.startswith(("5", "6", "9")) else "sz")
            provider_symbol = prefix + code if name in ("stock_zh_a_hist_sina", "stock_zh_a_hist_tx") else code
            kwargs = {"symbol": provider_symbol}
            if "start_date" in params: kwargs["start_date"] = start
            if "end_date" in params: kwargs["end_date"] = day.strftime("%Y%m%d")
            if "period" in params: kwargs["period"] = "daily"
            if "adjust" in params: kwargs["adjust"] = "qfq"
            if "adjustflag" in params: kwargs["adjustflag"] = "2"
            raw = fn(**kwargs)
            if raw is None or raw.empty:
                raise RuntimeError("接口返回空数据")
            mapping = {"日期": "trade_date", "date": "trade_date",
                       "开盘": "open", "收盘": "close", "最高": "high",
                       "最低": "low", "成交量": "vol", "volume": "vol",
                       "成交额": "amount", "amount": "amount",
                       "涨跌幅": "pct_chg", "change_percent": "pct_chg"}
            hist = raw.rename(columns=mapping).copy()
            if "trade_date" not in hist:
                raise RuntimeError("历史行情缺少交易日期列")
            hist["trade_date"] = pd.to_datetime(hist["trade_date"], errors="coerce").dt.strftime("%Y%m%d")
            hist = hist.dropna(subset=["trade_date"])
            hist["ts_code"] = f"{code}.SH" if code.startswith(("5", "6", "9")) else (
                f"{code}.BJ" if code.startswith(("4", "8")) else f"{code}.SZ")
            for col in ("close", "open", "high", "low", "vol", "amount"):
                if col in hist:
                    hist[col] = pd.to_numeric(hist[col], errors="coerce")
            if "close" not in hist or "vol" not in hist:
                raise RuntimeError("历史行情缺少收盘价/成交量")
            LOG.info("历史K线来源：%s（%s）", name, code)
            return hist
        except Exception as exc:
            errors.append(f"{name}: {str(exc)[:160]}")
    raise RuntimeError("所有历史行情来源均失败：" + "；".join(errors))


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
    spot = None
    errors = []
    spot_source = None
    # Prefer Sina/Tencent-compatible non-Push2 sources. EastMoney is last resort.
    for name in ("stock_zh_a_spot_sina", "stock_zh_a_spot", "stock_zh_a_spot_em"):
        fn = getattr(ak, name, None)
        if fn is None:
            continue
        try:
            candidate = fn()
            if candidate is None or candidate.empty:
                raise RuntimeError("接口返回空数据")
            spot, spot_source = candidate, name
            break
        except Exception as exc:
            errors.append(f"{name}: {str(exc)[:180]}")
            LOG.warning("全市场行情源 %s 不可用：%s", name, exc)
    if spot is None:
        raise RuntimeError("全市场行情源全部失败：" + "；".join(errors))
    LOG.info("全市场行情来源：%s", spot_source)
    latest = _normalise_spot(spot, day)

    # Sina's full-market quote feed may omit turnover rate entirely. Enrich the
    # snapshot from AKShare's explicit EastMoney spot endpoint instead of
    # silently publishing null turnover_rate values for every stock.
    turnover_coverage = (
        float(latest["turnover_rate"].notna().mean())
        if "turnover_rate" in latest and len(latest) else 0.0
    )
    if turnover_coverage < 0.80 and spot_source != "stock_zh_a_spot_em":
        turnover_fn = getattr(ak, "stock_zh_a_spot_em", None)
        if turnover_fn is not None:
            try:
                turnover_frame = turnover_fn()
                if turnover_frame is None or turnover_frame.empty:
                    raise RuntimeError("东方财富换手率接口返回空数据")
                code_col = next((c for c in ("代码", "证券代码", "symbol") if c in turnover_frame.columns), None)
                rate_col = next((c for c in ("换手率", "turnover_rate", "turnover") if c in turnover_frame.columns), None)
                if not code_col or not rate_col:
                    raise RuntimeError(f"换手率接口字段不完整：{list(turnover_frame.columns)}")
                turnover = turnover_frame[[code_col, rate_col]].copy()
                turnover["symbol"] = turnover[code_col].astype(str).str.extract(r"(\d{6})", expand=False)
                turnover["turnover_rate"] = pd.to_numeric(
                    turnover[rate_col].astype(str).str.replace("%", "", regex=False).str.replace(",", "", regex=False),
                    errors="coerce")
                turnover = turnover.dropna(subset=["symbol", "turnover_rate"])
                turnover = turnover.drop_duplicates("symbol", keep="last")
                if "turnover_rate" not in latest:
                    latest["turnover_rate"] = float("nan")
                rates = turnover.set_index("symbol")["turnover_rate"]
                latest["turnover_rate"] = latest["turnover_rate"].fillna(
                    latest["symbol"].map(rates))
                enriched = int(latest["turnover_rate"].notna().sum())
                LOG.info("换手率补充来源：stock_zh_a_spot_em；覆盖 %s/%s 只（%.1f%%）",
                         enriched, len(latest), enriched / max(1, len(latest)) * 100)
            except Exception as exc:
                LOG.warning("东方财富换手率补充失败，保留真实缺失值：%s", str(exc)[:220])
        else:
            LOG.warning("当前AKShare版本不含 stock_zh_a_spot_em，无法补充换手率")
    elif turnover_coverage >= 0.80:
        LOG.info("主行情源换手率覆盖 %.1f%%", turnover_coverage * 100)

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
    if not histories:
        raise RuntimeError("无法从历史K线接口独立核验本次行情日期；拒绝将未核验的现货快照标记为当日数据。")
    history = pd.concat(histories, ignore_index=True)
    observed_dates = pd.to_datetime(history["trade_date"], format="%Y%m%d", errors="coerce").dropna()
    if observed_dates.empty or observed_dates.max().date() != day:
        observed = observed_dates.max().date().isoformat() if not observed_dates.empty else "unavailable"
        raise RuntimeError(f"现货行情日期未通过历史K线交叉核验：期望 {day.isoformat()}，历史接口最新日期 {observed}")
    if histories:
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
    """Collect recent Chinese A-share news from GDELT and domestic financial feeds."""
    cutoff = pd.Timestamp.now(tz="Asia/Shanghai") - pd.Timedelta(hours=36)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_item(title, url, published, source, content="", domain=""):
        title = str(title or "").strip()
        content = str(content or "").strip()
        if not title or not re.search(r"[\u4e00-\u9fff]", title + content):
            return
        if not any(k in title + " " + content for k in (
            "A股", "沪深", "上证", "深证", "创业板", "科创板", "北交所", "上市公司",
            "证券", "股票", "股市", "基金", "涨停", "跌停", "财报", "业绩",
            "行业", "个股", "交易所", "资本市场", "沪指", "深成指", "主板",
            "半导体", "新能源", "机器人", "医药", "消费电子", "光伏", "电池", "储能",
            "芯片", "算力", "人工智能", "汽车", "军工", "银行", "券商", "地产",
            "有色金属", "钢铁", "煤炭", "电力", "港口", "航运", "白酒", "家电",
            "国务院", "工信部", "发改委", "央行", "产业政策")):
            return
        stamp = pd.to_datetime(published, errors="coerce")
        if pd.isna(stamp):
            return
        if stamp.tzinfo is None:
            stamp = stamp.tz_localize("Asia/Shanghai")
        else:
            stamp = stamp.tz_convert("Asia/Shanghai")
        if stamp < cutoff:
            return
        key = (source + ":" + title) if str(url or "").strip() in ("https://www.cls.cn/telegraph", "https://news.10jqka.com.cn/realtimenews.html", "https://finance.sina.com.cn/7x24", "https://kuaixun.eastmoney.com/7_24.html", "https://stock.eastmoney.com/a/czpnc.html") else (str(url or "").strip() or (source + ":" + title))
        if key in seen:
            return
        seen.add(key)
        result.append({"title": title, "summary": content[:1200] or None,
            "url": str(url or "").strip(), "domain": domain or "",
            "language": "Chinese", "source_country": "CN",
            "published": stamp.isoformat(), "source": source})

    # GDELT is useful for international coverage, but its Chinese-language
    # A-share query may legitimately return no articles; it is not the only feed.
    end = datetime.utcnow()
    start = end - timedelta(hours=36)
    queries = [
        '(A股 OR 沪深股市 OR 上市公司 OR 证券市场 OR 股票 OR 交易所) sourcelang:Chinese',
        '(半导体 OR 新能源 OR 机器人 OR 医药 OR 消费) (A股 OR 上市公司 OR 证券) sourcelang:Chinese',
    ]
    for query in queries:
        params = {"query": query, "mode": "ArtList", "format": "json",
            "sort": "HybridRel", "maxrecords": str(max_records),
            "startdatetime": start.strftime("%Y%m%d%H%M%S"),
            "enddatetime": end.strftime("%Y%m%d%H%M%S")}
        try:
            response = requests.get(GDELT_URL, params=params, timeout=12)
            response.raise_for_status()
            for item in response.json().get("articles", []) or []:
                if str(item.get("language", "")).strip().lower() not in ("chinese", "zh", "zh-cn"):
                    continue
                add_item(item.get("title"), item.get("url"), item.get("seendate"),
                    "GDELT", item.get("title"), item.get("domain", ""))
        except Exception as exc:
            LOG.warning("GDELT Chinese news unavailable: %s", str(exc)[:180])
        if len(result) >= max_records:
            return result[:max_records]

    ak = _ak()
    domestic_sources = [
        ("stock_news_global_ths", {}, "同花顺财经快讯", "https://news.10jqka.com.cn/realtimenews.html"),
        ("stock_news_global_cls", {"symbol": "全部"}, "财联社电报", "https://www.cls.cn/telegraph"),
        ("stock_news_global_sina", {}, "新浪财经快讯", "https://finance.sina.com.cn/7x24"),
        ("stock_news_global_em", {}, "东方财富财经快讯", "https://kuaixun.eastmoney.com/7_24.html"),
        ("stock_news_cjzc_em", {}, "东方财富财经早餐", "https://stock.eastmoney.com/a/czpnc.html"),
    ]
    for fn_name, kwargs, source_name, fallback_url in domestic_sources:
        if len(result) >= max_records:
            break
        fn = getattr(ak, fn_name, None)
        if fn is None:
            continue
        try:
            frame = fn(**kwargs)
            if frame is None or frame.empty:
                continue
            content_col = next((c for c in ("内容", "摘要", "summary", "digest") if c in frame.columns), None)
            title_col = next((c for c in ("标题", "title", "新闻标题") if c in frame.columns), content_col)
            date_col = next((c for c in ("发布时间", "发布日期", "时间", "showTime", "rtime") if c in frame.columns), None)
            url_col = next((c for c in ("链接", "url", "URL", "uniqueUrl") if c in frame.columns), None)
            if not title_col or not date_col:
                LOG.warning("%s news schema changed: %s", fn_name, list(frame.columns))
                continue
            for row in frame.to_dict("records"):
                published = row.get(date_col)
                if "发布日期" in row and "发布时间" in row:
                    published = str(row.get("发布日期")) + " " + str(row.get("发布时间"))
                add_item(row.get(title_col), row.get(url_col) or fallback_url,
                    published, source_name, row.get(content_col), fallback_url.split("/")[2])
        except Exception as exc:
            LOG.warning("%s news feed unavailable: %s", fn_name, str(exc)[:180])

    result.sort(key=lambda x: x.get("published", ""), reverse=True)
    return result[:max_records]

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
