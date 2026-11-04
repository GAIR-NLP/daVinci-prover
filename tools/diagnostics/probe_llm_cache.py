#!/usr/bin/env python3
"""Measure repeated streaming Chat Completions and provider-reported cache usage."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import getpass
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Any, AsyncIterator, Callable
from urllib.parse import urlsplit
import uuid
import warnings

import httpx


CACHE_PATHS = (
    ("prompt_tokens_details", "cached_tokens"),
    ("prompt_cache_hit_tokens",),
    ("cache_read_input_tokens",),
)


def numeric_usage(value: Any) -> Any:
    """Keep original numeric values and nesting, excluding arbitrary response text."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    if isinstance(value, dict):
        return {key: clean for key, item in value.items()
                if (clean := numeric_usage(item)) is not None}
    if isinstance(value, list):
        return [numeric_usage(item) for item in value]
    return None


def cache_metrics(usage: dict[str, Any] | None) -> dict[str, Any]:
    result = {}
    prompt_tokens = (usage or {}).get("prompt_tokens")
    for path in CACHE_PATHS:
        value: Any = usage
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            value = None
        ratio = None
        if (value is not None and isinstance(prompt_tokens, (int, float))
                and not isinstance(prompt_tokens, bool)
                and prompt_tokens > 0 and 0 <= value <= prompt_tokens):
            ratio = value / prompt_tokens
        result["usage." + ".".join(path)] = {
            "tokens": value, "prompt_token_ratio": ratio,
        }
    return result


async def sse_data(lines: AsyncIterator[str]) -> AsyncIterator[str]:
    parts: list[str] = []
    async for line in lines:
        if line == "":
            if parts:
                yield "\n".join(parts)
                parts.clear()
        elif line.startswith("data:"):
            parts.append(line[5:].removeprefix(" "))
    if parts:
        yield "\n".join(parts)


def text_chars(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, dict):
        return sum(text_chars(item) for item in value.values())
    if isinstance(value, list):
        return sum(text_chars(item) for item in value)
    return 0


def redact_strings(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    if isinstance(value, dict):
        return {key.replace(secret, "[REDACTED]"): redact_strings(item, secret)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact_strings(item, secret) for item in value]
    return value


async def probe_once(
    client: httpx.AsyncClient, endpoint: str, payload: dict[str, Any], api_key: str,
    *, timeout: float = 120, routing_key: str | None = None,
    clock: Callable[[], float] = time.perf_counter,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "http_status": None, "headers_seconds": None, "ttft_seconds": None,
        "total_seconds": None, "usage": None, "finish_reason": None,
        "model": None, "response_delta_chars": 0,
        "delta_chars": {"content": 0, "reasoning": 0, "tool_calls": 0},
        "stream_done": False, "error": None,
    }
    started = clock()
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "text/event-stream"}
    if routing_key is not None:
        headers["X-SMG-Routing-Key"] = visible_ascii(routing_key)

    async def consume() -> None:
        async with client.stream(
            "POST", endpoint, json=payload, headers=headers,
        ) as response:
            record["http_status"] = response.status_code
            record["headers_seconds"] = clock() - started
            if not response.is_success:
                record["error"] = f"HTTP {response.status_code}; response body omitted"
            else:
                async for data in sse_data(response.aiter_lines()):
                    if data.strip() == "[DONE]":
                        record["stream_done"] = True
                        break
                    try:
                        event = json.loads(data)
                    except (ValueError, TypeError):
                        record["error"] = "Invalid JSON in SSE event; event text omitted"
                        break
                    if not isinstance(event, dict):
                        record["error"] = "Invalid SSE event structure; event text omitted"
                        break
                    if event.get("error") is not None:
                        record["error"] = "API error in stream; error body omitted"
                        break
                    if isinstance(event.get("usage"), dict):
                        # Streaming providers can report usage only in the final event.
                        record["usage"] = numeric_usage(event["usage"])
                    if isinstance(event.get("model"), str):
                        record["model"] = event["model"].replace(api_key, "[REDACTED]")[:256]
                    choices = event.get("choices") or []
                    if not isinstance(choices, list):
                        record["error"] = "Invalid stream choices; event text omitted"
                        break
                    for choice in choices:
                        if not isinstance(choice, dict):
                            continue
                        if isinstance(choice.get("finish_reason"), str):
                            record["finish_reason"] = choice["finish_reason"].replace(
                                api_key, "[REDACTED]"
                            )[:128]
                        delta = choice.get("delta")
                        if not isinstance(delta, dict):
                            continue
                        counts = {
                            "content": text_chars(delta.get("content")),
                            "reasoning": sum(text_chars(delta.get(key)) for key in
                                             ("reasoning_content", "reasoning", "thinking")),
                            "tool_calls": text_chars(delta.get("tool_calls"))
                                          + text_chars(delta.get("function_call")),
                        }
                        if sum(counts.values()) and record["ttft_seconds"] is None:
                            record["ttft_seconds"] = clock() - started
                        for key, count in counts.items():
                            record["delta_chars"][key] += count
                    if record["error"]:
                        break
                if record["error"] is None and not record["stream_done"]:
                    record["error"] = "Stream ended without [DONE]; completion is unconfirmed"
    try:
        # A read timeout alone resets on heartbeats; this deadline covers the
        # complete request, including headers, keepalives and all generated tokens.
        await asyncio.wait_for(consume(), timeout=timeout)
    except asyncio.TimeoutError:
        record["error"] = "Overall request timeout; stream cancelled"
    except httpx.TimeoutException:
        record["error"] = "Network timeout; exception details omitted"
    except httpx.HTTPError:
        record["error"] = "HTTP transport error; exception details omitted"
    except (KeyboardInterrupt, asyncio.CancelledError):
        record["error"] = "Interrupted by user"
    finally:
        record["total_seconds"] = clock() - started
        record["response_delta_chars"] = sum(record["delta_chars"].values())
        record["cache_metrics"] = cache_metrics(record["usage"])
    return record


