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
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

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


def _archive_history(day: str) -> tuple[pd.DataFrame, dict]:
    """Download recent bulk daily quotes for the full A-share universe.

    Public archive is sourced from modi-hu/stock-data (EastMoney-derived).
    Daily close is stored in fen; change_percent is used to construct a
    backward-adjusted close series anchored to each stock's latest raw close.
    """
    day_obj = datetime.strptime(day, "%Y%m%d").date()
    month_keys = []
    for offset in range(3, -1, -1):
        total = day_obj.year * 12 + day_obj.month - 1 - offset
        year, month = divmod(total, 12)
        month_keys.append(f"{year:04d}-{month + 1:02d}")
    columns = ["symbol", "trade_date", "close", "volume", "change_percent"]
    parts = []
    failed_months = []
    for month in month_keys:
        url = f"https://raw.githubusercontent.com/modi-hu/stock-data/master/monthly/daily_quotes/{month}.csv"
        frame = None
        last_error = None
        for attempt in range(2):
            try:
                response = requests.get(url, timeout=(10, 45),
                    headers={"User-Agent":"A-Share-Alpha-Radar/2.0"})
                response.raise_for_status()
                if len(response.content) < 100:
                    raise RuntimeError("monthly archive is unexpectedly small")
                frame = pd.read_csv(BytesIO(response.content), usecols=columns,
                    dtype={"symbol": str}, skipinitialspace=True, low_memory=False)
                frame.columns = frame.columns.astype(str).str.strip()
                break
            except Exception as exc:
                last_error = str(exc)
                if attempt == 0:
                    time.sleep(2)
        if frame is None:
            failed_months.append({"month":month,"error":(last_error or "unknown")[:180]})
        else:
            parts.append(frame)
    if not parts:
        return pd.DataFrame(), {"status":"failed","months":month_keys,"failed_months":failed_months}
    raw = pd.concat(parts, ignore_index=True)
    raw["symbol"] = raw["symbol"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    raw["trade_date"] = raw["trade_date"].astype(str).str.replace("-", "", regex=False).str.replace(r"\.0$", "", regex=True).str[:8]
    raw["close_raw"] = pd.to_numeric(raw["close"], errors="coerce") / 100.0
    raw["volume"] = pd.to_numeric(raw["volume"], errors="coerce")
    raw["change_percent"] = pd.to_numeric(
        raw["change_percent"].astype(str).str.replace("%", "", regex=False), errors="coerce")
    raw = raw.dropna(subset=["symbol","trade_date","close_raw","volume"])
    raw = raw[(raw["close_raw"] > 0) & (raw["volume"] >= 0) & (raw["trade_date"] <= day)]
    raw = raw.drop_duplicates(["symbol","trade_date"], keep="last").sort_values(["symbol","trade_date"])
    raw["daily_factor"] = (1 + raw["change_percent"].fillna(0) / 100).clip(lower=0.01)
    raw["cum_factor"] = raw.groupby("symbol")["daily_factor"].cumprod()
    raw["last_factor"] = raw.groupby("symbol")["cum_factor"].transform("last")
    raw["last_close"] = raw.groupby("symbol")["close_raw"].transform("last")
    raw["close"] = raw["last_close"] * raw["cum_factor"] / raw["last_factor"]
    raw["vol"] = raw["volume"]
    raw["ts_code"] = raw["symbol"] + raw["symbol"].str[0].map(
        lambda x: ".SH" if x in ("5","6","9") else ".BJ" if x in ("4","8") else ".SZ")
    latest_archive_day = raw["trade_date"].max() if not raw.empty else None
    sessions = int(raw["trade_date"].nunique()) if not raw.empty else 0
    stats = {"status":"success" if latest_archive_day == day and sessions >= 60 and not failed_months else "partial",
        "source":"modi-hu/stock-data monthly/daily_quotes (EastMoney-derived public archive)",
        "months_downloaded":len(parts),"months_requested":len(month_keys),
        "failed_months":failed_months,"records":len(raw),
        "stocks":int(raw["symbol"].nunique()) if not raw.empty else 0,
        "history_sessions":sessions,"latest_archive_trade_date":latest_archive_day,
        "adjustment":"backward-adjusted from daily change_percent, anchored to latest raw close"}
    if latest_archive_day != day or sessions < 60:
        return pd.DataFrame(), stats
    return raw[["ts_code","trade_date","close","vol"]].copy(), stats


def _backfill_per_symbol(latest: pd.DataFrame, day: str) -> tuple[pd.DataFrame, dict]:
    limit = int(os.getenv("MAX_HISTORY_STOCKS", "100"))
    if limit == 0:
        limit = len(latest)
    latest = latest.copy()
    # Rotate through the full market universe deterministically. The cache
    # lets each symbol keep its 170-day history, while every run advances to
    # previously unprocessed codes instead of repeatedly sampling only the top 100.
    codes = sorted(set(latest["symbol"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)))
    cursor_path = DATA / "technical_history_cursor.json"
    try:
        cursor_state = json.loads(cursor_path.read_text(encoding="utf-8")) if cursor_path.exists() else {}
    except Exception:
        cursor_state = {}
    start_index = int(cursor_state.get("next_index", 0)) % max(1, len(codes))
    batch_size = min(limit, len(codes))
    selected_codes = [codes[(start_index + i) % len(codes)] for i in range(batch_size)] if codes else []
    cursor_next = (start_index + batch_size) % max(1, len(codes))
    cursor_path.write_text(json.dumps({"next_index":cursor_next,"universe_total":len(codes),
        "last_batch":selected_codes,"updated_for_trade_date":day}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    universe = latest[latest["symbol"].isin(selected_codes)].copy()
    day_obj = datetime.strptime(day, "%Y%m%d").date()
    full_start = (day_obj - timedelta(days=170)).strftime("%Y%m%d")
    cache_path = DATA / "technical_history.csv"
    if cache_path.exists():
        try:
            cache = pd.read_csv(cache_path, encoding="utf-8-sig", dtype={"ts_code": str, "trade_date": str})
        except Exception as exc:
            logging.warning("technical history cache unreadable; rebuilding: %s", exc)
            cache = pd.DataFrame(columns=["ts_code", "trade_date", "close", "vol"])
    else:
        cache = pd.DataFrame(columns=["ts_code", "trade_date", "close", "vol"])
    required = ["ts_code", "trade_date", "close", "vol"]
    if not set(required).issubset(cache.columns):
        cache = pd.DataFrame(columns=required)
    if not cache.empty:
        cache = cache[required].copy()
        cache["ts_code"] = cache["ts_code"].astype(str)
        cache["trade_date"] = cache["trade_date"].astype(str).str.replace(r"\.0$", "", regex=True).str[:8]
        cache["close"] = pd.to_numeric(cache["close"], errors="coerce")
        cache["vol"] = pd.to_numeric(cache["vol"], errors="coerce")
        cache = cache.dropna(subset=["ts_code", "trade_date", "close", "vol"])
        cache = cache.drop_duplicates(["ts_code", "trade_date"], keep="last")
    cached_symbols = set()
    requests_needed = []
    for _, row in universe.iterrows():
        code = str(row["symbol"]).zfill(6)
        ts_code = f"{code}.SH" if code.startswith(("5", "6", "9")) else (
            f"{code}.BJ" if code.startswith(("4", "8")) else f"{code}.SZ")
        old = cache[cache["ts_code"] == ts_code] if not cache.empty else pd.DataFrame()
        if not old.empty:
            last = pd.to_datetime(old["trade_date"], format="%Y%m%d", errors="coerce").max()
            sessions = old["trade_date"].nunique()
            if pd.notna(last) and last.strftime("%Y%m%d") >= day and sessions >= 60:
                cached_symbols.add(code)
                continue
            if pd.notna(last) and sessions >= 60:
                start = max(datetime.strptime(last.strftime("%Y%m%d"), "%Y%m%d").date() - timedelta(days=5),
                            day_obj - timedelta(days=170)).strftime("%Y%m%d")
            else:
                start = full_start
        else:
            start = full_start
        requests_needed.append((code, start))
    workers = max(1, min(int(os.getenv("HISTORY_WORKERS", "3")), 8))
    ak = _ak()
    histories = []
    failures = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_history_one, ak, code, day_obj, start): code
            for code, start in requests_needed
        }
        for future in as_completed(futures):
            try:
                part = future.result()
                if part is not None and not part.empty:
                    histories.append(part[required].copy())
            except Exception as exc:
                failures += 1
                if failures <= 10:
                    logging.warning("technical history %s failed: %s", futures[future], str(exc)[:220])
    if histories:
        fresh = pd.concat(histories, ignore_index=True)
        cache = pd.concat([cache, fresh], ignore_index=True)
    if not cache.empty:
        cache = cache.drop_duplicates(["ts_code", "trade_date"], keep="last").sort_values(["ts_code", "trade_date"])
        cache.to_csv(cache_path, index=False, encoding="utf-8-sig")
    stats = {"requested": len(requests_needed), "successful": len(histories), "failed": failures,
             "cache_hits": len(cached_symbols), "universe": len(universe),
             "universe_total": len(codes), "cursor_start": start_index, "cursor_next": cursor_next,
             "workers": workers, "window_days": 170}
    if cache.empty:
        return latest, stats
    raw = cache.drop_duplicates(["ts_code", "trade_date"], keep="last")
    tech = _technical_history(raw, day)
    if tech.empty:
        return latest, stats
    tech = tech.copy()
    tech["symbol"] = tech["ts_code"].astype(str).str.split(".").str[0].str.zfill(6)
    by_symbol = tech.set_index("symbol")
    for col in METRICS:
        if col in by_symbol:
            fresh_values = latest["symbol"].map(by_symbol[col])
            latest[col] = fresh_values.combine_first(latest[col] if col in latest else pd.Series(index=latest.index, dtype=float))
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
    try:
        archive_history, archive_stats = _archive_history(day)
    except Exception as exc:
        logging.warning("bulk daily quote archive failed; use per-symbol fallback: %s", str(exc)[:300])
        archive_history = pd.DataFrame()
        archive_stats = {"status":"failed","error":str(exc)[:300]}
    backfill_stats = {"requested": 0, "successful": 0, "failed": 0, "skipped": "bulk archive supplied full-market history"}
    archive_applied = False
    if not archive_history.empty:
        try:
            archive_tech = _technical_history(archive_history, day)
            if not archive_tech.empty:
                archive_tech = archive_tech.copy()
                archive_tech["symbol"] = archive_tech["ts_code"].astype(str).str.split(".").str[0].str.zfill(6)
                by_symbol = archive_tech.set_index("symbol")
                for col in METRICS:
                    if col in by_symbol:
                        latest[col] = latest["symbol"].map(by_symbol[col]).combine_first(
                            latest[col] if col in latest else pd.Series(index=latest.index, dtype=float))
                archive_applied = True
        except Exception as exc:
            logging.warning("bulk technical calculation failed; use per-symbol fallback: %s", str(exc)[:300])
            archive_stats["calculation_error"] = str(exc)[:300]
    if not archive_applied:
        latest, backfill_stats = _backfill_per_symbol(latest, day)
    latest.to_csv(LATEST, index=False, encoding="utf-8-sig")
    trade_date_iso = datetime.strptime(day, "%Y%m%d").date().isoformat()
    latest.to_csv(DATA / f"market_{trade_date_iso}.csv", index=False, encoding="utf-8-sig")
    coverage = {col: int(latest[col].notna().sum()) if col in latest else 0 for col in METRICS}
    result = {"trade_date": day, "stocks": len(latest),
        "history_sessions": archive_stats.get("history_sessions", snapshot_sessions),
        "snapshot_history_sessions": snapshot_sessions,
        "coverage": coverage, "bulk_archive": archive_stats,
        "per_symbol_history": backfill_stats,
        "method": "bulk daily quote archive with backward-adjusted returns; per-symbol bars are fallback only; no missing values fabricated"}
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
