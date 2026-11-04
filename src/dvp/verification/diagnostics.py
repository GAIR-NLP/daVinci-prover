"""Deterministic, compact projection of Lean verifier diagnostics."""

from __future__ import annotations

import json
import re
from typing import Any


_NO_GOALS = "no goals to be solved"
_PRIMARY_GOAL_LIMIT = 3
_PRIMARY_GOAL_TEXT_LIMIT = 2000
_FORMATTED_ERROR = re.compile(
    r"^(?:(?P<file>.*?\.lean):)?"
    r"(?P<line>\d+):(?P<column>\d+):\s*"
    r"(?:(?:error|warning|info):\s*)?(?P<message>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_TURNSTILE_LINE = re.compile(r"^[ \t]*⊢[ \t]*(?P<goal>.*)$")
_CASE_LINE = re.compile(r"^[ \t]*case(?:[ \t]+.*)?$")
_FLATTENED_CASE = re.compile(r"\s+case\s+(?=\S+(?:\s|$))")
_ELABORATOR_METAVAR = re.compile(
    r"(?<![A-Za-z0-9_?«])"
    r"\?(?:[mu]\.\d+|[A-Za-z_][A-Za-z0-9_']*)"
    r"(?![A-Za-z0-9_']|»)"
)
_LEAN_QUOTED_TEXT = re.compile(r'"(?:\\.|[^"\\])*"|«[^»]*»', re.DOTALL)


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )


def _coordinate(*values: Any) -> int | None:
    return next(
        (
            value
            for value in values
            if isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        ),
        None,
    )


def normalize_goal_conclusion(value: str) -> str:
    """Normalize only presentation noise in one Lean goal conclusion.

    Elaboration-generated metavariable names are unstable between otherwise
    equivalent checks.  Replacing them is deliberately the only
    semantic-looking rewrite here: expressions are not simplified, reordered,
    or algebraically normalized.
    """

    parts: list[str] = []
    cursor = 0
    for quoted in _LEAN_QUOTED_TEXT.finditer(value):
        unquoted = _ELABORATOR_METAVAR.sub(
            "?_",
            value[cursor:quoted.start()],
        )
        parts.append(re.sub(r"\s+", " ", unquoted))
        parts.append(quoted.group())
        cursor = quoted.end()
    unquoted = _ELABORATOR_METAVAR.sub("?_", value[cursor:])
    parts.append(re.sub(r"\s+", " ", unquoted))
    return "".join(parts).strip()


def _contains_elaborator_metavar(value: str) -> bool:
    cursor = 0
    for quoted in _LEAN_QUOTED_TEXT.finditer(value):
        if _ELABORATOR_METAVAR.search(value, cursor, quoted.start()):
            return True
        cursor = quoted.end()
    return _ELABORATOR_METAVAR.search(value, cursor) is not None


def _multiline_goal_conclusions(message: str) -> list[str]:
    """Extract turnstile conclusions while line boundaries are still intact."""

    lines = message.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    conclusions: list[str] = []
    index = 0
    while index < len(lines):
        match = _TURNSTILE_LINE.match(lines[index])
        if match is None:
            index += 1
            continue

        parts = [match.group("goal").strip()]
        index += 1
        while index < len(lines):
            continuation = lines[index]
            if (
                not continuation.strip()
                or _TURNSTILE_LINE.match(continuation)
                or _CASE_LINE.match(continuation)
            ):
                break
            parts.append(continuation.strip())
            index += 1
        conclusion = normalize_goal_conclusion(" ".join(parts))
        if conclusion:
            conclusions.append(conclusion)
    return conclusions


def _unquoted_turnstile_offsets(message: str) -> list[int]:
    offsets: list[int] = []
    cursor = 0
    for quoted in _LEAN_QUOTED_TEXT.finditer(message):
        offsets.extend(
            cursor + match.start()
            for match in re.finditer("⊢", message[cursor:quoted.start()])
        )
        cursor = quoted.end()
    offsets.extend(
        cursor + match.start()
        for match in re.finditer("⊢", message[cursor:])
    )
    return offsets


def _first_unquoted_match(
    pattern: re.Pattern[str],
    value: str,
) -> re.Match[str] | None:
    cursor = 0
    for quoted in _LEAN_QUOTED_TEXT.finditer(value):
        match = pattern.search(value, cursor, quoted.start())
        if match is not None:
            return match
        cursor = quoted.end()
    return pattern.search(value, cursor)


