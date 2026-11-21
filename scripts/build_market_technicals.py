#!/usr/bin/env python3
"""Rebuild market-wide technical indicators from persisted daily full-market snapshots.

This expands coverage without issuing thousands of per-symbol API requests. A
metric is emitted only after its required number of observations exists.
"""
from __future__ import annotations
import json
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
LATEST = DATA / "latest_market.csv"
OUT = DATA / "technical_coverage.json"
METRICS = ["ma5", "ma10", "ma20", "ma60", "rsi14",
           "macd_dif", "macd_dea", "macd_hist", "volatility20"]


def main():
    if not LATEST.exists():
        raise SystemExit("latest_market.csv missing")
    files = sorted(DATA.glob("market_????-??-??.csv"))
    if LATEST not in files:
        files.append(LATEST)
    parts = []
    for path in files:
        try:
            frame = pd.read_csv(path, encoding="utf-8-sig", dtype={"symbol": str})
            if {"symbol", "trade_date", "close"}.issubset(frame.columns):
                frame = frame[["symbol", "trade_date", "close"]].copy()
                parts.append(frame)
        except Exception as exc:
            print(f"skip {path.name}: {exc}")
    if not parts:
        raise SystemExit("no daily full-market snapshots")
    history = pd.concat(parts, ignore_index=True)
    history["symbol"] = history["symbol"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    history["trade_date"] = history["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True).str[:8]
    history["close"] = pd.to_numeric(history["close"], errors="coerce")
    history = history.dropna(subset=["symbol", "trade_date", "close"])
    history = history[history["close"] > 0].drop_duplicates(["symbol", "trade_date"], keep="last")
    history = history.sort_values(["symbol", "trade_date"])
    g = history.groupby("symbol", sort=False)["close"]
    history["ma5"] = g.transform(lambda x: x.rolling(5, min_periods=5).mean())
    history["ma10"] = g.transform(lambda x: x.rolling(10, min_periods=10).mean())
    history["ma20"] = g.transform(lambda x: x.rolling(20, min_periods=20).mean())
    history["ma60"] = g.transform(lambda x: x.rolling(60, min_periods=60).mean())
    delta = g.diff()
    gains, losses = delta.clip(lower=0), -delta.clip(upper=0)
    avg_gain = gains.groupby(history["symbol"]).transform(lambda x: x.ewm(alpha=1/14, adjust=False, min_periods=14).mean())
    avg_loss = losses.groupby(history["symbol"]).transform(lambda x: x.ewm(alpha=1/14, adjust=False, min_periods=14).mean())
    history["rsi14"] = 100 - 100 / (1 + avg_gain / avg_loss.replace(0, float("nan")))
    ema12 = g.transform(lambda x: x.ewm(span=12, adjust=False, min_periods=12).mean())
    ema26 = g.transform(lambda x: x.ewm(span=26, adjust=False, min_periods=26).mean())
    history["macd_dif"] = ema12 - ema26
    history["macd_dea"] = history.groupby("symbol")["macd_dif"].transform(lambda x: x.ewm(span=9, adjust=False, min_periods=9).mean())
    history["macd_hist"] = (history["macd_dif"] - history["macd_dea"]) * 2
    history["volatility20"] = g.transform(lambda x: x.pct_change().rolling(20, min_periods=20).std() * (252 ** 0.5) * 100)
    latest = pd.read_csv(LATEST, encoding="utf-8-sig", dtype={"symbol": str})
    latest["symbol"] = latest["symbol"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    day = str(latest["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True).max())[:8]
    calc = history[history["trade_date"] == day][["symbol"] + METRICS]
    latest = latest.drop(columns=[c for c in METRICS if c in latest.columns], errors="ignore").merge(calc, on="symbol", how="left")
    # Keep better independently collected history-based values if they exist.
    original = pd.read_csv(LATEST, encoding="utf-8-sig", dtype={"symbol": str})
    original["symbol"] = original["symbol"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    for col in METRICS:
        if col in original:
            values = original.set_index("symbol")[col]
            latest[col] = latest[col].where(latest[col].notna(), latest["symbol"].map(values))
    latest.to_csv(LATEST, index=False, encoding="utf-8-sig")
    coverage = {col: int(latest[col].notna().sum()) if col in latest else 0 for col in METRICS}
    result = {"trade_date": day, "stocks": len(latest), "history_sessions": int(history["trade_date"].nunique()),
              "coverage": coverage, "method": "persisted full-market daily close series; no missing values fabricated"}
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
