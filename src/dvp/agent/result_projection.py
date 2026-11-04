"""Bounded model-visible projections of tool evidence.

Internal evaluation and transport metadata stay outside chat. Distinct evidence that
fits is kept intact; oversized results report omissions and file reads include continuation arguments.
Full outcomes remain in the trajectory."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from dvp.protocol import ToolOutcome
from dvp.verification.diagnostics import primary_verifier_diagnostic

from .warning_projection import project_verifier_warnings


def _outcome_for_model(
    outcome: ToolOutcome | None,
    *,
    max_chars: int = 12_000,
    tool_name: str | None = None,
    path_aliases: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    if outcome is None:
        return {
            "success": False,
            "error": "Tool call was not executed by the episode.",
        }
    return _compact(
        dict(outcome.raw),
        max_chars=max_chars,
        tool_name=outcome.backend_name or outcome.name or tool_name,
        path_aliases=path_aliases,
    )


_TRANSPORT_KEYS = frozenset(
    {
        "api_response",
        "command",
        "command_line",
        "curl_command",
        "executed_command",
        "http_request",
        "http_response",
        "raw_request",
        "raw_response",
        "request_headers",
        "response_headers",
        "transport",
        "transport_metadata",
    }
)

# Only result envelopes carry episode scoring. Retrieval hits may have their
# own relevance score, and Lean source/diagnostic strings must remain intact.
_INTERNAL_RESULT_KEYS = frozenset({
    "stop", "terminal", "score", "reward", "reward_components",
    "objective_complete", "proof_complete", "local_proof_verified",
    "lookup_succeeded", "verified", "strict_verified", "draft_typechecked",
    "fixed_task_contract", "source_bundle_sha256", "source_bundle_manifest",
    "candidate_sha256", "original_source_sha256",
    "proof_code", "automatic_check_call_ids",
    "verification_profile",
})
_INTERNAL_RESULT_STATUSES = frozenset({
    "lookup_evidence_only", "auxiliary_verified_only",
    "draft_holes_remaining", "draft_invalid",
})
_RESULT_METADATA_KEYS = (
    "check_kind",
    "success",
    "returncode",
    "count",
    "status",
    "file_path",
    "diagnostic_count",
    "lean_executed",
    "linter_warnings_omitted",
    "duplicate_warnings_omitted",
)


def _compact(
    payload: dict[str, Any],
    *,
    max_chars: int = 12_000,
    tool_name: str | None = None,
    path_aliases: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Project backend evidence and bound one model-visible tool result."""
    budget = max(512, int(max_chars))
    normalized_name = _key(tool_name or "")
    payload = _model_result_projection(payload)
    payload = _without_transport(payload)
    if normalized_name == "python_execute":
        return _compact_python_result(payload, budget)
    if normalized_name == "file_read" and isinstance(payload.get("content"), str):
        return _compact_file_read(payload, budget)
    if normalized_name == "lean_retrieve" or _looks_like_retrieval(payload):
        return _compact_retrieval(payload, budget)
    if normalized_name == "content_search" and isinstance(payload.get("results"), list):
        return _compact_search(payload, budget)
    if isinstance(payload.get("verifications"), list):
        if payload["verifications"]:
            payload["check_kind"] = "compilation"
        return _compact_batch_verifier_result(payload, budget, path_aliases or {})
    verification = payload.get("verification")
    verifier_like = normalized_name == "lean_verify" or isinstance(
        verification,
        Mapping,
    )
    if verifier_like:
        payload["check_kind"] = "compilation"
        payload = _deduplicate_verifier_result(payload)
        payload = _shorten_verifier_paths(payload, path_aliases or {})
        if len(_json(payload)) <= budget:
            return payload
        return _essential_verifier_result(payload, budget)
    changed = False

    def sanitize(value: Any, parent: str = "") -> Any:
        nonlocal changed
        if isinstance(value, str):
            limit = 4000
            if len(value) > limit:
                changed = True
            return _truncate(value, limit)
        if isinstance(value, list):
            key = _key(parent)
            limit = {
                "errors": 6,
                "warnings": 3,
                "declarations": 12,
            }.get(key, 12)
            items = [sanitize(item, parent) for item in value[:limit]]
            if len(value) > limit:
                changed = True
                items.append(f"[{len(value) - limit} items omitted]")
            return items
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            items = sorted(value.items(), key=lambda pair: str(pair[0]))
            for key, item in items[:48]:
                text_key = str(key)
                if _key(text_key) in _TRANSPORT_KEYS:
                    changed = True
                    continue
                result[text_key] = sanitize(item, text_key)
            if len(items) > 48:
                changed = True
                result["__omitted_keys__"] = len(items) - 48
            return result
        return value

    compact = sanitize(payload)
    assert isinstance(compact, dict)
    if len(_json(compact)) <= budget:
        if not changed:
            return compact
        marked = {**compact, "result_truncated": True}
        if len(_json(marked)) <= budget:
            return marked
    return _essential_result(compact, budget)


