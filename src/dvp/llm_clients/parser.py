"""Parse model messages into the small, model-independent action protocol.

OpenAI-compatible servers do not all serialize tool calls in the same way.
This module intentionally keeps that compatibility handling at the driver
boundary so the episode/RL core only ever sees ``AssistantAction`` objects.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from typing import Any, Mapping

from dvp.protocol import AssistantAction, ToolCall, decode_tool_arguments


_TOOL_ARGUMENT_WRAPPER_KEYS = frozenset(
    {
        "tool",
        "tool_name",
        "name",
        "function",
        "arguments",
        "params",
        "parameters",
        "input",
    }
)
_INVALID_SCHEMA_COPY = "__dvp_invalid_tool_schema_copy__"
_LARGE_HISTORY_ARGUMENT_KEYS = frozenset(
    {
        "content",
        "code",
        "message",
        "new_str",
        "old_str",
        "proof_code",
    }
)
_DSML_INVOKE_START_RE = re.compile(
    r"<\s*｜DSML｜\s*invoke\s+name=(?P<quote>['\"])(?P<name>[^'\"]+)(?P=quote)\s*>",
    re.DOTALL,
)
_DSML_INVOKE_END_RE = re.compile(r"</\s*｜DSML｜\s*invoke\s*>", re.DOTALL)
_DSML_CALLS_START_RE = re.compile(r"<\s*｜DSML｜\s*(calls|function_calls)\s*>")
_DSML_PARAMETER_RE = re.compile(
    r"<\s*｜DSML｜\s*parameter\s+name=(?P<quote>['\"])(?P<name>[^'\"]+)(?P=quote)"
    r"(?:\s+string=(?P<string_quote>['\"])(?P<string_flag>true|false)(?P=string_quote))?"
    r"\s*>"
    r"(?P<value>.*?)"
    r"</\s*｜DSML｜\s*parameter\s*>",
    re.DOTALL,
)
_TOOL_MARKUP_START_RE = re.compile(
    r"\A\s*<\s*/?\s*(?:｜DSML｜"
    r"|(?:tool_calls?|function_calls?|invoke)\b)"
)
_JSON_FENCE_RE = re.compile(r"\A```(?:json)?\s*(\{.*?)\s*```\Z", re.DOTALL)
_TOOL_JSON_START_RE = re.compile(
    r'\A\s*\{\s*"(?:tool_calls|function_call|tool|tool_name)"\s*:'
    r'|\A\s*\{\s*"name"\s*:\s*"(?:[^"\\]|\\.)*"\s*,\s*'
    r'"(?:arguments|parameters|input)"\s*:'
)


def looks_like_tool_call_text(content: str) -> bool:
    """Recognize a standalone call envelope, not prose discussing an example.

    This only identifies tool intent. It never repairs a name, argument, or
    delimiter, and callers must give successfully parsed calls priority.
    Requiring an envelope at the start leaves prose and quoted markup on the
    ordinary completion path. Whole JSON fences retain the JSON-call syntax.
    """
    return bool(
        _TOOL_MARKUP_START_RE.match(content)
        or _TOOL_JSON_START_RE.match(_json_document_text(content))
    )


def normalize_tool_name(name: str | None) -> str:
    """Normalize gateway prefixes while preserving registry-level aliases."""

    normalized = str(name or "").strip()
    normalized = normalized.removeprefix("functions.")
    normalized = normalized.removeprefix("to=functions.")
    if "<|" in normalized:
        normalized = normalized.split("<|", 1)[0]
    return normalized


def extract_tool_calls(
    message: Mapping[str, Any],
    *,
    turn: int,
) -> list[ToolCall]:
    """Return native, JSON-in-content, or DSML tool calls in priority order."""

    native = message.get("tool_calls")
    if native is not None and not isinstance(native, list):
        return [_invalid_tool_call(
            None, turn=turn, index=1,
            error=f"Response tool_calls must be an array; received {type(native).__name__}.",
        )]
    if isinstance(native, list) and native:
        return [
            _tool_call_from_wire(item, turn=turn, index=index)
            for index, item in enumerate(native, start=1)
        ]

    for source_name in ("content", "reasoning_content"):
        content = message.get(source_name)
        if not isinstance(content, str) or not content.strip():
            continue
        if _TOOL_MARKUP_START_RE.match(content):
            # A broken envelope must not execute JSON embedded in its code, or
            # fall through to a different call mentioned in private reasoning.
            return _infer_dsml_tool_calls(content, turn=turn, source=source_name)
        json_call = _infer_json_tool_call(
            content,
            turn=turn,
            source=source_name,
        )
        if json_call is not None:
            return [json_call]
        if looks_like_tool_call_text(content):
            return []
    return []


def parse_assistant_action(
    message: Mapping[str, Any],
    *,
    turn: int,
) -> AssistantAction:
    """Convert one provider response into the episode's canonical action."""

    raw = dict(message)
    return AssistantAction.from_calls(
        extract_tool_calls(
            message,
            turn=turn,
        ),
        content=str(message.get("content") or ""),
        reasoning_content=str(message.get("reasoning_content") or ""),
        raw=raw,
    )


