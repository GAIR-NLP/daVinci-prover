"""Bounded retries for transient LeanExplore and source HTTP failures."""

from __future__ import annotations

from asyncio import TimeoutError, get_running_loop, sleep, wait_for
from collections.abc import Awaitable, Callable
from datetime import timezone
from email.utils import parsedate_to_datetime
from math import isfinite
from random import uniform
from time import time
from typing import TypeVar

import httpx


_T = TypeVar("_T")
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})
_RETRYABLE_TRANSPORT = (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError)


def _retry_after(error: httpx.HTTPStatusError) -> float:
    value = error.response.headers.get("Retry-After", "")
    try:
        seconds = float(value)
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = date.timestamp() - time()
        except (TypeError, ValueError, OverflowError):
            return 0.0
    return max(0.0, seconds) if isfinite(seconds) else 0.0


async def retry_request(
    call: Callable[[], Awaitable[_T]],
    *,
    timeout: float | None = None,
    deadline: float | None = None,
    retries: int = 2,
) -> _T:
    """Retry at most twice within one budget, preserving the final HTTP error.

    ``deadline`` uses the running event loop's monotonic clock. Pass it to share
    a budget across requests; if ``timeout`` is also given, the earlier bound
    wins. One timeout covers all attempts and backoff sleeps. A Retry-After
    that cannot fit the remaining budget immediately propagates its HTTP error.
    Cancellation and errors other than the listed transient failures propagate.
    """
    if not 0 <= retries <= 2:
        raise ValueError("retries must be between zero and two")
    clock = get_running_loop().time
    if timeout is not None:
        timeout_deadline = clock() + timeout
        deadline = timeout_deadline if deadline is None else min(deadline, timeout_deadline)
    if deadline is None or not isfinite(deadline):
        raise ValueError("A finite timeout or deadline is required")
    remaining = deadline - clock()
    if remaining <= 0:
        raise TimeoutError("LeanExplore request exceeded its time limit")

    async def attempt_request() -> _T:
        for attempt in range(retries + 1):
            if clock() >= deadline:
                raise TimeoutError("LeanExplore request exceeded its time limit")
            try:
                return await call()
            except (httpx.HTTPStatusError, *_RETRYABLE_TRANSPORT) as error:
                if attempt == retries:
                    raise
                if isinstance(error, httpx.HTTPStatusError):
                    if error.response.status_code not in _RETRYABLE_STATUS:
                        raise
                    retry_after = _retry_after(error)
                else:
                    retry_after = 0.0
                backoff = 0.25 * 2**attempt
                delay = max(backoff + uniform(0.0, backoff), retry_after)
                if delay >= deadline - clock():
                    raise
                await sleep(delay)
        raise AssertionError("Retry loop must return or raise")

    return await wait_for(attempt_request(), timeout=remaining)
