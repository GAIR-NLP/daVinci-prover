"""Exercise LiteLLM through a mock HTTP endpoint, including history replay."""

import asyncio
from copy import deepcopy
import json

import httpx
import litellm
import pytest

import dvp.llm_clients.client as llm


MESSAGES = [{"role": "user", "content": "prove the target"}]
TOOLS = [{"type": "function", "function": {
    "name": "file_read", "parameters": {"type": "object", "properties": {}},
}}]


def install_transport(monkeypatch, handler):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(llm, "AsyncClient", lambda **kw: original(transport=transport, **kw))


def make_client(**kwargs):
    return llm.LiteLLMClient(model="test", api_key="test-key", base_url="https://mock.invalid/v1", **kwargs)


def reply(message=None, **overrides):
    return httpx.Response(200, json={
        "id": "completion-1", "object": "chat.completion", "created": 1, "model": "served-test",
        "choices": [{"index": 0, "message": message or {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23},
        **overrides,
    })


@pytest.mark.parametrize("penalty", [None, 0.0, 0.4])
def test_sampling_omits_unset_fields_and_preserves_zero(monkeypatch, penalty):
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return reply()
    install_transport(monkeypatch, handler)
    client = make_client(presence_penalty=penalty, max_retries=0)
    assert asyncio.run(client.chat(messages=MESSAGES, tools=[]))["content"] == "ok"
    body = requests[0]
    assert body["model"] == "test" and body["messages"] == MESSAGES
    assert "temperature" not in body and "max_tokens" not in body
    if penalty is None:
        assert "presence_penalty" not in body
    else:
        assert body["presence_penalty"] == penalty
    assert body.get("stream", False) is False


def test_self_hosted_extensions_and_reasoning_tool_history_survive_wire(monkeypatch):
    bodies = []
    message = {
        "role": "assistant", "content": "", "reasoning_content": "Need to inspect the file.",
        "tool_calls": [{"id": "call-1", "type": "function", "function": {
            "name": "edit", "arguments": '{"broken":',
        }}],
    }
    def handler(request):
        bodies.append(json.loads(request.content))
        return reply(message)
    install_transport(monkeypatch, handler)
    client = make_client(
        max_retries=0, temperature=1.0, top_p=0.95, max_tokens=32768,
        reasoning_effort="high", thinking="enabled", chat_template_thinking=True,
        chat_template_reasoning_effort="high", stop_token_ids=[123],
    )
    async def run():
        result = await client.chat(messages=MESSAGES, tools=TOOLS)
        # Unknown names and invalid argument JSON must reach DVP's recovery logic.
        assert result["reasoning_content"] == message["reasoning_content"]
        assert result["tool_calls"][0]["function"] == message["tool_calls"][0]["function"]
        repaired = deepcopy(result)
        repaired["tool_calls"][0]["function"]["arguments"] = "{}"
        history = [*MESSAGES, repaired, {"role": "tool", "tool_call_id": "call-1", "content": "Unknown tool edit"}]
        await client.chat(messages=history, tools=TOOLS)
        assert bodies[-1]["messages"] == history
    asyncio.run(run())
    for body in bodies:
        assert body["max_tokens"] == 32768 and body["reasoning_effort"] == "high"
        assert body["thinking"] == {"type": "enabled"}
        assert body["chat_template_kwargs"] == {"thinking": True, "enable_thinking": True, "reasoning_effort": "high"}
        assert body["stop_token_ids"] == [123] and body["tools"] == TOOLS
        assert body["tool_choice"] == "auto"


@pytest.mark.parametrize("failure", [429, 503, "transport"])
def test_transient_failure_retries_unchanged_request_once(monkeypatch, failure):
    requests, keys = [], []
    def handler(request):
        requests.append(json.loads(request.content))
        keys.append(request.headers["X-SMG-Routing-Key"])
        if len(requests) == 1:
            if failure == "transport":
                raise httpx.ConnectError("temporarily unavailable", request=request)
            return httpx.Response(failure, json={"error": {"message": "temporarily unavailable"}})
        return reply()
    install_transport(monkeypatch, handler)
    client = make_client(max_retries=1, max_tokens=512)
    assert asyncio.run(client.chat(messages=MESSAGES, tools=TOOLS))["content"] == "ok"
    assert len(requests) == 2 and requests[0] == requests[1]
    assert keys == [client.routing_key] * 2
    assert client.generation_records[0]["sdk_request_count"] == 2


@pytest.mark.parametrize("status,error,exception", [
    (400, "This model's maximum context length is 1000 tokens. Your request has 700 input tokens and 512 output tokens.", litellm.ContextWindowExceededError),
    (400, "Unsupported temperature parameter", litellm.BadRequestError),
    (401, "Invalid API key", litellm.AuthenticationError),
    (403, "Permission denied", litellm.APIError),
    (403, "Inspire Auth check: statuscode: 504", litellm.APIError),
])
def test_permanent_errors_are_not_repaired_or_retried(monkeypatch, status, error, exception):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"error": {"message": error}})
    install_transport(monkeypatch, handler)
    client = make_client(max_tokens=512, max_retries=3)
    with pytest.raises(exception):
        asyncio.run(client.chat(messages=MESSAGES, tools=TOOLS))
    assert len(requests) == 1 and requests[0].url.path == "/v1/chat/completions"
    assert json.loads(requests[0].content)["max_tokens"] == client.max_tokens == 512
    assert client.generation_records[0]["error_type"] == exception.__name__


