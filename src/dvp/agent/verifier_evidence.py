"""Read verifier-owned evidence without trusting compiler-controlled text."""

from __future__ import annotations

import json
import re
from typing import Any

from dvp.protocol import ToolOutcome
from dvp.verification.diagnostics import (
    extract_goal_conclusions,
    normalize_goal_conclusion,
    primary_unsolved_goal_conclusions,
)

from .policy import (
    _LOCAL_TIMEOUT_PREFIX,
    _MISSING_LOCAL_VERIFIER,
)


def _verifier_error_messages(result: dict[str, Any]) -> tuple[str, ...]:
    """Return verifier-owned diagnostic fields, excluding compiler output."""

    messages: list[str] = []
    error = result.get("error")
    if isinstance(error, str):
        messages.append(error)
    errors = result.get("errors")
    if isinstance(errors, list):
        messages.extend(str(item) for item in errors if isinstance(item, str))
    elif isinstance(errors, str):
        messages.append(errors)
    return tuple(messages)


def _verifier_candidates(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Return top-level, strict, and permissive verifier result objects."""

    candidates = [raw]
    value = raw.get("verification")
    if isinstance(value, dict):
        candidates.append(value)
    return candidates


def _raw_verification_profile(raw: dict[str, Any]) -> str | None:
    for candidate in _verifier_candidates(raw):
        value = candidate.get("verification_profile")
        if value in {"draft_allow_sorry", "strict"}:
            return str(value)
    return None


def source_bundle_evidence(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Read structured source identity, never compiler-controlled output.

    File writes wrap the verifier result. Conflicting outer/inner identities
    must remain invalid evidence instead of silently selecting either one.
    """
    keys = ("source_bundle_sha256", "source_bundle_manifest")
    candidates = [
        {key: result.get(key) for key in keys}
        for result in _verifier_candidates(raw)
        if any(key in result for key in keys)
    ]
    if not candidates:
        return None
    if any(result != candidates[0] for result in candidates[1:]):
        return dict.fromkeys(keys)
    return candidates[0]


def _raw_source_placeholder_count(raw: dict[str, Any]) -> int | None:
    for candidate in _verifier_candidates(raw):
        value = candidate.get("source_placeholders")
        if not isinstance(value, dict):
            continue
        total = value.get("total")
        if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
            return total
    return None


def _raw_draft_typechecked(raw: dict[str, Any]) -> bool:
    return _raw_verification_profile(raw) == "draft_allow_sorry" and any(
        candidate.get("draft_typechecked") is True
        for candidate in _verifier_candidates(raw)
    )


def _trusted_verifier_infrastructure(raw: dict[str, Any]) -> bool:
    """Recognize only verifier-structured infrastructure failures.

    Lean compiler diagnostics are influenced by policy-generated code.  Their
    text must therefore never decide whether an RL sample is masked.  The
    checks require an explicit harness marker or the exact registry-owned
    startup error for a missing local verifier. Verifier timeouts are
    classified separately and never produce a loss-masked infrastructure abort.
    """

    for result in _verifier_candidates(raw):
        if result.get("_harness_infrastructure_error") is True:
            return True
        if result.get("success") is not False:
            continue

        command = result.get("command")
        if (
            result is raw
            and result.get("error") == _MISSING_LOCAL_VERIFIER
            and command is None
            and "returncode" not in result
        ):
            return True
    return False


def _structured_verifier_timeout(raw: dict[str, Any]) -> bool:
    """Recognize verifier timeouts without treating them as maskable outages."""

    for result in _verifier_candidates(raw):
        if result.get("success") is not False:
            continue
        messages = _verifier_error_messages(result)
        command = result.get("command")
        if not isinstance(command, (list, tuple)) or not command:
            continue
        if (
            "returncode" not in result
            and not result.get("output")
            and any(message.startswith(_LOCAL_TIMEOUT_PREFIX) for message in messages)
        ):
            return True
    return False


def normalize_lean_evidence(value: str) -> str:
    """Normalize unstable transport/location data, retaining Lean evidence."""

    normalized = re.sub(
        r"(?i)(custom[_-]?id['\"\s:=]+)[A-Za-z0-9_-]+",
        r"\1<ID>",
        value,
    )
    normalized = re.sub(r"(['\"]?env['\"]?\s*:\s*)\d+", r"\1<N>", normalized)
    normalized = re.sub(
        r"(?i)\bsha256:[0-9a-f]{32,64}\b",
        "<HASH>",
        normalized,
    )
    normalized = re.sub(
        r"(?i)\b[0-9a-f]{64}\b",
        "<HASH>",
        normalized,
    )
    normalized = re.sub(
        r"(?m)(?:[A-Za-z0-9_./-]+\.lean):\d+:\d+:",
        "<FILE>:<LINE>:<COL>:",
        normalized,
    )
    normalized = re.sub(
        r"(?m)^\s*\d+:\d+:\s*",
        "<LINE>:<COL>: ",
        normalized,
    )
    normalized = re.sub(
        r"(?i)\bline\s+\d+(?:\s*,?\s*column\s+\d+)?",
        "line <N> column <N>",
        normalized,
    )
    return re.sub(r"\s+", " ", normalized).strip()[:8000]


def normalized_error_evidence(outcome: ToolOutcome) -> str:
    raw = outcome.raw
    primary = raw.get("primary_diagnostic")
    if isinstance(primary, dict) and primary.get("message"):
        return normalize_lean_evidence(str(primary["message"]))
    verification = raw.get("verification")
    structured: list[Any] = []
    if isinstance(verification, dict):
        for key in ("errors", "error"):
            if verification.get(key):
                structured.append(verification[key])
    for key in ("errors", "error"):
        if raw.get(key):
            structured.append(raw[key])

    if structured:
        first = structured[0]
        if isinstance(first, (list, tuple)):
            first = next((item for item in first if item), "")
        sources = [first] if first else []
    else:
        sources = []
        if isinstance(verification, dict):
            for key in ("message", "stderr", "output"):
                if verification.get(key):
                    sources.append(verification[key])
                    break
        if not sources:
            for key in ("message", "stderr", "output"):
                if raw.get(key):
                    sources.append(raw[key])
                    break
        if not sources and outcome.error_kind:
            sources.append(outcome.feedback)
    rendered = "\n".join(
        (
            item
            if isinstance(item, str)
            else json.dumps(item, ensure_ascii=False, default=str)
        )
        for item in sources
    )
    return normalize_lean_evidence(rendered)


def target_unsolved_goals(outcome: ToolOutcome) -> tuple[str, ...]:
    """Return all goals from the selected diagnostic, not its display cap."""

    if outcome.error_kind != "unsolved_goals":
        return ()
    raw = outcome.raw
    verification = raw.get("verification")
    if not isinstance(verification, dict):
        verification = raw.get("draft_verification")
    if not isinstance(verification, dict) and outcome.backend_name == "lean_verify":
        verification = raw
    return primary_unsolved_goal_conclusions(raw, verification)


def target_draft_hole_goals(
    outcome: ToolOutcome,
) -> tuple[str, ...]:
    """Return normalized conclusions from bounded draft-hole feedback."""

    candidates: list[Any] = [outcome.raw.get("holes")]
    verification = outcome.raw.get("verification")
    if isinstance(verification, dict):
        candidates.append(verification.get("holes"))

    goals: list[str] = []
    for holes in candidates:
        if not isinstance(holes, dict):
            continue
        items = holes.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_goal = item.get("goal")
            if not isinstance(raw_goal, str) or not raw_goal.strip():
                continue
            extracted = extract_goal_conclusions(raw_goal)
            if extracted:
                goals.extend(extracted)
            else:
                normalized = normalize_goal_conclusion(raw_goal)
                if normalized:
                    goals.append(normalized)
        if goals:
            break
    return tuple(goals)
