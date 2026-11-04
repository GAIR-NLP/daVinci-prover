"""Offline checks for cache measurement and credential-safe reporting."""

import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "tools/diagnostics/probe_llm_cache.py"
SPEC = importlib.util.spec_from_file_location("probe_llm_cache", SCRIPT)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def sse(*events):
    return "\n\n".join(
        "data: " + (event if isinstance(event, str) else json.dumps(event))
        for event in events
    ) + "\n\n"


def test_role_only_is_not_first_token_and_usage_sources_stay_distinct():
    body = sse(
        {"choices": [{"delta": {"role": "assistant", "content": ""}}]},
        {"choices": [{"delta": {"reasoning_content": "think"}}]},
        {"model": "test-model", "choices": [{"delta": {"content": "OK"},
                                               "finish_reason": "length"}]},
        {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 5,
                                   "prompt_tokens_details": {"cached_tokens": 80},
                                   "cache_read_input_tokens": 120,
                                   "completion_tokens_details": {"reasoning_tokens": 3},
                                   "arbitrary_text": "do not report this"}},
        "[DONE]",
    )
    times = iter([0.0, 1.0, 4.0, 6.0])
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=body)
        )) as client:
            return await probe.probe_once(client, "https://test.invalid/v1/chat/completions", {},
                                          "dummy-key", clock=lambda: next(times))
    record = asyncio.run(run())
    assert record["headers_seconds"] == 1
    assert record["ttft_seconds"] == 4
    assert record["total_seconds"] == 6
    assert record["response_delta_chars"] == 7
    assert record["usage"]["completion_tokens_details"]["reasoning_tokens"] == 3
    assert "arbitrary_text" not in record["usage"]
    assert record["cache_metrics"]["usage.prompt_tokens_details.cached_tokens"] == {
        "tokens": 80, "prompt_token_ratio": 0.8,
    }
    assert record["cache_metrics"]["usage.cache_read_input_tokens"] == {
        "tokens": 120, "prompt_token_ratio": None,
    }
    assert record["cache_metrics"]["usage.prompt_cache_hit_tokens"]["tokens"] is None
    assert record["finish_reason"] == "length"
    assert record["stream_done"] and record["error"] is None


@pytest.mark.parametrize("usage", [None, {"prompt_tokens": 10},
                                   {"prompt_tokens_details": {"cached_tokens": 0}}])
def test_missing_usage_or_cache_is_unknown(usage):
    body = sse({"choices": [], "usage": usage}, "[DONE]")
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=body)
        )) as client:
            return await probe.probe_once(client, "https://test.invalid/v1/chat/completions", {}, "dummy-key")
    record = asyncio.run(run())
    assert record["usage"] == usage
    assert record["ttft_seconds"] is None
    metric = record["cache_metrics"]["usage.prompt_tokens_details.cached_tokens"]
    assert metric["tokens"] == (0 if usage and "prompt_tokens_details" in usage else None)
    assert metric["prompt_token_ratio"] is None


@pytest.mark.parametrize("failure", ["http", "api", "disconnect", "timeout"])
def test_error_stops_repeats_without_echoing_secrets(failure):
    requests = []
    secret = "a-private-test-token"

    def respond(request):
        requests.append(request)
        if failure == "http":
            return httpx.Response(401, text=f"Invalid credential: {secret}")
        if failure == "api":
            return httpx.Response(200, text=sse({"error": {"message": secret}}))
        if failure == "timeout":
            raise httpx.ReadTimeout(f"Header contained {secret}")
        return httpx.Response(200, text=sse({"choices": [{"delta": {"content": secret}}]}))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await probe.run_probes(client, "https://test.invalid/v1/chat/completions", {}, secret, 3)
    records = asyncio.run(run())
    assert len(requests) == len(records) == 1
    assert records[0]["error"]
    assert secret not in json.dumps(records)