def test_exhausted_transient_error_retains_sdk_type_and_request_budget(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(503, json={"error": {"message": "unavailable"}})
    install_transport(monkeypatch, handler)
    client = make_client(max_retries=1)
    with pytest.raises(litellm.ServiceUnavailableError) as caught:
        asyncio.run(client.chat(messages=MESSAGES, tools=TOOLS))
    assert llm.is_retryable_model_error(caught.value)
    assert len(requests) == 2
    record = client.generation_records[0]
    assert record["status"] == "error" and record["http_status"] == 503
    assert record["sdk_request_count"] == 2


def test_routing_affinity_is_per_client_and_reused_between_turns(monkeypatch):
    keys = []
    def handler(request):
        keys.append(request.headers["X-SMG-Routing-Key"])
        return reply()
    install_transport(monkeypatch, handler)
    first, second = make_client(), make_client(routing_key="task-2")
    async def run():
        for client in (first, first, second):
            await client.chat(messages=MESSAGES, tools=[])
    asyncio.run(run())
    assert keys == [first.routing_key, first.routing_key, "task-2"]
    assert first.routing_key != second.routing_key


@pytest.mark.parametrize("key", ["", "a b", "a\nb", "会话"])
def test_invalid_routing_key_fails_before_sending(key):
    with pytest.raises(ValueError, match="routing_key"):
        make_client(routing_key=key)


def test_usage_and_response_identity_remain_separate_from_model_history(monkeypatch):
    usage = {"prompt_tokens": 100, "completion_tokens": 8, "total_tokens": 108,
             "prompt_tokens_details": {"cached_tokens": 60}, "prompt_cache_hit_tokens": 60}
    install_transport(monkeypatch, lambda request: reply(usage=usage, system_fingerprint="worker-1"))
    client = make_client(max_tokens=32)
    result = asyncio.run(client.chat(messages=MESSAGES, tools=[]))
    record = client.generation_records[0]
    assert result == {"role": "assistant", "content": "ok"}
    assert record["usage"]["prompt_tokens_details"]["cached_tokens"] == 60
    assert record["usage"]["prompt_cache_hit_tokens"] == 60
    assert record["observed_model"] == "served-test" and record["response_id"] == "completion-1"
    assert record["system_fingerprint"] == "worker-1" and record["finish_reason"] == "stop"
    assert record["request_parameters"] == {"max_tokens": 32}
    assert record["backend"] == "litellm" and record["sdk_model"] == "openai/test"
    assert record["elapsed_seconds"] >= 0
    assert "prove the target" not in json.dumps(record) and "test-key" not in json.dumps(record)


@pytest.mark.parametrize("model,expected", [
    ("my-model", "openai/my-model"), ("Qwen/model", "openai/Qwen/model"),
    ("openai/my-model", "openai/my-model"), ("anthropic/claude-sonnet-4-5", "anthropic/claude-sonnet-4-5"),
    ("deepseek/deepseek-reasoner", "deepseek/deepseek-reasoner"),
])
def test_model_names_support_existing_endpoints_and_explicit_providers(model, expected):
    client = llm.LiteLLMClient(model=model)
    assert client.sdk_model == expected
    assert client.base_url == ("https://api.openai.com/v1" if expected.startswith("openai/") else None)


def test_native_provider_delegates_parameters_to_litellm(monkeypatch):
    seen = []
    async def complete(**kwargs):
        seen.append(kwargs)
        return litellm.ModelResponse(choices=[{"message": {"role": "assistant", "content": "done"}}])
    monkeypatch.setattr(litellm, "acompletion", complete)
    client = llm.LiteLLMClient(model="deepseek/deepseek-reasoner", reasoning_effort="high")
    assert asyncio.run(client.chat(messages=MESSAGES, tools=TOOLS))["content"] == "done"
    assert seen[0]["api_base"] is None and "client" not in seen[0]
    assert seen[0]["drop_params"] is False
    assert seen[0]["num_retries"] == seen[0]["max_retries"] == 0
    assert seen[0]["reasoning_effort"] == "high"


def test_cancelled_call_is_recorded_without_retry(monkeypatch):
    async def cancel(**kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(litellm, "acompletion", cancel)
    client = make_client()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(client.chat(messages=MESSAGES, tools=TOOLS))
    assert client.generation_records[0]["status"] == "cancelled"
    assert client.generation_records[0]["sdk_request_count"] == 1


def test_compatible_metrics_do_not_invent_missing_usage_or_finish_reason(monkeypatch):
    install_transport(monkeypatch, lambda request: httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": "ok"}}],
    }))
    client = make_client()
    asyncio.run(client.chat(messages=MESSAGES, tools=[]))
    record = client.generation_records[0]
    for key in ("usage", "finish_reason", "observed_model", "response_id", "system_fingerprint"):
        assert record[key] is None
    assert record["usage_source"] == "provider"


def test_metrics_filter_reflected_credentials_and_nonnumeric_usage(monkeypatch):
    install_transport(monkeypatch, lambda request: reply(
        id="test-key", model="test-key", system_fingerprint="test-key",
        usage={"prompt_tokens": 10, "test-key": 123, "echo": "test-key", "details": {"cached_tokens": 4}},
    ))
    client = make_client()
    asyncio.run(client.chat(messages=MESSAGES, tools=[]))
    record = client.generation_records[0]
    assert "test-key" not in json.dumps(record)
    assert record["usage"] == {"prompt_tokens": 10, "details": {"cached_tokens": 4}}
