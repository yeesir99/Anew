#!/usr/bin/env python3
"""Rebuild broad-market indicators and backfill long history for liquid stocks.

Full-market daily snapshots are preferred. When their history is too short,
fetch independent per-symbol bars for the top liquid universe. Missing values
remain null; no synthetic prices or indicators are created.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.collectors import _ak, _history_one, _technical_history

DATA = ROOT / "data"
LATEST = DATA / "latest_market.csv"
OUT = DATA / "technical_coverage.json"
METRICS = ["ma5", "ma10", "ma20", "ma60", "rsi14",
           "macd_dif", "macd_dea", "macd_hist", "volatility20"]
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def _day_key(value) -> str:
    raw = str(value or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        raise RuntimeError(f"invalid latest trade_date: {value}")
    return parsed.strftime("%Y%m%d")


def _snapshot_history() -> tuple[pd.DataFrame, int]:
    files = sorted(DATA.glob("market_????-??-??.csv"))
    latest_resolved = LATEST.resolve()
    if all(path.resolve() != latest_resolved for path in files):
        files.append(LATEST)
    parts = []
    for path in files:
        try:
            frame = pd.read_csv(path, encoding="utf-8-sig", dtype={"symbol": str})
            if {"symbol", "trade_date", "close"}.issubset(frame.columns):
                parts.append(frame[["symbol", "trade_date", "close"]].copy())
        except Exception as exc:
            logging.warning("skip %s: %s", path.name, exc)
    if not parts:
        return pd.DataFrame(), 0
    history = pd.concat(parts, ignore_index=True)
    history["symbol"] = history["symbol"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    history["trade_date"] = history["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True).str.replace("-", "", regex=False).str[:8]
    history["close"] = pd.to_numeric(history["close"], errors="coerce")
    history = history.dropna(subset=["symbol", "trade_date", "close"])
    history = history[history["close"] > 0].drop_duplicates(["symbol", "trade_date"], keep="last")
    history = history.sort_values(["symbol", "trade_date"])
    sessions = int(history["trade_date"].nunique())
    g = history.groupby("symbol", sort=False)["close"]
    history["ma5"] = g.transform(lambda x: x.rolling(5, min_periods=5).mean())
    history["ma10"] = g.transform(lambda x: x.rolling(10, min_periods=10).mean())
    history["ma20"] = g.transform(lambda x: x.rolling(20, min_periods=20).mean())
    history["ma60"] = g.transform(lambda x: x.rolling(60, min_periods=60).mean())
    delta = g.diff()
    gains, losses = delta.clip(lower=0), -delta.clip(upper=0)
    avg_gain = gains.groupby(history["symbol"]).transform(
        lambda x: x.ewm(alpha=1/14, adjust=False, min_periods=14).mean())
    avg_loss = losses.groupby(history["symbol"]).transform(
        lambda x: x.ewm(alpha=1/14, adjust=False, min_periods=14).mean())
    history["rsi14"] = 100 - 100 / (1 + avg_gain / avg_loss.replace(0, float("nan")))
    ema12 = g.transform(lambda x: x.ewm(span=12, adjust=False, min_periods=12).mean())
    ema26 = g.transform(lambda x: x.ewm(span=26, adjust=False, min_periods=26).mean())
    history["macd_dif"] = ema12 - ema26
    history["macd_dea"] = history.groupby("symbol")["macd_dif"].transform(
        lambda x: x.ewm(span=9, adjust=False, min_periods=9).mean())
    history["macd_hist"] = (history["macd_dif"] - history["macd_dea"]) * 2
    history["volatility20"] = g.transform(
        lambda x: x.pct_change().rolling(20, min_periods=20).std() * (252 ** 0.5) * 100)
    return history[history["trade_date"] == _day_key(pd.read_csv(LATEST, encoding="utf-8-sig").iloc[0]["trade_date"])].copy(), sessions


def _backfill_per_symbol(latest: pd.DataFrame, day: str) -> tuple[pd.DataFrame, dict]:
    limit = int(os.getenv("MAX_HISTORY_STOCKS", "100"))
    if limit == 0:
        limit = len(latest)
    latest = latest.copy()
    if "amount" in latest:
        latest["amount"] = pd.to_numeric(latest["amount"], errors="coerce")
        universe = latest.sort_values("amount", ascending=False, na_position="last").head(limit)
    else:
        universe = latest.head(limit)
    day_obj = datetime.strptime(day, "%Y%m%d").date()
    start = (day_obj - timedelta(days=170)).strftime("%Y%m%d")
    workers = max(1, min(int(os.getenv("HISTORY_WORKERS", "3")), 8))
    ak = _ak()
    histories = []
    failures = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_history_one, ak, str(row["symbol"]).zfill(6), day_obj, start): str(row["symbol"]).zfill(6)
            for _, row in universe.iterrows()
        }
        for future in as_completed(futures):
            try:
                part = future.result()
                if part is not None and not part.empty:
                    histories.append(part)
            except Exception as exc:
                failures += 1
                if failures <= 10:
                    logging.warning("technical history %s failed: %s", futures[future], str(exc)[:220])
    stats = {"requested": len(universe), "successful": len(histories), "failed": failures,
             "workers": workers, "window_days": 170}
    if not histories:
        return latest, stats
    raw = pd.concat(histories, ignore_index=True).drop_duplicates(["ts_code", "trade_date"], keep="last")
    tech = _technical_history(raw, day)
    if tech.empty:
        return latest, stats
    tech = tech.copy()
    tech["symbol"] = tech["ts_code"].astype(str).str.split(".").str[0].str.zfill(6)
    by_symbol = tech.set_index("symbol")
    for col in METRICS:
        if col in by_symbol:
            fresh = latest["symbol"].map(by_symbol[col])
            latest[col] = fresh.combine_first(latest[col] if col in latest else pd.Series(index=latest.index, dtype=float))
    session_counts = raw.groupby("ts_code")["trade_date"].nunique()
    stats["history_sessions_median"] = float(session_counts.median()) if not session_counts.empty else 0
    stats["history_sessions_min"] = int(session_counts.min()) if not session_counts.empty else 0
    stats["history_sessions_max"] = int(session_counts.max()) if not session_counts.empty else 0
    return latest, stats


def main():
    if not LATEST.exists():
        raise SystemExit("latest_market.csv missing")
    original = pd.read_csv(LATEST, encoding="utf-8-sig", dtype={"symbol": str})
    if original.empty or "symbol" not in original or "trade_date" not in original:
        raise SystemExit("latest_market.csv missing required symbol/trade_date columns")
    original["symbol"] = original["symbol"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    day = _day_key(original.iloc[0]["trade_date"])
    history, snapshot_sessions = _snapshot_history()
    latest = original.copy()
    if not history.empty:
        calc = history[["symbol"] + [c for c in METRICS if c in history.columns]]
        for col in METRICS:
            if col in latest:
                latest = latest.drop(columns=[col])
        latest = latest.merge(calc, on="symbol", how="left")
        for col in METRICS:
            if col in original:
                values = original.set_index("symbol")[col]
                latest[col] = latest[col].where(latest[col].notna(), latest["symbol"].map(values))
    current_coverage = int(latest["ma20"].notna().sum()) if "ma20" in latest else 0
    backfill_stats = {"requested": 0, "successful": 0, "failed": 0, "skipped": "coverage already sufficient"}
    if current_coverage < min(100, len(latest)):
        latest, backfill_stats = _backfill_per_symbol(latest, day)
    latest.to_csv(LATEST, index=False, encoding="utf-8-sig")
    trade_date_iso = datetime.strptime(day, "%Y%m%d").date().isoformat()
    latest.to_csv(DATA / f"market_{trade_date_iso}.csv", index=False, encoding="utf-8-sig")
    coverage = {col: int(latest[col].notna().sum()) if col in latest else 0 for col in METRICS}
    result = {"trade_date": day, "stocks": len(latest), "history_sessions": snapshot_sessions,
        "coverage": coverage, "per_symbol_history": backfill_stats,
        "method": "persisted full-market daily snapshots plus independent per-symbol bars for liquid stocks; no missing values fabricated"}
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
