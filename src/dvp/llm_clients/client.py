"""LiteLLM adapter for model calls, task affinity, and generation metrics."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
import math
import os
from time import monotonic
from typing import Any
from uuid import uuid4

# Loading the SDK must not fetch a remote pricing catalogue during offline runs.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

from httpx import AsyncClient, Response
import litellm
from openai import APIError, AsyncOpenAI
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential


class _ResponseCapture:
    """Observe compatible API replies before SDK normalization, without changing them."""

    data: dict[str, Any] | None = None

    async def observe(self, response: Response) -> None:
        if not response.is_success:
            return
        await response.aread()
        try:
            data = response.json()
        except (ValueError, UnicodeError):
            return
        if (
            isinstance(data, dict) and isinstance(data.get("choices"), list)
            and data["choices"] and isinstance(data["choices"][0], dict)
            and isinstance(data["choices"][0].get("message"), dict)
        ):
            self.data = data


def is_retryable_model_error(error: BaseException) -> bool:
    """Only transport failures and transient service errors merit a retry."""
    if isinstance(error, (litellm.BadRequestError, litellm.AuthenticationError,
                          litellm.PermissionDeniedError)):
        return False
    return isinstance(error, (litellm.APIConnectionError, litellm.Timeout)) or (
        isinstance(error, APIError)
        and getattr(error, "status_code", None) in {408, 409, 429, 500, 502, 503, 504}
    )


@dataclass(kw_only=True)
class LiteLLMClient:
    """Use LiteLLM's provider adapters; retain DVP's conversation interface."""

    model: str
    api_key: str | None = None
    base_url: str | None = None
    routing_key: str | None = None
    timeout: float = 1200.0
    temperature: float | None = None
    top_p: float | None = None
    presence_penalty: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    thinking: str | None = None
    chat_template_thinking: bool | None = None
    chat_template_reasoning_effort: str | None = None
    stop_token_ids: list[int] | None = None
    trust_env: bool | None = None
    max_retries: int = 3
    generation_records: list[dict[str, Any]] = field(default_factory=list, init=False)
    sdk_model: str = field(init=False)

    def __post_init__(self) -> None:
        if not self.model:
            raise ValueError("model is required")
        if self.routing_key is not None and (
            not self.routing_key or not self.routing_key.isascii()
            or any(ord(c) <= 32 or ord(c) == 127 for c in self.routing_key)
        ):
            raise ValueError("routing_key must be nonempty ASCII without whitespace or control characters")
        self.routing_key = self.routing_key or f"dvp-{uuid4().hex}"
        prefix, separator, _ = self.model.partition("/")
        self.sdk_model = (
            self.model if separator and prefix in litellm.provider_list
            else f"openai/{self.model}"
        )
        if self.base_url:
            self.base_url = self.base_url.rstrip("/").removesuffix("/chat/completions")
        elif self.sdk_model.startswith("openai/"):
            self.base_url = "https://api.openai.com/v1"
        if self.trust_env is None:
            self.trust_env = os.getenv("DAVINCI_LLM_TRUST_ENV", "true").strip().lower() not in {
                "0", "false", "no", "off", "disabled",
            }
        self.max_retries = max(0, int(self.max_retries))

    def _parameters(self) -> dict[str, Any]:
        parameters = {
            name: getattr(self, name)
            for name in ("temperature", "top_p", "presence_penalty", "max_tokens", "reasoning_effort")
            if getattr(self, name) is not None and getattr(self, name) != ""
        }
        extra_body: dict[str, Any] = {}
        if self.thinking:
            target = extra_body if self.sdk_model.startswith("openai/") else parameters
            target["thinking"] = {"type": self.thinking}
        template: dict[str, Any] = {}
        if self.chat_template_thinking is not None:
            template.update(thinking=self.chat_template_thinking, enable_thinking=self.chat_template_thinking)
        if self.chat_template_reasoning_effort:
            template["reasoning_effort"] = self.chat_template_reasoning_effort
        if template:
            extra_body["chat_template_kwargs"] = template
        if self.stop_token_ids:
            extra_body["stop_token_ids"] = list(self.stop_token_ids)
        if extra_body:
            parameters["extra_body"] = extra_body
        return parameters

    async def chat(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
        tool_choice: str | dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started = monotonic()
        parameters = self._parameters()
        record: dict[str, Any] = {
            "call_index": len(self.generation_records) + 1,
            "backend": "litellm", "sdk_model": self.sdk_model,
            "routing_key": self.routing_key, "input_message_count": len(messages),
            "request_parameters": deepcopy(parameters), "sdk_request_count": 0,
            "status": "error", "usage": None, "finish_reason": None,
            "observed_model": None, "response_id": None, "system_fingerprint": None,
            "http_status": None, "error_type": None,
        }
        kwargs = dict(
            model=self.sdk_model, api_key=self.api_key, api_base=self.base_url,
            messages=deepcopy(messages), timeout=self.timeout, stream=False,
            # Tenacity owns the retry budget; neither SDK may retry underneath it.
            num_retries=0, max_retries=0, drop_params=False, caching=False,
            # DVP executes function tools itself; do not load the optional MCP gateway.
            _skip_mcp_handler=True,
            extra_headers={"X-SMG-Routing-Key": self.routing_key}, **parameters,
        )
        if tools:
            kwargs.update(tools=deepcopy(tools), tool_choice=tool_choice or "auto")
        try:
            if self.sdk_model.startswith("openai/"):
                capture = _ResponseCapture()
                # Inject the upstream SDK client solely to configure proxy trust and
                # close its transport after each call, as the previous client did.
                async with AsyncOpenAI(
                    api_key=self.api_key or "EMPTY", base_url=self.base_url,
                    max_retries=0, timeout=self.timeout,
                    http_client=AsyncClient(
                        trust_env=self.trust_env, timeout=self.timeout,
                        event_hooks={"response": [capture.observe]},
                    ),
                ) as sdk_client:
                    response = await self._complete({**kwargs, "client": sdk_client}, record, capture)
            else:
                response = await self._complete(kwargs, record)
            choice = response["choices"][0]
            record.update(
                status="success", http_status=200,
                finish_reason=_metadata_string(choice.get("finish_reason"), self.api_key),
                usage=_numeric_usage(response.get("usage"), self.api_key),
                usage_source="provider" if self.sdk_model.startswith("openai/") else "litellm",
                observed_model=_metadata_string(response.get("model"), self.api_key),
                response_id=_metadata_string(response.get("id"), self.api_key),
                system_fingerprint=_metadata_string(response.get("system_fingerprint"), self.api_key),
            )
            return deepcopy(choice["message"])
        except BaseException as exc:
            record.update(
                status="cancelled" if isinstance(exc, asyncio.CancelledError) else "error",
                error_type=type(exc).__name__, http_status=getattr(exc, "status_code", None),
            )
            raise
        finally:
            record["elapsed_seconds"] = monotonic() - started
            self.generation_records.append(record)

    async def _complete(
        self, kwargs: dict[str, Any], record: dict[str, Any], capture: _ResponseCapture | None = None,
    ) -> dict[str, Any]:
        async for attempt in AsyncRetrying(
            retry=retry_if_exception(is_retryable_model_error),
            stop=stop_after_attempt(self.max_retries + 1),
            wait=wait_exponential(multiplier=1, max=30), reraise=True,
        ):
            with attempt:
                record["sdk_request_count"] += 1
                if capture is not None:
                    capture.data = None
                try:
                    response = await litellm.acompletion(**kwargs)
                except APIError as exc:
                    # A received assistant message is an agent action, including
                    # malformed tool envelopes that SDK normalization rejects.
                    if capture is None or capture.data is None:
                        raise
                    record["sdk_conversion_error"] = type(exc).__name__
                    return capture.data
                if capture is not None and capture.data is not None:
                    return capture.data
                return response.model_dump(exclude_none=True)


def _numeric_usage(value: Any, api_key: str | None = None) -> Any:
    """Keep token counts, not arbitrary provider text, in metric artifacts."""
    if isinstance(value, dict):
        return {
            key: clean for key, item in value.items()
            if not (api_key and api_key in key)
            and (clean := _numeric_usage(item, api_key)) is not None
        }
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return None


def _metadata_string(value: Any, api_key: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    return value.replace(api_key, "[REDACTED]") if api_key else value
