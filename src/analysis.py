"""Technical indicators, market scans and transparent risk heuristics."""
from __future__ import annotations

import numpy as np
import pandas as pd


def enrich_market(frame: pd.DataFrame) -> pd.DataFrame:
    df = frame.copy()
    numeric = ["close", "open", "high", "low", "pre_close", "vol", "amount",
               "turnover_rate", "volume_ratio", "pe", "pb", "ma5", "ma10",
               "ma20", "ma60", "rsi14", "macd_dif", "macd_dea", "macd_hist",
               "volatility20"]
    for col in numeric:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "pre_close" in df and "close" in df:
        df["pct_chg_calc"] = (df["close"] / df["pre_close"] - 1) * 100
    elif "pct_chg" in df:
        df["pct_chg_calc"] = pd.to_numeric(df["pct_chg"], errors="coerce")
    else:
        df["pct_chg_calc"] = np.nan
    return df


def risk_flags(row: pd.Series) -> list[str]:
    flags = []
    change = row.get("pct_chg_calc")
    ratio = row.get("volume_ratio")
    rsi = row.get("rsi14")
    close, ma20 = row.get("close"), row.get("ma20")
    if pd.notna(change) and change <= -7:
        flags.append("单日跌幅较大")
    if pd.notna(change) and change >= 9:
        flags.append("单日涨幅较大，注意波动与流动性")
    if pd.notna(change) and pd.notna(ratio) and change < 0 and ratio >= 2:
        flags.append("放量下跌")
    if pd.notna(rsi) and rsi >= 80:
        flags.append("RSI14偏高，关注短期过热风险")
    if pd.notna(rsi) and rsi <= 20:
        flags.append("RSI14偏低，波动及流动性风险需核验")
    if pd.notna(close) and pd.notna(ma20) and close < ma20:
        flags.append("收盘价低于MA20")
    if "amount" in row and pd.notna(row.get("amount")) and row.get("amount") <= 0:
        flags.append("成交额缺失或为零")
    return flags


def build_summary(frame: pd.DataFrame) -> dict:
    df = enrich_market(frame)
    change = df["pct_chg_calc"].dropna()
    summary = {
        "stock_count": int(len(df)),
        "up": int((change > 0).sum()),
        "down": int((change < 0).sum()),
        "flat": int((change == 0).sum()),
        "limit_like_up": int((change >= 9.8).sum()),
        "limit_like_down": int((change <= -9.8).sum()),
        "median_change_pct": round(float(change.median()), 3) if len(change) else None,
        "total_amount": round(float(pd.to_numeric(df.get("amount"), errors="coerce").fillna(0).sum()), 2)
                        if "amount" in df else None,
    }
    return summary


def top_table(df: pd.DataFrame, column: str, ascending: bool = False, n: int = 10) -> pd.DataFrame:
    if column not in df.columns:
        return pd.DataFrame()
    out = df.sort_values(column, ascending=ascending).head(n).copy()
    preferred = [c for c in ["ts_code", "name", "industry", column, "pct_chg_calc",
                              "ma5", "ma20", "rsi14", "macd_hist", "amount",
                              "volume_ratio", "turnover_rate"] if c in out.columns]
    return out[preferred]


def scan_risks(frame: pd.DataFrame) -> pd.DataFrame:
    df = enrich_market(frame)
    df["risk_flags"] = df.apply(lambda row: "；".join(risk_flags(row)), axis=1)
    return df[df["risk_flags"] != ""].copy()
