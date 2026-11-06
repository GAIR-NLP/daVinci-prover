"""Conversation metric snapshots stay separate from model-visible messages."""

from types import SimpleNamespace

import pytest

from dvp.artifacts.conversation import build_conversation_artifact


def artifact(client):
    return build_conversation_artifact(
        driver=SimpleNamespace(messages=[{"role": "assistant", "content": "ok"}]),
        episode=SimpleNamespace(tool_schemas=lambda: []),
        client=client,
        result={"success": True},
    )


def test_generation_records_are_independent_deep_snapshots():
    records = [{
        "call_index": 1, "status": "success",
        "usage": {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 80}},
    }]
    client = SimpleNamespace(generation_records=records)
    saved = artifact(client)
    metadata = saved["generation_metadata"]
    assert metadata["available"] is True
    assert metadata["calls"] == records
    assert saved["messages"] == [{"role": "assistant", "content": "ok"}]
    assert saved["schema_version"] == "dvp.conversation.v4"
    assert saved["rejected_responses"] == []
    assert set(metadata) == {"available", "calls"}

    records[0]["usage"]["prompt_tokens_details"]["cached_tokens"] = 0
    records.append({"call_index": 2, "status": "error", "usage": None})
    assert len(metadata["calls"]) == 1
    assert metadata["calls"][0]["usage"]["prompt_tokens_details"]["cached_tokens"] == 80
    metadata["calls"][0]["usage"]["prompt_tokens"] = 999
    assert records[0]["usage"]["prompt_tokens"] == 100


@pytest.mark.parametrize("client", [SimpleNamespace(), SimpleNamespace(generation_records=[])])
def test_clients_without_records_remain_compatible(client):
    metadata = artifact(client)["generation_metadata"]
    assert metadata["available"] is False
    assert metadata["calls"] == []
    assert set(metadata) == {"available", "calls"}


def test_failed_call_without_usage_is_still_an_available_record():
    record = {"status": "cancelled", "usage": None, "error_type": "CancelledError"}
    metadata = artifact(SimpleNamespace(generation_records=[record]))["generation_metadata"]
    assert metadata["available"] is True
    assert metadata["calls"] == [record]
    assert set(metadata) == {"available", "calls"}


def test_rejected_responses_are_independent_of_model_messages_and_live_records():
    rejected = [{"turn": 1, "response": {"reasoning_content": "discarded thinking"}}]
    messages = [{"role": "user", "content": "Use a tool."}]
    saved = build_conversation_artifact(
        driver=SimpleNamespace(messages=messages, rejected_responses=rejected),
        episode=SimpleNamespace(tool_schemas=lambda: []), client=SimpleNamespace(), result={},
    )
    assert saved["messages"] == messages
    assert "discarded thinking" not in str(saved["messages"])
    assert saved["rejected_responses"] == rejected
    rejected[0]["response"]["reasoning_content"] = "changed"
    assert saved["rejected_responses"][0]["response"]["reasoning_content"] == "discarded thinking"
    saved["rejected_responses"][0]["turn"] = 99
    assert rejected[0]["turn"] == 1
