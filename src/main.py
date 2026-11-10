"""CLI entry point: python -m src.main close|morning."""
from __future__ import annotations

import json
import logging
import sys
from datetime import date
from pathlib import Path

import pandas as pd

from .collectors import (collect_announcements, collect_market, collect_news,
                         is_open_day, write_json)
from .reporting import render_report

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
LOG = logging.getLogger("anew")


def setup() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    DATA.mkdir(exist_ok=True)
    REPORTS.mkdir(exist_ok=True)


def close_run() -> None:
    day = date.today()
    if not is_open_day(day):
        LOG.info("%s 非上交所交易日，跳过收盘采集。", day)
        return
    LOG.info("开始采集 %s A股收盘数据（免费数据源）", day)
    market = collect_market(day)
    if market.empty:
        raise RuntimeError("行情数据为空，停止生成报告以避免误报。")
    news = collect_news()
    announcements = collect_announcements(day)
    market.to_csv(DATA / "latest_market.csv", index=False, encoding="utf-8-sig")
    market.to_csv(DATA / f"market_{day.isoformat()}.csv", index=False, encoding="utf-8-sig")
    write_json(str(DATA / "latest_news.json"), news)
    write_json(str(DATA / "latest_announcements.json"), announcements)
    report = render_report(day.isoformat(), market, news, announcements, "A股收盘量化观察")
    (REPORTS / f"{day.isoformat()}-close.md").write_text(report, encoding="utf-8")
    LOG.info("收盘报告完成：覆盖 %s 只股票", len(market))


def morning_run() -> None:
    path = DATA / "latest_market.csv"
    if not path.exists():
        raise RuntimeError("尚无收盘行情数据。请先成功运行 close workflow。")
    market = pd.read_csv(path)
    if market.empty:
        raise RuntimeError("latest_market.csv 为空，停止生成晨报。")
    news = collect_news()
    ann_path = DATA / "latest_announcements.json"
    announcements = json.loads(ann_path.read_text(encoding="utf-8")) if ann_path.exists() else []
    trade_date = str(market.iloc[0].get("trade_date", "最近交易日"))
    day = date.today().isoformat()
    report = render_report(trade_date, market, news, announcements, "A股早间研究晨报")
    (REPORTS / f"{day}-morning.md").write_text(report, encoding="utf-8")
    write_json(str(DATA / "morning_news.json"), news)
    LOG.info("晨报已生成。")


def main() -> None:
    setup()
    if len(sys.argv) != 2 or sys.argv[1] not in {"close", "morning"}:
        raise SystemExit("用法：python -m src.main close|morning")
    close_run() if sys.argv[1] == "close" else morning_run()


if __name__ == "__main__":
    main()