def tool_call_to_wire(call: ToolCall) -> dict[str, Any]:
    """Render a canonical tool call in OpenAI chat-history format."""

    return {
        "id": call.id,
        "type": "function",
        "function": {
            "name": call.name,
            "arguments": json.dumps(call.arguments, ensure_ascii=False, default=str),
        },
    }


def assistant_action_to_history(action: AssistantAction) -> dict[str, Any]:
    """Build a compact assistant message with canonicalized tool calls."""

    message: dict[str, Any] = {
        "role": "assistant",
        "content": None if any(call.synthetic for call in action.tool_calls) else (action.content or ""),
    }
    if action.reasoning_content and not any(call.synthetic for call in action.tool_calls):
        message["reasoning_content"] = action.reasoning_content
    if action.tool_calls:
        message["tool_calls"] = [
            tool_call_to_wire(
                ToolCall(
                    id=call.id,
                    name=call.name,
                    arguments=(
                        _compact_arguments_for_history(call.arguments)
                        if call.synthetic
                        else call.arguments
                    ),
                    synthetic=call.synthetic,
                    source=call.source,
                )
            )
            for call in action.tool_calls
        ]
    return message


def _compact_arguments_for_history(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """Keep synthetic calls valid without duplicating large generated code."""

    def compact(key: str, value: Any, depth: int = 0) -> Any:
        if isinstance(value, str):
            if key in _LARGE_HISTORY_ARGUMENT_KEYS and value:
                digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
                return (
                    f"[omitted from chat history: {key} had {len(value)} chars; "
                    f"sha256={digest}; full action is in the trajectory]"
                )
            if len(value) > 1200:
                return value[:1200] + f"\n...[{len(value) - 1200} chars omitted]"
            return value
        if isinstance(value, list):
            items = [compact(key, item, depth + 1) for item in value[:12]]
            if len(value) > 12:
                items.append(f"[{len(value) - 12} items omitted]")
            return items
        if isinstance(value, Mapping):
            if depth >= 3:
                return f"[nested object with {len(value)} keys omitted]"
            items = list(value.items())
            result = {
                str(item_key): compact(str(item_key), item_value, depth + 1)
                for item_key, item_value in items[:24]
            }
            if len(items) > 24:
                result["__omitted_keys__"] = len(items) - 24
            return result
        return value

    return {
        str(key): compact(str(key), value)
        for key, value in arguments.items()
    }


def _invalid_tool_call(
    item: Any,
    *,
    turn: int,
    index: int,
    error: str,
) -> ToolCall:
    """Keep malformed native items observable without inventing an executable call."""
    call_id = item.get("id") if isinstance(item, Mapping) else None
    return ToolCall(
        id=str(call_id or f"call_{turn}_{index}_invalid_tool_call"),
        name="invalid_tool_call", arguments={}, argument_parse_error=error,
    )


def _tool_call_from_wire(
    item: Any,
    *,
    turn: int,
    index: int,
) -> ToolCall:
    if not isinstance(item, Mapping):
        return _invalid_tool_call(
            item, turn=turn, index=index,
            error=f"Tool call {index} must be an object; received {type(item).__name__}.",
        )
    if "type" in item and item["type"] != "function":
        return _invalid_tool_call(
            item, turn=turn, index=index,
            error=f"Tool call {index} has an unsupported type; expected 'function'.",
        )
    function = item.get("function")
    if "function" in item and not isinstance(function, Mapping):
        return _invalid_tool_call(
            item, turn=turn, index=index,
            error=f"Tool call {index} function must be an object.",
        )
    function = function if isinstance(function, Mapping) else {}
    raw_name = function.get("name") if "function" in item else item.get("name")
    if not isinstance(raw_name, str):
        return _invalid_tool_call(
            item, turn=turn, index=index,
            error=f"Tool call {index} function.name must be a non-empty string.",
        )
    name = normalize_tool_name(raw_name)
    if not name:
        return _invalid_tool_call(
            item, turn=turn, index=index,
            error=f"Tool call {index} function.name must be a non-empty string.",
        )
    arguments, parse_error = decode_tool_arguments(
        function.get("arguments") if "function" in item else item.get("arguments")
    )
    return ToolCall(
        id=str(item.get("id") or f"call_{turn}_{index}_{name}"),
        name=name,
        arguments=_normalize_argument_object(arguments),
        synthetic=bool(item.get("synthetic", False)),
        argument_parse_error=parse_error,
    )


def _json_document_text(content: str) -> str:
    stripped = content.strip()
    match = _JSON_FENCE_RE.fullmatch(stripped)
    return match.group(1) if match else stripped


def _extract_json_object(content: str) -> dict[str, Any] | None:
    # A JSON example inside prose or code is not an instruction to execute it.
    try:
        parsed = json.loads(_json_document_text(content))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _loads_json_object(value: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _looks_like_json_schema(value: Mapping[str, Any]) -> bool:
    if "properties" in value or "oneOf" in value or "anyOf" in value:
        return True
    if value.get("type") == "object" and isinstance(value.get("required"), list):
        return True
    schema_keys = {
        "type",
        "description",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "default",
    }
    return bool(value) and set(value).issubset(schema_keys) and "type" in value


def _normalize_argument_object(args: dict[str, Any]) -> dict[str, Any]:
    function = args.get("function")
    if isinstance(function, Mapping):
        nested = function.get("arguments")
        if isinstance(nested, Mapping):
            return _normalize_argument_object(dict(nested))
        if isinstance(nested, str):
            parsed = _loads_json_object(nested)
            if parsed is not None:
                return _normalize_argument_object(parsed)

    for key in ("arguments", "params", "input"):
        nested = args.get(key)
        if isinstance(nested, Mapping):
            return _normalize_argument_object(dict(nested))
        if isinstance(nested, str):
            parsed = _loads_json_object(nested)
            if parsed is not None:
                return _normalize_argument_object(parsed)

    parameters = args.get("parameters")
    if isinstance(parameters, Mapping):
        parameters = dict(parameters)
        if _looks_like_json_schema(parameters):
            real_args = {
                key: value
                for key, value in args.items()
                if key not in _TOOL_ARGUMENT_WRAPPER_KEYS
                and key
                not in {
                    "type",
                    "description",
                    "required",
                    "properties",
                    "additionalProperties",
                }
            }
            return real_args or {_INVALID_SCHEMA_COPY: True}
        return _normalize_argument_object(parameters)
    if isinstance(parameters, str):
        parsed = _loads_json_object(parameters)
        if parsed is not None:
            return _normalize_argument_object(parsed)

    if _looks_like_json_schema(args):
        return {_INVALID_SCHEMA_COPY: True}
    return {
        key: value
        for key, value in args.items()
        if key not in {"tool", "tool_name", "name"}
    }


def _infer_json_tool_call(
    content: str,
    *,
    turn: int,
    source: str,
) -> ToolCall | None:
    args = _extract_json_object(content)
    if args is None:
        return None

    explicit_name = args.get("tool") or args.get("tool_name") or args.get("name")
    if isinstance(explicit_name, str):
        name = normalize_tool_name(explicit_name)
        arguments = _normalize_argument_object(args)
    elif "file_path" in args and "content" in args:
        name, arguments = "file_write", args
    elif "file_path" in args and "new_str" in args:
        name, arguments = "file_edit", args
    elif "file_path" in args and "line_range" in args:
        name, arguments = "file_read", args
    elif "path" in args and any(key in args for key in ("content_pattern", "pattern", "query")):
        name, arguments = "content_search", args
    elif "directory_path" in args and "pattern" in args:
        name, arguments = "file_search", args
    elif any(key in args for key in ("natural_language_query", "lean_name", "keywords")):
        name, arguments = "lean_retrieve", args
    elif "code" in args:
        # A bare ``{"code": ...}`` action has one stationary, profile-aware
        # meaning regardless of hidden episode hints.
        name, arguments = "lean_verify", args
    else:
        return None

    parse_error = _argument_container_error(args)
    return ToolCall(
        id=f"synthetic_{turn}_{name}",
        name=name,
        arguments={} if parse_error else arguments,
        synthetic=True,
        source=source,
        argument_parse_error=parse_error,
    )


def _argument_container_error(args: Mapping[str, Any]) -> str | None:
    """Do not salvage sibling fields when an explicit argument wrapper is bad."""
    function = args.get("function")
    if isinstance(function, Mapping) and "arguments" in function:
        nested, error = decode_tool_arguments(function["arguments"])
        return error or _argument_container_error(nested)
    for key in ("arguments", "params", "input", "parameters"):
        if key in args:
            nested, error = decode_tool_arguments(args[key])
            return error or _argument_container_error(nested)
    return None

def _parse_dsml_non_string(value: str) -> Any:
    stripped = value.strip()
    if stripped == "":
        return ""
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    lowered = stripped.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        return float(stripped)
    except ValueError:
        return stripped


def _parse_dsml_parameter(*, name: str, value: str, string_flag: str | None) -> Any:
    decoded = html.unescape(value)
    if string_flag == "false":
        return _parse_dsml_non_string(decoded)
    if name in {
        "content",
        "code",
        "new_str",
        "old_str",
        "message",
    }:
        return decoded
    return decoded.strip()


def _infer_dsml_tool_calls(
    content: str,
    *,
    turn: int,
    source: str,
) -> list[ToolCall]:
    """Decode complete invocations; reject the whole textual batch if damaged."""
    content = content.strip()
    wrapper = _DSML_CALLS_START_RE.match(content)
    if wrapper is not None:
        closing = re.search(r"</\s*｜DSML｜\s*" + wrapper.group(1) + r"\s*>\s*\Z", content)
        if closing is None:
            return []
        content = content[wrapper.end():closing.start()].strip()
    calls: list[ToolCall] = []
    while content:
        invoke = _DSML_INVOKE_START_RE.match(content)
        if invoke is None:
            return []
        raw_name = invoke.group("name")
        name = normalize_tool_name(raw_name)
        end_match = _DSML_INVOKE_END_RE.search(content, invoke.end())
        if not name or end_match is None:
            return []
        body = content[invoke.end():end_match.start()].strip()
        arguments: dict[str, Any] = {}
        while body:
            parameter = _DSML_PARAMETER_RE.match(body)
            if parameter is None or parameter.group("name") in arguments:
                return []
            if re.search(r"<\s*/?\s*｜DSML｜", parameter.group("value")):
                # A missing parameter close must not absorb the next tag as
                # code. Literal protocol tags in source must be escaped.
                return []
            parameter_name = parameter.group("name")
            arguments[parameter_name] = _parse_dsml_parameter(
                name=parameter_name,
                value=parameter.group("value") or "",
                string_flag=parameter.group("string_flag"),
            )
            body = body[parameter.end():].strip()
        calls.append(
            ToolCall(
                id=f"synthetic_{turn}_{len(calls) + 1}_{name}",
                name=name,
                arguments=arguments,
                synthetic=True,
                source=f"{source}:dsml",
            )
        )
        content = content[end_match.end():].strip()
    return calls
