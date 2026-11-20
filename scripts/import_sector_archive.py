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


def load_sector_names():
    """Resolve BK board codes to Chinese names with API, AKShare and cache fallbacks."""
    names = {}
    errors = []
    cache = DATA / "sector_names.csv"
    # EastMoney's public board-list endpoint returns f12=BK code, f14=Chinese name.
    for board_type in ("t:2", "t:3"):
        url = ("https://push2.eastmoney.com/api/qt/clist/get?"
               "pn=1&pz=1000&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:90+" +
               board_type + "&fields=f12,f14")
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 A-Share-Alpha-Radar/2.0",
                "Referer": "https://quote.eastmoney.com/",
                "Accept": "application/json,text/plain,*/*",
            })
            with urllib.request.urlopen(req, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
            rows = ((payload.get("data") or {}).get("diff") or [])
            for row in rows:
                code = str(row.get("f12") or "").strip()
                name = str(row.get("f14") or "").strip()
                if code.startswith("BK") and name and name not in ("-", "--"):
                    names[code] = name
        except Exception as exc:
            errors.append("EastMoney " + board_type + ": " + str(exc)[:180])
    # AKShare is a secondary live source; accept its actual board-code/name columns.
    if not names:
        try:
            import akshare as ak
            for fn_name in ("stock_board_industry_name_em", "stock_board_concept_name_em"):
                fn = getattr(ak, fn_name, None)
                if fn is None:
                    continue
                frame = fn()
                if frame is None or frame.empty:
                    continue
                code_col = next((x for x in ("板块代码", "板块编码", "代码", "f12") if x in frame.columns), None)
                name_col = next((x for x in ("板块名称", "行业名称", "名称", "f14") if x in frame.columns), None)
                if not code_col or not name_col:
                    errors.append(fn_name + ": expected code/name columns absent")
                    continue
                for _, row in frame.iterrows():
                    code = str(row.get(code_col, "")).strip()
                    name = str(row.get(name_col, "")).strip()
                    if code.startswith("BK") and name and name.lower() != "nan":
                        names[code] = name
        except Exception as exc:
            errors.append("AKShare: " + str(exc)[:180])
    # A previously validated cache survives temporary upstream rate limits.
    if cache.exists():
        try:
            with cache.open(encoding="utf-8-sig", newline="") as stream:
                for row in csv.DictReader(stream):
                    code, name = row.get("sector_code", "").strip(), row.get("industry_name", "").strip()
                    if code.startswith("BK") and name:
                        names.setdefault(code, name)
        except Exception as exc:
            errors.append("cache: " + str(exc)[:180])
    if names:
        cache.parent.mkdir(parents=True, exist_ok=True)
        with cache.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=["sector_code", "industry_name"])
            writer.writeheader()
            writer.writerows({"sector_code": code, "industry_name": name}
                             for code, name in sorted(names.items()))
    return names, errors


def main():
    sector_names, name_errors = load_sector_names()
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
                    "industry_name": sector_names.get(code),
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
    # Backfill names into all previously imported history, not just newly fetched rows.
    for item in merged.values():
        code = str(item.get("sector_code") or item.get("industry") or "").strip()
        if code in sector_names:
            item["industry_name"] = sector_names[code]
    if merged:
        fields = ["trade_date", "industry", "sector_code", "change_pct", "turnover_cny",
                  "main_net_inflow_cny", "main_net_ratio_pct", "industry_name", "source", "captured_at"]
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
        "detail": f"EastMoney-derived third-party archive; latest trade date={latest}; imported rows={imported}; sector names mapped={len(sector_names)}; unresolved codes={sum(1 for x in merged.values() if not x.get('industry_name'))}."
                    + ((" Mapping errors: " + " | ".join(name_errors)) if name_errors else "")\n                    + ((" Archive errors: " + " | ".join(errors)) if errors else ""),
    }
    doc.update({"trade_date": latest, "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "sources": list(sources.values())})
    source_file.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": status, "latest_trade_date": latest, "imported_rows": imported,
                      "history_rows": len(merged), "mapped_sector_names": len(sector_names), "mapping_errors": name_errors, "errors": errors}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
