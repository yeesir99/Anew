#!/usr/bin/env python3
"""Validate evidence joins and weighted theme-health coverage."""
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
weights = {"industry_thesis": 20, "policy_event": 10, "capital_persistence": 20,
           "trend_structure": 20, "earnings_delivery": 15, "valuation_crowding": 15}
if not themes:
    errors.append("no theme evidence records")
with_constituents = 0
total_codes = set()
health_coverage_values = []
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
        health = model.get("health") or {}
        dimensions = health.get("dimensions") or {}
        observed = [key for key in weights if dimensions.get(key) is not None]
        expected_health_coverage = sum(weights[key] for key in observed)
        if health.get("coverage") != expected_health_coverage:
            errors.append(f"{name}: health coverage {health.get('coverage')} != {expected_health_coverage}")
        if health.get("score") is not None and expected_health_coverage == 0:
            errors.append(f"{name}: health score has no coverage weight")
        if health.get("score") is None and expected_health_coverage > 0:
            errors.append(f"{name}: health score missing despite scored dimensions")
        if expected_health_coverage < 100 and model.get("full_score_status") != "pending_required_evidence":
            errors.append(f"{name}: incomplete health score incorrectly marked complete")
        for key, value in dimensions.items():
            if key in weights and value is not None and not 0 <= value <= 100:
                errors.append(f"{name}: {key} score outside [0,100]")
        if dimensions.get("valuation_crowding") is not None:
            errors.append(f"{name}: valuation_crowding requires validated valuation percentile source")
        health_coverage_values.append(expected_health_coverage)
    codes = item.get("constituent_codes") or []
    if len(codes) != item.get("constituent_count", -1):
        errors.append(f"{name}: constituent_count mismatch")
    if codes:
        with_constituents += 1
    total_codes.update(codes)
    member_set = set(codes)
    for bucket in ("financials", "earnings_forecasts", "institutional_holdings",
                   "public_fund_holdings", "contracts", "company_events"):
        rows = item.get(bucket, {}).get("records", [])
        if not isinstance(rows, list):
            errors.append(f"{name}: {bucket}.records is not a list")
            continue
        for row in rows:
            raw = row.get("股票代码") or row.get("证券代码") or row.get("代码")
            if raw is not None:
                code = str(raw).split(".")[0].strip().zfill(6)
                if len(code) == 6 and code not in member_set:
                    errors.append(f"{name}: {bucket} contains non-constituent {code}")
                    break
source_failures = [x for x in evidence.get("sources", []) if x.get("status") == "failed"]
summary = {"themes": len(themes), "themes_with_constituents": with_constituents,
           "unique_constituents": len(total_codes), "failed_sources": len(source_failures),
           "health_coverage_avg": round(sum(health_coverage_values) / len(health_coverage_values), 1)
                if health_coverage_values else 0,
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
print("PASS: theme evidence joins and weighted health coverage are structurally valid")
