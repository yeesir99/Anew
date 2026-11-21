#!/usr/bin/env python3
"""Fail closed on malformed public industry ranking payloads."""
from __future__ import annotations

import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LATEST = ROOT / "site" / "data" / "latest.json"
RESEARCH = ROOT / "site" / "data" / "research.json"


def finite_number(value):
    if value is None or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def main():
    market = json.loads(LATEST.read_text(encoding="utf-8"))
    research = json.loads(RESEARCH.read_text(encoding="utf-8"))
    if research.get("privacy") != "public_research_only":
        raise RuntimeError("Public privacy boundary missing")
    sections = research.get("sections", {})
    for section_name in ("industries", "rotation", "themes"):
        items = sections.get(section_name, {}).get("items", [])
        if not isinstance(items, list):
            raise RuntimeError(f"{section_name}.items must be a list")
        for item in items:
            if not str(item.get("industry") or item.get("sector_code") or "").strip():
                raise RuntimeError(f"{section_name}: missing industry identity")
            display_name = str(item.get("industry_name") or item.get("display_name") or item.get("industry") or "").strip()
            if not display_name or display_name.startswith("BK"):
                raise RuntimeError(f"{section_name}: missing Chinese industry name for {item.get('sector_code') or item.get('industry')}")
            if not finite_number(item.get("change_pct", item.get("latest_change"))):
                raise RuntimeError(f"{section_name}: invalid/missing change percentage")
            if not finite_number(item.get("turnover_cny")) or float(item["turnover_cny"]) <= 0:
                raise RuntimeError(f"{section_name}: invalid/missing turnover")
            item_day = str(item.get("trade_date") or "")
            if not item_day or item_day > str(market["trade_date"]):
                raise RuntimeError(f"{section_name}: missing or future industry trade date")
            score = item.get("proxy_score", item.get("rotation_strength_score"))
            if score is not None and not finite_number(score):
                raise RuntimeError(f"{section_name}: non-finite proxy score")
    live_section = sections.get("industry-live", {})
    live_items = live_section.get("items", [])
    if live_section.get("status") == "success":
        if not isinstance(live_items, list) or not live_items:
            raise RuntimeError("THS live industry status is success but items are empty")
        if str(live_section.get("trade_date") or "") != str(market.get("trade_date") or ""):
            raise RuntimeError("THS live industry date does not match the latest market trade date")
        for item in live_items:
            if not str(item.get("industry_name") or "").strip():
                raise RuntimeError("THS live industry row missing industry name")
            if not finite_number(item.get("change_pct")):
                raise RuntimeError("THS live industry row has invalid change percentage")
            if item.get("turnover_cny") is not None and not finite_number(item.get("turnover_cny")):
                raise RuntimeError("THS live industry row has invalid turnover")
    industries = sections.get("industries", {}).get("items", [])
    if len(industries) > 20:
        raise RuntimeError("Industry ranking exceeds TOP 20")
    print(json.dumps({
        "market_trade_date": market.get("trade_date"),
        "industry_count": len(industries),
        "industry_trade_date": sections.get("industries", {}).get("note", "not supplied"),
        "status": "valid" if industries else "unavailable_no_valid_industry_rows",
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
