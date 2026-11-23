#!/usr/bin/env python3
"""Build an explicit research-data quality contract without relabelling stale data."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

try:
    from theme_mapping_rules import has_mapping_provenance
except ModuleNotFoundError:
    from scripts.theme_mapping_rules import has_mapping_provenance

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SITE_DATA = ROOT / "site" / "data"
MIN_FLOW_ROWS = 1
REQUIRED_FLOW_COVERAGE_PCT = 95.0


def read_json(path: Path, default=None):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def read_csv(path: Path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def finite(value):
    try:
        return value is not None and float(value) == float(value) and abs(float(value)) != float("inf")
    except (TypeError, ValueError):
        return False


def evaluate_quality(market, research, flow_rows, ths_snapshot, theme_evidence, technical, valuation=None, source_registry=None):
    """Pure quality evaluation; missing/stale inputs remain missing, never inferred."""
    trade_day = str((market or {}).get("trade_date") or "")
    valid_stocks = int(((market or {}).get("coverage") or {}).get("valid_stock_count") or 0)
    market_valid = bool(trade_day and valid_stocks >= 2500 and (market or {}).get("data_status") == "validated")
    checks = {}

    source_items = (source_registry or {}).get("sources", [])
    source_by_name = {str(item.get("source") or ""): item for item in source_items}
    periods = ("今日", "5日", "10日")
    for period in periods:
        rows = [row for row in flow_rows
                if str(row.get("trade_date") or "") == trade_day
                and str(row.get("indicator") or "") == period
                and (finite(row.get("main_net_inflow_cny")) or finite(row.get("net_inflow_cny")))
                and str(row.get("source") or "").strip()]
        upstream = source_by_name.get("sector_flow_" + period, {})
        ths_snapshot_current = (period == "今日"
            and str((ths_snapshot or {}).get("trade_date") or "") == trade_day
            and (ths_snapshot or {}).get("status") == "success")
        ths_rows = []
        if ths_snapshot_current:
            ths_rows = [row for row in (ths_snapshot or {}).get("items", [])
                        if (finite(row.get("net_inflow_cny")) or finite(row.get("main_net_inflow_cny")))
                        and row.get("industry_name")]
        # Never merge EastMoney main-net estimates with THS provider-defined net
        # amounts. Use the independent THS snapshot only when the current
        # EastMoney period collection failed or was not recorded.
        ths_fallback = bool(ths_snapshot_current and upstream.get("status") != "success")
        if ths_fallback:
            rows = ths_rows
            upstream = {"status": "success", "records": (ths_snapshot or {}).get("records"),
                        "coverage_pct": (ths_snapshot or {}).get("coverage_pct"),
                        "provider": "THS", "detail": "separate live industry snapshot"}
        providers = sorted({str(row.get("source") or "") for row in rows})
        upstream_status = upstream.get("status", "not_recorded")
        coverage_pct = upstream.get("coverage_pct")
        period_available = len(rows) >= MIN_FLOW_ROWS and upstream_status == "success"
        period_valid = (period_available and isinstance(coverage_pct, (int, float))
                        and coverage_pct >= REQUIRED_FLOW_COVERAGE_PCT)
        checks["capital_flow_" + period] = {
            "status": "validated" if period_valid else "partial",
            "available": period_available,
            "trade_date": trade_day,
            "records": len(rows) if period_available else 0,
            "retained_historical_rows": len(rows),
            "coverage_pct": coverage_pct,
            "required_coverage_pct": REQUIRED_FLOW_COVERAGE_PCT,
            "latest_collection_status": upstream_status,
            "latest_collection_detail": upstream.get("detail"),
            "fallback_provider": "THS" if ths_fallback else None,
            "minimum_records": MIN_FLOW_ROWS,
            "providers": providers,
            "metric_definitions": sorted({str(row.get("metric_definition") or
                ("EastMoney main-net estimate" if row.get("main_net_inflow_cny") is not None else "provider net amount"))
                for row in rows}),
            "note": "记录存在且本次采集成功仅代表可用；只有来源明确提供覆盖率且达到95%才计为完整。THS当日快照独立展示，不与5日/10日混口径。",
        }

    themes = (theme_evidence or {}).get("themes") or {}
    expected_themes = [x for x in (((research or {}).get("sections") or {}).get("themes") or {}).get("items", [])
                       if isinstance(x, dict)]
    expected_names = [str(x.get("industry_name") or x.get("industry") or "").strip()
                      for x in expected_themes]
    expected_names = [x for x in expected_names if x]
    mapped = []
    unresolved = []
    for name in expected_names:
        item = themes.get(name) or {}
        codes = item.get("constituent_codes") or []
        mapping = item.get("mapping") or {}
        has_provenance = has_mapping_provenance(mapping)
        if codes and has_provenance:
            mapped.append(name)
        else:
            unresolved.append(name)
    theme_coverage = (len(mapped) / len(expected_names)) if expected_names else 0.0
    checks["theme_mapping"] = {
        "status": "validated" if expected_names and not unresolved else "partial",
        "mapped": len(mapped),
        "expected": len(expected_names),
        "coverage_pct": round(theme_coverage * 100, 1),
        "unresolved": unresolved,
        "note": "只有具备成分股代码和映射来源证据的主题才计为已映射。",
    }

    stock_total = int((technical or {}).get("stocks") or valid_stocks or 0)
    tech_coverage = (technical or {}).get("coverage") or {}
    tech_values = [int(v) for v in tech_coverage.values() if isinstance(v, (int, float))]
    tech_min = min(tech_values) if tech_values else 0
    checks["technical_indicators"] = {
        "status": "validated" if stock_total and tech_min >= stock_total else "partial",
        "stocks": stock_total,
        "minimum_indicator_coverage": tech_min,
        "coverage_pct": round(tech_min / stock_total * 100, 1) if stock_total else 0,
        "method": (technical or {}).get("method"),
    }

    valuation_status = "validated" if (valuation or {}).get("status") == "validated" and (valuation or {}).get("records", 0) else "unavailable"
    checks["valuation_crowding"] = {
        "status": valuation_status,
        "note": "缺少可靠的历史PE/PB分位数据时必须保持缺失，不以涨跌幅、换手率或资金流替代。",
    }

    all_checks = [v["status"] for v in checks.values()]
    if not market_valid:
        level = "L0_INVALID"
    elif all(status == "validated" for status in all_checks):
        level = "L3_RESEARCH_COMPLETE"
    elif not any(status == "validated" for status in all_checks):
        level = "L1_MARKET_VALID"
    else:
        level = "L2_RESEARCH_DEGRADED"
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "market_trade_date": trade_day or None,
        "market_valid": market_valid,
        "quality_level": level,
        "research_complete": level == "L3_RESEARCH_COMPLETE",
        "checks": checks,
        "policy": {
            "stale_rows_do_not_satisfy_current_trade_date": True,
            "flow_completeness_requires_provider_coverage_pct": REQUIRED_FLOW_COVERAGE_PCT,
            "missing_values_are_not_filled": True,
            "deployment_success_is_not_research_completeness": True,
            "valuation_proxy_substitution_forbidden": True,
        },
    }


def main():
    market = read_json(SITE_DATA / "latest.json", {})
    research = read_json(SITE_DATA / "research.json", {})
    flows = read_csv(DATA / "capital_flows.csv")
    ths = read_json(DATA / "industry_snapshot_ths.json", {})
    evidence = read_json(DATA / "theme_evidence.json", {})
    technical = read_json(DATA / "technical_coverage.json", {})
    valuation = read_json(DATA / "valuation_crowding.json", {})
    source_registry = read_json(DATA / "research_sources.json", {})
    report = evaluate_quality(market, research, flows, ths, evidence, technical, valuation, source_registry)
    research["research_quality"] = report
    for path in (DATA / "research_quality.json", SITE_DATA / "research_quality.json"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (SITE_DATA / "research.json").write_text(
        json.dumps(research, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    # Degraded research remains publishable with an explicit status. Only an
    # invalid market snapshot should fail this step.
    if not report["market_valid"]:
        raise SystemExit("FAIL: market snapshot is invalid; research quality cannot be evaluated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
