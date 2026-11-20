#!/usr/bin/env python3
"""Point-in-time, next-session industry momentum backtest.

Uses only persisted daily industry snapshots. Signals are formed at close t
from the prior five sessions and evaluated on t+1. No future rows enter a signal.
"""
from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "data" / "industry_history.csv"
OUT = ROOT / "site" / "data" / "backtest.json"
FEE_BPS_ROUND_TRIP = 20
TOP_FRACTION = 0.20


def main():
    if not SOURCE.exists():
        result = {"status": "unavailable", "reason": "行业日线历史尚未积累",
                  "minimum_sessions": 10, "sessions": 0}
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return result
    with SOURCE.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    daily = {}
    for row in rows:
        try:
            day = row["trade_date"]
            name = row["industry"]
            change = float(row["change_pct"])
            if math.isfinite(change):
                daily.setdefault(day, {})[name] = change
        except (KeyError, TypeError, ValueError):
            continue
    dates = sorted(daily)
    if len(dates) < 10 or len({k for d in daily.values() for k in d}) < 10:
        result = {"status": "warming_up", "reason": "至少需要10个有效交易日及10个行业样本",
                  "minimum_sessions": 10, "sessions": len(dates), "industry_count": len({k for d in daily.values() for k in d})}
    else:
        strategy, benchmark = [], []
        for i in range(5, len(dates)):
            signal_day, next_day = dates[i - 1], dates[i]
            prior_days = dates[i - 5:i]
            scores = {}
            for industry in daily[signal_day]:
                values = [daily[d].get(industry) for d in prior_days]
                if all(v is not None for v in values):
                    growth = 1.0
                    for v in values:
                        growth *= 1.0 + v / 100.0
                    scores[industry] = (growth - 1.0) * 100.0
            actual = daily[next_day]
            ranked = sorted(scores, key=scores.get, reverse=True)
            selected = [x for x in ranked[:max(1, math.ceil(len(ranked) * TOP_FRACTION))] if x in actual]
            if not selected:
                continue
            gross = sum(actual[x] for x in selected) / len(selected)
            net = gross - FEE_BPS_ROUND_TRIP / 100.0
            available = [v for v in actual.values() if math.isfinite(v)]
            strategy.append({"date": next_day, "return_pct": net, "gross_return_pct": gross,
                             "selected_industries": len(selected)})
            benchmark.append(sum(available) / len(available) if available else 0.0)
        if len(strategy) < 5:
            result = {"status": "warming_up", "reason": "有效的无前视偏差样本不足5个交易日",
                      "sessions": len(dates), "observations": len(strategy)}
        else:
            def equity(values):
                out, value = [], 1.0
                for x in values:
                    value *= 1.0 + x / 100.0
                    out.append(value)
                return out
            curve = equity([x["return_pct"] for x in strategy])
            bench_curve = equity(benchmark[:len(strategy)])
            peak, max_dd = 1.0, 0.0
            for value in curve:
                peak = max(peak, value)
                max_dd = min(max_dd, (value / peak - 1.0) * 100.0)
            result = {"status": "available", "strategy": "行业过去5个交易日动量排名前20%，等权持有下一交易日",
                "benchmark": "当日可用行业等权收益", "fee_bps_round_trip": FEE_BPS_ROUND_TRIP,
                "observations": len(strategy), "sessions": len(dates),
                "cumulative_return_pct": round((curve[-1] - 1.0) * 100.0, 3),
                "benchmark_cumulative_return_pct": round((bench_curve[-1] - 1.0) * 100.0, 3),
                "win_rate_pct": round(sum(x["return_pct"] > 0 for x in strategy) / len(strategy) * 100.0, 2),
                "max_drawdown_pct": round(max_dd, 3), "daily": strategy[-60:],
                "limitations": ["行业样本和分类沿用上游公开接口，可能发生口径变更。",
                    "仅为行业动量基准实验，不含停牌、涨跌停成交约束、滑点细节及税费。",
                    "20bp往返成本为统一假设，不是实际成交成本；历史结果不代表未来收益。",
                    "当前数据从云端迁移后开始持续积累，不存在可恢复的NAS历史数据库。"]}
    result["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result.get(k) for k in ("status", "sessions", "observations", "reason")}, ensure_ascii=False))
    return result


if __name__ == "__main__":
    main()