def _flattened_goal_conclusions(message: str) -> list[str]:
    """Compatibility extraction for adapters that already collapsed newlines."""

    offsets = _unquoted_turnstile_offsets(message)
    conclusions: list[str] = []
    for index, offset in enumerate(offsets):
        end = offsets[index + 1] if index + 1 < len(offsets) else len(message)
        fragment = message[offset + 1:end]
        # Between two turnstiles, a flattened Lean message normally contains
        # the next ``case`` header and its local context.  It is not part of
        # the preceding conclusion.  If no recognizable header survives,
        # retain the whole fragment rather than guessing at Lean syntax.
        if index + 1 < len(offsets):
            next_case = _first_unquoted_match(_FLATTENED_CASE, fragment)
            if next_case is not None:
                fragment = fragment[: next_case.start()]
        conclusion = normalize_goal_conclusion(fragment)
        if conclusion:
            conclusions.append(conclusion)
    return conclusions


def extract_goal_conclusions(message: str) -> tuple[str, ...]:
    """Return ordered, whitespace/meta-stable ``⊢`` conclusions."""

    if "⊢" not in message:
        return ()
    if "\n" in message or "\r" in message:
        multiline = _multiline_goal_conclusions(message)
        if multiline:
            return tuple(multiline)
    return tuple(_flattened_goal_conclusions(message))


def select_primary_goal(goals: tuple[str, ...]) -> str | None:
    """Prefer the first closed-looking goal, otherwise preserve source order."""

    if not goals:
        return None
    return next(
        (goal for goal in goals if not _contains_elaborator_metavar(goal)),
        goals[0],
    )


def _normalize(item: Any, index: int) -> dict[str, Any] | None:
    if isinstance(item, str):
        message = item
        result: dict[str, Any] = {"_index": index}
    elif isinstance(item, dict):
        message = _text(
            item.get("message", item.get("data", item.get("error")))
        )
        result = {"_index": index}
        file_name = item.get("file") or item.get("filename")
        if isinstance(file_name, str) and file_name:
            result["file"] = file_name
        position = item.get("pos", item.get("position"))
        end_position = item.get("endPos", item.get("end_position"))
        position = position if isinstance(position, dict) else {}
        end_position = end_position if isinstance(end_position, dict) else {}
        coordinates = {
            "line": _coordinate(
                position.get("line"),
                item.get("line"),
                item.get("line_start"),
            ),
            "column": _coordinate(
                position.get("column"),
                item.get("column"),
                item.get("column_start"),
            ),
            "end_line": _coordinate(
                end_position.get("line"),
                item.get("end_line"),
                item.get("line_end"),
            ),
            "end_column": _coordinate(
                end_position.get("column"),
                item.get("end_column"),
                item.get("column_end"),
            ),
        }
        result.update({
            key: value
            for key, value in coordinates.items()
            if value is not None
        })
    else:
        return None

    match = _FORMATTED_ERROR.match(message.strip())
    if match:
        message = match.group("message")
        result.setdefault("line", int(match.group("line")))
        result.setdefault("column", int(match.group("column")))
        if match.group("file"):
            result.setdefault("file", match.group("file"))
    # Goal extraction must precede both whitespace folding and the 2K
    # diagnostic display cap.  ``_goals`` is internal to source selection and
    # is removed before the public primary diagnostic is returned.
    goals = extract_goal_conclusions(message)
    if goals:
        result["_goals"] = goals
    message = re.sub(r"\s+", " ", message).strip()
    if not message:
        return None
    result["message"] = message[:2000]
    return result


def _diagnostics(result: dict[str, Any]) -> list[dict[str, Any]]:
    errors = result.get("errors")
    items = errors if isinstance(errors, list) else ([errors] if errors else [])
    return [
        diagnostic
        for index, item in enumerate(items)
        if (diagnostic := _normalize(item, index)) is not None
    ]


