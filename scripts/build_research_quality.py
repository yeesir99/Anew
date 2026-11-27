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
REQUIRED_TECHNICAL_COVERAGE_PCT = 95.0


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

    registry_current = str((source_registry or {}).get("trade_date") or "") == trade_day
    source_items = (source_registry or {}).get("sources", []) if registry_current else []
    source_by_name = {str(item.get("source") or ""): item for item in source_items}
    periods = ("今日", "5日", "10日")
    for period in periods:
        rows = [row for row in flow_rows
                if str(row.get("trade_date") or "") == trade_day
                and str(row.get("indicator") or "") == period
                and (finite(row.get("main_net_inflow_cny")) or finite(row.get("net_inflow_cny")))
                and str(row.get("source") or "").strip()]
        upstream = source_by_name.get("sector_flow_" + period, {})
        ths_snapshot_current = (registry_current and period == "今日"
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
        expected_provider = upstream.get("provider")
        def row_provider(row):
            declared = str(row.get("provider") or "").strip()
            if declared in {"THS", "EastMoney"}:
                return declared
            source = str(row.get("source") or "")
            return "THS" if "THS" in source else "EastMoney" if "EastMoney" in source else None
        provider_contract_valid = expected_provider in {"THS", "EastMoney"}
        if provider_contract_valid:
            rows = [row for row in rows if row_provider(row) == expected_provider]
        else:
            rows = []
        providers = sorted({str(row.get("source") or "") for row in rows})
        upstream_status = upstream.get("status", "not_recorded")
        coverage_pct = upstream.get("coverage_pct")
        declared_records = int(upstream.get("records") or 0)
        record_count_matches = declared_records > 0 and len(rows) == declared_records
        period_available = (len(rows) >= MIN_FLOW_ROWS and upstream_status == "success"
                            and provider_contract_valid and record_count_matches)
        period_valid = (period_available and isinstance(coverage_pct, (int, float))
                        and coverage_pct >= REQUIRED_FLOW_COVERAGE_PCT)
        checks["capital_flow_" + period] = {
            "status": "validated" if period_valid else "partial",
            "available": period_available,
            "trade_date": trade_day,
            "records": len(rows) if period_available else 0,
            "declared_records": declared_records,
            "record_count_matches": record_count_matches,
            "upstream_status": upstream_status,
            "source_detail": upstream.get("detail"),
            "duplicate_conflicts": upstream.get("conflicting_duplicates") or [],
            "provider_contract_valid": provider_contract_valid,
            "expected_provider": expected_provider,
            "coverage_pct": coverage_pct,
            "required_coverage_pct": REQUIRED_FLOW_COVERAGE_PCT,
            "latest_collection_status": upstream_status,
            "latest_collection_detail": upstream.get("detail"),
            "fallback_provider": "THS" if upstream.get("provider") == "THS" else None,
            "minimum_records": MIN_FLOW_ROWS,
            "providers": providers,
            "metric_definitions": sorted({str(row.get("metric_definition") or
                ("EastMoney main-net estimate" if row.get("main_net_inflow_cny") is not None else "provider net amount"))
                for row in rows}),
            "note": "记录存在且本次采集成功仅代表可用；只有来源明确提供覆盖率且达到95%才计为完整。THS当日快照独立展示，不与5日/10日混口径。",
        }

    # Industry ranking freshness is a separate dimension from the historical
    # sector archive. Prefer a current EastMoney snapshot, otherwise use the
    # independently labelled THS snapshot without mixing its taxonomy.
    industry_em = source_by_name.get("industry_snapshot", {})
    industry_ths = source_by_name.get("industry_snapshot_ths", {})
    ths_industry_current = bool(
        registry_current
        and str((ths_snapshot or {}).get("trade_date") or "") == trade_day
        and (ths_snapshot or {}).get("status") == "success"
        and industry_ths.get("status") == "success"
    )
    if industry_em.get("status") == "success":
        industry_source = industry_em
        industry_provider = industry_em.get("provider") or "EastMoney"
        industry_snapshot_current = registry_current
    elif ths_industry_current:
        industry_source = industry_ths
        industry_provider = "THS"
        industry_snapshot_current = True
    else:
        industry_source = industry_em if industry_em else industry_ths
        industry_provider = industry_source.get("provider")
        industry_snapshot_current = False
    industry_records = int(industry_source.get("records") or 0)
    industry_coverage = industry_source.get("coverage_pct")
    industry_available = bool(industry_snapshot_current and industry_records > 0)
    industry_valid = bool(
        industry_available
        and isinstance(industry_coverage, (int, float))
        and industry_coverage >= REQUIRED_FLOW_COVERAGE_PCT
    )
    checks["industry_snapshot"] = {
        "status": "validated" if industry_valid else "partial",
        "available": industry_available,
        "provider": industry_provider,
        "trade_date": trade_day if industry_snapshot_current else None,
        "records": industry_records if industry_available else 0,
        "coverage_pct": industry_coverage,
        "required_coverage_pct": REQUIRED_FLOW_COVERAGE_PCT,
        "note": "行业实时快照与历史归档分开验证；THS备用榜单独展示，不与申万历史行业序列混合。",
    }

    themes = (theme_evidence or {}).get("themes") or {}
    expected_themes = [x for x in (((research or {}).get("sections") or {}).get("themes") or {}).get("items", [])
                       if isinstance(x, dict)]
    expected_names = [str(x.get("industry_name") or x.get("industry") or "").strip()
                      for x in expected_themes]
    expected_names = [x for x in expected_names if x]
    mapped = []
    unresolved = []
    evidence_trade_day = str((theme_evidence or {}).get("market_trade_date") or "")
    theme_evidence_date_matches = evidence_trade_day == trade_day
    for name in expected_names:
        item = themes.get(name) or {}
        if not theme_evidence_date_matches:
            unresolved.append(name)
            continue
        codes = item.get("constituent_codes") or []
        mapping = item.get("mapping") or {}
        count_matches = int(item.get("constituent_count") or 0) == len(codes)
        codes_valid = bool(codes) and all(
            isinstance(code, str) and len(code) == 6 and code.isdigit() for code in codes)
        has_provenance = has_mapping_provenance(mapping)
        if codes_valid and count_matches and has_provenance:
            mapped.append(name)
        else:
            unresolved.append(name)
    theme_coverage = (len(mapped) / len(expected_names)) if expected_names else 0.0
    checks["theme_mapping"] = {
        "status": "validated" if expected_names and not unresolved and theme_evidence_date_matches else "partial",
        "evidence_trade_date": evidence_trade_day or None,
        "trade_date_matches_market": theme_evidence_date_matches,
        "mapped": len(mapped),
        "expected": len(expected_names),
        "coverage_pct": round(theme_coverage * 100, 1),
        "unresolved": unresolved,
        "note": "只有具备成分股代码和映射来源证据的主题才计为已映射。",
    }

    technical_stocks = int((technical or {}).get("stocks") or 0)
    stock_total = valid_stocks
    technical_universe_matches_market = bool(stock_total and technical_stocks == stock_total)
    # Technical coverage is valid only for the exact market trade date. A prior
    # day's complete indicator file must never be carried forward as current.
    def day_key(value):
        raw = str(value or "").strip()
        digits = "".join(ch for ch in raw if ch.isdigit())
        return digits[:8] if len(digits) >= 8 else raw
    technical_date_matches = bool(
        day_key((technical or {}).get("trade_date")) and
        day_key((technical or {}).get("trade_date")) == day_key(trade_day)
    )
    technical_status_valid = (technical or {}).get("status") == "validated"
    tech_coverage = (technical or {}).get("coverage") or {}
    required_indicators = ("ma5", "ma10", "ma20", "ma60", "rsi14",
                           "macd_dif", "macd_dea", "macd_hist", "volatility20")
    tech_values = [int(tech_coverage.get(key) or 0) for key in required_indicators]
    tech_min = min(tech_values) if tech_values else 0
    minimum_technical_count = int(stock_total * REQUIRED_TECHNICAL_COVERAGE_PCT / 100.0 + 0.999)
    tech_coverage_pct = round(min(100.0, tech_min / stock_total * 100), 1) if stock_total else 0
    technical_valid = (technical_universe_matches_market and technical_date_matches
                       and technical_status_valid and tech_min >= minimum_technical_count)
    checks["technical_indicators"] = {
        "status": "validated" if technical_valid else "partial",
        "stocks": stock_total,
        "technical_snapshot_stocks": technical_stocks,
        "universe_matches_market": technical_universe_matches_market,
        "trade_date_matches_market": technical_date_matches,
        "technical_snapshot_trade_date": (technical or {}).get("trade_date"),
        "technical_snapshot_status": (technical or {}).get("status"),
        "minimum_indicator_coverage": tech_min,
        "coverage_pct": tech_coverage_pct,
        "required_coverage_pct": REQUIRED_TECHNICAL_COVERAGE_PCT,
        "minimum_required_count": minimum_technical_count,
        "method": (technical or {}).get("method"),
    }

    valuation_trade_day = str((valuation or {}).get("trade_date") or "")
    valuation_records = int((valuation or {}).get("records") or 0)
    valuation_coverage = (valuation or {}).get("coverage_pct")
    valuation_provider_dates = (valuation or {}).get("provider_dates") or {}
    valuation_source_urls = (valuation or {}).get("source_urls") or {}
    valuation_scope_valid = (
        (valuation or {}).get("scope") == "broad_market"
        and (valuation or {}).get("coverage_basis") == "required_broad_market_valuation_metrics"
        and str(valuation_source_urls.get("pe") or "").startswith("https://")
        and str(valuation_source_urls.get("pb") or "").startswith("https://")
    )
    valuation_dates_match = (
        str(valuation_provider_dates.get("pe") or "") == trade_day
        and str(valuation_provider_dates.get("pb") or "") == trade_day
    )
    valuation_provenance = bool(
        (valuation or {}).get("source")
        and (valuation or {}).get("provider")
        and (valuation or {}).get("metric_definition")
        and valuation_scope_valid
    )
    valuation_valid = bool(
        (valuation or {}).get("status") == "validated"
        and valuation_trade_day == trade_day
        and valuation_dates_match
        and valuation_records > 0
        and valuation_provenance
        and isinstance(valuation_coverage, (int, float))
        and valuation_coverage >= REQUIRED_FLOW_COVERAGE_PCT
    )
    valuation_status = "validated" if valuation_valid else "unavailable"
    checks["valuation_crowding"] = {
        "status": valuation_status,
        "trade_date": valuation_trade_day or None,
        "trade_date_matches_market": valuation_trade_day == trade_day,
        "records": valuation_records,
        "coverage_pct": valuation_coverage,
        "provenance_valid": valuation_provenance,
        "scope": (valuation or {}).get("scope"),
        "note": (valuation or {}).get("note"),
        "metric_definition": (valuation or {}).get("metric_definition"),
        "provider_dates": valuation_provider_dates,
        "provider_dates_match_market": valuation_dates_match,
        "note": "仅验证全A股市场整体PE/PB历史分位，不代表任何行业或主题的估值分位；PE与PB来源日期必须分别匹配行情交易日，字段覆盖至少95%。缺失时不以涨跌幅、换手率或资金流替代。",
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
            "technical_indicator_minimum_coverage_pct": REQUIRED_TECHNICAL_COVERAGE_PCT,
            "flow_rows_require_explicit_provider_identity": True,
            "theme_evidence_requires_market_trade_date": True,
            "industry_snapshot_separate_from_historical_archive": True,
            "valuation_requires_trade_date_source_provider_and_coverage": True,
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
