"""Retry transient retrieval failures without multiplying the request budget."""

import asyncio
from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest

from dvp.toolkits.retrieval import explore_retry as retry


def status_error(status, retry_after=None):
    request = httpx.Request("GET", "https://example.invalid/search")
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    response = httpx.Response(status, headers=headers, request=request)
    return httpx.HTTPStatusError("request failed", request=request, response=response)


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=100.0, sleeps=[])

    async def sleep(delay):
        state.sleeps.append(delay)
        state.now += delay

    monkeypatch.setattr(retry, "get_running_loop", lambda: SimpleNamespace(time=lambda: state.now))
    monkeypatch.setattr(retry, "sleep", sleep)
    monkeypatch.setattr(retry, "uniform", lambda _lower, upper: upper)
    return state


@pytest.mark.parametrize("error", [
    httpx.ConnectError("temporary connection failure"),
    httpx.ReadError("connection interrupted"),
    httpx.ReadTimeout("read timed out"),
    httpx.RemoteProtocolError("peer disconnected"),
    *(status_error(status) for status in (408, 429, 500, 502, 503, 504)),
])
def test_transient_failure_recovers(error, clock):
    calls = []
    result = object()

    async def request():
        calls.append(True)
        if len(calls) == 1:
            raise error
        return result

    assert asyncio.run(retry.retry_request(request, timeout=10)) is result
    assert len(calls) == 2
    assert clock.sleeps == [0.5]


@pytest.mark.parametrize("error", [
    *(status_error(status) for status in (400, 401, 403, 404, 501)),
    httpx.UnsupportedProtocol("invalid URL scheme"),
    httpx.LocalProtocolError("invalid request"),
    ValueError("invalid result"),
])
def test_permanent_failure_propagates_without_retry(error, clock):
    calls = []

    async def request():
        calls.append(True)
        raise error

    with pytest.raises(type(error)) as raised:
        asyncio.run(retry.retry_request(request, timeout=10))
    assert raised.value is error
    assert len(calls) == 1
    assert clock.sleeps == []


@pytest.mark.parametrize("header", [
    "3",
    format_datetime(datetime.fromtimestamp(1_700_000_003, timezone.utc), usegmt=True),
])
def test_rate_limit_honors_seconds_and_http_date(header, clock, monkeypatch):
    monkeypatch.setattr(retry, "time", lambda: 1_700_000_000)
    calls = []

    async def request():
        calls.append(clock.now)
        if len(calls) == 1:
            raise status_error(429, header)
        return "ok"

    assert asyncio.run(retry.retry_request(request, timeout=10)) == "ok"
    assert calls == [100.0, 103.0]
    assert clock.sleeps == [3.0]


def test_retry_after_beyond_budget_does_not_request_early(clock):
    error = status_error(429, "30")
    calls = []

    async def request():
        calls.append(True)
        raise error

    with pytest.raises(httpx.HTTPStatusError) as raised:
        asyncio.run(retry.retry_request(request, timeout=5))
    assert raised.value is error
    assert len(calls) == 1
    assert clock.sleeps == []


@pytest.mark.parametrize("header", ["nonsense", "nan", "inf", "-4"])
def test_invalid_retry_after_falls_back_to_backoff(header, clock):
    calls = []

    async def request():
        calls.append(True)
        if len(calls) == 1:
            raise status_error(429, header)
        return "ok"

    assert asyncio.run(retry.retry_request(request, timeout=10)) == "ok"
    assert clock.sleeps == [0.5]


def test_exhausted_retries_preserve_last_error_and_exponential_backoff(clock):
    errors = [httpx.ConnectError(str(i)) for i in range(3)]
    calls = []

    async def request():
        calls.append(True)
        raise errors[len(calls) - 1]

    with pytest.raises(httpx.ConnectError) as raised:
        asyncio.run(retry.retry_request(request, timeout=10))
    assert raised.value is errors[-1]
    assert len(calls) == 3
    assert clock.sleeps == [0.5, 1.0]


def test_attempt_time_and_backoff_share_the_budget(clock):
    error = httpx.ConnectError("unavailable")
    calls = []

    async def request():
        calls.append(True)
        clock.now += 2
        raise error

    with pytest.raises(httpx.ConnectError) as raised:
        asyncio.run(retry.retry_request(request, timeout=5))
    assert raised.value is error
    assert len(calls) == 2
    assert clock.sleeps == [0.5]


def test_external_cancellation_stops_active_attempt(clock):
    calls = []
    cancelled = []

    async def scenario():
        started = asyncio.Event()

        async def request():
            calls.append(True)
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.append(True)

        task = asyncio.create_task(retry.retry_request(request, timeout=10))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert calls == [True]
    assert cancelled == [True]
    assert clock.sleeps == []


def test_total_timeout_cancels_hung_request(clock):
    calls = []
    cancelled = []

    async def request():
        calls.append(True)
        try:
            await asyncio.Future()
        finally:
            cancelled.append(True)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(retry.retry_request(request, timeout=0.001))
    assert calls == [True]
    assert cancelled == [True]
    assert clock.sleeps == []


def test_expired_deadline_never_starts_request(clock):
    async def request():
        pytest.fail("An expired budget must not issue a request")

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(retry.retry_request(request, timeout=10, deadline=clock.now))


def test_deadline_can_be_shared_without_relative_timeout(clock):
    async def request():
        return "ok"

    assert asyncio.run(retry.retry_request(request, deadline=clock.now + 10)) == "ok"
