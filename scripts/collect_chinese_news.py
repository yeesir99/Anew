#!/usr/bin/env python3
"""Refresh Chinese-language A-share news independently of trading-day close collection."""
from __future__ import annotations
import json
from pathlib import Path
from src.collectors import collect_news

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def main():
    items = collect_news(max_records=80)
    DATA.mkdir(exist_ok=True)
    for name in ("latest_news.json", "morning_news.json"):
        (DATA / name).write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"source":"GDELT Chinese-language A-share query",
        "records":len(items),"languages":sorted({str(x.get("language")) for x in items}),
        "files":["data/latest_news.json","data/morning_news.json"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
