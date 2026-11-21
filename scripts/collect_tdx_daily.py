#!/usr/bin/env python3
"""Download and validate the official Tongdaxin SH/SZ/BJ daily-bar archive.

The 500+ MB ZIP is ephemeral: only a compact latest-bar audit table and a
coverage report are retained. Raw, unadjusted bars are NOT fed into scoring.
"""
from __future__ import annotations

import json
import logging
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
LATEST = DATA / "latest_market.csv"
OUT_CSV = DATA / "tdx_daily_latest.csv"
OUT_STATUS = DATA / "tdx_daily_status.json"
URL = "https://data.tdx.com.cn/vipdoc/hsjday.zip"
USER_AGENT = "A-Share-Alpha-Radar/2.1 (official TDX archive validation)"
DTYPE = np.dtype({
    "names": ["date", "open", "high", "low", "close", "amount", "volume", "reserved"],
    "formats": ["<u4", "<i4", "<i4", "<i4", "<i4", "<f4", "<u4", "<u4"],
    "offsets": [0, 4, 8, 12, 16, 20, 24, 28],
    "itemsize": 32,
})
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def remote_metadata() -> dict:
    response = requests.head(URL, timeout=(15, 45), headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    return {"etag": response.headers.get("ETag"),
            "last_modified": response.headers.get("Last-Modified"),
            "content_length": int(response.headers.get("Content-Length") or 0),
            "content_type": response.headers.get("Content-Type", "")}


def download_archive(path: Path) -> dict:
    response = requests.get(URL, stream=True, timeout=(20, 180),
                            headers={"User-Agent": USER_AGENT})
    response.raise_for_status()
    content_type = response.headers.get("Content-Type", "")
    total = 0
    with path.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 1024):
            if chunk:
                handle.write(chunk)
                total += len(chunk)
    if total < 10 * 1024 * 1024:
        raise RuntimeError(f"official archive unexpectedly small: {total} bytes; content-type={content_type}")
    if not zipfile.is_zipfile(path):
        raise RuntimeError(f"official endpoint did not return a ZIP archive; content-type={content_type}")
    return {"bytes": total, "content_type": content_type,
            "etag": response.headers.get("ETag"),
            "last_modified": response.headers.get("Last-Modified")}


def parse_archive(path: Path, allowed_ts_codes: set[str]) -> list[dict]:
    records = []
    seen_codes = set()
    with zipfile.ZipFile(path) as archive:
        members = [n for n in archive.namelist()
                   if n.lower().endswith(".day") and not n.endswith("/")]
        if len(members) < 1000:
            raise RuntimeError(f"archive contains too few .day files: {len(members)}")
        for index, member in enumerate(members, 1):
            name = member.replace("\\", "/").rsplit("/", 1)[-1]
            match = re.fullmatch(r"(sh|sz|bj)(\d{6})\.day", name, flags=re.I)
            if not match:
                continue
            market, code = match.group(1).lower(), match.group(2)
            ts_code = f"{code}.{market.upper()}"
            # TDX archives also contain index and other instrument .day files.
            # Restrict to the exact A-share universe from the validated market
            # snapshot, matching both code AND exchange to avoid index collisions.
            if ts_code not in allowed_ts_codes:
                continue
            try:
                raw = archive.read(member)  # zipfile verifies CRC on read
                if not raw or len(raw) % 32:
                    continue
                bars = np.frombuffer(raw, dtype=DTYPE)
                dates = bars["date"]
                valid = ((dates >= 19900101) & (dates <= 21000101) &
                         (bars["close"] > 0) & (bars["high"] > 0) &
                         (bars["low"] > 0) & np.isfinite(bars["amount"]))
                if not np.any(valid):
                    continue
                bars = bars[valid]
                # Files are expected to be chronological; sort defensively.
                order = np.argsort(bars["date"], kind="stable")
                bars = bars[order]
                last = bars[-1]
                records.append({
                    "symbol": code, "ts_code": ts_code,
                    "trade_date": str(int(last["date"])),
                    "open": round(float(last["open"]) / 100.0, 4),
                    "high": round(float(last["high"]) / 100.0, 4),
                    "low": round(float(last["low"]) / 100.0, 4),
                    "close": round(float(last["close"]) / 100.0, 4),
                    "amount": round(float(last["amount"]), 2),
                    "volume": int(last["volume"]),
                    "history_sessions": int(len(bars)),
                })
                seen_codes.add(code)
            except Exception as exc:
                logging.warning("skip corrupt TDX member %s: %s", member, str(exc)[:160])
            if index % 1000 == 0:
                logging.info("Parsed %d/%d daily files; valid securities=%d",
                             index, len(members), len(seen_codes))
    if len(records) < 1000:
        raise RuntimeError(f"parsed too few validated A-share securities from official archive: {len(records)}")
    return records


