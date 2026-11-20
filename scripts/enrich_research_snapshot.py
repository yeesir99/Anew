#!/usr/bin/env python3
"""Build evidence-labelled research modules and a 12-section public daily note."""
from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
SITE = ROOT / "site" / "data"
REPORTS = ROOT / "reports"


def read_csv(name):
    path = DATA / name
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def main():
    latest_path = SITE / "latest.json"
    research_path = SITE / "research.json"
    if not latest_path.exists() or not research_path.exists():
        raise RuntimeError("必须先构建通过校验的 latest.json / research.json")
    market = json.loads(latest_path.read_text(encoding="utf-8"))
    research = json.loads(research_path.read_text(encoding="utf-8"))
    if research.get("privacy") != "public_research_only":
        raise RuntimeError("公开隐私标记校验失败")
    industries = read_csv("industry_history.csv")
    flows = read_csv("capital_flows.csv")
    etfs = read_csv("etf_shares.csv")
    source_doc = {}
    try:
        source_doc = json.loads((DATA / "research_sources.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    day = market.get("trade_date")
    history_by_name = {}
    for row in industries:
        if row.get("trade_date") and row.get("industry"):
            history_by_name.setdefault(row["industry"], []).append(row)
    rotation = []
    current = [x for x in industries if x.get("trade_date") == day]
    for row in current:
        name = row["industry"]
        seq = sorted(history_by_name.get(name, []), key=lambda x: x["trade_date"])
        vals = [num(x.get("change_pct")) for x in seq]
        vals = [x for x in vals if x is not None]
        def cumulative(n):
            if len(vals) < n:
                return None
            r = 1.0
            for v in vals[-n:]:
                r *= 1.0 + v / 100.0
            return round((r - 1.0) * 100.0, 3)
        r5, r10, r20 = cumulative(5), cumulative(10), cumulative(20)
        rotation.append({"industry": name, "latest_change": num(row.get("change_pct")),
            "turnover_cny": num(row.get("turnover_cny")), "return_5d_pct": r5,
            "return_10d_pct": r10, "return_20d_pct": r20,
            "rotation_strength_score": round(50 + (r5 or 0)*1.2 + (r10 or 0)*0.5 + (r20 or 0)*0.2, 2),
            "history_sessions": len(vals), "history_ready": len(vals) >= 20})
    rotation.sort(key=lambda x: x["rotation_strength_score"], reverse=True)
    amounts = [x["turnover_cny"] for x in rotation if x["turnover_cny"] is not None]
    amount_rank = {x: i for i, x in enumerate(sorted(amounts))}
    themes = []
    for i, row in enumerate(rotation[:20]):
        themes.append({"industry": row["industry"], "change_pct": row["latest_change"],
            "turnover_cny": row["turnover_cny"], "proxy_score": row["rotation_strength_score"],
            "model": {"coverage": 20 if row["latest_change"] is not None else 0,
                      "stage": "量价观察候选，非产业主线结论",
                      "health": {"score": None, "coverage": 0,
                                 "note": "缺少独立产业、政策、盈利与机构持仓证据，不计算综合健康分"}},
            "evidence": ["行业当日涨跌幅", "行业成交额"]})
    flow_items = []
    for row in flows:
        if row.get("trade_date") == day:
            flow_items.append({"trade_date": row.get("trade_date"), "indicator": row.get("indicator"),
                "industry": row.get("industry"), "change_pct": num(row.get("change_pct")),
                "main_net_inflow": num(row.get("main_net_inflow_cny")),
                "main_net_ratio": num(row.get("main_net_ratio_pct")), "source": row.get("source")})
    flow_items.sort(key=lambda x: (x.get("indicator") != "今日", -(x.get("main_net_inflow") or 0)))
    etf_items = []
    for row in etfs:
        if row.get("trade_date") != day:
            continue
        prior = [x for x in etfs if x.get("exchange") == row.get("exchange") and
                 x.get("code") == row.get("code") and x.get("trade_date", "") < day]
        previous = max(prior, key=lambda x: x["trade_date"]) if prior else None
        shares, old = num(row.get("shares")), num(previous.get("shares")) if previous else None
        change = ((shares / old - 1) * 100) if shares is not None and old not in (None, 0) else None
        etf_items.append({"trade_date": day, "exchange": row.get("exchange"), "code": row.get("code"),
            "name": row.get("name"), "shares": shares, "change_pct": round(change, 4) if change is not None else None,
            "previous_date": previous.get("trade_date") if previous else None, "source": row.get("source")})
    breadth = market.get("market_breadth", {})
    sections = research.setdefault("sections", {})
    sections["overview"] = {"count": market.get("coverage", {}).get("valid_stock_count", 0),
        "up": market.get("coverage", {}).get("up", 0), "down": market.get("coverage", {}).get("down", 0)}
    sections["market-breadth"] = {"item": {"trade_date": day, "payload": breadth}}
    sections["industries"] = {"items": sorted(rotation, key=lambda x: x.get("latest_change") or -999, reverse=True)}
    sections["themes"] = {"items": themes}
    sections["rotation"] = {"items": rotation}
    sections["capital-flows"] = {"items": flow_items,
        "note": "东方财富公开主力资金估算，不是机构专属账户流向。"}
    sections["etf-shares"] = {"items": etf_items,
        "note": "份额变化按相同代码、交易所和来源口径比较；份额变化不等于机构净买入。"}
    sections["data-sources"] = {"items": source_doc.get("sources", [])}
    sections["backtest"] = json.loads((SITE / "backtest.json").read_text(encoding="utf-8")) if (SITE / "backtest.json").exists() else {"status": "unavailable"}
    # Twelve sections; unavailable fields are explicitly labelled rather than filled with synthetic values.
    sections_report = [
        ("1. 市场广度与风险偏好", f"交易日 {day}；覆盖 {market.get('coverage', {}).get('valid_stock_count', 0)} 只；上涨 {market.get('coverage', {}).get('up', 0)}、下跌 {market.get('coverage', {}).get('down', 0)}；涨跌幅中位数 {breadth.get('median_change_pct', '缺失')}%。"),
        ("2. 行业强弱排名", "；".join(f"{x['industry']} {x.get('latest_change')}%" for x in sorted(rotation, key=lambda x: x.get("latest_change") or -999, reverse=True)[:10]) or "行业快照暂不可用。"),
        ("3. 主线候选与证据覆盖", "仅列出量价观察候选；行业涨幅不等同产业主线，政策/订单/盈利验证未接入，不给出确定性主线结论。"),
        ("4. 行业轮动（5/10/20日）", "5/10/20日复合收益按已持久化交易日快照计算；未满窗口的行业显示缺失，不用短样本补齐。"),
        ("5. 公开资金流估算", f"本日记录 {sum(x.get('indicator') == '今日' for x in flow_items)} 条；仅为公开主力资金估算，非机构账户数据。"),
        ("6. ETF份额变化", f"本日份额快照 {len(etf_items)} 条；只有存在上一交易日同口径记录时才计算变化率。"),
        ("7. 宽基指数与宏观环境", "当前公开流水线未接入稳定的指数/宏观历史序列；本栏不生成虚构指数结论。"),
        ("8. 个股活跃度与异动", f"成交活跃榜按全市场成交额排序；成交额表示交易活动，不表示净流入。"),
        ("9. 技术结构与趋势", "当前可用全市场日快照；MA/RSI/MACD全市场连续序列尚未形成，不将少量样本外推至全市场。"),
        ("10. 公告、政策与事件", "公告与新闻索引如未取得将标记为空；重大事项必须打开交易所/巨潮资讯原文核验。"),
        ("11. 盈利、估值与基本面", "尚无覆盖全市场且可持续的授权一致预期数据；不以单只股票或行业估值快照替代全市场判断。"),
        ("12. 风险、数据质量与行动清单", f"数据状态：{market.get('data_status')}；数据日：{day}；行情上游可能延迟或限流。该报告仅作研究辅助，不构成投资建议。")
    ]
    report_lines = ["# A-Share Alpha Radar｜12栏目公开研究日报", "", f"> 交易日：{day}；生成时间：{datetime.now(timezone.utc).isoformat(timespec='seconds')}；状态：{market.get('data_status')}", ""]
    for heading, body in sections_report:
        report_lines.extend([f"## {heading}", body, ""])
    report_lines.extend(["## 数据口径与限制",
        "- 行业历史从本次云端持久化启用后逐日累积；NAS 已退役，不存在可恢复的 NAS 历史库。",
        "- 主力资金为公开市场估算，ETF份额变化不是机构净买入；成交额不是净流入。",
        "- 主线、机构持仓、订单簿、授权一致预期等缺失数据不会用推断值填充。",
        "- 公开站点只包含公开市场数据，不读取个人持仓、成本、账户或私人证据。"])
    markdown = "\n".join(report_lines) + "\n"
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / f"{day}-research.md").write_text(markdown, encoding="utf-8")
    sections["reports"] = {"items": [{"trade_date": day, "title": "12栏目公开研究日报",
        "markdown": markdown, "data_status": market.get("data_status")}],
        "note": "仅公开市场数据；不包含私人组合字段。"}
    research["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    research["market_status"] = market.get("data_status", "unavailable")
    research["notes"] = list(research.get("notes", [])) + [
        "行业/资金/ETF模块只有当日采集成功且通过日期检查时才更新。",
        "行业轮动和回测需要持续积累的同口径历史数据；新云端历史从迁移后开始。"]
    research_path.write_text(json.dumps(research, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"trade_date": day, "industries": len(rotation), "themes": len(themes),
        "capital_flows": len(flow_items), "etf_shares": len(etf_items), "report_sections": 12}, ensure_ascii=False))


if __name__ == "__main__":
    main()
