#!/usr/bin/env python3
"""Validate theme evidence joins without changing score thresholds."""
from __future__ import annotations
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
evidence_path = root / "data" / "theme_evidence.json"
research_path = root / "site" / "data" / "research.json"
if not evidence_path.exists() or not research_path.exists():
    raise SystemExit("FAIL: theme_evidence.json or research.json is missing")
evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
research = json.loads(research_path.read_text(encoding="utf-8"))
themes = evidence.get("themes", {})
errors = []
if not themes:
    errors.append("no theme evidence records")
with_constituents = 0
total_codes = set()
for name, item in themes.items():
    codes = item.get("constituent_codes") or []
    if len(codes) != item.get("constituent_count", -1):
        errors.append(f"{name}: constituent_count mismatch")
    if codes:
        with_constituents += 1
    total_codes.update(codes)
    member_set = set(codes)
    for bucket in ("financials", "earnings_forecasts", "institutional_holdings",
                   "public_fund_holdings", "contracts", "company_events"):
        for row in item.get(bucket, {}).get("records", []):
            raw = row.get("股票代码") or row.get("证券代码") or row.get("代码")
            if raw is not None:
                code = str(raw).split(".")[0].strip().zfill(6)
                if len(code) == 6 and code not in member_set:
                    errors.append(f"{name}: {bucket} contains non-constituent {code}")
                    break
    for bucket in ("financials", "earnings_forecasts", "institutional_holdings",
                   "public_fund_holdings", "contracts", "company_events"):
        rows = item.get(bucket, {}).get("records", [])
        if rows and not isinstance(rows, list):
            errors.append(f"{name}: {bucket}.records is not a list")
source_failures = [x for x in evidence.get("sources", []) if x.get("status") == "failed"]
summary = {"themes": len(themes), "themes_with_constituents": with_constituents,
           "unique_constituents": len(total_codes), "failed_sources": len(source_failures),
           "quality_status": evidence.get("quality_status")}
print(json.dumps(summary, ensure_ascii=False))
if evidence.get("quality_status") != "validated":
    errors.append("collector quality_status is not validated")
if with_constituents == 0 or not total_codes:
    errors.append("no valid theme constituent mapping; evidence cannot be joined")
if errors:
    print("FAIL:")
    for error in errors[:50]:
        print(" - " + error)
    raise SystemExit(1)
print("PASS: theme evidence joins and coverage are structurally valid")
