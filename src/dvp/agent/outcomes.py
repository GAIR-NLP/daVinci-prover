"""Normalize backend results and classify failures into the episode protocol."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from dvp.protocol import ToolCall, ToolOutcome
from dvp.toolkits.surface import Dispatch
from dvp.verification.contracts import mask_lean_comments_and_strings
from dvp.verification.diagnostics import primary_verifier_diagnostic
from dvp.verification.lean import contains_placeholder

from . import candidate
from .policy import _LEAN_BACKENDS, verification_backend_enabled
from .verifier_evidence import (
    _raw_verification_profile,
    _structured_verifier_timeout,
    _trusted_verifier_infrastructure,
    source_bundle_evidence,
)

if TYPE_CHECKING:
    from .episode import ProverEpisode


_ARGUMENT_VALIDATION_ERRORS = {
    "lean_verify": frozenset({
        "code and file_path must be strings or null",
        "code must not be empty",
        "file_path must not be empty",
    }),
    "file_edit": frozenset({
        "Provide old_str or old_line_range",
        "Use edits or single-edit arguments, not both",
        "edits must be a non-empty array",
        "Each edit must be an object",
    }),
    "file_read": frozenset({
        "ValueError: start_column must be a non-negative integer",
    }),
    "lean_retrieve": frozenset({
        "Provide at least one of natural_language_query, lean_name, or keywords.",
    }),
    "content_search": frozenset({
        "ValueError: Content search context counts must be nonnegative",
        "ValueError: max_matches_per_file must be positive",
    }),
}


def _argument_validation_error(
    backend_name: str,
    raw: dict[str, Any],
    verification: dict[str, Any] | None,
) -> bool:
    """Recognize handler validation failures before Lean was invoked."""
    error = raw.get("error")
    if not isinstance(error, str):
        return False
    if backend_name == "file_edit":
        error = re.sub(r"^Edit [1-9]\d*: ", "", error)
    if not (
        error in _ARGUMENT_VALIDATION_ERRORS.get(backend_name, ())
        or error.startswith("Missing required argument(s): ")
        or error.startswith("TypeError: ")
        or backend_name == "file_edit" and error.startswith(("Unknown argument(s): ", "Unknown edit argument(s): "))
    ):
        return False
    for payload in (raw, verification or {}):
        if _raw_verification_profile(payload) or any(
            payload.get(key)
            for key in (
                "lean_executed", "command", "errors", "warnings", "diagnostics",
                "output", "stderr", "stdout", "response",
            )
        ):
            return False
    return True


def normalize_outcome(
    episode: ProverEpisode,
    call: ToolCall,
    dispatch: Dispatch,
    raw: dict[str, Any],
) -> ToolOutcome:
    backend = dispatch.backend_name
    verifier_backend = verification_backend_enabled(backend)
    verification = (
        raw.get("verification")
        if verifier_backend and isinstance(raw.get("verification"), dict)
        else None
    )
    if verification is None and backend == "lean_verify":
        verification = raw
    verification_profile = _raw_verification_profile(raw)
    candidate_code = (
        candidate.dispatch_candidate_code(episode, dispatch)
        if verifier_backend else None
    )
    if (
        backend in {"file_write", "file_edit"}
        and isinstance(raw.get("file_path"), str)
    ):
        # A batch's shared/default path need not be the checked primary file.
        # Bind placeholder classification to the same file as its evidence.
        candidate_code = candidate.read_workspace_path(episode, raw["file_path"])
    candidate_has_placeholder = bool(
        candidate_code
        and contains_placeholder(mask_lean_comments_and_strings(candidate_code))
    )

    verified: bool | None = None
    if verification is not None and verification_profile != "draft_allow_sorry":
        verified = bool(verification.get("success"))

    text = result_text(raw)
    trusted_infrastructure = bool(
        raw.get("_harness_infrastructure_error") is True
        or (verifier_backend and _trusted_verifier_infrastructure(raw))
    )
    verifier_timeout = bool(
        verifier_backend and _structured_verifier_timeout(raw)
    )
    if verifier_backend:
        (
            primary_diagnostic,
            diagnostic_count,
            repair_hint,
        ) = primary_verifier_diagnostic(raw, verification)
    else:
        primary_diagnostic = None
        diagnostic_count = 0
        repair_hint = None
    error_kind = classify_error(
        backend_name=backend or call.name,
        raw=raw,
        verification=verification,
        text=text,
        trusted_infrastructure=trusted_infrastructure,
        verifier_timeout=verifier_timeout,
        candidate_has_placeholder=candidate_has_placeholder,
        primary_diagnostic=primary_diagnostic,
    )
    failed = bool(
        not raw.get("success", False)
        or (verification is not None and verification.get("success") is False)
    )
    infrastructure = failed and trusted_infrastructure
    # Backend score/stop claims are evidence, not the reward boundary.
    # Verification is promoted only after the exact-target contract succeeds.
    reward = 0.0
    terminal = False
    normalized_raw = dict(raw)
    if primary_diagnostic is not None:
        normalized_raw["primary_diagnostic"] = primary_diagnostic
        normalized_raw["diagnostic_count"] = diagnostic_count
    if repair_hint is not None:
        normalized_raw["repair_hint"] = repair_hint
    return ToolOutcome(
        call_id=call.id,
        name=call.name,
        backend_name=backend,
        arguments=dict(call.arguments),
        executed=True,
        valid=bool(raw.get("success", False)),
        verified=verified,
        terminal=terminal,
        reward=reward,
        error_kind=error_kind,
        artifact_id=artifact_id(episode, dispatch, raw, verified),
        feedback=text[: episode.config.max_tool_result_chars],
        infrastructure_error=infrastructure,
        raw=normalized_raw,
    )


def result_text(raw: dict[str, Any]) -> str:
    chunks: list[str] = []
    for key in ("error", "message", "output", "stderr", "stdout", "errors", "warnings"):
        value = raw.get(key)
        if value:
            chunks.append(
                json.dumps(value, ensure_ascii=False)
                if not isinstance(value, str)
                else value
            )
    verification = raw.get("verification")
    if isinstance(verification, dict):
        for key in (
            "error",
            "message",
            "output",
            "stderr",
            "stdout",
            "errors",
        ):
            value = verification.get(key)
            if value:
                chunks.append(
                    json.dumps(value, ensure_ascii=False)
                    if not isinstance(value, str)
                    else value
                )
    if not chunks:
        chunks.append(json.dumps(raw, ensure_ascii=False, default=str))
    return "\n".join(chunks)


def classify_error(
    *,
    backend_name: str,
    raw: dict[str, Any],
    verification: dict[str, Any] | None,
    text: str,
    trusted_infrastructure: bool,
    verifier_timeout: bool,
    candidate_has_placeholder: bool,
    primary_diagnostic: dict[str, Any] | None,
) -> str | None:
    failed = bool(
        not raw.get("success", False)
        or (verification is not None and verification.get("success") is False)
    )
    if not failed:
        return None
    if trusted_infrastructure:
        return "infrastructure"
    if verifier_timeout:
        return "verifier_timeout"
    if backend_name == "python_execute":
        # Interpreter output is data, never evidence of a protocol error or a
        # proof verdict. Only the trusted handler classifies Python failures.
        kind = raw.get("error_kind")
        return kind if kind in {
            "invalid_arguments", "capability_disabled", "tool_timeout", "tool_output_limit",
        } else "tool_error"
    # Structured compiler diagnostics are authoritative.  Select exactly
    # one in source order so later errors cannot change the class.  A
    # mixed "no goals to be solved" message is deliberately excluded by
    # primary_verifier_diagnostic; it becomes actionable only when every
    # verifier error has that form.
    primary_kind = (
        primary_diagnostic.get("kind") if isinstance(primary_diagnostic, dict) else None
    )
    if primary_kind in {
        "unknown_identifier",
        "type_mismatch",
        "unsolved_goals",
        "redundant_tactic",
    }:
        return str(primary_kind)
    if primary_diagnostic is None and _argument_validation_error(backend_name, raw, verification):
        return "invalid_arguments"
    if candidate_has_placeholder:
        return "placeholder"
    if primary_diagnostic is not None:
        if verification is not None and not verification.get("success"):
            return "verification_failed"
        if backend_name in _LEAN_BACKENDS:
            return "verification_failed"
        return "tool_error"

    # Older/local tool adapters may expose only unstructured text. Keep a
    # compatibility fallback, but never use it when structured diagnostic
    # evidence was available.
    lower = text.lower()
    if "unknown tool" in lower:
        return "unknown_tool"
    if (
        "missing required argument" in lower
        or "provide exactly one" in lower
        or "typeerror:" in lower
        or "error: unterminated" in lower
        or "bad escape" in lower
    ):
        return "invalid_arguments"
    if "timed out" in lower or "timeout" in lower:
        return (
            "verifier_timeout"
            if backend_name in _LEAN_BACKENDS or verification is not None
            else "tool_timeout"
        )
    if any(
        marker in lower
        for marker in (
            "path escapes",
            "read-only",
            "must be under",
            "workspace-relative",
            "path must start",
            "filenotfounderror:",
            "file not found",
            "no such file",
        )
    ):
        return "invalid_path"
    if "unknown identifier" in lower or "unknown constant" in lower:
        return "unknown_identifier"
    if "type mismatch" in lower or "application type mismatch" in lower:
        return "type_mismatch"
    if "unsolved goals" in lower or "declaration uses 'sorry'" in lower:
        return "unsolved_goals"
    if verification is not None and not verification.get("success"):
        return "verification_failed"
    if not raw.get("success", False):
        return "tool_error"
    return None


def artifact_id(
    episode: ProverEpisode,
    dispatch: Dispatch,
    raw: dict[str, Any],
    verified: bool | None,
) -> str | None:
    has_snapshot = "proof_code" in raw
    content = raw["proof_code"] if has_snapshot else (
        dispatch.backend_arguments.get("content")
        or dispatch.backend_arguments.get("code")
    )
    path = raw.get("file_path") or dispatch.backend_arguments.get("file_path")
    if not has_snapshot and not content and path and (verified or source_bundle_evidence(raw) is not None):
        content = candidate.read_workspace_path(episode, str(path))
    if isinstance(content, str) and content:
        prefix = "verified" if verified else "artifact"
        return f"{prefix}:sha256:{candidate.source_fingerprint(content, raw)}"
    return f"path:{path}" if path else None
