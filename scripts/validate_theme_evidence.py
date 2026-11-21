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
    research_theme = next((x for x in research.get("sections", {}).get("themes", {}).get("items", [])
                           if str(x.get("industry_name") or x.get("industry") or "") == name), None)
    if research_theme is None:
        errors.append(f"{name}: missing matching research theme")
    else:
        model = research_theme.get("model") or {}
        score = model.get("score")
        coverage = model.get("potential_score_coverage")
        expected = 0 if research_theme.get("change_pct") is None else 15
        if coverage != expected:
            errors.append(f"{name}: potential-score coverage {coverage} != {expected}")
        if expected == 15 and score is None:
            errors.append(f"{name}: observed market-structure dimension has no partial score")
        dimensions = model.get("dimensions") or {}
        for key in ("policy_macro", "industry_trend", "capital_institutions", "earnings", "catalyst"):
            if dimensions.get(key) is not None:
                errors.append(f"{name}: unverified dimension {key} must remain null")
        health = model.get("health") or {}
        health_dims = health.get("dimensions") or {}
        if health.get("score") is not None and health.get("coverage", 0) <= 0:
            errors.append(f"{name}: health score has no coverage weight")
        if health.get("coverage", 0) < 100 and model.get("full_score_status") != "pending_required_evidence":
            errors.append(f"{name}: incomplete health score incorrectly marked complete")
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
quality_status = evidence.get("quality_status")
if quality_status not in ("validated", "degraded"):
    errors.append("collector quality_status is missing or unknown")
if with_constituents == 0 or not total_codes:
    errors.append("no valid theme constituent mapping; evidence cannot be joined")
if errors:
    print("FAIL:")
    for error in errors[:50]:
        print(" - " + error)
    raise SystemExit(1)
if quality_status == "degraded":
    print("DEGRADED: structurally valid partial mapping; unresolved themes and source failures remain visible")
    for name, item in themes.items():
        if not (item.get("constituent_codes") or []):
            print(" - unresolved theme: " + name)
    raise SystemExit(0)
print("PASS: theme evidence joins and coverage are structurally valid")
