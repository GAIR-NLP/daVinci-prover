"""Self-contained model conversation artifacts for offline SFT collection."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping

from dvp.protocol import jsonable


CONVERSATION_SCHEMA_VERSION = "dvp.conversation.v3"

_CLIENT_SAMPLING_FIELDS = (
    "model",
    "temperature",
    "top_p",
    "presence_penalty",
    "max_tokens",
    "reasoning_effort",
    "thinking",
    "chat_template_thinking",
    "chat_template_reasoning_effort",
    "stop_token_ids",
)


def build_conversation_artifact(
    *,
    driver: Any,
    episode: Any,
    client: Any,
    result: Mapping[str, Any],
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture the exact linear chat history consumed by the model driver.

    ``driver.messages`` contains the actual compact tool projections and
    harness feedback, rather than the richer backend outcomes in the episode
    trace. Metrics live in ``generation_metadata.calls`` and never enter the
    model-visible history. Each call has its own provider usage semantics,
    so no totals are inferred. Rejected raw responses, including their
    thinking, live separately in ``rejected_responses`` for auditing.
    Usable text from output-truncated responses is retained in ``messages``;
    incomplete thinking is omitted by default. Explicitly enabling
    ``retain_truncated_thinking`` retains it unless severe repetition is detected.
    This is context retention in a
    new request, not native token-level continuation. Filtered raw replies remain
    in ``rejected_responses`` without resending malformed tool calls. The legacy
    history policy keeps normalized assistant/tool exchanges, including rejected actions, directly
    in ``messages``; those retained replies are not discarded-response entries.
    """

    config = getattr(episode, "config", None)
    sampling = {
        field: jsonable(getattr(client, field))
        for field in _CLIENT_SAMPLING_FIELDS
        if hasattr(client, field)
    }
    records = getattr(client, "generation_records", [])
    calls = deepcopy(records) if isinstance(records, list) else []
    return {
        "schema_version": CONVERSATION_SCHEMA_VERSION,
        "episode": {
            "schema_version": getattr(config, "schema_version", None),
            "policy_version": getattr(config, "policy_version", None),
            "config_id": getattr(config, "config_id", None),
            "enable_lean_retrieve": getattr(config, "enable_lean_retrieve", True),
            "max_consecutive_format_errors": getattr(config, "max_consecutive_format_errors", 3),
            "max_consecutive_output_truncations": getattr(config, "max_consecutive_output_truncations", 5),
            "retain_truncated_thinking": getattr(config, "retain_truncated_thinking", False),
            "history_policy": getattr(config, "history_policy", "current"),
            "episode_id": getattr(episode, "episode_id", None),
        },
        "metadata": jsonable(dict(metadata or {})),
        "sampling": sampling,
        "routing": {
            "header": "X-SMG-Routing-Key",
            "routing_key": getattr(client, "routing_key", None),
        },
        "tools": jsonable(episode.tool_schemas()),
        "messages": jsonable(deepcopy(list(getattr(driver, "messages", [])))),
        "rejected_responses": jsonable(deepcopy(
            list(getattr(driver, "rejected_responses", []))
        )),
        "generation_metadata": {
            "available": bool(calls),
            "calls": jsonable(calls),
        },
        "result": jsonable(dict(result)),
    }
