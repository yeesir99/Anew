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


def collect_market(pro, day: date) -> pd.DataFrame:
    ds = day.strftime("%Y%m%d")
    basic = pro.stock_basic(exchange="", list_status="L",
                            fields="ts_code,symbol,name,area,industry,market,list_date")
    daily = pro.daily(trade_date=ds)
    if daily is None or daily.empty:
        raise RuntimeError(f"Tushare daily 在 {ds} 未返回行情；请确认数据已入库及接口权限。")
    if basic is not None and not basic.empty:
        daily = daily.merge(basic, on="ts_code", how="left")
    try:
        extra = pro.daily_basic(trade_date=ds,
                                fields="ts_code,trade_date,turnover_rate,volume_ratio,pe,pb,total_mv,circ_mv")
        if extra is not None and not extra.empty:
            daily = daily.merge(extra.drop(columns=["trade_date"], errors="ignore"),
                                on="ts_code", how="left")
    except Exception as exc:
        LOG.warning("daily_basic 获取失败（可能是权限/积分不足），继续使用日线数据：%s", exc)
    return daily


def collect_news(max_records: int = 80) -> list[dict[str, Any]]:
    """Fetch a compact set of global finance/economy news from GDELT DOC."""
    end = datetime.utcnow()
    start = end - timedelta(hours=30)
    params = {
        "query": '(finance OR economy OR markets OR central bank OR stocks OR China) '
                 'sourcelang:english',
        "mode": "ArtList", "format": "json", "sort": "HybridRel",
        "maxrecords": str(max_records),
        "startdatetime": start.strftime("%Y%m%d%H%M%S"),
        "enddatetime": end.strftime("%Y%m%d%H%M%S"),
    }
    for attempt in range(3):
        try:
            response = requests.get(GDELT_URL, params=params, timeout=35)
            response.raise_for_status()
            payload = response.json()
            articles = payload.get("articles", []) or []
            seen, result = set(), []
            for item in articles:
                url = item.get("url")
                if not url or url in seen:
                    continue
                seen.add(url)
                result.append({
                    "title": item.get("title", ""),
                    "url": url,
                    "domain": item.get("domain", ""),
                    "language": item.get("language", ""),
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
