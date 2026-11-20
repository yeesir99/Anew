#!/usr/bin/env python3
"""Build a sanitized public A-share market snapshot from the community CSV archive.

This collector deliberately does not read portfolio data, credentials, or the
legacy PostgreSQL database.
"""
from __future__ import annotations

import csv
import json
import os
import statistics
import sys
import urllib.request
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Any

TZ = timezone(timedelta(hours=8))
BASE = "https://raw.githubusercontent.com/modi-hu/stock-data/master/monthly/daily_quotes"
OUT = Path(os.getenv("RADAR_OUTPUT_DIR", "cloud/site/data"))
MIN_ROWS = int(os.getenv("RADAR_MIN_VALID_ROWS", "2500"))
MAX_AGE_DAYS = int(os.getenv("RADAR_MAX_DATA_AGE_DAYS", "7"))
MAX_WEEKDAY_GAP = int(os.getenv("RADAR_MAX_WEEKDAY_GAP", "2"))


def num(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"nan", "none", "null", "-"}:
        return None
    try:
        result = float(text)
        return result if result == result and abs(result) != float("inf") else None
    except (TypeError, ValueError):
        return None


def parse_date(value: str) -> date | None:
    value = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(value[:10], fmt).date()
        except ValueError:
            pass
    return None


def download_month(month: str) -> bytes:
    url = f"{BASE}/{month}.csv"
    request = urllib.request.Request(url, headers={"User-Agent": "A-Share-Alpha-Radar/2.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = response.read(100 * 1024 * 1024 + 1)
    if len(payload) > 100 * 1024 * 1024:
        raise RuntimeError("上游月度CSV超过100MB保护阈值")
    if len(payload) < 100:
        raise RuntimeError("上游月度CSV为空或内容异常")
    return payload


def rows_from_csv(payload: bytes) -> list[dict[str, Any]]:
    text = payload.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(text.splitlines())
    if not reader.fieldnames:
        raise RuntimeError("CSV缺少表头")
    normalized = [str(x).strip() for x in reader.fieldnames]
    reader.fieldnames = normalized
    required = {"symbol", "trade_date", "change_percent", "amount"}
    missing = required - set(normalized)
    if missing:
        raise RuntimeError("CSV缺少必要字段：" + ", ".join(sorted(missing)))
    output = []
    for raw in reader:
        row = {str(k).strip(): (v.strip() if isinstance(v, str) else v) for k, v in raw.items() if k is not None}
        symbol = str(row.get("symbol", "")).strip()
        if len(symbol) != 6 or not symbol.isdigit():
            continue
        trade_day = parse_date(str(row.get("trade_date", "")))
        change = num(row.get("change_percent"))
        amount = num(row.get("amount"))
        close = num(row.get("close"))
        if trade_day is None or change is None or amount is None or amount < 0:
            continue
        if close is not None and close <= 0:
            continue
        name = str(row.get("name") or row.get("stock_name") or row.get("security_name") or "").strip()
        output.append({
            "symbol": symbol,
            "name": name or symbol,
            "trade_date": trade_day.isoformat(),
            "change_pct": round(change, 4),
            "amount_cny": round(amount, 2),
            "close_cny": round(close / 100, 4) if close is not None else None,
            "turnover_rate": num(row.get("turnover_rate")),
            "volume_ratio": num(row.get("volume_ratio")),
        })
    return output


def build_snapshot(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        raise RuntimeError("没有通过校验的A股记录")
    latest_day = max(parse_date(x["trade_date"]) for x in records)
    assert latest_day is not None
    latest = [x for x in records if x["trade_date"] == latest_day.isoformat()]
    by_symbol = {x["symbol"]: x for x in latest}
    latest = list(by_symbol.values())
    if len(latest) < MIN_ROWS:
        raise RuntimeError(f"有效股票数量不足：{len(latest)} < {MIN_ROWS}，拒绝发布不完整快照")
    today = datetime.now(TZ).date()
    age = (today - latest_day).days
    weekday_gap = sum(1 for offset in range(1, age + 1) if (latest_day + timedelta(days=offset)).weekday() < 5)
    if age < 0 or age > MAX_AGE_DAYS:
        raise RuntimeError(f"上游最新交易日过旧：{latest_day}，数据年龄{age}天，阈值{MAX_AGE_DAYS}天")
    if weekday_gap > MAX_WEEKDAY_GAP:
        raise RuntimeError(f"上游行情落后{weekday_gap}个工作日：最新交易日{latest_day}，阈值{MAX_WEEKDAY_GAP}个工作日；拒绝把旧快照标记为当前数据")

    changes = [x["change_pct"] for x in latest]
    up = sum(x > 0 for x in changes)
    down = sum(x < 0 for x in changes)
    flat = len(changes) - up - down
    turnover = sum(x["amount_cny"] for x in latest)
    by_change = sorted(latest, key=lambda x: x["change_pct"], reverse=True)
    by_amount = sorted(latest, key=lambda x: x["amount_cny"], reverse=True)

    def compact(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{k: x[k] for k in ("symbol", "name", "change_pct", "amount_cny", "close_cny", "turnover_rate", "volume_ratio")} for x in items[:20]]

    return {
        "schema_version": 1,
        "product": "A-Share Alpha Radar",
        "trade_date": latest_day.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_status": "validated",
        "freshness": {"calendar_age_days": age, "weekday_gap": weekday_gap, "max_allowed_weekday_gap": MAX_WEEKDAY_GAP},
        "coverage": {"valid_stock_count": len(latest), "up": up, "down": down, "unchanged": flat},
        "market_breadth": {
            "advance_ratio_pct": round(up / len(latest) * 100, 2),
            "decline_ratio_pct": round(down / len(latest) * 100, 2),
            "median_change_pct": round(statistics.median(changes), 4),
            "aggregate_turnover_cny": round(turnover, 2),
            "advance_decline_ratio": round(up / down, 3) if down else None,
        },
        "leaders": compact(by_change),
        "laggards": compact(list(reversed(by_change))),
        "most_active": compact(by_amount),
        "sources": [{
            "name": "modi-hu/stock-data monthly daily_quotes",
            "url": "https://github.com/modi-hu/stock-data",
            "upstream": "EastMoney, collected by tr1v3r/skr (per provider README)",
            "limitations": [
                "This is a third-party cache, not an independent upstream data source.",
                "A successful workflow does not prove the upstream feed is complete beyond the validation checks.",
                "Turnover is trading activity, not net capital inflow.",
                "Public main-flow estimates are not institutional account-level flows.",
            ],
        }],
        "disclaimer": "Descriptive market data only; not investment advice, probability, or return forecast.",
    }


def main() -> int:
    month = datetime.now(TZ).strftime("%Y-%m")
    records = rows_from_csv(download_month(month))
    snapshot = build_snapshot(records)
    OUT.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
    (OUT / "latest.json").write_text(encoded, encoding="utf-8")
    history = OUT / "history"
    history.mkdir(parents=True, exist_ok=True)
    (history / f"{snapshot['trade_date']}.json").write_text(encoded, encoding="utf-8")
    print(json.dumps({"trade_date": snapshot["trade_date"], "valid_stock_count": snapshot["coverage"]["valid_stock_count"], "output": str(OUT / "latest.json")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"PUBLIC_DATA_COLLECTION_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
