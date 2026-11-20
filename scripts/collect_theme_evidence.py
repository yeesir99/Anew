#!/usr/bin/env python3
"""Collect auditable public evidence for industry/theme candidates.

The collector deliberately keeps source records, report dates and coverage. It
never manufactures a complete mainline score from missing evidence.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import akshare as ak
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SITE = ROOT / "site" / "data"
LOG = logging.getLogger("theme-evidence")
KEYWORDS = (
    "中标", "重大合同", "销售合同", "采购合同", "订单", "框架协议", "项目建设",
    "产能", "投产", "扩产", "批复", "核准", "备案", "产品获批", "注册证",
    "战略合作", "供货", "定点", "政策", "规划", "专项", "补贴", "试点",
)


def clean(value):
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return None if text in ("", "nan", "None", "--", "-") else text


def number(value):
    try:
        result = pd.to_numeric(str(value).replace(",", "").replace("%", "").strip(), errors="coerce")
        return None if pd.isna(result) else float(result)
    except Exception:
        return None


def norm_code(value):
    raw = clean(value)
    if raw is None:
        return None
    raw = re.sub(r"\\.0$", "", raw)
    digits = re.sub(r"\\D", "", raw)
    return digits.zfill(6) if 1 <= len(digits) <= 6 else None


def source_function(name):
    fn = getattr(ak, name, None)
    if fn is not None:
        return fn
    def missing(*args, **kwargs):
        raise RuntimeError(name + " unavailable in installed AkShare")
    return missing


def records(frame):
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    return [{str(k): clean(v) if not isinstance(v, (int, float)) else (number(v)) for k, v in row.items()}
            for row in frame.to_dict("records")]


def find_col(columns, candidates=(), contains=()):
    for name in candidates:
        if name in columns:
            return name
    for name in columns:
        if any(token in str(name) for token in contains):
            return name
    return None


def latest_quarter_dates():
    today = date.today()
    candidates = []
    for year in (today.year, today.year - 1):
        for month, day in ((12, 31), (9, 30), (6, 30), (3, 31)):
            end = date(year, month, day)
            if end <= today:
                candidates.append(end)
    return sorted(candidates, reverse=True)


def call_source(name, fn, *args, **kwargs):
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        frame = fn(*args, **kwargs)
        rows = records(frame)
        return rows, {"source": name, "status": "success" if rows else "empty",
                      "records": len(rows), "checked_at": started}
    except Exception as exc:
        LOG.warning("%s failed: %s", name, exc)
        return [], {"source": name, "status": "failed", "records": 0,
                    "detail": str(exc)[:400], "checked_at": started}


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    SITE.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)
    research_path = SITE / "research.json"
    if not research_path.exists():
        raise RuntimeError("research.json missing; run dashboard builder first")
    research = json.loads(research_path.read_text(encoding="utf-8"))
    themes = research.get("sections", {}).get("themes", {}).get("items", [])
    if not themes:
        raise RuntimeError("No theme candidates in research.json")

    source_status = []
    theme_members = {}
    all_codes = set()
    # Constituents are fetched per theme; this is the join key for all evidence.
    for theme in themes:
        name = clean(theme.get("industry_name") or theme.get("industry"))
        if not name:
            continue
        rows, status = call_source("constituents:" + name, source_function("stock_board_industry_cons_em"), symbol=name)
        source_status.append(status)
        code_col = find_col(rows[0].keys(), ("代码", "股票代码", "证券代码")) if rows else None
        name_col = find_col(rows[0].keys(), ("名称", "股票简称", "证券简称")) if rows else None
        members = []
        for row in rows:
            code = re.sub(r"\D", "", str(row.get(code_col) or ""))
            if len(code) != 6:
                continue
            members.append({"code": code, "name": row.get(name_col)})
            all_codes.add(code)
        theme_members[name] = members
        time.sleep(0.25)

    # Latest published quarterly financial report and forecast, each as a full-market
    # table. Keep the exact reporting period and source columns.
    financial_rows, financial_status = [], {"source": "quarterly_financials", "status": "unavailable", "records": 0}
    forecast_rows, forecast_status = [], {"source": "earnings_forecasts", "status": "unavailable", "records": 0}
    report_date = None
    for quarter in latest_quarter_dates():
        key = quarter.strftime("%Y%m%d")
        candidate, status = call_source("stock_yjbb_em:" + key, source_function("stock_yjbb_em"), date=key)
        source_status.append(status)
        if candidate:
            financial_rows, financial_status, report_date = candidate, status, key
            break
        time.sleep(0.3)
    for quarter in latest_quarter_dates():
        key = quarter.strftime("%Y%m%d")
        candidate, status = call_source("stock_yjyg_em:" + key, source_function("stock_yjyg_em"), date=key)
        source_status.append(status)
        if candidate:
            forecast_rows, forecast_status = candidate, status
            break
        time.sleep(0.3)

    # Institution ownership: broad-market institutional ownership and fund
    # reporting coverage are distinct measures and are retained separately.
    institution_rows, institution_status = [], {"source": "institution_holdings", "status": "unavailable", "records": 0}
    fund_rows, fund_status = [], {"source": "public_fund_holdings_cninfo", "status": "unavailable", "records": 0}
    if report_date:
        year = report_date[:4]
        month = int(report_date[4:6])
        quarter_num = 1 if month == 3 else 2 if month == 6 else 3 if month == 9 else 4
        fn = getattr(ak, "stock_institute_hold", None)
        if fn:
            institution_rows, institution_status = call_source(
                "stock_institute_hold:" + year + str(quarter_num), fn, quarter=year + str(quarter_num))
            source_status.append(institution_status)
        fn = getattr(ak, "fund_report_stock_cninfo", None)
        if fn:
            fund_rows, fund_status = call_source("fund_report_stock_cninfo:" + report_date, fn, date=report_date)
            source_status.append(fund_status)

    # Contract disclosures: fetch a 180-day window once, then join by constituents.
    end_day = date.today()
    start_day = end_day - timedelta(days=180)
    contract_rows, contract_status = call_source(
        "stock_zdhtmx_em", source_function("stock_zdhtmx_em"),
        start_date=start_day.strftime("%Y%m%d"), end_date=end_day.strftime("%Y%m%d"))
    source_status.append(contract_status)

    # Recent company announcements are retained as event evidence, not asserted
    # to be government policy. A separate official policy source is not yet
    # integrated and therefore remains explicitly missing.
    notice_rows = []
    notice_statuses = []
    cursor = end_day
    business_days = 0
    while business_days < 5:
        if cursor.weekday() < 5:
            day_key = cursor.strftime("%Y%m%d")
            rows, status = call_source("stock_notice_report:" + day_key,
                source_function("stock_notice_report"), date=day_key, notice_category="全部")
            notice_rows.extend(rows)
            notice_statuses.append(status)
            business_days += 1
            time.sleep(0.25)
        cursor -= timedelta(days=1)
    source_status.extend(notice_statuses)

    def indexed(rows, code_candidates):
        if not rows:
            return {}
        col = find_col(rows[0].keys(), code_candidates)
        result = {}
        if not col:
            return result
        for row in rows:
            code = re.sub(r"\D", "", str(row.get(col) or ""))
            if len(code) == 6:
                result.setdefault(code, []).append(row)
        return result

    finance_by_code = indexed(financial_rows, ("股票代码", "证券代码"))
    forecast_by_code = indexed(forecast_rows, ("股票代码", "证券代码"))
    institution_by_code = indexed(institution_rows, ("证券代码", "股票代码"))
    fund_by_code = indexed(fund_rows, ("股票代码", "证券代码"))
    contract_by_code = indexed(contract_rows, ("股票代码", "证券代码", "代码"))
    notice_by_code = indexed(notice_rows, ("股票代码", "证券代码", "代码"))

    output = {}
    for theme in themes:
        name = clean(theme.get("industry_name") or theme.get("industry"))
        members = theme_members.get(name, [])
        codes = {x["code"] for x in members}
        financial = [r for c in codes for r in finance_by_code.get(c, [])]
        forecasts = [r for c in codes for r in forecast_by_code.get(c, [])]
        institutions = [r for c in codes for r in institution_by_code.get(c, [])]
        funds = [r for c in codes for r in fund_by_code.get(c, [])]
        contracts = [r for c in codes for r in contract_by_code.get(c, [])]
        notices = [r for c in codes for r in notice_by_code.get(c, [])]
        # Keep relevant event titles only; full raw rows remain auditable.
        event_rows = []
        for row in notices:
            title_col = find_col(row.keys(), ("公告标题", "标题", "公告名称"), ("标题",))
            title = clean(row.get(title_col)) if title_col else None
            if title and any(word in title for word in KEYWORDS):
                event_rows.append(row)
        output[name] = {
            "sector_code": theme.get("sector_code"),
            "constituent_count": len(members),
            "constituent_codes": sorted(codes),
            "financials": {"report_date": report_date, "records": financial,
                "coverage": len({re.sub(r"\\D", "", str(r.get("股票代码") or r.get("证券代码") or "")) for r in financial})},
            "earnings_forecasts": {"records": forecasts, "coverage": len(forecasts)},
            "institutional_holdings": {"report_date": report_date, "records": institutions,
                "coverage": len(institutions), "source": "Sina stock_institute_hold"},
            "public_fund_holdings": {"report_date": report_date, "records": funds,
                "coverage": len(funds), "source": "CNINFO fund_report_stock_cninfo"},
            "contracts": {"records": contracts, "coverage": len(contracts), "source": "AKShare/EastMoney stock_zdhtmx_em"},
            "company_events": {"records": event_rows, "coverage": len(event_rows), "source": "AKShare stock_notice_report"},
            "government_policy": {"records": [], "coverage": 0,
                "status": "not_integrated", "note": "尚未接入部委/地方政府原文政策库；公司公告不能冒充政府政策。"},
        }

    evidence = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "market_trade_date": research.get("generated_at"),
        "financial_report_date": report_date,
        "themes": output,
        "sources": source_status,
        "limitations": [
            "业绩、预告和持仓按公开报告期披露，存在季度滞后；必须显示报告期。",
            "合同和公告是上市公司披露，不等于已确认收入或全行业订单总量。",
            "机构持仓与公募基金持仓分别统计，不等同实时机构净买入。",
            "政府政策原文尚未接入，不从公司公告推断政策出台。",
        ],
    }
    (DATA / "theme_evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Enrich the public research JSON with evidence and explicit coverage. Do not
    # change the existing full-score gate or manufacture a full score.
    for theme in themes:
        name = clean(theme.get("industry_name") or theme.get("industry"))
        item = output.get(name, {})
        theme["evidence_data"] = item
        theme["evidence_coverage"] = {
            "constituents": item.get("constituent_count", 0),
            "financials": item.get("financials", {}).get("coverage", 0),
            "earnings_forecasts": item.get("earnings_forecasts", {}).get("coverage", 0),
            "institutional_holdings": item.get("institutional_holdings", {}).get("coverage", 0),
            "public_fund_holdings": item.get("public_fund_holdings", {}).get("coverage", 0),
            "contracts": item.get("contracts", {}).get("coverage", 0),
            "company_events": item.get("company_events", {}).get("coverage", 0),
            "government_policy": item.get("government_policy", {}).get("coverage", 0),
        }
        theme["model"] = theme.get("model") or {}
        theme["model"]["evidence_status"] = "partial" if any(theme["evidence_coverage"].values()) else "unavailable"
        theme["model"]["full_score_status"] = "pending_required_evidence"
        missing = []
        for key, label in (("financials", "财务"), ("earnings_forecasts", "业绩预告"),
                           ("institutional_holdings", "机构持仓"), ("public_fund_holdings", "公募基金持仓"),
                           ("contracts", "重大合同"), ("company_events", "公司事件"), ("government_policy", "政府政策")):
            if theme["evidence_coverage"].get(key, 0) == 0:
                missing.append(label)
        theme["model"]["missing_evidence"] = missing
    research.setdefault("sections", {})["themes"] = {"items": themes,
        "note": "完整主线评分沿用原有证据门槛；新增财报、业绩预告、机构/公募持仓、重大合同和公司公告证据，缺失政府政策原文单独标注。"}
    research["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    research_path.write_text(json.dumps(research, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    summary = {"themes": len(output), "constituents": len(all_codes),
        "financial_report_date": report_date, "financial_records": len(financial_rows),
        "forecast_records": len(forecast_rows), "institution_records": len(institution_rows),
        "fund_records": len(fund_rows), "contract_records": len(contract_rows),
        "notice_records": len(notice_rows), "source_statuses": source_status}
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