def main() -> None:
    if not LATEST.exists():
        raise SystemExit("latest_market.csv missing; run market collector first")
    market = pd.read_csv(LATEST, encoding="utf-8-sig", dtype={"symbol": str})
    if market.empty or not {"symbol", "ts_code", "trade_date", "close"}.issubset(market.columns):
        raise SystemExit("latest_market.csv lacks symbol/ts_code/trade_date/close")
    market["ts_code"] = market["ts_code"].astype(str).str.upper().str.strip()
    allowed_ts_codes = set(market["ts_code"].dropna())
    target_day = "".join(ch for ch in str(market.iloc[0]["trade_date"]) if ch.isdigit())[:8]
    if len(target_day) != 8:
        raise SystemExit(f"invalid target trade date: {target_day!r}")

    DATA.mkdir(parents=True, exist_ok=True)
    previous = {}
    if OUT_STATUS.exists():
        try:
            previous = json.loads(OUT_STATUS.read_text(encoding="utf-8"))
        except Exception:
            previous = {}
    remote = remote_metadata()
    unchanged = (OUT_CSV.exists() and remote.get("etag") and
                 remote.get("etag") == previous.get("archive_etag"))
    if unchanged:
        logging.info("Official archive ETag unchanged; reusing validated local extract")
        tdx = pd.read_csv(OUT_CSV, encoding="utf-8-sig", dtype={"symbol": str})
        download = {"bytes": previous.get("archive_bytes", 0),
                    "content_type": remote.get("content_type"),
                    "etag": remote.get("etag"),
                    "last_modified": remote.get("last_modified"),
                    "download_skipped": True}
    else:
        with tempfile.TemporaryDirectory(prefix="tdx-hsjday-") as temp:
            archive_path = Path(temp) / "hsjday.zip"
            download = download_archive(archive_path)
            records = parse_archive(archive_path, allowed_ts_codes)
        tdx = pd.DataFrame(records).sort_values("symbol")
        tdx.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    # Re-apply the exact universe filter on cached extracts too, so an
    # older pre-filter CSV cannot survive merely because the remote ETag
    # has not changed.
    tdx["ts_code"] = tdx["ts_code"].astype(str).str.upper().str.strip()
    tdx = tdx[tdx["ts_code"].isin(allowed_ts_codes)].drop_duplicates("ts_code", keep="last")
    if len(tdx) < 1000:
        raise RuntimeError(f"cached/parsed TDX extract has too few matching A-share codes: {len(tdx)}")
    tdx.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")
    joined = market[["ts_code", "close"]].copy()
    joined["ts_code"] = joined["ts_code"].astype(str).str.upper().str.strip()
    joined["close"] = pd.to_numeric(joined["close"], errors="coerce")
    compare = tdx.merge(joined, on="ts_code", how="inner", suffixes=("_tdx", "_market"))
    same_day = compare[compare["trade_date"] == target_day].copy()
    same_day["abs_close_diff"] = (same_day["close_tdx"] - same_day["close_market"]).abs()
    comparable = same_day.dropna(subset=["abs_close_diff"])
    mismatch = int((comparable["abs_close_diff"] > 0.03).sum())
    latest_date = str(tdx["trade_date"].max())
    session_median = float(tdx["history_sessions"].median())
    status = {
        "schema_version": 2,
        "source": "Tongdaxin official hsjday.zip",
        "source_url": URL,
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target_trade_date": target_day,
        "archive_latest_trade_date": latest_date,
        "archive_bytes": download["bytes"],
        "download_skipped_unchanged": bool(download.get("download_skipped", False)),
        "archive_content_type": download["content_type"],
        "archive_etag": download["etag"],
        "archive_last_modified": download["last_modified"],
        "archive_files_parsed": int(len(tdx)),
        "market_symbols": int(market["symbol"].nunique()),
        "symbols_in_archive": int(tdx["symbol"].nunique()),
        "same_day_price_comparisons": int(len(comparable)),
        "same_day_close_mismatches_over_0_03": mismatch,
        "median_history_sessions": round(session_median, 1),
        "status": "validated" if latest_date >= target_day and len(comparable) >= 1000 else "stale_or_partial",
        "usage_policy": "unadjusted reference data only; not injected into scoring or adjusted technical indicators",
        "notes": [
            "Official full daily-bar archive parsed and CRC-checked by ZIP member reads.",
            "Prices and volumes are retained as a secondary-source audit, not treated as adjusted series.",
            "A corporate-action adjustment source is required before using this history for long-window technical scoring."
        ],
    }
    OUT_STATUS.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(status, ensure_ascii=False))


if __name__ == "__main__":
    main()
