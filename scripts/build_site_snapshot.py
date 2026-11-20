#!/usr/bin/env python3
"""Build privacy-safe static JSON for the public Pages dashboard."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "latest_market.csv"
OUT = ROOT / "site" / "data"
FALLBACK = ROOT / ".cache" / "public-fallback" / "latest.json"
CN = ZoneInfo("Asia/Shanghai")


def day_value(value: object) -> date:
    text = str(value).strip()
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    raise ValueError(f"Unrecognized trade_date: {value!r}")


def weekday_gap(start: date, end: date) -> int:
    if end <= start:
        return 0
    return sum(1 for n in range(1, (end - start).days + 1)
               if (start + timedelta(days=n)).weekday() < 5)


def main() -> None:
    if FALLBACK.exists():
        snapshot = json.loads(FALLBACK.read_text(encoding="utf-8"))
        count = int(snapshot.get("coverage", {}).get("valid_stock_count", 0))
        if count < 2500:
            raise RuntimeError(f"Fallback market coverage too low: {count}")
        snapshot["data_status"] = "stale" if snapshot.get("freshness", {}).get("weekday_gap", 0) > 2 else snapshot.get("data_status", "validated")
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "latest.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
        breadth = snapshot["market_breadth"]
        sections = {
            "overview": {"count": count, "up": breadth["up"], "down": breadth["down"]},
            "market-breadth": {"item": {"trade_date": snapshot["trade_date"], "payload": breadth}},
            "industries": {"items": []}, "themes": {"items": []}, "rotation": {"items": []},
            "capital-flows": {"items": []}, "etf-shares": {"items": []},
            "external-market-data": {"leaders": []},
            "data-sources": {"items": [{"source": "modi-hu/stock-data archive",
                "status": snapshot["data_status"], "detail": "第三方缓存；最终上游为东方财富；成交额不代表资金净流入"}]}
        }
        research = {"privacy": "public_research_only", "generated_at": snapshot["generated_at"],
                    "market_status": snapshot["data_status"], "sections": sections,
                    "jobs": [{"name": "archive_fallback", "status": snapshot["data_status"]}],
                    "notes": ["本公开快照不包含个人持仓、成本、账户、私人笔记或证据台账。",
                              "备用行情来自第三方历史归档；如标记滞后，只能作为历史参考。"]}
        (OUT / "research.json").write_text(json.dumps(research, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
        print(json.dumps({"source": "archive_fallback", "trade_date": snapshot["trade_date"],
                          "stocks": count, "status": snapshot["data_status"]}, ensure_ascii=False))
        return
    if not SOURCE.exists():
        raise FileNotFoundError(f"Missing close snapshot: {SOURCE}")
    frame = pd.read_csv(SOURCE, encoding="utf-8-sig")
    required = {"symbol", "name", "close", "amount", "trade_date"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"Missing required market columns: {sorted(missing)}")
    frame["trade_date"] = frame["trade_date"].map(day_value)
    trade_day = frame["trade_date"].max()
    frame = frame[frame["trade_date"] == trade_day].copy()
    for col in ("close", "amount", "pct_chg", "turnover_rate", "volume_ratio"):
        if col in frame:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["symbol", "close", "amount"])
    frame = frame[(frame["close"] > 0) & (frame["amount"] >= 0)]
    frame["symbol"] = frame["symbol"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    frame = frame.drop_duplicates("symbol", keep="last")
    if len(frame) < 2500:
        raise RuntimeError(f"Market coverage too low: {len(frame)} rows; refusing to publish")

    change_col = "pct_chg" if "pct_chg" in frame else None
    if change_col is None:
        raise RuntimeError("No percentage-change field; cannot build market breadth")
    changes = frame[change_col].dropna()
    if changes.empty:
        raise RuntimeError("No valid percentage changes")
    up, down = int((changes > 0).sum()), int((changes < 0).sum())
    amount = float(frame["amount"].sum())
    now = datetime.now(CN)
    gap = weekday_gap(trade_day, now.date())
    age = (now.date() - trade_day).days
    status = "stale" if age < 0 or gap > 2 else "validated"

    def rows(order: str) -> list[dict]:
        cols = ["symbol", "name", change_col, "amount"]
        for optional in ("close", "turnover_rate", "volume_ratio"):
            if optional in frame:
                cols.append(optional)
        data = frame.sort_values(change_col if order == "change" else "amount",
                                 ascending=(order == "laggard")).head(20) if order != "laggard" else frame.sort_values(change_col).head(20)
        result = []
        for rec in data[cols].to_dict(orient="records"):
            result.append({
                "symbol": str(rec["symbol"]),
                "name": str(rec.get("name") or rec["symbol"]),
                "change_pct": round(float(rec[change_col]), 4) if pd.notna(rec.get(change_col)) else None,
                "amount_cny": round(float(rec["amount"]), 2),
                "close_cny": round(float(rec["close"]), 4) if pd.notna(rec.get("close")) else None,
                "turnover_rate": round(float(rec["turnover_rate"]), 4) if pd.notna(rec.get("turnover_rate")) else None,
                "volume_ratio": round(float(rec["volume_ratio"]), 4) if pd.notna(rec.get("volume_ratio")) else None,
            })
        return result

    breadth = {
        "up": up, "down": down, "flat": int((changes == 0).sum()),
        "total": int(len(changes)),
        "advance_ratio_pct": round(up / len(changes) * 100, 2),
        "decline_ratio_pct": round(down / len(changes) * 100, 2),
        "median_change_pct": round(float(changes.median()), 4),
        "aggregate_turnover_cny": round(amount, 2),
        "advance_decline_ratio": round(up / down, 3) if down else None,
    }
    snapshot = {
        "schema_version": 1, "product": "A-Share Alpha Radar",
        "trade_date": trade_day.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "data_status": status,
        "freshness": {"calendar_age_days": age, "weekday_gap": gap, "max_allowed_weekday_gap": 2},
        "coverage": {"valid_stock_count": int(len(frame)), "up": up, "down": down, "unchanged": breadth["flat"]},
        "market_breadth": breadth,
        "leaders": rows("change"), "laggards": rows("laggard"), "most_active": rows("amount"),
        "sources": [{
            "name": "AKShare A-share spot snapshot",
            "url": "https://github.com/akfamily/akshare",
            "upstream": "EastMoney public market endpoint (via AKShare)",
            "limitations": [
                "Public third-party endpoint; not an independent source.",
                "The provider may throttle, change fields, or return delayed data.",
                "Turnover is trading activity, not net capital inflow.",
                "No portfolio, account, holdings, cost-basis, or private evidence data is read."
            ]
        }],
        "disclaimer": "Descriptive public-market data only; not investment advice."
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "latest.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    sections = {
        "overview": {"count": int(len(frame)), "up": up, "down": down},
        "market-breadth": {"item": {"trade_date": trade_day.isoformat(), "payload": breadth}},
        "industries": {"items": []}, "themes": {"items": []}, "rotation": {"items": []},
        "capital-flows": {"items": []}, "etf-shares": {"items": []},
        "external-market-data": {"leaders": []},
        "data-sources": {"items": [{
            "source": "AKShare / EastMoney", "status": status,
            "detail": f"trade_date={trade_day.isoformat()}, stocks={len(frame)}, weekday_gap={gap};成交额不代表净流入"
        }]}
    }
    research = {
        "privacy": "public_research_only", "generated_at": snapshot["generated_at"],
        "market_status": status, "sections": sections,
        "jobs": [{"name": "public_market_snapshot", "status": status}],
        "notes": ["本公开快照不包含个人持仓、成本、账户、私人笔记或证据台账。",
                  "行业主线、行业轮动、机构资金和完整12栏目报告尚未通过独立数据源及隐私审查，故不伪造结果。"]
    }
    (OUT / "research.json").write_text(json.dumps(research, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"trade_date": trade_day.isoformat(), "stocks": len(frame),
                      "weekday_gap": gap, "status": status}, ensure_ascii=False))


if __name__ == "__main__":
    main()