async def run_probes(
    client: httpx.AsyncClient, endpoint: str, payload: dict[str, Any], api_key: str, repeats: int,
    *, timeout: float = 120, routing_key: str | None = None, no_routing_key: bool = False,
    on_record: Callable[[list[dict[str, Any]]], None] | None = None,
) -> list[dict[str, Any]]:
    if no_routing_key and routing_key is not None:
        raise ValueError("routing_key and no_routing_key are mutually exclusive")
    if not no_routing_key and routing_key is None:
        routing_key = "cache-probe-" + uuid.uuid4().hex
    records = []
    for index in range(repeats):
        record = await probe_once(client, endpoint, payload, api_key, timeout=timeout,
                                  routing_key=routing_key)
        record["request_number"] = index + 1
        records.append(record)
        if on_record is not None:
            on_record(records)
        if record["error"] is not None:
            break
    return records


def positive_int(value: str) -> int:
    result = int(value)
    if result <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return result


def positive_seconds(value: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return result


def visible_ascii(value: str) -> str:
    if not value or any(not 33 <= ord(char) <= 126 for char in value):
        raise argparse.ArgumentTypeError("must be nonempty printable ASCII without whitespace")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Send REAL, potentially billable streaming Chat Completions requests to probe "
            "prefix-cache reuse. Each run uses a fresh synthetic prefix unless --probe-id is reused. "
            "Repeats share one X-SMG-Routing-Key by default to request sticky routing. "
            "TTFT includes network, queueing, prefill and generation; it is not pure prefill. "
            "Missing usage/cache fields mean unknown, not zero. Timing alone does not prove "
            "cache reuse. No automatic retries; any HTTP/API/stream error stops the run."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--base-url", required=True,
                        help="API base or full chat/completions URL; a bare host gets /v1 automatically")
    parser.add_argument("--model", required=True, help="Model identifier accepted by the server")
    parser.add_argument("--api-key-env", default="INF_API_KEY",
                        help="Environment variable holding the key; otherwise prompt invisibly on a TTY")
    routing = parser.add_mutually_exclusive_group()
    routing.add_argument("--routing-key", type=visible_ascii,
                         help="X-SMG-Routing-Key for every repeat; default is a new random key per run")
    routing.add_argument("--no-routing-key", action="store_true",
                         help="Omit the sticky-routing header for an explicit control experiment")
    parser.add_argument("--probe-id", type=visible_ascii,
                        help="Synthetic prompt ID; reuse with identical options to reproduce the prompt")
    parser.add_argument("--repeats", type=positive_int, default=3, help="Sequential identical requests")
    parser.add_argument("--prefix-lines", type=positive_int, default=96, help="Synthetic reference lines")
    parser.add_argument("--max-tokens", type=positive_int, default=16, help="Maximum generated tokens")
    parser.add_argument("--timeout", type=positive_seconds, default=120,
                        help="Overall wall-time limit in seconds per request, including SSE heartbeats")
    parser.add_argument("--thinking", choices=("enabled", "disabled"), default="enabled",
                        help="Send thinking plus chat_template_kwargs thinking/enable_thinking")
    parser.add_argument("--output", type=Path,
                        help="Optional JSON report path; contains no prompt, answer or API credential")
    args = parser.parse_args(argv)
    try:
        parsed = urlsplit(args.base_url)
        valid = (parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username
                 and not parsed.password and not parsed.query and not parsed.fragment)
        parsed.port  # Validate the port before constructing the HTTP client.
    except ValueError:
        valid = False
    if not valid:
        parser.error("base URL must be HTTP(S), with no credentials, query or fragment")
    endpoint = args.base_url.rstrip("/")
    if parsed.path in ("", "/"):
        endpoint += "/v1"
    if not endpoint.endswith("/chat/completions"):
        endpoint += "/chat/completions"
    api_key = os.environ.get(args.api_key_env, "")
    if not api_key:
        if not sys.stdin.isatty():
            parser.error("API key environment variable is unset and no interactive terminal is available")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                api_key = getpass.getpass("API key (hidden; never saved): ")
        except (EOFError, KeyboardInterrupt):
            parser.error("API key input cancelled")
        except getpass.GetPassWarning:
            parser.error("Secure hidden terminal input is unavailable; use the API key environment variable")
    if not api_key or not api_key.isascii() or any(char in api_key for char in "\r\n"):
        parser.error("API key must be nonempty ASCII and must not contain newlines")

    routing_key = None if args.no_routing_key else args.routing_key or "cache-probe-" + uuid.uuid4().hex
    probe_id = args.probe_id or uuid.uuid4().hex
    prompt = f"Independent cache probe nonce: {probe_id}\n"
    prompt += "Read these reference statements, then reply with only OK.\n"
    prompt += "\n".join(
        f"Reference {index:04d}: For a finite set of natural numbers, addition is associative "
        "and commutative, zero is its identity, and induction proves statements for every "
        "natural number from a base case and an induction step."
        for index in range(args.prefix_lines)
    )
    prompt += "\nReply with only OK."
    payload = {
        "model": args.model, "messages": [{"role": "user", "content": prompt}],
        "stream": True, "stream_options": {"include_usage": True},
        "max_tokens": args.max_tokens, "temperature": 0,
        "thinking": {"type": args.thinking},
        "chat_template_kwargs": {"thinking": args.thinking == "enabled",
                                 "enable_thinking": args.thinking == "enabled"},
    }
    metadata = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": endpoint.replace(api_key, "[REDACTED]"),
        "requested_model": args.model.replace(api_key, "[REDACTED]"),
        "requested_repeats": args.repeats, "prefix_lines": args.prefix_lines,
        "routing_key": routing_key, "routing_header": "X-SMG-Routing-Key",
        "probe_id": probe_id,
        "http_client_reused_across_requests": True, "http_keepalive_enabled": True,
        "connection_note": "One HTTP client per run; actual connection reuse depends on the server.",
        "prompt_chars": len(prompt), "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "max_tokens": args.max_tokens, "thinking": args.thinking,
        "request_wall_timeout_seconds": args.timeout,
        "ttft_definition": "Time to first nonempty content, reasoning or tool-call delta; includes network and queueing",
        "cache_note": "Provider counter sources are preserved; null means unknown. Timing alone cannot prove cache reuse.",
    }

    def report_progress(records: list[dict[str, Any]]) -> None:
        # Write after every response, so a later timeout leaves the measurements
        # already obtained available on disk and on the terminal.
        report = redact_strings({"metadata": metadata, "records": records}, api_key)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8")
        print(json.dumps({"record": report["records"][-1]}, ensure_ascii=False), flush=True)

    async def run() -> list[dict[str, Any]]:
        async with httpx.AsyncClient(timeout=args.timeout, trust_env=False) as client:
            return await run_probes(client, endpoint, payload, api_key, args.repeats,
                                    timeout=args.timeout, routing_key=routing_key,
                                    no_routing_key=args.no_routing_key, on_record=report_progress)

    print(json.dumps({"metadata": redact_strings(metadata, api_key)}, ensure_ascii=False), flush=True)
    records = asyncio.run(run())
    return 1 if any(record["error"] for record in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
