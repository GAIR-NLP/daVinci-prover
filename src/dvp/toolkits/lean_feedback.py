"""Stable Lean verification profiles and bounded hole feedback."""

from __future__ import annotations

import re
from typing import Any

from dvp.verification.lean import contains_placeholder
from dvp.verification.contracts import mask_lean_comments_and_strings


_MAX_LEAN_OUTPUT_CHARS = 12_000
_MAX_HOLES = 3
_MAX_HOLE_GOAL_CHARS = 4_000
_MAX_SOURCE_PLACEHOLDERS = 3
_SOURCE_PLACEHOLDER_RE = re.compile(
    r"(?<![\w'])(?:sorry|admit)(?![\w'])"
)


def _contains_lean_hole(code: str) -> bool:
    return contains_placeholder(mask_lean_comments_and_strings(code))


def _annotate_verification(
    verification: dict[str, Any],
    *,
    is_draft: bool,
    code: str,
) -> dict[str, Any]:
    """Describe compilation and unfinished holes, never final acceptance."""

    elaborated = bool(verification.get("success"))
    holes = _project_holes(verification)
    source_placeholders = project_source_placeholders(code)
    dependencies = verification.get("dependency_placeholders")
    dependency_holes = isinstance(dependencies, dict) and bool(dependencies.get("total"))
    sorry_warning = any(
        re.search(r"declaration uses ['`]sorry['`]", str(warning))
        for warning in verification.get("warnings") or []
    )
    # A helper or macro can introduce a hole even when the entry contains no
    # literal sorry. Preserve successful compilation without submitting an
    # unfinished draft to the once-per-attempt Comparator verdict.
    is_draft = bool(is_draft or source_placeholders["total"] or holes["total"]
                    or dependency_holes or sorry_warning)
    profile = "draft_allow_sorry" if is_draft else "strict"
    return {
        **verification,
        "verification_profile": profile,
        "draft_typechecked": bool(elaborated and is_draft),
        "strict_verified": bool(elaborated and not is_draft),
        "holes": holes,
        "source_placeholders": source_placeholders,
    }


def _lean_execution_status(verification: dict[str, Any]) -> bool | None:
    """Distinguish completed checks, preflight rejection, and unknown launch state.

    A command without a return code may only have been planned: modular checks
    can exhaust their deadline before launching it. Infrastructure failures can
    likewise report a wrapper's exit code without having launched Lean.
    """
    explicit = verification.get("lean_executed")
    if isinstance(explicit, bool):
        return explicit
    compiled = verification.get("compiled_modules")
    if isinstance(compiled, list) and compiled:
        return True
    if "command" not in verification:
        return None
    command = verification["command"]
    if command is None or command == []:
        return False
    returncode = verification.get("returncode")
    if (
        isinstance(command, (list, tuple))
        and command
        and isinstance(returncode, int)
        and not isinstance(returncode, bool)
        and not verification.get("_harness_infrastructure_error")
    ):
        return True
    return None


def _project_holes(verification: dict[str, Any]) -> dict[str, Any]:
    """Project elaborated hole goals without discarding the raw verification result."""

    raw_sorries = _raw_sorries(verification)
    if not isinstance(raw_sorries, list):
        raw_sorries = []

    items: list[dict[str, Any]] = []
    for raw in raw_sorries:
        if len(items) >= _MAX_HOLES:
            break
        projected = _project_hole(raw)
        if projected:
            items.append(projected)
    total = len(raw_sorries)
    return {
        "total": total,
        "omitted": max(0, total - len(items)),
        "items": items,
    }


def project_source_placeholders(code: str) -> dict[str, Any]:
    """Count source assumptions independently of verifier elaboration.

    A compiler error may prevent Lean from returning any local sorry goals.
    The masked source still gives an exact, deterministic count and location
    for every explicit ``sorry``/``admit`` token.
    """

    masked = mask_lean_comments_and_strings(code)
    matches = list(_SOURCE_PLACEHOLDER_RE.finditer(masked))
    items = [
        {
            "kind": match.group(0),
            "range": {
                "start": _source_position(code, match.start()),
                "end": _source_position(code, match.end()),
            },
        }
        for match in matches[:_MAX_SOURCE_PLACEHOLDERS]
    ]
    return {
        "total": len(matches),
        "omitted": max(0, len(matches) - len(items)),
        "items": items,
    }


def _source_position(code: str, offset: int) -> dict[str, int]:
    line_start = code.rfind("\n", 0, offset) + 1
    return {
        "line": code.count("\n", 0, offset) + 1,
        "column": offset - line_start,
    }


def _raw_sorries(verification: dict[str, Any]) -> Any:
    """Read nested or flattened local draft results."""

    candidates: list[Any] = []
    response = verification.get("response")
    if isinstance(response, dict):
        candidates.append(response.get("sorries"))
    candidates.append(verification.get("sorries"))
    return next((value for value in candidates if isinstance(value, list)), [])


def _project_hole(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    start = _project_position(
        value.get("pos", value.get("position", value.get("start")))
    )
    if not start:
        start = _project_flat_position(
            value,
            line_keys=("line", "line_start", "start_line"),
            column_keys=("column", "column_start", "start_column"),
        )
    end = _project_position(
        value.get(
            "endPos",
            value.get("end_position", value.get("end")),
        )
    )
    if not end:
        end = _project_flat_position(
            value,
            line_keys=("end_line", "line_end"),
            column_keys=("end_column", "column_end"),
        )
    if start:
        source_range: dict[str, Any] = {"start": start}
        if end:
            source_range["end"] = end
        result["range"] = source_range
    goal = value.get("goal")
    if isinstance(goal, str) and goal.strip():
        result["goal"] = _truncate_hole_goal(goal)
    return result or None


def _project_position(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, int] = {}
    for key in ("line", "column"):
        coordinate = value.get(key)
        if (
            isinstance(coordinate, int)
            and not isinstance(coordinate, bool)
            and coordinate >= 0
        ):
            result[key] = coordinate
    return result


def _project_flat_position(
    value: dict[str, Any],
    *,
    line_keys: tuple[str, ...],
    column_keys: tuple[str, ...],
) -> dict[str, int]:
    result: dict[str, int] = {}
    line = next(
        (
            value.get(key)
            for key in line_keys
            if _is_coordinate(value.get(key))
        ),
        None,
    )
    column = next(
        (
            value.get(key)
            for key in column_keys
            if _is_coordinate(value.get(key))
        ),
        None,
    )
    if line is not None:
        result["line"] = line
    if column is not None:
        result["column"] = column
    return result


def _is_coordinate(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    )


def _truncate_hole_goal(goal: str) -> str:
    if len(goal) <= _MAX_HOLE_GOAL_CHARS:
        return goal
    return goal[: _MAX_HOLE_GOAL_CHARS - 18] + "\n...<truncated>..."
