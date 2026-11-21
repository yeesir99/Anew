#!/usr/bin/env python3
"""Refresh Chinese-language A-share news independently of trading-day close collection."""
from __future__ import annotations
import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from src.collectors import collect_news

DATA = ROOT / "data"


def main():
    items = collect_news(max_records=80)
    DATA.mkdir(exist_ok=True)
    for name in ("latest_news.json", "morning_news.json"):
        (DATA / name).write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    status = {"checked_at":datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "status":"success" if items else "empty","records":len(items),
        "sources":sorted({str(x.get("source")) for x in items}),
        "languages":sorted({str(x.get("language")) for x in items})}
    (DATA / "news_collection_status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
    print(json.dumps({"source":"GDELT and domestic Chinese financial feeds",
        **status,"files":["data/latest_news.json","data/morning_news.json"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
