#!/usr/bin/env python3
"""Collect auditable public evidence for industry/theme candidates.

The collector deliberately keeps source records, report dates and coverage. It
never manufactures a complete mainline score from missing evidence.
"""
from __future__ import annotations

import json
import logging
import inspect
import re
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import akshare as ak
import pandas as pd
from import_sector_archive import is_concept_code

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SITE = ROOT / "site" / "data"
LOG = logging.getLogger("theme-evidence")
THEME_ALIASES = {
    "集成电路制造": ("半导体", "集成电路", "芯片概念"),
    "橡胶助剂": ("橡胶制品", "化工新材料"),
    "厨房小家电": ("小家电", "厨卫电器"),
    "半导体材料": ("半导体", "电子化学品", "新材料概念"),
    "钼": ("小金属", "小金属概念"),
    "医疗器械": ("医疗器械", "医疗器械概念"),
    "辅助生殖": ("辅助生殖", "医疗服务"),
    "商业服务": ("专业服务", "商业服务"),
    "烟草与大麻": ("烟草与大麻",),
    "个护用品Ⅲ": ("美容护理", "个护用品"),
    "白酒概念": ("白酒", "白酒概念"),
    "养老概念": ("养老概念", "养老金"),
    "港口航运": ("港口航运", "航运概念"),
    "第三代半导体": ("第三代半导体", "半导体"),
    "机器人概念": ("机器人概念", "机器人"),
    "乳业": ("乳业",),
    "可选消费经销Ⅲ": ("可选消费经销Ⅲ", "贸易"),
    "参股民营银行": ("参股民营银行", "银行"),
    "粤港澳大湾区": ("粤港澳大湾区",),
    "科创次新股": ("科创次新股", "次新股"),
}


POLICY_SYNONYMS = {
    "集成电路制造": ("集成电路", "半导体", "芯片"),
    "半导体材料": ("半导体", "集成电路", "电子材料"),
    "光伏设备": ("光伏", "太阳能", "新能源"),
    "电池": ("电池", "储能", "新能源"),
    "汽车整车": ("汽车", "新能源汽车", "智能网联"),
    "工业互联网": ("工业互联网", "工业软件", "智能制造"),
    "机器人": ("机器人", "智能制造", "人工智能"),
    "通信设备": ("通信", "5G", "6G", "算力网络"),
    "软件开发": ("软件", "人工智能", "工业软件", "数据"),
}


class OfficialPolicyLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.href = None
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self.href = dict(attrs).get("href")
            self.parts = []

    def handle_data(self, data):
        if self.href is not None:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.href is not None:
            title = " ".join("".join(self.parts).split())
            if title:
                self.links.append((self.href, title))
            self.href = None
            self.parts = []


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
    raw = re.sub(r"\.0$", "", raw)
    digits = re.sub(r"\D", "", raw)
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
    # AkShare 1.16+ no longer ships the old THS constituent functions. Use the
    # supported EastMoney industry/concept endpoints and the BK code from the
    # validated sector archive. Cache successes and stop hammering Push2 on 429.
    cache_path = DATA / "theme_constituents_cache.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    except Exception:
        cache = {}
    cache_dirty = False
    theme_member_meta = {}
    push2_circuit_open = False

    def fetch_members(theme, name):
        nonlocal cache_dirty, push2_circuit_open
        code = clean(theme.get("sector_code"))
        concept = is_concept_code(code)
        cached = cache.get(name) or {}
        cached_members = cached.get("members") or []
        cached_at = clean(cached.get("fetched_at"))
        if cached_members and cached_at:
            try:
                age_days = (datetime.now(timezone.utc) - datetime.fromisoformat(cached_at.replace("Z","+00:00"))).days
                if age_days <= 30:
                    return cached_members, {"source":"constituents:"+name,"status":"cached",
                        "records":len(cached_members),"mapping_source":cached.get("source"),
                        "mapping_date":cached_at,"checked_at":datetime.now(timezone.utc).isoformat(timespec="seconds")}
            except Exception:
                pass
        if push2_circuit_open:
            return [], {"source":"constituents:"+name,"status":"failed","records":0,
                "detail":"EastMoney Push2 circuit breaker open after upstream rate limit",
                "checked_at":datetime.now(timezone.utc).isoformat(timespec="seconds")}
        attempts = [("stock_board_concept_cons_em" if concept else "stock_board_industry_cons_em",
                     "EastMoney concept" if concept else "EastMoney industry")]
        if not code:
            attempts = [("stock_board_industry_cons_em","EastMoney industry"),
                        ("stock_board_concept_cons_em","EastMoney concept")]
        errors = []
        candidates = tuple(dict.fromkeys(x for x in (code, name, *THEME_ALIASES.get(name, ())) if x))
        for fn_name, label in attempts:
            fn = getattr(ak, fn_name, None)
            if fn is None:
                errors.append(fn_name + " unavailable in installed AkShare")
                continue
            try:
                if "symbol" not in inspect.signature(fn).parameters:
                    errors.append(fn_name + " has no symbol parameter")
                    continue
            except Exception as exc:
                errors.append(fn_name + " signature unavailable: " + str(exc)[:100])
                continue
            for candidate in candidates:
                try:
                    frame = fn(symbol=candidate)
                    rows = records(frame)
                    if not rows:
                        errors.append(fn_name + "(" + candidate + ") returned empty")
                        continue
                    code_col = find_col(rows[0].keys(), ("代码","股票代码","证券代码","成分股代码"))
                    name_col = find_col(rows[0].keys(), ("名称","股票简称","证券简称","成分股名称"))
                    if not code_col:
                        errors.append(fn_name + " missing security-code column")
                        continue
                    members = list({norm_code(row.get(code_col)): {"code":norm_code(row.get(code_col)),
                        "name":row.get(name_col)} for row in rows if norm_code(row.get(code_col))})
                    if members:
                        fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
                        cache[name] = {"members":members,"fetched_at":fetched,"source":fn_name,
                            "matched_symbol":candidate,"sector_code":code}
                        cache_dirty = True
                        return members, {"source":"constituents:"+name,"status":"success",
                            "records":len(members),"provider":label,"matched_symbol":candidate,
                            "mapping_source":fn_name,"mapping_date":fetched,"checked_at":fetched}
                    errors.append(fn_name + "(" + candidate + ") returned no valid six-digit codes")
                except Exception as exc:
                    message = str(exc)
                    errors.append(fn_name + "(" + candidate + "): " + message[:140])
                    if "Push2RateLimitError" in message or "短时间内请求过多" in message or "429" in message:
                        push2_circuit_open = True
                        break
                time.sleep(0.8)
            if push2_circuit_open:
                break
        return [], {"source":"constituents:"+name,"status":"failed","records":0,
            "detail":"; ".join(errors)[:400],
            "checked_at":datetime.now(timezone.utc).isoformat(timespec="seconds")}

    for theme in themes:
        name = clean(theme.get("industry_name") or theme.get("industry"))
        if not name:
            continue
        members, status = fetch_members(theme, name)
        source_status.append(status)
        theme_members[name] = members
        theme_member_meta[name] = status
        all_codes.update(x["code"] for x in members)
    if cache_dirty:
        cache_path.write_text(json.dumps(cache,ensure_ascii=False,indent=2)+"\\n",encoding="utf-8")

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
            # AkShare has shipped incompatible signatures for this endpoint.
            # Inspect the installed function instead of assuming a parameter.
            try:
                params = inspect.signature(fn).parameters
                period = year + "Q" + str(quarter_num)
                if "quarter" in params:
                    institution_rows, institution_status = call_source(
                        "stock_institute_hold:" + period, fn, quarter=year + str(quarter_num))
                elif "symbol" in params:
                    institution_rows, institution_status = call_source(
                        "stock_institute_hold:" + period, fn, symbol=period)
                else:
                    institution_status = {"source": "stock_institute_hold:" + period,
                        "status": "failed", "records": 0,
                        "detail": "Unsupported installed signature: " + str(inspect.signature(fn))}
            except Exception as exc:
                institution_status = {"source": "stock_institute_hold", "status": "failed",
                    "records": 0, "detail": str(exc)[:400]}
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

    # Official government policy/notice feed. Kept separate from listed-company announcements.
    policy_rows = []
    policy_status = {"source": "MIIT_official_release_index", "status": "unavailable", "records": 0}
    policy_url = "https://www.miit.gov.cn/RRSdy/index.html"
    try:
        response = requests.get(policy_url, timeout=20, headers={"User-Agent": "Mozilla/5.0 A-Share-Alpha-Radar"})
        response.raise_for_status()
        parser = OfficialPolicyLinkParser()
        parser.feed(response.text)
        for href, title in parser.links:
            url = urljoin(policy_url, href)
            host = urlparse(url).netloc.lower()
            if not host.endswith("miit.gov.cn"):
                continue
            if not any(token in title for token in ("通知", "公告", "方案", "规划", "办法", "意见", "标准", "目录", "通告")):
                continue
            policy_rows.append({"title": title, "url": url, "source": "工业和信息化部官网文件发布索引",
                                "published_date": None, "document_type": "official_ministry_notice"})
        policy_status = {"source": "MIIT_official_release_index",
                         "status": "success" if policy_rows else "empty",
                         "records": len(policy_rows), "url": policy_url}
    except Exception as exc:
        policy_status = {"source": "MIIT_official_release_index", "status": "failed",
                         "records": 0, "detail": str(exc)[:400], "url": policy_url}
    source_status.append(policy_status)

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
                source_function("stock_notice_report"), date=day_key, symbol="全部")
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
            code = norm_code(row.get(col))
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
            "mapping": theme_member_meta.get(name, {}),
            "constituent_codes": sorted(codes),
            "financials": {"report_date": report_date, "records": financial,
                "coverage": len({re.sub(r"\D", "", str(r.get("股票代码") or r.get("证券代码") or "")) for r in financial})},
            "earnings_forecasts": {"records": forecasts, "coverage": len(forecasts)},
            "institutional_holdings": {"report_date": report_date, "records": institutions,
                "coverage": len(institutions), "source": "Sina stock_institute_hold"},
            "public_fund_holdings": {"report_date": report_date, "records": funds,
                "coverage": len(funds), "source": "CNINFO fund_report_stock_cninfo"},
            "contracts": {"records": contracts, "coverage": len(contracts), "source": "AKShare/EastMoney stock_zdhtmx_em"},
            "company_events": {"records": event_rows, "coverage": len(event_rows), "source": "AKShare stock_notice_report"},
            "government_policy": {"records": [r for r in policy_rows
                if any(k in r["title"] for k in POLICY_SYNONYMS.get(name, (name,)))],
                "coverage": sum(1 for r in policy_rows
                if any(k in r["title"] for k in POLICY_SYNONYMS.get(name, (name,)))),
                "status": "success" if any(any(k in r["title"] for k in POLICY_SYNONYMS.get(name, (name,))) for r in policy_rows) else "no_matching_official_document",
                "source": "工业和信息化部官网文件发布索引"},
        }

    evidence = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "market_trade_date": research.get("generated_at"),
        "financial_report_date": report_date,
        "themes": output,
        "sources": source_status,
        "quality_status": "validated" if sum(1 for x in source_status if x.get("source","").startswith("constituents:") and x.get("status") in ("success","cached")) == len(themes) and len(all_codes) > 0 else "degraded",
        "quality_summary": {
            "themes_total": len(themes),
            "themes_with_constituents": sum(1 for x in theme_members.values() if x),
            "unique_constituents": len(all_codes),
            "failed_sources": sum(1 for x in source_status if x.get("status") == "failed"),
            "empty_sources": sum(1 for x in source_status if x.get("status") == "empty"),
        },
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
    sections = research.setdefault("sections", {})
    old_sources = sections.get("data-sources", {}).get("items", [])
    merged_sources = {x.get("source"): x for x in old_sources if isinstance(x, dict) and x.get("source")}
    merged_sources.update({x.get("source"): x for x in source_status if x.get("source")})
    sections["data-sources"] = {"items": list(merged_sources.values())}
    research["evidence_quality"] = {
        "status": evidence["quality_status"],
        "themes_total": len(themes),
        "themes_with_constituents": evidence["quality_summary"]["themes_with_constituents"],
        "unique_constituents": len(all_codes),
        "failed_sources": evidence["quality_summary"]["failed_sources"],
        "empty_sources": evidence["quality_summary"]["empty_sources"],
        "note": "degraded 表示证据采集不完整；不改变完整主线评分门槛，也不将缺失值补零。"
    }
    research["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    research_path.write_text(json.dumps(research, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    summary = {"themes": len(output), "constituents": len(all_codes),
        "financial_report_date": report_date, "financial_records": len(financial_rows),
        "forecast_records": len(forecast_rows), "institution_records": len(institution_rows),
        "fund_records": len(fund_rows), "contract_records": len(contract_rows),
        "notice_records": len(notice_rows), "source_statuses": source_status}
    print(json.dumps({**summary, "quality_status": evidence["quality_status"],
        "quality_summary": evidence["quality_summary"]}, ensure_ascii=False))
    # Non-zero exit is intentional: the workflow may continue publishing market
    # data, but Actions must visibly record incomplete research-evidence collection.
    return 0 if evidence["quality_status"] == "validated" else 2


if __name__ == "__main__":
    raise SystemExit(main())
