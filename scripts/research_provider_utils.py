"""Small bounded retry helper for public research data providers."""
from __future__ import annotations

import time
import re

import requests
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def call_with_retry(
    operation_fn: Callable[[], T],
    *,
    operation: str,
    attempts: int = 2,
    delay_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    should_retry: Callable[[Exception], bool] | None = None,
) -> T:
    """Retry a provider transport call a bounded number of times.

    Schema/value validation intentionally happens after this helper; malformed
    successful responses are not retried as if they were transient network errors.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    if delay_seconds < 0:
        raise ValueError("delay_seconds cannot be negative")
    errors = []
    attempts_made = 0
    for attempt in range(1, attempts + 1):
        attempts_made = attempt
        try:
            return operation_fn()
        except Exception as exc:
            errors.append(f"attempt {attempt}/{attempts}: {type(exc).__name__}: {str(exc)[:240]}")
            retry_allowed = should_retry(exc) if should_retry is not None else True
            if attempt < attempts and delay_seconds and retry_allowed:
                sleep(delay_seconds * attempt)
            elif not retry_allowed:
                break
    raise RuntimeError(f"{operation} failed after {attempts_made} bounded attempt(s): " + " | ".join(errors))


def fetch_eastmoney_concept_members(
    concept_name: str,
    *,
    get=None,
    timeout_seconds: float = 12.0,
) -> tuple[list[dict], dict]:
    """Fetch an exact EastMoney concept board and its constituents via mirror hosts.

    AkShare's wrapper uses the primary push2 host only. Public mirror hosts are
    tried in a bounded order so a single 502/timeout does not erase valid data.
    Exact concept-name matching is mandatory; parent/alias substitution is not.
    """
    if get is None:
        def get(url, **kwargs):
            try:
                from curl_cffi import requests as curl_requests
                response = curl_requests.get(url, impersonate="chrome", **kwargs)
                response.raise_for_status()
                return response
            except Exception as curl_error:
                try:
                    response = requests.get(url, **kwargs)
                    response.raise_for_status()
                    return response
                except Exception as requests_error:
                    raise RuntimeError(
                        f"EastMoney transport failed via curl_cffi and requests: "
                        f"{type(curl_error).__name__}: {str(curl_error)[:120]} | "
                        f"{type(requests_error).__name__}: {str(requests_error)[:120]}"
                    ) from requests_error
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
        "Referer": "https://quote.eastmoney.com/",
        "Accept": "application/json, text/plain, */*",
    }
    hosts = ("push2delay.eastmoney.com", "82.push2.eastmoney.com",
             "79.push2.eastmoney.com", "29.push2.eastmoney.com",
             "push2.eastmoney.com")
    list_params = {
        "pn": 1, "pz": 500, "po": 1, "np": 1, "fltt": 2, "invt": 2,
        "fs": "m:90+t:3+f:!50", "fields": "f12,f14",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
    }
    list_param_variants = (
        list_params,
        {**list_params, "fs": "m:90+t:3"},
    )

    def fetch_diff(host, params, operation):
        url = f"https://{host}/api/qt/clist/get"
        response = get(url, params=params, headers=headers, timeout=timeout_seconds)
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise ValueError(f"{operation}: missing data object from {host}")
        diff = data.get("diff")
        if not isinstance(diff, list):
            raise ValueError(f"{operation}: missing diff array from {host}")
        return diff, data, url

    normal = lambda value: re.sub(r"[\s·（）()ⅢⅣ]", "", str(value or "")).lower()
    target = normal(concept_name)
    matched = None
    list_errors = []
    taxonomy_response_received = False
    for host in hosts:
        for params_variant in list_param_variants:
            try:
                diff, _, _ = fetch_diff(host, params_variant, "EastMoney concept taxonomy")
                taxonomy_response_received = True
                matched = next((row for row in diff
                                if normal(row.get("f14")) == target), None)
                if matched:
                    break
            except Exception as exc:
                list_errors.append(f"{host}: {type(exc).__name__}: {str(exc)[:160]}")
        if matched:
            break
    if not matched:
        if list_errors and not taxonomy_response_received:
            raise RuntimeError("EastMoney concept taxonomy mirrors failed: " + " | ".join(list_errors))
        raise LookupError(f"exact EastMoney concept name not found: {concept_name}")

    board_code = str(matched.get("f12") or "").strip()
    matched_name = str(matched.get("f14") or "").strip()
    if not board_code or not matched_name:
        raise ValueError("EastMoney exact concept row missing board code/name")

    member_params = {
        "pn": 1, "pz": 1000, "po": 1, "np": 1, "fltt": 2, "invt": 2,
        "fs": f"b:{board_code}", "fields": "f12,f14",
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
    }
    member_hosts = ("push2delay.eastmoney.com", "29.push2.eastmoney.com",
                    "82.push2.eastmoney.com", "79.push2.eastmoney.com",
                    "push2.eastmoney.com")
    rows = []
    page = 1
    total = None
    member_errors = []
    while True:
        page_params = {**member_params, "pn": page}
        page_rows = None
        for host in member_hosts:
            try:
                diff, data, source_url = fetch_diff(host, page_params, "EastMoney concept constituents")
                if not diff:
                    member_errors.append(f"{host}: empty constituent page")
                    continue
                page_rows = diff
                if total is None:
                    total = int(data.get("total") or len(diff))
                break
            except Exception as exc:
                member_errors.append(f"{host}: {type(exc).__name__}: {str(exc)[:160]}")
        if page_rows is None:
            raise RuntimeError("EastMoney constituent mirrors failed: " + " | ".join(member_errors[-8:]))
        if not page_rows:
            break
        for row in page_rows:
            code = str(row.get("f12") or "").strip().zfill(6)
            name = str(row.get("f14") or "").strip()
            if len(code) == 6 and code.isdigit():
                rows.append({"code": code, "name": name or None})
        if total is not None and len(rows) >= total:
            break
        if len(page_rows) < int(member_params["pz"]):
            break
        page += 1
        if page > 20:
            raise ValueError("EastMoney concept constituent pagination exceeded 20 pages")

    deduped = {row["code"]: row for row in rows}
    members = [deduped[key] for key in sorted(deduped)]
    if not members:
        raise ValueError(f"EastMoney exact concept returned no constituents: {matched_name}")
    source_url = f"https://quote.eastmoney.com/center/boardlist.html#boards2-90.{board_code}"
    return members, {
        "status": "success",
        "records": len(members),
        "provider": "EastMoney",
        "taxonomy": "EastMoney exact concept board",
        "taxonomy_node_id": board_code,
        "taxonomy_node_name": matched_name,
        "matched_symbol": matched_name,
        "mapping_source": "EastMoney exact concept taxonomy via mirror hosts",
        "source_url": source_url,
        "mirror_hosts": list(hosts),
    }