def test_cli_uses_identical_requests_new_nonce_per_run_and_saves_no_prompt_or_secret(
    monkeypatch, tmp_path, capsys,
):
    requests = []
    routing_keys = []
    client_factory = httpx.AsyncClient
    secret = "a-private-test-token"

    def respond(request):
        requests.append(json.loads(request.content))
        routing_keys.append(request.headers.get("X-SMG-Routing-Key"))
        assert request.url.path == "/v1/chat/completions"
        completed_this_run = (len(requests) - 1) % 3
        if completed_this_run:
            assert len(json.loads(output.read_text())["records"]) == completed_this_run
        return httpx.Response(200, text=sse(
            {"model": secret, "choices": [{"delta": {"tool_calls": [
                {"function": {"arguments": "{}"}}
            ]}, "finish_reason": "tool_calls"}]}, "[DONE]",
        ))

    def make_client(**kwargs):
        assert kwargs["trust_env"] is False
        return client_factory(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr(probe.httpx, "AsyncClient", make_client)
    monkeypatch.setenv("PROBE_TEST_TOKEN", secret)
    output = tmp_path / "report.json"
    args = ["--base-url", "https://test.invalid", "--model", "test-model",
            "--api-key-env", "PROBE_TEST_TOKEN", "--output", str(output)]
    assert probe.main(args) == 0
    first_report = json.loads(output.read_text())
    assert len(first_report["records"]) == 3
    assert requests[0] == requests[1] == requests[2]
    assert routing_keys[0] == routing_keys[1] == routing_keys[2]
    assert routing_keys[0] == first_report["metadata"]["routing_key"]
    assert routing_keys[0]
    assert first_report["metadata"]["http_client_reused_across_requests"] is True
    assert first_report["records"][0]["ttft_seconds"] is not None
    assert first_report["records"][0]["model"] == "[REDACTED]"
    assert probe.main(args) == 0
    second_report = json.loads(output.read_text())
    assert requests[3] == requests[4] == requests[5]
    assert routing_keys[3] == routing_keys[4] == routing_keys[5]
    assert routing_keys[3] != routing_keys[0]
    assert second_report["metadata"]["probe_id"] != first_report["metadata"]["probe_id"]
    assert requests[0]["messages"] != requests[3]["messages"]
    printed = capsys.readouterr().out
    assert secret not in printed + output.read_text()
    assert requests[0]["messages"][0]["content"] not in printed + output.read_text()


@pytest.mark.parametrize("routing_options, expected_key", [
    (["--routing-key", "stable-test-route"], "stable-test-route"),
    (["--no-routing-key"], None),
])
def test_explicit_probe_id_reproduces_prompt_and_routing_choice(
    monkeypatch, tmp_path, routing_options, expected_key,
):
    requests = []
    client_factory = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        return httpx.Response(200, text=sse("[DONE]"))

    monkeypatch.setattr(probe.httpx, "AsyncClient", lambda **kwargs: client_factory(
        transport=httpx.MockTransport(respond), **kwargs,
    ))
    monkeypatch.setenv("PROBE_TEST_TOKEN", "dummy-test-key")
    output = tmp_path / "report.json"
    args = ["--base-url", "https://test.invalid/v1", "--model", "test-model",
            "--api-key-env", "PROBE_TEST_TOKEN", "--probe-id", "repeatable-probe",
            "--repeats", "2", "--output", str(output), *routing_options]
    assert probe.main(args) == 0
    first_report = json.loads(output.read_text())
    assert probe.main(args) == 0
    second_report = json.loads(output.read_text())
    assert len(requests) == 4
    assert len({request.content for request in requests}) == 1
    assert {request.headers.get("X-SMG-Routing-Key") for request in requests} == {expected_key}
    assert first_report["metadata"]["routing_key"] == expected_key
    assert first_report["metadata"]["prompt_sha256"] == second_report["metadata"]["prompt_sha256"]
    assert first_report["metadata"]["probe_id"] == "repeatable-probe"


def test_run_probes_default_generates_one_routing_key_for_all_repeats():
    routing_keys = []

    def respond(request):
        routing_keys.append(request.headers.get("X-SMG-Routing-Key"))
        return httpx.Response(200, text=sse("[DONE]"))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            for _ in range(2):
                await probe.run_probes(client, "https://test.invalid/v1/chat/completions", {},
                                       "dummy-key", 2)
    asyncio.run(run())
    assert routing_keys[0] == routing_keys[1]
    assert routing_keys[2] == routing_keys[3]
    assert routing_keys[0] and routing_keys[2] and routing_keys[0] != routing_keys[2]


def test_missing_key_without_terminal_fails_before_request(monkeypatch, capsys):
    monkeypatch.delenv("PROBE_TEST_MISSING_TOKEN", raising=False)
    monkeypatch.setattr(probe.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit) as raised:
        probe.main(["--base-url", "https://test.invalid/v1", "--model", "test-model",
                    "--api-key-env", "PROBE_TEST_MISSING_TOKEN"])
    assert raised.value.code == 2
    assert "no interactive terminal" in capsys.readouterr().err


def test_heartbeats_do_not_extend_overall_deadline():
    requests = []

    class Heartbeats(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                yield b": keepalive\n\n"
                await asyncio.sleep(0.005)

    def respond(request):
        requests.append(request)
        return httpx.Response(200, stream=Heartbeats())

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await probe.run_probes(client, "https://test.invalid/v1/chat/completions", {},
                                          "dummy-key", 3, timeout=0.05)
    records = asyncio.run(run())
    assert len(requests) == len(records) == 1
    assert "Overall request timeout" in records[0]["error"]
    assert records[0]["ttft_seconds"] is None
    assert records[0]["total_seconds"] < 1
