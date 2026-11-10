"""Markdown report rendering and optional LLM interpretation."""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any

import pandas as pd
import requests

from .analysis import build_summary, enrich_market, scan_risks, top_table


def _md_table(frame: pd.DataFrame) -> str:
    if frame.empty:
        return "暂无可用数据。"
    return frame.to_markdown(index=False)


def ai_interpret(summary: dict, news: list[dict], announcements: list[dict],
                 leaders: pd.DataFrame, risks: pd.DataFrame) -> str | None:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
    payload = {
        "market_summary": summary,
        "news": news[:25],
        "announcements": announcements[:20],
        "leaders": leaders.head(10).to_dict(orient="records"),
        "risk_samples": risks.head(20)[["ts_code", "name", "risk_flags"]].to_dict(orient="records")
                        if not risks.empty else [],
    }
    prompt = (
        "你是严谨的A股研究助理。请用中文撰写结构化市场解读：宏观/全球新闻脉络、"
        "A股广度与成交、强弱方向、值得核验的公告、风险观察。严格区分事实与推测；"
        "不得编造新闻内容、公司事件、行业归因或目标价；数据不足要明确说明。"
        "不提供买卖指令或收益承诺。输入数据如下：\n" + str(payload)
    )
    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": model, "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.2},
            timeout=90,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        return f"AI 解读暂不可用：{exc}"


def render_report(day: str, frame: pd.DataFrame, news: list[dict[str, Any]],
                  announcements: list[dict[str, Any]], title: str) -> str:
    df = enrich_market(frame)
    summary = build_summary(df)
    gainers = top_table(df, "pct_chg_calc", ascending=False)
    losers = top_table(df, "pct_chg_calc", ascending=True)
    active = top_table(df, "amount", ascending=False)
    risks = scan_risks(df)
    ai_text = ai_interpret(summary, news, announcements, gainers, risks)
    lines = [
        f"# {title}｜{day}", "",
        f"> 生成时间：{datetime.now().astimezone().isoformat(timespec='seconds')}  ",
        "> 本报告为数据整理与研究辅助，不构成投资建议。", "",
        "## 一、市场概览",
        f"- 覆盖股票：{summary['stock_count']}",
        f"- 上涨 / 下跌 / 平盘：{summary['up']} / {summary['down']} / {summary['flat']}",
        f"- 涨停附近（涨幅≥9.8%）/ 跌停附近（跌幅≤-9.8%）：{summary['limit_like_up']} / {summary['limit_like_down']}",
        f"- 涨跌幅中位数：{summary['median_change_pct']}%",
        f"- 成交额合计（Tushare amount 口径）：{summary['total_amount']}", "",
        "## 二、涨幅与跌幅榜", "### 涨幅榜", _md_table(gainers),
        "### 跌幅榜", _md_table(losers), "### 成交额榜", _md_table(active), "",
        "## 三、风险观察",
        _md_table(risks[[c for c in ["ts_code", "name", "pct_chg_calc", "volume_ratio", "risk_flags"]
                          if c in risks.columns]].head(50)) if not risks.empty else "未触发当前规则的风险标记。",
        "", "## 四、财经新闻（需打开原文核验）",
    ]
    if news:
        lines.extend([f"- [{item.get('title') or '无标题'}]({item.get('url')}) — {item.get('domain','')} · {item.get('published','')}"
                      for item in news[:40]])
    else:
        lines.append("本次未取得新闻结果。")
    lines.extend(["", "## 五、交易所/上市公司公告索引"])
    if announcements:
        for item in announcements[:50]:
            label = item.get("title") or item.get("name") or item.get("ts_code") or "公告"
            url = item.get("url", "")
            lines.append(f"- [{label}]({url}) — {item.get('ts_code','')} · {item.get('ann_date','')}")
    else:
        lines.append("本次未取得公告索引。请在 Tushare 账号确认 anns_d 权限，并以巨潮资讯/交易所官网原文为准。")
    lines.extend(["", "## 六、AI 综合解读" if ai_text else "## 六、解读说明"])
    lines.append(ai_text if ai_text else "未配置 OPENAI_API_KEY，当前报告仅包含可复核的规则化统计与风险扫描。")
    lines.extend(["", "## 七、口径与局限",
                  "- 涨停附近阈值为粗略扫描，不适用于所有板块、ST 股票及特殊交易状态；不是官方涨跌停判定。",
                  "- 风险规则是提示条件，不是预测；行业热点需结合可靠行业/概念分类数据进一步验证。",
                  "- 新闻与公告均需点击原文核验，自动摘要可能遗漏上下文。"])
    return "\n".join(lines) + "\n"
