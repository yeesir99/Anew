"""Small bounded retry helper for public research data providers."""
from __future__ import annotations

import time
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
    for attempt in range(1, attempts + 1):
        try:
            return operation_fn()
        except Exception as exc:
            errors.append(f"attempt {attempt}/{attempts}: {type(exc).__name__}: {str(exc)[:240]}")
            if attempt < attempts and delay_seconds:
                sleep(delay_seconds * attempt)
    raise RuntimeError(f"{operation} failed after {attempts} bounded attempt(s): " + " | ".join(errors))