def _source_ordered(
    raw: dict[str, Any],
    verification: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    candidates = [verification] if isinstance(verification, dict) else []
    value = raw.get("verification")
    if isinstance(value, dict) and all(
        value is not item for item in candidates
    ):
        candidates.append(value)
    if all(raw is not item for item in candidates):
        candidates.append(raw)

    diagnostics: list[dict[str, Any]] = []
    for candidate in candidates:
        diagnostics = _diagnostics(candidate)
        if diagnostics:
            break
    return sorted(
        diagnostics,
        key=lambda item: (
            0 if isinstance(item.get("line"), int) else 1,
            item.get("line", 0),
            item.get("column", 0),
            item["_index"],
        ),
    )


def _kind(message: str) -> str:
    lower = message.lower()
    if "unknown identifier" in lower or "unknown constant" in lower:
        return "unknown_identifier"
    if "type mismatch" in lower or "application type mismatch" in lower:
        return "type_mismatch"
    if "unsolved goals" in lower or "declaration uses 'sorry'" in lower:
        return "unsolved_goals"
    return "verification_failed"


def _primary_actionable(
    diagnostics: list[dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    no_goals = [
        item
        for item in diagnostics
        if _NO_GOALS in str(item["message"]).lower()
    ]
    only_no_goals = len(no_goals) == len(diagnostics)
    actionable = (
        diagnostics
        if only_no_goals
        else [item for item in diagnostics if item not in no_goals]
    )
    return actionable[0], only_no_goals


def primary_unsolved_goal_conclusions(
    raw: dict[str, Any],
    verification: dict[str, Any] | None,
) -> tuple[str, ...]:
    """Return every conclusion from the selected unsolved-goals diagnostic.

    This lossless internal projection is intentionally separate from the
    capped model-facing ``primary_goals`` summary.
    """

    diagnostics = _source_ordered(raw, verification)
    if diagnostics:
        primary, only_no_goals = _primary_actionable(diagnostics)
        if (
            not only_no_goals
            and _kind(str(primary["message"])) == "unsolved_goals"
        ):
            goals = primary.get("_goals")
            if isinstance(goals, tuple):
                return goals

    # Compatibility for callers replaying an already-projected diagnostic.
    primary = raw.get("primary_diagnostic")
    if not isinstance(primary, dict) or primary.get("kind") != "unsolved_goals":
        return ()
    displayed = primary.get("primary_goals")
    if isinstance(displayed, list):
        normalized = [
            normalize_goal_conclusion(item)
            for item in displayed
            if isinstance(item, str)
        ]
        return tuple(goal for goal in normalized if goal)
    message = primary.get("message")
    return extract_goal_conclusions(message) if isinstance(message, str) else ()


def primary_verifier_diagnostic(
    raw: dict[str, Any],
    verification: dict[str, Any] | None,
) -> tuple[dict[str, Any] | None, int, dict[str, Any] | None]:
    """Return the first actionable error and an optional non-mutating repair."""

    diagnostics = _source_ordered(raw, verification)
    if not diagnostics:
        return None, 0, None

    selected, only_no_goals = _primary_actionable(diagnostics)
    primary = dict(selected)
    primary.pop("_index", None)
    goals = primary.pop("_goals", ())
    primary["kind"] = (
        "redundant_tactic"
        if only_no_goals
        else _kind(str(primary["message"]))
    )
    if primary["kind"] == "unsolved_goals" and goals:
        primary_goal = select_primary_goal(goals)
        if primary_goal is not None:
            primary["primary_goal"] = primary_goal[:_PRIMARY_GOAL_TEXT_LIMIT]
        displayed = [
            goal[:_PRIMARY_GOAL_TEXT_LIMIT]
            for goal in goals[:_PRIMARY_GOAL_LIMIT]
        ]
        primary["primary_goals"] = displayed
        omitted = len(goals) - len(displayed)
        if omitted:
            primary["primary_goals_omitted"] = omitted

    if not only_no_goals:
        return primary, len(diagnostics), None
    repair: dict[str, Any] = {
        "action": "delete_redundant_tactic",
        "applied": False,
        "automatic_retry": False,
        "instruction": (
            "Delete the tactic at the reported source location or range, "
            "then verify the revised candidate."
        ),
    }
    if isinstance(primary.get("line"), int):
        start = {"line": primary["line"]}
        if isinstance(primary.get("column"), int):
            start["column"] = primary["column"]
        source_range: dict[str, Any] = {"start": start}
        if (
            isinstance(primary.get("end_line"), int)
            or isinstance(primary.get("end_column"), int)
        ):
            end = {
                "line": primary.get("end_line", primary["line"]),
            }
            if isinstance(primary.get("end_column"), int):
                end["column"] = primary["end_column"]
            source_range["end"] = end
        repair["source_range"] = source_range
    return primary, len(diagnostics), repair
