#!/usr/bin/env python3
"""Collect auditable public evidence for industry/theme candidates.

The collector deliberately keeps source records, report dates and coverage. It
never manufactures a complete mainline score from missing evidence.
"""
from __future__ import annotations

import json
import logging
import os
import inspect
import re
import time
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import akshare as ak
import pandas as pd
from import_sector_archive import is_concept_code
from theme_mapping_rules import (merge_exact_components, star_next_new_eligible,
    private_bank_records_for_trade_date, consistent_provider_flow_persistence)
try:
    from scripts.research_provider_utils import call_with_retry, fetch_baidu_concept_tags, fetch_eastmoney_concept_members
except ModuleNotFoundError:
    from research_provider_utils import call_with_retry, fetch_baidu_concept_tags, fetch_eastmoney_concept_members

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SITE = ROOT / "site" / "data"
LOG = logging.getLogger("theme-evidence")
THEME_MAPPING_POLICY_VERSION = 3
THEME_ALIASES = {
    # Only near-equivalent taxonomy labels are allowed. Broad parent sectors
    # (e.g. semiconductor, small metals, medical services, trade) are excluded
    # because they do not prove membership in the narrower research theme.
    "集成电路制造": ("集成电路制造",),
    "橡胶助剂": ("橡胶助剂",),
    "厨房小家电": ("厨房小家电", "小家电"),
    "半导体材料": ("半导体材料",),
    "钼": ("钼",),
    "医疗器械": ("医疗器械", "医疗器械概念"),
    "辅助生殖": ("辅助生殖",),
    "商业服务": ("商业服务",),
    "烟草与大麻": ("烟草与大麻",),
    "个护用品Ⅲ": ("个护用品Ⅲ", "个护用品"),
    "白酒概念": ("白酒", "白酒概念"),
    "养老概念": ("养老概念", "养老产业", "养老服务"),
    "港口航运": ("港口航运", "航运港口"),
    "第三代半导体": ("第三代半导体",),
    "机器人概念": ("机器人概念", "机器人"),
    "乳业": ("乳业", "乳品"),
    "可选消费经销Ⅲ": ("可选消费经销Ⅲ",),
    "参股民营银行": ("参股民营银行", "民营银行", "民营银行概念"),
    "粤港澳大湾区": ("粤港澳大湾区", "粤港澳大湾区概念", "大湾区", "粤港澳"),
    "科创次新股": ("科创次新股",),
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


def _score_key(value):
    return re.sub(r"[\s·（）()ⅢⅣ]", "", str(value or "")).lower()


def _breadth_score(rows, code_set, value_candidates, min_coverage=0.25, min_names=3):
    """Score cross-sectional operating breadth only when the sample is adequate."""
    by_code = {}
    for row in rows or []:
        code = norm_code(row.get("股票代码") or row.get("证券代码"))
        if not code or code not in code_set:
            continue
        col = find_col(row.keys(), value_candidates)
        value = number(row.get(col)) if col else None
        if value is not None:
            by_code[code] = value
    required = max(min_names, int(len(code_set) * min_coverage + 0.999)) if code_set else min_names
    if len(by_code) < required:
        return None, {"sample": len(by_code), "required": required, "coverage": len(by_code) / len(code_set) if code_set else 0}
    positive = sum(1 for value in by_code.values() if value > 0)
    breadth = positive / len(by_code) * 100.0
    return round(breadth, 1), {"sample": len(by_code), "required": required,
        "coverage": round(len(by_code) / len(code_set), 3), "positive": positive,
        "positive_breadth": round(breadth, 1)}


def _policy_score(policy):
    rows = (policy or {}).get("records") or []
    # Only successfully fetched official full text qualifies; index titles alone do not.
    verified = [row for row in rows if row.get("body_status") == "success" and row.get("body_text")]
    if not verified:
        return None, {"verified_documents": 0, "reason": "no_matching_official_full_text"}
    return min(95.0, 65.0 + 10.0 * min(len(verified), 3)), {
        "verified_documents": len(verified), "source": "MIIT official full text"}


def model_score(change_pct, flow_5d=None, flow_10d=None, evidence=None):
    """Score only dimensions supported by sufficiently broad, attributable evidence."""
    evidence = evidence or {}
    change = number(change_pct)
    f5, f10 = flow_5d, flow_10d
    structure = None if change is None else max(0.0, min(100.0, 50.0 + change * 8.0))
    persistence = consistent_provider_flow_persistence(f5, f10)
    codes = set(evidence.get("constituent_codes") or [])
    revenue_score, revenue_meta = _breadth_score(
        (evidence.get("financials") or {}).get("records") or [], codes,
        ("营业收入-同比增长", "营业收入同比增长", "营业收入同比", "营业收入同比增长率"))
    earnings_score, earnings_meta = _breadth_score(
        (evidence.get("financials") or {}).get("records") or [], codes,
        ("净利润-同比增长", "净利润同比增长", "净利润同比", "净利润同比增长率"))
    policy_score, policy_meta = _policy_score(evidence.get("government_policy"))
    health_dims = {"industry_thesis": revenue_score, "policy_event": policy_score,
        "capital_persistence": persistence,
        "trend_structure": round(structure, 1) if structure is not None else None,
        "earnings_delivery": earnings_score, "valuation_crowding": None}
    health_weights = {"industry_thesis":20,"policy_event":10,"capital_persistence":20,
        "trend_structure":20,"earnings_delivery":15,"valuation_crowding":15}
    available = [(health_dims[k], w) for k, w in health_weights.items()
                 if health_dims[k] is not None]
    health_coverage = sum(w for _, w in available)
    health_score = (round(sum(v*w for v, w in available) / health_coverage, 1)
                    if health_coverage else None)
    return {"score": round(structure, 1) if structure is not None else None,
        "coverage": 15 if structure is not None else 0,
        "dimensions": {"policy_macro": None, "industry_trend": None,
            "capital_institutions": None,
            "market_structure": round(structure, 1) if structure is not None else None,
            "earnings": None, "catalyst": None},
        "stage": "证据不足，暂不判定",
        "health": {"score": health_score, "coverage": health_coverage,
            "dimensions": health_dims,
            "evidence_quality": {"industry_thesis": revenue_meta,
                "earnings_delivery": earnings_meta, "policy_event": policy_meta},
            "report_date": (evidence.get("financials") or {}).get("report_date"),
            "note": "产业逻辑与业绩兑现分别采用成分股营收、净利润同比正增长广度；至少3家且覆盖成分股25%才计分。政策维度只计工信部官网匹配原文。估值拥挤度无可靠估值分位源，继续缺失；缺失维度不补值。"}}


def flow_ratio_for_theme(flow_rows, theme, period):
    """Return a provider-tagged signed flow amount; never compare mixed providers."""
    name = _score_key(theme.get("industry_name") or theme.get("industry"))
    code = _score_key(theme.get("sector_code"))
    target_day = str(theme.get("trade_date") or "")
    if not target_day:
        return None
    matches = []
    for row in flow_rows:
        if str(row.get("trade_date") or "") != target_day:
            continue
        if str(row.get("indicator") or "").strip() != period:
            continue
        row_keys = {_score_key(row.get("industry_name")), _score_key(row.get("industry")),
                    _score_key(row.get("sector_code"))}
        row_keys.discard("")
        if not ((name and name in row_keys) or (code and code in row_keys)):
            continue
        provider = str(row.get("provider") or "")
        if row.get("main_net_inflow_cny") not in (None, ""):
            value = number(row.get("main_net_inflow_cny"))
            provider = provider or "EastMoney"
        else:
            value = number(row.get("net_inflow_cny"))
            provider = provider or ("THS" if "THS" in str(row.get("source") or "") else "")
        if value is not None and provider:
            matches.append((provider, value))
    if not matches:
        return None
    providers = {provider for provider, _ in matches}
    if len(providers) != 1:
        return None
    return matches[0]


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
    market_universe = []
    market_trade_day = None
    try:
        market_frame = pd.read_csv(DATA / "latest_market.csv", dtype=str, encoding="utf-8-sig")
        market_code_col = find_col(market_frame.columns, ("symbol", "股票代码", "代码"))
        market_name_col = find_col(market_frame.columns, ("name", "股票名称", "名称"))
        market_date_col = find_col(market_frame.columns, ("trade_date", "交易日期", "日期"))
        if market_code_col:
            market_universe = [
                (str(row.get(market_code_col) or "").strip().zfill(6),
                 str(row.get(market_name_col) or "").strip() if market_name_col else None)
                for row in market_frame.to_dict("records")
                if str(row.get(market_code_col) or "").strip()
            ]
        if market_date_col and not market_frame.empty:
            market_trade_day = str(market_frame.iloc[0][market_date_col])
    except Exception as exc:
        LOG.warning("Baidu fallback universe unavailable: %s", str(exc)[:200])
    baidu_concept_cache = None
    baidu_concept_meta = None
    baidu_status_recorded = False
    # Sina publishes industry and popular-concept classifications independently
    # of EastMoney Push2. Resolve the requested theme against this taxonomy first,
    # then fetch only the matched category's members. EastMoney remains a fallback.
    cache_path = DATA / "theme_constituents_cache.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    except Exception:
        cache = {}
    cache_dirty = False
    theme_member_meta = {}
    push2_circuit_open = False
    sina_nodes = {}
    sina_headers = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
                    "Referer":"http://vip.stock.finance.sina.com.cn/mkt/"}
    try:
        # Call Sina's taxonomy endpoint directly with a bounded timeout; the
        # legacy AKShare helper does not expose a request timeout.
        taxonomy_url = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodes"
        response = requests.get(taxonomy_url, headers=sina_headers, timeout=15)
        response.raise_for_status()
        payload = response.json()
        groups = payload[1][0][1]
        for group_item in groups:
            try:
                if not isinstance(group_item, (list, tuple)) or len(group_item) < 2:
                    continue
                raw_group = str(group_item[0])
                group = re.sub(r"<[^>]+>", "", raw_group).strip()
                rows = group_item[1]
                if isinstance(rows, dict):
                    if rows and all(not isinstance(v, (list, tuple, dict)) for v in rows.values()):
                        rows = [rows]
                    elif rows and all(isinstance(v, dict) for v in rows.values()):
                        rows = list(rows.values())
                if not isinstance(rows, list) or not rows:
                    continue
                frame = pd.DataFrame(rows)
                if frame.empty or frame.shape[1] < 3:
                    continue
                name_col = frame.columns[0]
                code_col = frame.columns[2]
                parsed = [
                    {"name":clean(row.get(name_col)),"node":clean(row.get(code_col))}
                    for row in frame.to_dict("records")
                    if clean(row.get(name_col)) and clean(row.get(code_col))]
                if parsed:
                    sina_nodes[group] = parsed
            except Exception as group_exc:
                LOG.debug("Skip malformed Sina taxonomy group %s: %s", group_item[0] if group_item else "?", group_exc)
    except Exception as exc:
        LOG.warning("Sina classification taxonomy unavailable: %s", exc)

    def fetch_ths_concept_members(theme, name):
        """Fallback for THS concept boards when Sina has no exact taxonomy node."""
        ths_cookie = ""
        try:
            import py_mini_racer
            from akshare.datasets import get_ths_js
            js_runtime = py_mini_racer.MiniRacer()
            with open(get_ths_js("ths.js"), "r", encoding="utf-8") as js_file:
                js_runtime.eval(js_file.read())
            ths_cookie = str(js_runtime.call("v"))
        except Exception as cookie_exc:
            LOG.warning("THS cookie generation unavailable: %s", str(cookie_exc)[:160])
        # Try exact THS concept-name matching for every unresolved theme.
        # Some EastMoney BK concept codes are absent from the local concept-code
        # registry; exact name matching avoids incorrectly classifying them as industries.
        name_fn = getattr(ak, "stock_board_concept_name_ths", None)
        if name_fn is None:
            return [], None
        try:
            names_frame = name_fn()
            name_col = find_col(names_frame.columns, ("name", "概念名称", "名称"))
            code_col = find_col(names_frame.columns, ("code", "概念代码", "代码"))
            if not name_col or not code_col:
                return [], None
            candidates = tuple(dict.fromkeys((name,) + THEME_ALIASES.get(name, ())))
            normal = lambda x: re.sub(r"[\s·（）()ⅢⅣ]", "", str(x or "")).lower()
            selected = None
            for candidate in candidates:
                matched = names_frame[names_frame[name_col].astype(str).map(normal) == normal(candidate)]
                if not matched.empty:
                    selected = (str(matched.iloc[0][name_col]), str(matched.iloc[0][code_col]))
                    break
            if not selected:
                return [], None
            matched_name, board_code = selected
            members = {}
            headers = {"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
                       "Referer":"https://q.10jqka.com.cn/gn/",
                       "Cookie":f"v={ths_cookie}" if ths_cookie else ""}
            for page in range(1, 21):
                url = f"https://q.10jqka.com.cn/gn/detail/field/199112/order/desc/page/{page}/ajax/1/code/{board_code}"
                response = requests.get(url, headers=headers, timeout=15)
                response.raise_for_status()
                try:
                    tables = pd.read_html(response.text)
                except ValueError:
                    break
                if not tables:
                    break
                frame = tables[0]
                code_col = find_col(frame.columns, ("代码", "股票代码", "证券代码"))
                name_col = find_col(frame.columns, ("名称", "股票简称", "证券简称"))
                if not code_col:
                    raise RuntimeError("THS concept table missing stock code: " + str(list(frame.columns)))
                page_rows = 0
                for row in frame.to_dict("records"):
                    code_value = norm_code(row.get(code_col))
                    if code_value:
                        members[code_value] = {"code":code_value,
                            "name":clean(row.get(name_col)) if name_col else None}
                        page_rows += 1
                if page_rows == 0 or len(frame) < 10:
                    break
                time.sleep(0.15)
            if not members:
                return [], None
            return list(members.values()), {"provider":"THS","taxonomy":"概念板块",
                "taxonomy_node_id":board_code,"taxonomy_node_name":matched_name,
                "source_url":f"https://q.10jqka.com.cn/gn/detail/field/199112/order/desc/code/{board_code}",
                "matched_symbol":matched_name,
                "mapping_source":"THS stock_board_concept_name_ths + gn constituent endpoint",
                "status":"success"}
        except Exception as exc:
            LOG.warning("THS concept constituents %s failed: %s", name, exc)
            return [], None

    def fetch_sina_members(theme, name):
        concept = is_concept_code(clean(theme.get("sector_code")))
        group_order = ("热门概念","申万行业","申万一级","申万二级","申万三级","地域板块") if concept else (
            "申万行业","申万一级","申万二级","申万三级","热门概念","地域板块")
        candidates = tuple(dict.fromkeys((name,) + THEME_ALIASES.get(name, ())))
        normal = lambda x: re.sub(r"[\s·（）()ⅢⅣ]", "", str(x or "")).lower()
        match = None
        for candidate in candidates:
            for group in group_order:
                for item in sina_nodes.get(group, []):
                    if normal(item["name"]) == normal(candidate):
                        match = (group, item)
                        break
                if match:
                    break
            if match:
                break
        if not match:
            return [], None
        group, item = match
        count_url = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount"
        data_url = "http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
        count_response = requests.get(count_url, params={"node":item["node"]}, headers=sina_headers, timeout=15)
        count_response.raise_for_status()
        total = int(count_response.json())
        if total <= 0:
            return [], {"provider":"Sina","taxonomy":group,"matched_symbol":item["name"],
                        "mapping_source":"Sina stock_classify_board","status":"empty"}
        members = {}
        pages = min(20, (total + 79) // 80)
        for page in range(1, pages + 1):
            params = {"page":page,"num":"80","sort":"symbol","asc":"1","node":item["node"],
                      "symbol":"","_s_r_a":"init"}
            response = requests.get(data_url, params=params, headers=sina_headers, timeout=15)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list) or not payload:
                break
            frame = pd.DataFrame(payload)
            code_col = find_col(frame.columns, ("symbol","code","代码","证券代码"))
            name_col = find_col(frame.columns, ("name","名称","证券简称"))
            if not code_col:
                raise RuntimeError("Sina constituent payload missing code field: " + str(list(frame.columns)))
            for row in frame.to_dict("records"):
                code_value = norm_code(row.get(code_col))
                if code_value:
                    members[code_value] = {"code":code_value,
                        "name":clean(row.get(name_col)) if name_col else None}
            time.sleep(0.15)
        if not members:
            return [], {"provider":"Sina","taxonomy":group,"matched_symbol":item["name"],
                        "mapping_source":"Sina stock_classify_board","status":"empty"}
        return list(members.values()), {"provider":"Sina","taxonomy":group,
            "taxonomy_node_id":item["node"],"taxonomy_node_name":item["name"],
            "source_url":data_url,
            "matched_symbol":item["name"],"mapping_source":"Sina stock_classify_board",
            "status":"success"}

    def fetch_members(theme, name):
        nonlocal cache_dirty, push2_circuit_open
        code = clean(theme.get("sector_code"))

        def fetch_baidu_exact_concept_members(component_name):
            nonlocal baidu_concept_cache, baidu_concept_meta, baidu_status_recorded
            if not market_universe:
                return [], {"status":"unavailable","provider":"Baidu Stock Market",
                            "taxonomy":"Baidu per-stock concept tags",
                            "detail":"latest_market.csv has no usable stock universe"}
            if baidu_concept_cache is None:
                try:
                    baidu_concept_cache, baidu_concept_meta = fetch_baidu_concept_tags(
                        [code for code, _ in market_universe],
                        batch_size=20, max_workers=4, timeout_seconds=8.0
                    )
                    baidu_concept_meta["checked_at"] = datetime.now(timezone.utc).isoformat()
                    baidu_concept_meta["trade_date"] = market_trade_day
                except Exception as exc:
                    baidu_concept_cache = {}
                    baidu_concept_meta = {
                        "status":"unavailable","provider":"Baidu Stock Market",
                        "taxonomy":"Baidu per-stock concept tags",
                        "source_url":"https://finance.pae.baidu.com/api/getrelatedblock",
                        "checked_at":datetime.now(timezone.utc).isoformat(),
                        "trade_date":market_trade_day,
                        "detail":f"{type(exc).__name__}: {str(exc)[:240]}",
                    }
                if not baidu_status_recorded:
                    source_status.append({
                        "source":"baidu_concept_tags",
                        "status":baidu_concept_meta.get("status","unavailable"),
                        "provider":"Baidu Stock Market",
                        "coverage_pct":baidu_concept_meta.get("coverage_pct"),
                        "records":baidu_concept_meta.get("records",0),
                        "detail":baidu_concept_meta.get("detail") or
                                 f"returned {baidu_concept_meta.get('returned_codes',0)}/{baidu_concept_meta.get('input_codes',0)} stock records",
                    })
                    baidu_status_recorded = True
            normal = lambda value: re.sub(r"[\s·（）()ⅢⅣ]", "", str(value or "")).lower()
            names_by_code = {code:name for code,name in market_universe}
            members = []
            concept_codes = set()
            for code, concepts in (baidu_concept_cache or {}).items():
                for concept in concepts:
                    if normal(concept.get("name")) == normal(component_name):
                        concept_code = concept.get("concept_code")
                        if concept_code:
                            concept_codes.add(str(concept_code))
                        members.append({
                            "code":code, "name":names_by_code.get(code),
                            "concept_code":concept_code,
                            "membership_reason":f"exact Baidu per-stock concept tag: {component_name}",
                        })
                        break
            if not members:
                return [], {
                    "status":"unresolved" if baidu_concept_meta.get("status") == "success" else baidu_concept_meta.get("status","unavailable"),
                    "provider":"Baidu Stock Market",
                    "taxonomy":"Baidu per-stock concept tags",
                    "matched_symbol":component_name,
                    "mapping_source":"Baidu exact per-stock concept-name match",
                    "source_url":"https://finance.pae.baidu.com/api/getrelatedblock",
                    "checked_at":baidu_concept_meta.get("checked_at"),
                    "trade_date":market_trade_day,
                    "detail":f"no exact Baidu concept tag found for {component_name}",
                }
            meta = {
                **(baidu_concept_meta or {}),
                "status":baidu_concept_meta.get("status","partial"),
                "provider":"Baidu Stock Market",
                "taxonomy":"Baidu per-stock concept tags",
                "taxonomy_node_id":next(iter(concept_codes)) if len(concept_codes)==1 else None,
                "taxonomy_node_name":component_name,
                "matched_symbol":component_name,
                "mapping_source":"Baidu exact per-stock concept-name match",
                "source_url":"https://finance.pae.baidu.com/api/getrelatedblock",
                "records":len(members),
                "candidate_count":len(members),
                "checked_at":baidu_concept_meta.get("checked_at"),
                "trade_date":market_trade_day,
            }
            return members, meta

        def persist_baidu_mapping(members, meta):
            nonlocal cache_dirty
            if members and meta and meta.get("status") == "success":
                fetched = meta.get("checked_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")
                cache[name] = {
                    "members":members,"fetched_at":fetched,"source":meta.get("provider"),
                    "mapping_policy_version":THEME_MAPPING_POLICY_VERSION,
                    "matched_symbol":meta.get("matched_symbol"),
                    "taxonomy":meta.get("taxonomy"),
                    "taxonomy_node_id":meta.get("taxonomy_node_id"),
                    "taxonomy_node_name":meta.get("taxonomy_node_name"),
                    "source_url":meta.get("source_url"),
                    "mapping_source":meta.get("mapping_source"),"sector_code":code,
                }
                cache_dirty = True
            return members, {"source":"constituents:"+name, **(meta or {})}

        def fetch_eastmoney_exact_concept_members(component_name):
            try:
                return fetch_eastmoney_concept_members(component_name)
            except LookupError as exc:
                return [], {"status":"unresolved","provider":"EastMoney",
                            "taxonomy":"EastMoney exact concept board",
                            "detail":str(exc)[:240]}
            except Exception as exc:
                return [], {"status":"failed","provider":"EastMoney",
                            "taxonomy":"EastMoney exact concept board",
                            "detail":f"{type(exc).__name__}: {str(exc)[:240]}"}

        # Composite/special themes are deliberately excluded from the generic
        # name-alias cache. Each requires its own explicit evidence contract.
        if name == "烟草与大麻":
            components = {}
            component_members = {}
            for component in ("烟草", "工业大麻"):
                try:
                    members, meta = fetch_ths_concept_members(theme, component)
                except Exception as exc:
                    members, meta = [], {"status":"failed","provider":"THS",
                                         "detail":f"{type(exc).__name__}: {str(exc)[:180]}"}
                if not members or not meta or meta.get("status") != "success":
                    members, meta = fetch_eastmoney_exact_concept_members(component)
                if not members or not meta or meta.get("status") != "success":
                    members, meta = fetch_baidu_exact_concept_members(component)
                tagged_members = [{
                    **member,
                    "theme_component": component,
                    "membership_reason": f"returned by the exact {(meta or {}).get('provider') or 'unverified'} constituent source for component {component}",
                    "membership_evidence": {
                        "provider": (meta or {}).get("provider"),
                        "concept_code": member.get("concept_code"),
                        "taxonomy": (meta or {}).get("taxonomy"),
                        "taxonomy_node_id": (meta or {}).get("taxonomy_node_id"),
                        "taxonomy_node_name": (meta or {}).get("taxonomy_node_name"),
                        "matched_symbol": (meta or {}).get("matched_symbol"),
                        "mapping_source": (meta or {}).get("mapping_source"),
                        "source_url": (meta or {}).get("source_url"),
                    },
                } for member in members]
                component_members[component] = tagged_members
                components[component] = {"status": meta.get("status") if meta else "unresolved",
                                          "provider": meta.get("provider") if meta else None,
                                          "matched_symbol": meta.get("matched_symbol") if meta else None,
                                          "taxonomy_node_id": meta.get("taxonomy_node_id") if meta else None,
                                          "taxonomy_node_name": meta.get("taxonomy_node_name") if meta else None,
                                          "source_url": meta.get("source_url") if meta else None,
                                          "mapping_source": meta.get("mapping_source") if meta else None,
                                          "records": len(tagged_members), "members": tagged_members}
                if not members:
                    return [], {"source":"constituents:"+name,"status":"pending_components",
                        "provider":meta.get("provider") if meta else None,
                        "taxonomy":"separate exact concept components",
                        "mapping_source":"exact THS/EastMoney concept taxonomy; both components required",
                        "matched_symbol":"烟草 + 工业大麻",
                        "components":{key:{k:v for k,v in value.items() if k != "members"}
                                     for key,value in components.items()},
                        "detail":"烟草与工业大麻必须分别精确命中；任一组件缺失则不合并"}
            members = merge_exact_components(components)
            if not members:
                return [], {"source":"constituents:"+name,"status":"pending_components",
                    "mapping_source":"THS exact concept components; merge validation failed",
                    "matched_symbol":"烟草 + 工业大麻","components":{
                        key:{k:v for k,v in value.items() if k != "members"}
                        for key,value in components.items()}}
            fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
            component_summary = {key:{k:v for k,v in value.items() if k != "members"}
                                 for key,value in components.items()}
            component_providers = sorted({str(value.get("provider") or "unknown")
                                           for value in components.values()})
            cache[name] = {"members":members,"fetched_at":fetched,"source":"+".join(component_providers),
                "mapping_policy_version":THEME_MAPPING_POLICY_VERSION,
                "matched_symbol":"烟草 + 工业大麻","taxonomy":"separate exact concept components",
                "mapping_source":"exact THS/EastMoney concept taxonomy; two components merged",
                "components":component_summary,"sector_code":code}
            cache_dirty = True
            return members, {"source":"constituents:"+name,"status":"success",
                "records":len(members),"provider":" + ".join(component_providers),
                "taxonomy":"separate exact concept components",
                "matched_symbol":"烟草 + 工业大麻",
                "mapping_source":"exact THS/EastMoney concept taxonomy; two components merged",
                "components":component_summary,"mapping_date":fetched,"checked_at":fetched}

        if name == "参股民营银行":
            # Taxonomy labels are discovery-only. Every included company must
            # have a primary disclosure proving equity ownership in a named bank.
            evidence_path = DATA / "private_bank_equity_evidence.json"
            try:
                evidence_doc = json.loads(evidence_path.read_text(encoding="utf-8")) if evidence_path.exists() else {}
            except Exception as evidence_exc:
                evidence_doc = {}
                LOG.warning("Private-bank evidence file unreadable: %s", str(evidence_exc)[:160])
            evidence_records = private_bank_records_for_trade_date(
                evidence_doc.get("records") or [], trade_day.isoformat())
            verified_members = [{
                "code":str(record["listed_company_code"]).strip(),
                "name":clean(record.get("listed_company_name")),
                "investee_bank_name":record.get("investee_bank_name"),
                "holding_pct":float(record["holding_pct"]),
                "disclosure_date":record["disclosure_date"],
                "evidence_age_days":record.get("evidence_age_days"),
                "review_age_days":record.get("review_age_days"),
                "evidence_status":record.get("evidence_status"),
                "source_url":record.get("source_url"),
                "source_excerpt":record.get("source_excerpt"),
                "membership_reason":"listed-company primary disclosure confirms equity ownership in the named private bank",
            } for record in evidence_records]

            candidate_key = "参股民营银行_candidates"
            cached_candidates = cache.get(candidate_key) or {}
            cached_at = cached_candidates.get("fetched_at")
            cache_valid = False
            if (cached_candidates.get("candidate_only") is True
                    and cached_candidates.get("mapping_policy_version") == THEME_MAPPING_POLICY_VERSION
                    and cached_candidates.get("source_url") and cached_at):
                try:
                    cached_dt = datetime.fromisoformat(str(cached_at).replace("Z", "+00:00"))
                    if cached_dt.tzinfo is None:
                        cached_dt = cached_dt.replace(tzinfo=timezone.utc)
                    cache_valid = (datetime.now(timezone.utc) - cached_dt).total_seconds() <= CACHE_TTL_DAYS * 86400
                except Exception:
                    cache_valid = False
            if cache_valid:
                candidates = cached_candidates.get("members") or []
                candidate_meta = {
                    "status":"success","provider":cached_candidates.get("source"),
                    "taxonomy":cached_candidates.get("taxonomy"),
                    "matched_symbol":cached_candidates.get("matched_symbol"),
                    "mapping_source":cached_candidates.get("mapping_source"),
                    "source_url":cached_candidates.get("source_url"),
                    "checked_at":cached_at,
                }
            else:
                candidates, candidate_meta = fetch_ths_concept_members({"sector_code": None}, "参股民营银行")
                if not candidates:
                    candidates, candidate_meta = fetch_eastmoney_exact_concept_members("参股民营银行")
                if not candidates:
                    candidates, candidate_meta = fetch_baidu_exact_concept_members("参股民营银行")
                if candidates and candidate_meta and candidate_meta.get("status") == "success":
                    fetched_candidates = candidate_meta.get("checked_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")
                    cache[candidate_key] = {
                        "members":candidates,"fetched_at":fetched_candidates,
                        "source":candidate_meta.get("provider"),
                        "mapping_policy_version":THEME_MAPPING_POLICY_VERSION,
                        "matched_symbol":"参股民营银行",
                        "taxonomy":candidate_meta.get("taxonomy"),
                        "source_url":candidate_meta.get("source_url"),
                        "mapping_source":candidate_meta.get("mapping_source"),
                        "coverage_pct":candidate_meta.get("coverage_pct"),
                        "candidate_only":True,
                    }
                    cache_dirty = True

            candidate_codes = sorted({str(item.get("code") or "").zfill(6) for item in candidates})
            verified_codes = sorted({str(item.get("code") or "").zfill(6) for item in verified_members})
            unverified_codes = sorted(set(candidate_codes) - set(verified_codes))
            candidate_source_valid = bool(candidate_meta and candidate_meta.get("status") == "success")
            fully_verified = bool(candidate_source_valid and candidate_codes and not unverified_codes)
            status = "success" if fully_verified else (
                "pending_relation_evidence" if candidate_codes or verified_members else
                candidate_meta.get("status", "unavailable") if candidate_meta else "unavailable"
            )
            checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return verified_members, {
                "source":"constituents:"+name,
                "status":status,
                "records":len(verified_members),
                "provider":(
                    (candidate_meta.get("provider") + " + reviewed primary disclosures")
                    if candidate_meta and candidate_meta.get("provider")
                    else "reviewed primary disclosures"
                ),
                "taxonomy":(
                    (candidate_meta.get("taxonomy") or "candidate taxonomy")
                    + " + listed-company equity disclosures"
                ),
                "matched_symbol":"参股民营银行（逐条披露核验）",
                "mapping_source":"primary disclosure required for every candidate; taxonomy is discovery-only",
                "source_url":candidate_meta.get("source_url") if candidate_meta else (
                    verified_members[0].get("source_url") if verified_members else None
                ),
                "candidate_source_url":candidate_meta.get("source_url") if candidate_meta else None,
                "candidate_count":len(candidate_codes),
                "verified_count":len(verified_codes),
                "unverified_candidate_count":len(unverified_codes),
                "unverified_candidate_codes":unverified_codes[:100],
                "candidate_coverage_pct":candidate_meta.get("coverage_pct") if candidate_meta else None,
                "verified_relationships":verified_members,
                "mapping_date":checked_at,
                "checked_at":checked_at,
                "detail":(
                    f"已核验 {len(verified_codes)}/{len(candidate_codes)} 个候选；"
                    "未取得逐家公司原始披露的候选不会加入成分"
                ),
            }

        if name == "科创次新股":
            listing_fn = getattr(ak, "stock_info_sh_name_code", None)
            if listing_fn is None:
                return [], {"source":"constituents:"+name,"status":"unavailable",
                            "provider":"Shanghai Stock Exchange",
                            "detail":"AkShare stock_info_sh_name_code endpoint unavailable"}
            try:
                frame = call_with_retry(
                    lambda: listing_fn(symbol="科创板"),
                    operation="SSE STAR Market official listing roster",
                    attempts=2, delay_seconds=0.5,
                )
                if frame is None or frame.empty:
                    raise ValueError("SSE STAR Market listing roster is empty")
                code_col = find_col(frame.columns, ("证券代码", "股票代码", "代码"))
                name_col = find_col(frame.columns, ("证券简称", "股票简称", "名称"))
                date_col = find_col(frame.columns, ("上市日期", "上市时间", "LIST_DATE"))
                if not code_col or not date_col:
                    raise ValueError(f"SSE listing roster schema mismatch: {list(frame.columns)}")
                digits = "".join(ch for ch in str(market_trade_day or "") if ch.isdigit())
                if len(digits) < 8:
                    raise ValueError(f"invalid report trade date for STAR listing rule: {market_trade_day!r}")
                as_of = date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
                star_codes, valid_dates, eligible = [], 0, []
                for row in frame.to_dict("records"):
                    code = str(row.get(code_col) or "").strip().zfill(6)
                    if not code.startswith(("688", "689")) or len(code) != 6 or not code.isdigit():
                        continue
                    star_codes.append(code)
                    listed = pd.to_datetime(row.get(date_col), errors="coerce")
                    if pd.isna(listed):
                        continue
                    valid_dates += 1
                    listed_date = listed.date()
                    if star_next_new_eligible(code, listed_date, as_of):
                        eligible.append({
                            "code":code,
                            "name":str(row.get(name_col) or "").strip() if name_col else None,
                            "listing_date":listed_date.isoformat(),
                            "membership_reason":"SSE official STAR listing date within one calendar year",
                            "membership_evidence":{
                                "provider":"Shanghai Stock Exchange",
                                "taxonomy":"official STAR Market listing roster",
                                "source_url":"https://www.sse.com.cn/assortment/stock/list/share/",
                                "listing_date":listed_date.isoformat(),
                                "as_of_trade_date":as_of.isoformat(),
                                "policy":"less than one calendar year",
                            },
                        })
                coverage_pct = round(valid_dates / max(1, len(star_codes)) * 100, 1)
                checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                meta = {
                    "status":"success" if star_codes and coverage_pct >= 95.0 else "partial",
                    "provider":"Shanghai Stock Exchange",
                    "taxonomy":"official STAR Market listing roster",
                    "taxonomy_node_id":"SSE:STAR",
                    "taxonomy_node_name":"科创板",
                    "matched_symbol":"SSE 科创板上市日期",
                    "mapping_source":"SSE official STAR listing dates + one calendar year eligibility rule",
                    "source_url":"https://www.sse.com.cn/assortment/stock/list/share/",
                    "checked_at":checked_at,
                    "trade_date":as_of.isoformat(),
                    "coverage_pct":coverage_pct,
                    "records":len(eligible),
                    "candidate_count":len(eligible),
                    "roster_count":len(star_codes),
                    "eligibility_rule":"688/689 listing date within one calendar year as of report trade date",
                }
                source_status.append({
                    "source":"sse_star_listing_roster",
                    "status":meta["status"],
                    "provider":meta["provider"],
                    "coverage_pct":coverage_pct,
                    "records":len(star_codes),
                    "detail":f"{len(eligible)} eligible new listings as of {as_of.isoformat()}",
                })
                return eligible, {"source":"constituents:"+name, **meta}
            except Exception as exc:
                return [], {"source":"constituents:"+name,
                            "status":"unavailable" if any(token in str(exc).lower() for token in ("timeout","connection","502","403","remote disconnected")) else "failed",
                            "provider":"Shanghai Stock Exchange",
                            "taxonomy":"official STAR Market listing roster",
                            "source_url":"https://www.sse.com.cn/assortment/stock/list/share/",
                            "detail":f"{type(exc).__name__}: {str(exc)[:240]}"}
        try:
            members, meta = fetch_sina_members(theme, name)
            if not members:
                members, meta = fetch_ths_concept_members(theme, name)
            if members:
                fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
                source_name = meta.get("provider", "Sina")
                cache[name] = {"members":members,"fetched_at":fetched,"source":source_name,
                    "mapping_policy_version":THEME_MAPPING_POLICY_VERSION,
                    "matched_symbol":meta.get("matched_symbol"),"taxonomy":meta.get("taxonomy"),
                    "taxonomy_node_id":meta.get("taxonomy_node_id"),
                    "taxonomy_node_name":meta.get("taxonomy_node_name"),
                    "source_url":meta.get("source_url"),
                    "mapping_source":meta.get("mapping_source"),"sector_code":code}
                cache_dirty = True
                return members, {"source":"constituents:"+name,"status":"success","records":len(members),
                    **meta,"mapping_date":fetched,"checked_at":fetched}
        except Exception as exc:
            LOG.warning("Sina/THS constituents %s failed: %s", name, exc)
        # EastMoney Push2 fallback. Open a circuit on the first upstream rate limit.
        if push2_circuit_open:
            members, meta = fetch_baidu_exact_concept_members(name)
            return persist_baidu_mapping(members, meta)
        concept = is_concept_code(code)
        attempts = [("stock_board_concept_cons_em" if concept else "stock_board_industry_cons_em",
                     "EastMoney concept" if concept else "EastMoney industry")]
        if not code:
            attempts = [("stock_board_industry_cons_em","EastMoney industry"),
                        ("stock_board_concept_cons_em","EastMoney concept")]
        errors = []
        candidates = tuple(dict.fromkeys(x for x in (name, *THEME_ALIASES.get(name, ()), code) if x))
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
                            "mapping_policy_version":THEME_MAPPING_POLICY_VERSION,
                            "matched_symbol":candidate,"taxonomy":label,
                            "taxonomy_node_id":None,"taxonomy_node_name":candidate,
                            "source_url":None,
                            "mapping_source":fn_name,"sector_code":code}
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
        members, meta = fetch_baidu_exact_concept_members(name)
        if not members and meta:
            meta["detail"] = (meta.get("detail") or "") + ("; " + "; ".join(errors)[:240] if errors else "")
        return persist_baidu_mapping(members, meta)

    for theme in themes:
        name = clean(theme.get("industry_name") or theme.get("industry"))
        if not name:
            continue
        members, status = fetch_members(theme, name)
        if members and name not in {"参股民营银行", "烟草与大麻", "科创次新股"}:
            evidence_ref = {
                "provider": status.get("provider"),
                "taxonomy": status.get("taxonomy"),
                "taxonomy_node_id": status.get("taxonomy_node_id"),
                "taxonomy_node_name": status.get("taxonomy_node_name"),
                "matched_symbol": status.get("matched_symbol"),
                "mapping_source": status.get("mapping_source"),
                "source_url": status.get("source_url"),
                "mapping_date": status.get("mapping_date"),
                "reason": "security returned by the constituent endpoint for the recorded taxonomy node",
            }
            for member in members:
                member["membership_evidence"] = dict(evidence_ref)
        source_status.append(status)
        theme_members[name] = members
        theme_member_meta[name] = status
        all_codes.update(x["code"] for x in members)
    if cache_dirty:
        cache_path.write_text(json.dumps(cache,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")

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
                                "published_date": None, "document_type": "official_ministry_notice",
                                "body_text": None, "body_status": "not_fetched"})
        # Fetch official article bodies for auditable policy evidence. An index
        # title alone is not counted as a policy match or score input.
        for item in policy_rows[:20]:
            try:
                article = requests.get(item["url"], timeout=10,
                    headers={"User-Agent":"Mozilla/5.0 A-Share-Alpha-Radar"})
                article.raise_for_status()
                content_type = article.headers.get("Content-Type", "").lower()
                if "pdf" in content_type:
                    item["body_status"] = "pdf_not_parsed"
                    continue
                raw_html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", article.text)
                plain = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw_html))
                plain = unescape(plain)
                plain = re.sub(r"\s+", " ", plain).strip()
                date_match = re.search(r"(20\d{2})[年./-](\d{1,2})[月./-](\d{1,2})日?", plain)
                if date_match:
                    item["published_date"] = f"{int(date_match.group(1)):04d}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"
                item["body_text"] = plain[:12000] if len(plain) >= 200 else None
                item["body_status"] = "success" if item["body_text"] else "empty"
            except Exception as article_exc:
                item["body_status"] = "failed"
                item["body_error"] = str(article_exc)[:180]
            time.sleep(0.1)
        policy_status = {"source": "MIIT_official_release_index",
                         "status": "success" if any(x.get("body_status") == "success" for x in policy_rows) else "partial" if policy_rows else "empty",
                         "records": len(policy_rows),
                         "full_text_records": sum(1 for x in policy_rows if x.get("body_status") == "success"),
                         "url": policy_url}
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
            "private_bank_equity_evidence": (theme_member_meta.get(name, {}).get("verified_relationships", [])
                if name == "参股民营银行" else []),
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
                if r.get("body_status") == "success" and
                any(k in (r["title"] + " " + (r.get("body_text") or "")) for k in POLICY_SYNONYMS.get(name, (name,)))],
                "coverage": sum(1 for r in policy_rows if r.get("body_status") == "success" and
                any(k in (r["title"] + " " + (r.get("body_text") or "")) for k in POLICY_SYNONYMS.get(name, (name,)))),
                "index_matches": sum(1 for r in policy_rows
                if any(k in r["title"] for k in POLICY_SYNONYMS.get(name, (name,)))),
                "status": "success" if any(r.get("body_status") == "success" and
                    any(k in (r["title"] + " " + (r.get("body_text") or "")) for k in POLICY_SYNONYMS.get(name, (name,))) for r in policy_rows) else "no_matching_official_full_text",
                "source": "工业和信息化部官网文件发布索引及原文页"},
        }

    # Bind every theme-evidence payload to the actual market snapshot date.
    # research.generated_at is a build timestamp, not a trading date.
    market_snapshot_path = SITE / "latest.json"
    market_snapshot = json.loads(market_snapshot_path.read_text(encoding="utf-8")) if market_snapshot_path.exists() else {}
    market_trade_date = str(market_snapshot.get("trade_date") or "")
    if not market_trade_date:
        raise RuntimeError("latest.json has no trade_date; refusing to publish unanchored theme evidence")
    evidence = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "market_trade_date": market_trade_date,
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
            "估值拥挤度暂缺可靠的全成分股估值分位数据，不以涨跌幅或换手率替代。",
            "产业逻辑和业绩兑现按成分股最新季度营收、净利润同比正增长广度评分；样本不足时保持缺失。",
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
        flow_rows = research.get("sections", {}).get("capital-flows", {}).get("items", [])
        score_result = model_score(theme.get("change_pct"),
            flow_ratio_for_theme(flow_rows, theme, "5日"),
            flow_ratio_for_theme(flow_rows, theme, "10日"), item)
        theme["model"]["score"] = score_result["score"]
        theme["model"]["potential_score_coverage"] = score_result["coverage"]
        theme["model"]["dimensions"] = score_result["dimensions"]
        theme["model"]["health"] = score_result["health"]
        theme["model"]["stage"] = score_result["stage"]
        theme["model"]["evidence_status"] = "partial" if any(theme["evidence_coverage"].values()) else "unavailable"
        theme["model"]["partial_score_status"] = "partial" if score_result["coverage"] < 100 else "complete"
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
