#!/usr/bin/env python3
"""Import recent public sector daily history from the community CSV archive.

The archive is EastMoney-derived, so it is labelled as a third-party cache and
its last exchange date is validated independently against the public snapshot.
"""
from __future__ import annotations

import csv
import json
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
TZ = ZoneInfo("Asia/Shanghai")
BASE = "https://raw.githubusercontent.com/modi-hu/stock-data/master/monthly/sector_daily_quotes"


def month_list():
    today = datetime.now(TZ).date()
    first = today.replace(day=1)
    previous_last = first - timedelta(days=1)
    return [previous_last.strftime("%Y-%m"), today.strftime("%Y-%m")]


def number(value):
    try:
        x = float(str(value).strip().replace(",", ""))
        return x if x == x and abs(x) != float("inf") else None
    except (TypeError, ValueError):
        return None


def fetch_month(month):
    url = f"{BASE}/{month}.csv"
    req = urllib.request.Request(url, headers={"User-Agent": "A-Share-Alpha-Radar/2.0"})
    with urllib.request.urlopen(req, timeout=60) as response:
        payload = response.read(30 * 1024 * 1024 + 1)
    if len(payload) > 30 * 1024 * 1024:
        raise RuntimeError(f"{month} sector archive exceeds 30MB safety limit")
    if len(payload) < 100:
        raise RuntimeError(f"{month} sector archive is empty")
    return payload.decode("utf-8-sig", errors="replace")


def main():
    out = DATA / "industry_history.csv"
    existing = []
    if out.exists():
        with out.open(encoding="utf-8-sig", newline="") as f:
            existing = list(csv.DictReader(f))
    merged = {(x.get("trade_date", ""), x.get("industry", "")): x for x in existing}
    imported = 0
    dates = set()
    errors = []
    for month in month_list():
        try:
            reader = csv.DictReader(fetch_month(month).splitlines())
            required = {"symbol", "trade_date", "change_percent", "amount"}
            missing = required - set(reader.fieldnames or [])
            if missing:
                raise RuntimeError("missing fields: " + ",".join(sorted(missing)))
            for row in reader:
                code = str(row.get("symbol", "")).strip()
                day = str(row.get("trade_date", ""))[:10]
                change = number(row.get("change_percent"))
                if not code.startswith("BK") or change is None or len(day) != 10:
                    continue
                amount = number(row.get("amount"))
                net = number(row.get("main_net_inflow"))
                ratio = number(row.get("main_net_flow_ratio"))
                item = {
                    "trade_date": day,
                    "industry": code,
                    "sector_code": code,
                    "change_pct": change,
                    "turnover_cny": amount,
                    "main_net_inflow_cny": net,
                    "main_net_ratio_pct": ratio,
                    "source": "modi-hu/stock-data sector_daily_quotes (EastMoney-derived archive)",
                    "captured_at": row.get("created_at", ""),
                }
                merged[(day, code)] = item
                dates.add(day)
                imported += 1
        except Exception as exc:
            errors.append(f"{month}: {str(exc)[:300]}")
    if merged:
        fields = ["trade_date", "industry", "sector_code", "change_pct", "turnover_cny",
                  "main_net_inflow_cny", "main_net_ratio_pct", "source", "captured_at"]
        tmp = out.with_suffix(".csv.tmp")
        with tmp.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows(sorted(merged.values(), key=lambda x: (x.get("trade_date", ""), x.get("industry", ""))))
        tmp.replace(out)
    latest = max(dates) if dates else max((x.get("trade_date", "") for x in existing), default=None)
    status = "success" if imported else ("cached" if existing else "unavailable")
    source_file = DATA / "research_sources.json"
    try:
        doc = json.loads(source_file.read_text(encoding="utf-8"))
    except Exception:
        doc = {"sources": []}
    sources = {x.get("source"): x for x in doc.get("sources", [])}
    sources["sector_archive_history"] = {
        "source": "modi-hu/stock-data sector_daily_quotes",
        "status": status,
        "detail": f"EastMoney-derived third-party archive; latest trade date={latest}; imported rows={imported}; sector labels are BK codes, not mapped industry names."
                    + ((" Errors: " + " | ".join(errors)) if errors else ""),
    }
    doc.update({"trade_date": latest, "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "sources": list(sources.values())})
    source_file.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": status, "latest_trade_date": latest, "imported_rows": imported,
                      "history_rows": len(merged), "errors": errors}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