def _compact_batch_verifier_result(
    payload: Mapping[str, Any], budget: int, aliases: Mapping[str, str],
) -> dict[str, Any]:
    """Display each check once, keeping its diagnostics bound to its file.

    The batch's primary check controls episode evidence elsewhere. This view
    only projects the recorded checks; it never combines their proof metadata.
    """
    entries = [
        dict(item) for item in payload["verifications"]
        if isinstance(item, Mapping) and isinstance(item.get("verification"), Mapping)
    ]
    total = len(payload["verifications"])
    primary = next((index for index, item in enumerate(entries)
                    if item.get("file_path") == payload.get("file_path")), None)
    result = {key: value for key, value in payload.items() if key != "verifications"}
    if primary is not None and isinstance(result.get("verification"), Mapping):
        # Normalization adds these hints to the primary envelope. Move them
        # beside that check before removing the duplicate top-level check.
        for key in ("holes", "source_placeholders",
                    "dependency_placeholders", "primary_diagnostic",
                    "diagnostic_count", "repair_hint"):
            if key in result:
                entries[primary][key] = result.pop(key)
        if result.get("error") == entries[primary]["verification"].get("error"):
            result.pop("error", None)
        result.pop("verification")
    entries = [
        _shorten_verifier_paths(
            _deduplicate_verifier_result(_model_result_projection(item)), aliases,
        )
        for item in entries
    ]
    for item in entries:
        if item["verification"].get("file_path") == item.get("file_path"):
            item["verification"].pop("file_path")
    result = _shorten_verifier_paths(_deduplicate_verifier_result(result), aliases)
    result.update(
        verification_count=total, verifications=entries,
        verifications_shown=len(entries), verifications_omitted=total - len(entries),
    )
    if len(_json(result)) <= budget:
        return result

    # Keep all independent diagnostics before spending space on console output
    # or diffs. Each reduced check carries its own truncation marker.
    without_console = []
    for item in entries:
        reduced = _without_verifier_console(item)
        reduced["verification"] = _without_verifier_console(item["verification"])
        if reduced != item:
            reduced["result_truncated"] = True
        without_console.append(reduced)
    reduced = _without_verifier_console(result)
    reduced.update(verifications=without_console, result_truncated=True)
    if len(_json(reduced)) <= budget:
        return reduced

    compact: dict[str, Any] = {
        "result_truncated": True, "verification_count": total,
        "verifications": [], "verifications_shown": 0, "verifications_omitted": total,
    }
    for key in ("check_kind", "success", "file_saved", "lean_executed", "edits_applied"):
        if key in result:
            compact[key] = result[key]
    # The selected primary and other failures get room before successful checks.
    order = sorted(range(len(entries)), key=lambda index: (
        index != primary, entries[index]["verification"].get("success") is not False, index,
    ))
    selected: list[int] = []
    for position, index in enumerate(order):
        remaining = budget - len(_json(compact)) - 16
        allowance = min(remaining, max(320, remaining // (len(order) - position)))
        summary = _batch_verification_summary(entries[index], allowance)
        if summary is None:
            continue
        trial = {
            **compact, "verifications": [*compact["verifications"], summary],
            "verifications_shown": len(selected) + 1,
            "verifications_omitted": total - len(selected) - 1,
        }
        if len(_json(trial)) <= budget:
            compact = trial
            selected.append(index)
    # After reserving independent checks, expand their diagnostics with any
    # space left over. A long first error cannot crowd out all other files.
    for position, index in enumerate(selected):
        previous = compact["verifications"][position]
        allowance = budget - len(_json(compact)) + len(_json(previous))
        summary = _batch_verification_summary(entries[index], allowance)
        if summary is not None:
            trial = {**compact, "verifications": list(compact["verifications"])}
            trial["verifications"][position] = summary
            if len(_json(trial)) <= budget:
                compact = trial
    # These describe saved bytes, independently of the check status. Preserve
    # the complete list when it fits and otherwise explicitly count omissions.
    files = result.get("files")
    if isinstance(files, list):
        trial = {**compact, "files": files}
        if len(_json(trial)) <= budget:
            compact = trial
        elif len(_json({**compact, "files_omitted": len(files)})) <= budget:
            compact["files_omitted"] = len(files)
    return compact


def _batch_verification_summary(
    item: Mapping[str, Any], budget: int,
) -> dict[str, Any] | None:
    """Use the existing diagnostic compactor while reserving the exact path."""
    if len(_json(item)) <= budget:
        return dict(item)
    identity = {key: item[key] for key in ("file_path",) if key in item}
    allowance = budget - len(_json(identity))
    if allowance <= 64:
        return None
    summary = {
        **_essential_verifier_result(
            {key: value for key, value in item.items() if key not in identity}, allowance,
        ),
        **identity,
    }
    return summary if len(_json(summary)) <= budget else None


def _shorten_verifier_paths(
    payload: Mapping[str, Any], aliases: Mapping[str, str],
) -> dict[str, Any]:
    """Shorten known locations, never Lean code, quoted strings, or goals.

    Only the leading location of a compiler diagnostic and explicit location
    fields are changed. In particular, console output can contain arbitrary
    #eval/#check output and is not rewritten. The verifier already maps its
    immutable module snapshots back to the original workspace paths.
    """
    prefixes = sorted(aliases.items(), key=lambda item: len(item[0]), reverse=True)

    def location(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        for prefix, replacement in prefixes:
            root = prefix.rstrip("/") + "/"
            if value.startswith(root):
                return replacement + value[len(root):]
        return value

    def diagnostic(value: Any) -> Any:
        if isinstance(value, str):
            # Match only the first header, not later lines quoting source or
            # an error-like string generated by the candidate itself.
            match = re.match(
                r"(?P<file>/[^\r\n]+?\.lean)(?=:\d+:\d+:\s*(?:error|warning|info)(?:\(|:))",
                value,
            )
            if match:
                return location(match.group("file")) + value[match.end():]
            return value
        if isinstance(value, Mapping):
            return {
                key: location(item) if key in {"file", "filename", "file_path"} else item
                for key, item in value.items()
            }
        return value

    result = dict(payload)
    for key in ("verification", "contract_verification"):
        nested = result.get(key)
        if isinstance(nested, Mapping):
            result[key] = _shorten_verifier_paths(nested, aliases)
    for key in ("errors", "warnings", "infos"):
        items = result.get(key)
        if isinstance(items, list):
            result[key] = [diagnostic(item) for item in items]
    for key in ("error", "message", "primary_diagnostic"):
        if key in result:
            result[key] = diagnostic(result[key])
    for key in ("file", "filename", "file_path"):
        if key in result:
            result[key] = location(result[key])
    for key in ("holes", "source_placeholders", "dependency_placeholders"):
        group = result.get(key)
        if isinstance(group, Mapping):
            group = dict(group)
            if isinstance(group.get("items"), list):
                group["items"] = [diagnostic(item) for item in group["items"]]
            result[key] = group
        elif isinstance(group, list):
            result[key] = [diagnostic(item) for item in group]
    # Empty console/error metadata adds no evidence. Empty placeholder groups
    # are removed separately; positive counts and omission flags stay visible.
    for key in ("output", "stdout", "stderr", "error", "primary_diagnostic", "repair_hint"):
        if key in result and result[key] in (None, ""):
            result.pop(key)
    for key in ("warnings", "infos"):
        if result.get(key) == []:
            result.pop(key)
    return result


def _without_transport(value: Any) -> Any:
    """Remove transport fields without discarding proof or source evidence."""
    if isinstance(value, Mapping):
        return {
            str(key): _without_transport(item)
            for key, item in value.items()
            if _key(str(key)) not in _TRANSPORT_KEYS
        }
    if isinstance(value, list):
        return [_without_transport(item) for item in value]
    return value


def _compact_file_read(payload: dict[str, Any], budget: int) -> dict[str, Any]:
    if len(_json(payload)) <= budget:
        return payload
    lines = payload["content"].splitlines()
    bounds = payload.get(
        "returned_line_range", payload.get("line_range", [1, len(lines) + 1])
    )
    start, end = bounds
    total = payload.get("total_lines", end - 1)
    base = _essential_base(payload, budget)
    base.update(
        total_lines=total,
        requested_line_range=payload.get("requested_line_range"),
        truncated=True,
        content_truncated=True,
    )

    def page(count: int) -> dict[str, Any]:
        next_line = start + count
        next_range = (
            [next_line, end] if count < len(lines) else payload.get("next_line_range")
        )
        result = {
            **base,
            "content": "\n".join(lines[:count]),
            "line_range": [start, next_line],
            "returned_line_range": [start, next_line],
            "remaining_lines": max(0, total - next_line + 1),
            "lines_omitted": len(lines) - count,
            "next_line_range": next_range,
            "next_read": {
                "file_path": payload.get("file_path"),
                "line_range": next_range,
            },
        }
        if payload.get("start_column"):
            result["start_column"] = payload["start_column"]
        return result

    low, high = 0, len(lines)
    while low < high:
        mid = (low + high + 1) // 2
        if len(_json(page(mid))) <= budget:
            low = mid
        else:
            high = mid - 1
    if low:
        return page(low)
    # A single unusually long line must be resumable without losing its middle.
    rendered = lines[0] if lines else ""
    prefix, separator, body = rendered.partition("|")
    if not separator:
        prefix, body = "", rendered
    else:
        prefix += separator
    offset = payload.get("start_column", 0)

    def fragment(count: int) -> dict[str, Any]:
        result = page(0)
        result.update(
            content=prefix + body[:count],
            partial_line=start,
            returned_columns=[offset, offset + count],
            line_range=[start, start + 1],
            returned_line_range=[start, start + 1],
        )
        result["next_read"]["start_column"] = offset + count
        return result

    low, high = 0, len(body)
    while low < high:
        mid = (low + high + 1) // 2
        if len(_json(fragment(mid))) <= budget:
            low = mid
        else:
            high = mid - 1
    result = fragment(low)
    if low and len(_json(result)) <= budget:
        return result
    return {
        "success": False,
        "result_truncated": True,
        "error": "The tool result budget cannot fit file pagination metadata; increase max_tool_result_chars.",
    }


def _compact_search(payload: dict[str, Any], budget: int) -> dict[str, Any]:
    if len(_json(payload)) <= budget:
        return payload
    matches = payload["results"]
    result = _essential_base(payload, budget - 256)
    for key in (
        "file_count",
        "truncated",
        "file_limit_reached",
        "files_with_more_matches",
        "scan_limit_reached",
    ):
        if key in payload and len(_json({**result, key: payload[key]})) < budget // 2:
            result[key] = payload[key]
    result.update(
        results=[],
        results_shown=0,
        results_omitted=len(matches),
        continuation="Narrow the search path or pattern for omitted matches; use file_read for full context.",
    )
    if "error" in payload:
        _add_fitted_field(result, "error", payload["error"], budget)
    for item in matches:
        candidate = {
            **result,
            "results": [*result["results"], item],
            "results_shown": len(result["results"]) + 1,
            "results_omitted": len(matches) - len(result["results"]) - 1,
        }
        if len(_json(candidate)) > budget:
            if not result["results"]:
                reduced = {
                    key: item[key] for key in ("file", "line", "text") if key in item
                }
                reduced["context_omitted"] = True
                if len(_json({**result, "results": [reduced]})) > budget:
                    reduced["text"] = _truncate(
                        str(reduced.get("text", "")), max(32, budget // 4)
                    )
                    reduced["text_truncated"] = True
                candidate = {
                    **result,
                    "results": [reduced],
                    "results_shown": 1,
                    "results_omitted": len(matches) - 1,
                }
                if len(_json(candidate)) <= budget:
                    result = candidate
            break
        result = candidate
    return result


def _model_result_projection(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Expose operation results and diagnostics without episode bookkeeping.

    ``success`` describes the operation that ran: reading a file, searching,
    writing a file, or compiling Lean. Draft compilation can succeed with
    holes; their goals and source locations remain visible. Final acceptance
    is decided from the original outcome, never from this chat projection.
    """
    projected = {
        key: value for key, value in payload.items()
        if key not in _INTERNAL_RESULT_KEYS and not _key(str(key)).startswith("_harness")
    }
    status = projected.get("status")
    if isinstance(status, str) and status in _INTERNAL_RESULT_STATUSES:
        projected.pop("status")
    for key in ("holes", "source_placeholders", "dependency_placeholders"):
        if key in projected:
            group = projected[key]
            if isinstance(group, list):
                group = [item for item in group if not _empty_placeholder_group(item)]
                projected[key] = group
            if _empty_placeholder_group(group):
                projected.pop(key)
    for key in ("verification", "contract_verification"):
        nested = projected.get(key)
        if isinstance(nested, Mapping):
            projected[key] = _model_result_projection(nested)
            projected[key].pop("check_kind", None)
    checks = projected.get("verifications")
    if isinstance(checks, list):
        projected["verifications"] = []
        for item in checks:
            if isinstance(item, Mapping):
                item = _model_result_projection(item)
                item.pop("check_kind", None)
            projected["verifications"].append(item)
    verification = projected.get("verification")
    if isinstance(verification, dict):
        # File writes carry one copy of the hole diagnostics.
        for key in ("holes", "source_placeholders",
                    "dependency_placeholders"):
            if key in projected:
                verification.pop(key, None)
    return projected


def _empty_placeholder_group(value: Any) -> bool:
    """Zero placeholder counts say nothing about a theorem's axiom dependencies."""
    return value == [] or (
        isinstance(value, Mapping) and value.get("total") == 0
        and not any(value.get(key) for key in ("items", "goal", "goals", "omitted"))
    )


def _compact_python_result(payload: Mapping[str, Any], budget: int) -> dict[str, Any]:
    """Retain numerical output and tracebacks under the whole chat budget."""
    if len(_json(payload)) <= budget:
        return dict(payload)
    result: dict[str, Any] = {"result_truncated": True}
    for key in ("success", "returncode", "timed_out", "output_truncated", "error_kind"):
        if key in payload:
            _fit_evidence_field(result, result, key, payload[key], budget)
    if payload.get("error"):
        _fit_text_field(result, result, "error", str(payload["error"]), min(budget, len(_json(result)) + 128))
    streams = [(key, payload[key]) for key in ("stdout", "stderr")
               if isinstance(payload.get(key), str) and payload[key]]
    for index, (key, value) in enumerate(streams):
        used = len(_json(result))
        allowance = used + max(0, budget - used) // (len(streams) - index)
        _fit_text_field(result, result, key, value, allowance)
    return result


def _essential_result(payload: Mapping[str, Any], budget: int) -> dict[str, Any]:
    """Keep task evidence—not transport bulk—inside the hard chat budget."""
    result = _essential_base(payload, budget)

    verification = payload.get("verification")
    verification_failed = (
        isinstance(verification, Mapping) and verification.get("success") is False
    )
    if verification_failed:
        priority = (
            "error",
            "primary_diagnostic",
            "repair_hint",
            "source_placeholders",
            "holes",
            "verification",
            "declarations",
            "results",
            "matches",
            "message",
            "output",
        )
    else:
        priority = (
            "error",
            "primary_diagnostic",
            "repair_hint",
            "results",
            "matches",
            "exact",
            "similar",
            "declarations",
            "source_placeholders",
            "holes",
            "verification",
            "message",
            "output",
        )
    for key in priority:
        if key in payload:
            _add_fitted_field(result, key, payload[key], budget)
    return result


def _essential_base(
    payload: Mapping[str, Any],
    budget: int,
) -> dict[str, Any]:
    """Reserve operation status and source locations for a bounded result."""
    result: dict[str, Any] = {"result_truncated": True}
    for key in _RESULT_METADATA_KEYS:
        value = payload.get(key)
        if not isinstance(value, (bool, int, float, str)):
            continue
        trial = {**result, key: value}
        if len(_json(trial)) <= budget:
            result = trial
    return result


def _looks_like_retrieval(payload: Mapping[str, Any]) -> bool:
    results = payload.get("results")
    if not isinstance(results, list) or not results:
        return False
    sample = results[0]
    return isinstance(sample, Mapping) and any(
        key in sample for key in ("fullname", "signature", "semantic")
    )


def _concise_retrieval_fields(item: Mapping[str, Any]) -> dict[str, Any]:
    """Select one short description without changing the recorded retrieval.

    Dependency lists and a second prose description are not part of the default
    search result. A generator's description must not describe a different
    declaration as though it were the requested theorem.
    """
    result = {
        key: value for key, value in item.items()
        if key not in {
            "dependencies", "dependencies_total", "dependencies_omitted",
            "semantic", "semantic_truncated", "semantic_omitted",
            "docstring", "docstring_truncated", "docstring_omitted",
        }
    }
    if item.get("signature_status") == "name_mismatch":
        return result
    for key in ("semantic", "docstring"):
        text = item.get(key)
        if not isinstance(text, str) or not text.strip():
            continue
        limit = 200
        if len(text) > limit:
            suffix = " …[truncated]"
            prefix = text[:limit - len(suffix)].rstrip()
            boundary = prefix.rfind(" ")
            if boundary >= len(prefix) * 3 // 4:
                prefix = prefix[:boundary]
            result[key] = prefix + suffix
            result[key + "_truncated"] = True
        else:
            result[key] = text
            if item.get(key + "_truncated"):
                result[key + "_truncated"] = True
        break
    return result


def _compact_retrieval(
    payload: Mapping[str, Any],
    budget: int,
) -> dict[str, Any]:
    """Keep full declaration parameters; omit whole hits before cutting binders."""
    if payload.get("retrieval_backend") == "lean_explore" or any(
        isinstance(item, Mapping) and item.get("source_origin") == "lean_explore"
        for item in payload.get("results", [])
    ):
        return _compact_lean_explore(payload, budget)
    payload = {
        **payload,
        "results": [
            _concise_retrieval_fields(item) if isinstance(item, Mapping) else item
            for item in payload.get("results", [])
        ],
    }
    if len(_json(payload)) <= budget:
        if any(
            isinstance(item, Mapping) and any(
                item.get(key) for key in
                ("semantic_truncated", "docstring_truncated", "source_code_truncated")
            ) for item in payload["results"]
        ):
            payload["result_truncated"] = True
        if len(_json(payload)) <= budget:
            return payload
    candidates = [
        item for item in payload.get("results", []) if isinstance(item, Mapping)
    ]
    base = _essential_base(payload, budget - 256)
    base.update(results=[], results_shown=0, results_omitted=len(candidates))
    # A partial retrieval may still have useful hits; keep the failure visible.
    for key in ("retrieval_backend", "truncated", "error", "message"):
        if key in payload:
            _fit_evidence_field(base, base, key, payload[key], budget)
    if not candidates:
        if "output" in payload:
            _fit_evidence_field(base, base, "output", payload["output"], budget)
        return base
    for item in candidates:
        declaration = {
            key: item[key]
            for key in (
                "fullname",
                "signature",
                "variables",
                "filename",
                "line_start",
                "line_end",
                "kind",
                "id",
                "module",
                "source_origin",
                "source_link",
                "source_repository",
                "source_revision",
                "source_error",
                "source_code_truncated",
                "signature_status",
                "signature_error",
                "source_declaration",
                "context",
                "context_status",
                "context_error",
            )
            if key in item
        }
        if "fullname" not in declaration and item.get("name"):
            declaration["fullname"] = item["name"]
        # Reserve omission metadata for optional evidence. Signature and
        # lexical context are already present in full or the hit is skipped.
        for key in ("source_code", "docstring", "semantic"):
            if item.get(key):
                declaration[key + "_omitted"] = True
        trial = {
            **base,
            "results": [*base["results"], declaration],
            "results_shown": len(base["results"]) + 1,
            "results_omitted": len(candidates) - len(base["results"]) - 1,
        }
        if (
            len(_json(trial)) > budget
            and len(str(declaration.get("source_link", ""))) > budget // 2
        ):
            # An unusually long URL must not hide otherwise usable evidence.
            # Repository and revision still identify the source version.
            del declaration["source_link"]
            declaration["source_link_omitted"] = True
        if len(_json(trial)) > budget:
            break
        base = trial

    def restore(index: int, key: str, value: Any, **metadata: Any) -> bool:
        original = base["results"][index]
        replacement = {**original, key: value, **metadata}
        replacement.pop(key + "_omitted", None)
        base["results"][index] = replacement
        if len(_json(base)) <= budget:
            return True
        base["results"][index] = original
        return False

    # Signature and lexical context were admitted together. Only optional
    # proof source and the already bounded description use spare space.
    for key in ("source_code", "docstring", "semantic", "score", "score_kind"):
        for index in range(len(base["results"])):
            value = candidates[index].get(key)
            if value is None or value == "":
                continue
            marker = key + "_truncated"
            metadata = {marker: True} if candidates[index].get(marker) else {}
            if restore(index, key, value, **metadata):
                continue
            if key == "source_code" and isinstance(value, str):
                for limit in (2000, 1000, 400, 160):
                    if restore(
                        index, key, _truncate(value, limit), source_code_truncated=True
                    ):
                        break
    if not base["results"]:
        # Never present a severed signature or missing typeclass context as complete.
        message = "The complete declaration exceeds the result budget. Read its source or increase max_tool_result_chars."
        first = candidates[0]
        locator = {
            key: first[key]
            for key in (
                "fullname", "filename", "line_start", "line_end",
                "source_link", "source_repository", "source_revision",
            )
            if key in first
        }
        for key, value in (("message", message), ("first_result_location", locator)):
            if len(_json({**base, key: value})) <= budget:
                base[key] = value
    return base


def _compact_lean_explore(
    payload: Mapping[str, Any], budget: int
) -> dict[str, Any]:
    """Return concise descriptions with atomic headers and lexical context.

    LeanExplore repeats repository metadata and context boilerplate per hit.
    Normalize that even when the original payload fits. A source_id indexes
    sources; its repository/revision plus the hit's path and line range locate
    the original declaration. The lossless outcome is never mutated.
    """
    candidates = [
        _concise_retrieval_fields(item)
        for item in payload.get("results", []) if isinstance(item, Mapping)
    ]
    base = _essential_base(payload, max(0, budget - 320))
    base.update(
        retrieval_backend="lean_explore",
        results=[], results_shown=0, results_omitted=len(candidates),
    )
    for key in ("truncated", "error", "message", "output"):
        if key in payload:
            _fit_evidence_field(base, base, key, payload[key], budget)

    context_note = (
        "Context is incomplete lexical evidence, not an elaborated Lean type. "
        "context_ref refers to an earlier result's rank."
    )
    selected: list[Mapping[str, Any]] = []
    context_ranks: dict[str, int] = {}

    def put_text(hit: dict[str, Any], key: str, text: str, limit: int) -> None:
        hit[key] = _truncate(text, limit)
        if len(text) > limit:
            hit[key + "_truncated"] = True

    for index, item in enumerate(candidates):
        hit = {
            key: item[key]
            for key in (
                "fullname", "signature", "variables", "kind",
                "line_start", "line_end", "signature_status",
                "source_declaration", "signature_error", "source_error",
                "context_status", "context_error", "source_code_role",
                "source_code_truncated", "semantic_truncated", "docstring_truncated",
            )
            if key in item and (not key.endswith("_error") or item[key])
        }
        hit["rank"] = index + 1
        if hit.get("signature_status") == "source_header":
            # A valid header already identifies the declaration. Keep status
            # and generator names only when they explain incomplete evidence.
            hit.pop("signature_status")
            hit.pop("source_declaration", None)
        if hit.get("context_status") == "available":
            hit.pop("context_status")
        if "fullname" not in hit and item.get("name"):
            hit["fullname"] = item["name"]
        # Keep the directly readable filename, while avoiding a second copy of
        # the same repository-relative path. Module, API id and URL repeat it.
        filename = item.get("filename")
        filename_path = str(filename).removeprefix("target/") if filename else None
        path = item.get("source_path") or filename_path
        if filename:
            hit["filename"] = filename
        if path and path != filename_path:
            hit["source_path"] = path
        source = {
            key: item["source_" + key]
            for key in ("repository", "revision")
            if item.get("source_" + key)
        }
        sources = list(base.get("sources", []))
        if source:
            if source not in sources:
                sources.append(source)
            hit["source_id"] = sources.index(source)
        if item.get("source_link") and not (
            source.get("repository") and source.get("revision") and path
        ):
            hit["source_link"] = item["source_link"]

        context = item.get("context")
        context_identity = None
        if isinstance(context, Mapping):
            # Commands include external binders, local notation and opens. They
            # are kept atomically; a clipped command is not a usable signature.
            hit["context"] = {
                key: context[key] for key in ("namespace", "commands")
                if key in context
            }
            hit["context"]["complete"] = False
            warnings = context.get("warnings")
            if isinstance(warnings, list) and warnings:
                # The shared note explains the limitation; preserve a concrete
                # example and an explicit count instead of repeated old scopes.
                hit["context"]["warnings"] = [warnings[0]]
                omitted = len(warnings) - 1 + int(context.get("warnings_omitted", 0))
                if omitted:
                    hit["context"]["warnings_omitted"] = omitted
        elif context:
            hit["context"] = context
        if "context" in hit:
            context_identity = _json(hit["context"])
            if context_identity in context_ranks:
                hit.pop("context")
                hit["context_ref"] = context_ranks[context_identity]

        semantic = item.get("semantic")
        docstring = item.get("docstring")
        explanation_key = (
            "semantic" if isinstance(semantic, str) and semantic.strip()
            else "docstring" if isinstance(docstring, str) and docstring.strip()
            else None
        )
        if explanation_key:
            hit[explanation_key] = item[explanation_key]
        code = item.get("source_code")
        if isinstance(code, str) and code:
            # Presence means the caller requested proof source. Reserve a
            # useful excerpt before admitting more lower-ranked declarations.
            put_text(hit, "source_code", code, 1000 if index < 3 else 600)
        trial = {
            **base,
            "results": [*base["results"], hit],
            "results_shown": len(selected) + 1,
            "results_omitted": len(candidates) - len(selected) - 1,
            # Reserve the flag now so later truncation never exceeds the cap.
            "result_truncated": True,
        }
        if sources:
            trial["sources"] = sources
            trial["source_note"] = (
                "Source paths = filename minus target/ unless source_path is set. "
                "Check local revision before file_read."
            )
        if context:
            trial["context_note"] = context_note
        if len(_json(trial)) > budget:
            # The description is already concise. Shorten only opted-in proof
            # source; a signature or scope command must never be clipped.
            if isinstance(code, str) and code:
                put_text(hit, "source_code", code, 256)
        if len(_json(trial)) > budget:
            # A LibraryNote or an unusually large context must not prevent
            # subsequent ordinary declarations from being shown.
            continue
        base = trial
        selected.append(item)
        if context_identity is not None:
            context_ranks.setdefault(context_identity, hit["rank"])

    def restore(index: int, key: str, value: Any, **metadata: Any) -> bool:
        original = base["results"][index]
        replacement = {**original, key: value}
        replacement.pop(key + "_omitted", None)
        truncated_key = key + "_truncated"
        if truncated_key not in metadata:
            if truncated_key in selected[index]:
                replacement[truncated_key] = selected[index][truncated_key]
            else:
                replacement.pop(truncated_key, None)
        replacement.update(metadata)
        base["results"][index] = replacement
        if len(_json(base)) <= budget:
            return True
        base["results"][index] = original
        return False

    # Proof bodies are explicitly requested evidence. They may use remaining
    # space; default descriptions must not grow to fill the result budget.
    for key in ("source_code",):
        for index, item in enumerate(selected):
            value = item.get(key)
            if not isinstance(value, str) or not value:
                continue
            if restore(index, key, value):
                continue
            current = base["results"][index]
            if key not in current:
                continue
            low, high = len(current[key]), len(value)
            while low <= high:
                mid = (low + high) // 2
                snippet = _truncate(value, mid)
                if restore(index, key, snippet, **{key + "_truncated": True}):
                    low = mid + 1
                else:
                    high = mid - 1

    def has_omissions(value: Any) -> bool:
        if not isinstance(value, Mapping):
            return False
        return any(
            (key.endswith(("_omitted", "_truncated")) and bool(part))
            or has_omissions(part)
            for key, part in value.items()
        )

    if (
        has_omissions({key: value for key, value in base.items() if key != "result_truncated"})
        or any(has_omissions(hit) for hit in base["results"])
    ):
        base["result_truncated"] = True
    else:
        base.pop("result_truncated", None)
    if candidates and not selected:
        # No cut signature is presented as callable. Counts remain useful even
        # if this tiny budget cannot also accommodate a locator and explanation.
        message = "No complete declaration with explanation fits the result budget; increase max_tool_result_chars."
        if len(_json({**base, "message": message})) <= budget:
            base["message"] = message
        first = candidates[0]
        locator = {
            key: first[key] for key in
            ("fullname", "source_repository", "source_revision", "source_path", "line_start")
            if key in first
        }
        if len(_json({**base, "first_result_location": locator})) <= budget:
            base["first_result_location"] = locator
    return base



def _deduplicate_verifier_result(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Remove proven copies within one check, retaining original diagnostics.

    Console text is removed only at complete line boundaries. Unique probe or
    process output remains evidence even when the check also reports errors.
    This function operates on the chat projection, never the recorded outcome.
    """
    result = dict(payload)
    nested = result.get("verification")
    if isinstance(nested, Mapping):
        result["verification"] = _deduplicate_verifier_result(nested)

    result, warning_console = project_verifier_warnings(result)
    diagnostics = [
        item.rstrip("\r\n")
        for key in ("errors", "infos")
        for item in (result[key] if isinstance(result.get(key), list) else [])
        if isinstance(item, str) and item.strip()
    ] + warning_console
    consoles: list[str] = []
    for key in ("output", "stderr", "stdout"):
        original = result.get(key)
        if not isinstance(original, str) or not original:
            continue
        text = original
        for diagnostic in sorted(set(diagnostics), key=len, reverse=True):
            text = re.sub(
                r"^" + re.escape(diagnostic) + r"(?=\r?$)",
                "", text, flags=re.MULTILINE,
            )
        # A combined output field may also contain an entire stdout/stderr.
        if text.strip() and any(
            "\n" + text.strip("\r\n") + "\n"
            in "\n" + previous.strip("\r\n") + "\n"
            for previous in consoles
        ):
            text = ""
        if text != original:
            text = text.strip("\r\n")
            if text.strip():
                result[key] = text
            else:
                result.pop(key)
        if text.strip():
            consoles.append(text)

    primary = result.get("primary_diagnostic")
    if isinstance(primary, Mapping) and isinstance(primary.get("message"), str):
        expected, _, _ = primary_verifier_diagnostic(result, None)
        # Use the verifier's own normalization, including its source location
        # and display cap. Never discard a distinct/adapted repair message.
        if expected and all(
            primary.get(key) == expected.get(key)
            for key in ("message", "file", "line", "column", "end_line", "end_column")
        ):
            result["primary_diagnostic"] = {
                key: value for key, value in primary.items() if key != "message"
            }
    return result


def _essential_verifier_result(
    payload: Mapping[str, Any],
    budget: int,
) -> dict[str, Any]:
    """Keep independent diagnostics before duplicated console output or diffs."""
    nested = isinstance(payload.get("verification"), Mapping)
    source = payload["verification"] if nested else payload
    # Diagnostics take priority; restore distinct console text within the budget.
    trimmed = _without_verifier_console(payload)
    if nested:
        trimmed["verification"] = _without_verifier_console(source)
    trimmed["result_truncated"] = True
    if len(_json(trimmed)) <= budget:
        _fit_verifier_console(trimmed, payload, budget)
        return trimmed
    result = _essential_base(payload, budget - 256)
    evidence = {
        key: source[key]
        for key in ("success", "returncode", "linter_warnings_omitted", "duplicate_warnings_omitted")
        if key in source
    }
    if nested:
        result["verification"] = evidence
    else:
        result.update(evidence)
        evidence = result
    # A long error may bury its goal between the excerpt's head and tail.
    # Reserve the selected goal before admitting that error's console context.
    primary = payload.get("primary_diagnostic")
    if isinstance(primary, Mapping) and primary.get("primary_goal"):
        _fit_evidence_field(result, result, "primary_diagnostic", primary, budget - 160)
    _fit_axiom_reports(result, evidence, _axiom_reports(source), budget)
    if nested:
        _fit_axiom_reports(result, result, _axiom_reports(payload), budget)
    for key in ("error", "message"):
        if key in source:
            _fit_evidence_field(result, evidence, key, source[key], budget)
    # Explicit counts stay attached to the corresponding diagnostic list.
    for key in ("errors", "warnings", "infos"):
        items = source.get(key)
        if not isinstance(items, list) or not items:
            continue
        evidence[key] = []
        evidence[key + "_total"] = len(items)
        evidence[key + "_omitted"] = len(items)
        if len(_json(result)) > budget:
            del evidence[key]
            del evidence[key + "_total"]
            del evidence[key + "_omitted"]
            continue
        for item in items:
            evidence[key].append(item)
            evidence[key + "_omitted"] -= 1
            if len(_json(result)) <= budget:
                continue
            evidence[key].pop()
            evidence[key + "_omitted"] += 1
            # A very large first diagnostic still needs a useful, marked excerpt.
            if not evidence[key]:
                for limit in (budget // 2, budget // 4, 400, 160, 64):
                    excerpt = _small_summary(
                        item, string_limit=max(32, limit), list_limit=8
                    )
                    evidence[key].append(excerpt)
                    evidence[key + "_omitted"] -= 1
                    evidence[key + "_truncated"] = True
                    if len(_json(result)) <= budget:
                        break
                    evidence[key].pop()
                    evidence[key + "_omitted"] += 1
                    del evidence[key + "_truncated"]
            break
    for key in (
        "goal",
        "goals",
        "unsolved_goal",
        "unsolved_goals",
        "holes",
        "source_placeholders",
        "dependency_placeholders",
    ):
        if key in source:
            _fit_evidence_field(result, evidence, key, source[key], budget)
    # Preserve draft goals and the recommended first repair, without hiding other errors.
    for key in (
        "source_placeholders",
        "dependency_placeholders",
        "holes",
        "primary_diagnostic",
        "repair_hint",
        "declarations",
        "error",
        "message",
    ):
        if key not in payload or key in result:
            continue
        trial = {**result, key: payload[key]}
        if len(_json(trial)) <= budget:
            result[key] = payload[key]
        else:
            _add_fitted_field(result, key, payload[key], budget)
    # Deduplication already removed repeated diagnostic blocks. Remaining
    # console text can contain independent #check or process evidence.
    _deduplicate_axiom_reports(evidence)
    if nested:
        _deduplicate_axiom_reports(result)
    _fit_verifier_console(result, payload, budget)
    return result


def _fit_verifier_console(
    result: dict[str, Any], payload: Mapping[str, Any], budget: int,
) -> None:
    """Keep unique console evidence from each result envelope separately."""
    pairs = [(payload, result)]
    if isinstance(payload.get("verification"), Mapping):
        pairs.insert(0, (payload["verification"], result["verification"]))
    for source, target in pairs:
        for key in ("output", "stderr", "stdout"):
            if source.get(key):
                text = str(source[key])
                # Only remove exact copies already retained as compiler text.
                for report in _axiom_reports(target):
                    text = re.sub(r"(?m)^" + re.escape(report) + r"(?=\r?$)", "", text)
                if text.strip():
                    _fit_text_field(result, target, key, text.strip("\r\n"), budget)


_AXIOM_REPORT = re.compile(
    r"(?m)^(?:(?:[^\r\n]*?\.lean:)?\d+:\d+: info:\s*)?'.+' "
    r"(?:depends on axioms:\s*\[[^\]]*\]|does not depend on any axioms)[ \t]*"
)


def _console_axiom_reports(payload: Mapping[str, Any]) -> list[str]:
    """Read exact compiler reports without interpreting which axioms are allowed."""
    existing = payload.get("axiom_reports", [])
    reports = [item for item in existing if isinstance(item, str)] if isinstance(existing, list) else []
    for key in ("output", "stderr", "stdout"):
        text = payload.get(key)
        if isinstance(text, str):
            reports.extend(match.group() for match in _AXIOM_REPORT.finditer(text))
    return list(dict.fromkeys(reports))


def _info_axiom_reports(payload: Mapping[str, Any]) -> list[str]:
    messages = list(payload.get("infos", [])) if isinstance(payload.get("infos"), list) else []
    response = payload.get("response")
    if isinstance(response, Mapping) and isinstance(response.get("messages"), list):
        messages.extend(response["messages"])
    reports = []
    for item in messages:
        text = item.get("data", item.get("message")) if isinstance(item, Mapping) else item
        if isinstance(text, str):
            reports.extend(match.group() for match in _AXIOM_REPORT.finditer(text))
    return list(dict.fromkeys(reports))


def _axiom_reports(payload: Mapping[str, Any]) -> list[str]:
    return list(dict.fromkeys([*_console_axiom_reports(payload), *_info_axiom_reports(payload)]))


def _deduplicate_axiom_reports(payload: dict[str, Any]) -> None:
    reports = payload.get("axiom_reports")
    if not isinstance(reports, list):
        return
    represented = _info_axiom_reports(payload)
    retained = [report for report in reports if report not in represented]
    payload["axiom_reports"] = retained
    if "axiom_reports_total" in payload:
        payload["axiom_reports_total"] -= len(reports) - len(retained)
    if not retained and not payload.get("axiom_reports_omitted"):
        for key in ("axiom_reports", "axiom_reports_total", "axiom_reports_omitted"):
            payload.pop(key, None)


def _without_verifier_console(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Discard console filler only after retaining its axiom-report evidence."""
    result = {key: value for key, value in payload.items()
              if key not in {"output", "stdout", "stderr", "diff"}}
    retained = _info_axiom_reports(payload)
    reports = [report for report in _console_axiom_reports(payload) if report not in retained]
    if reports:
        result["axiom_reports"] = reports
    return result


def _fit_axiom_reports(
    envelope: dict[str, Any], target: dict[str, Any], reports: list[str], budget: int,
) -> None:
    if not reports:
        return
    target["axiom_reports"] = reports
    if len(_json(envelope)) <= budget:
        return
    target["axiom_reports"] = []
    target["axiom_reports_total"] = len(reports)
    target["axiom_reports_omitted"] = len(reports)
    for report in reports:
        target["axiom_reports"].append(report)
        target["axiom_reports_omitted"] -= 1
        if len(_json(envelope)) > budget:
            target["axiom_reports"].pop()
            target["axiom_reports_omitted"] += 1


def _fit_evidence_field(
    envelope: dict[str, Any],
    target: dict[str, Any],
    key: str,
    value: Any,
    budget: int,
) -> None:
    """Keep nested evidence within the whole envelope and mark any reduction."""
    if isinstance(value, str):
        _fit_text_field(envelope, target, key, value, budget)
        return
    target[key] = value
    if len(_json(envelope)) <= budget:
        return
    del target[key]
    for string_limit, list_limit in (
        (1600, 8),
        (800, 6),
        (400, 4),
        (200, 3),
        (100, 2),
        (48, 1),
    ):
        target[key] = _small_summary(
            value, string_limit=string_limit, list_limit=list_limit
        )
        target[key + "_truncated"] = True
        if len(_json(envelope)) <= budget:
            return
        del target[key]
        del target[key + "_truncated"]


def _fit_text_field(
    envelope: dict[str, Any],
    target: dict[str, Any],
    key: str,
    text: str,
    budget: int,
) -> None:
    """Fit text against the whole JSON envelope, accounting for escaping."""
    target[key] = text
    if len(_json(envelope)) <= budget:
        return
    del target[key]
    low, high = 16, len(text)
    best: str | None = None
    while low <= high:
        mid = (low + high) // 2
        target[key] = _truncate(text, mid)
        target[key + "_truncated"] = True
        if len(_json(envelope)) <= budget:
            best = target[key]
            low = mid + 1
        else:
            high = mid - 1
    target.pop(key, None)
    target.pop(key + "_truncated", None)
    if best is not None:
        target[key] = best
        target[key + "_truncated"] = True


def _add_fitted_field(
    result: dict[str, Any],
    key: str,
    value: Any,
    budget: int,
) -> None:
    """Add the richest bounded summary that still fits the whole result."""

    for string_limit, list_limit in (
        (1600, 8),
        (800, 6),
        (400, 4),
        (200, 3),
        (100, 2),
        (48, 1),
    ):
        candidate = _small_summary(
            value,
            string_limit=string_limit,
            list_limit=list_limit,
        )
        trial = {**result, key: candidate}
        if len(_json(trial)) <= budget:
            result[key] = candidate
            return


def _small_summary(
    value: Any,
    *,
    string_limit: int,
    list_limit: int,
    depth: int = 0,
) -> Any:
    if isinstance(value, str):
        return _truncate(value, string_limit)
    if isinstance(value, list):
        return [
            _small_summary(
                item,
                string_limit=string_limit,
                list_limit=list_limit,
                depth=depth + 1,
            )
            for item in value[:list_limit]
        ]
    if isinstance(value, Mapping):
        if depth >= 4:
            return f"[{len(value)} keys]"
        return {
            str(key): _small_summary(
                item,
                string_limit=string_limit,
                list_limit=list_limit,
                depth=depth + 1,
            )
            for key, item in list(value.items())[:24]
        }
    return value


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    marker = "...[omitted]..."
    kept = max(1, limit - len(marker))
    tail = kept // 4
    return value[: kept - tail] + marker + (value[-tail:] if tail else "")


def _key(key: str) -> str:
    return key.strip().lower().replace("-", "_").replace(" ", "_")


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        default=str,
        sort_keys=True,
        separators=(",", ":"),
    )
