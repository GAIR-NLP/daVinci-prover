"""Declaration routing for candidate Lean tools.

These scans identify likely target submissions. Lean compiles the candidate;
final proof acceptance belongs exclusively to upstream comparator.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from dvp.verification.lean import (
    mask_lean_comments_and_strings,
    target_theorem_header,
)


_DECLARATION_RE = re.compile(
    r"(?m)^[ \t]*"
    r"(?:@\[[^\]]*\][ \t\r\n]*)*"
    r"(?P<start>(?:(?:private|protected|noncomputable|unsafe)[ \t\r\n]+)*)"
    r"(?P<kind>theorem|lemma)[ \t\r\n]+"
    r"(?P<name>[^\s:({]+)"
)
_BLOCK_COMMAND_RE = re.compile(
    r"(?m)^[ \t]*(?P<command>namespace|section|end)\b"
    r"(?P<tail>[^\r\n]*)"
)


@dataclass(frozen=True)
class _Declaration:
    start: int
    full_name: str


def contains_exact_target_declaration(
    code: str,
    theorem_statement: str,
    *,
    target_name: str | None = None,
) -> bool:
    """Return whether ``code`` contains the requested declaration itself.

    Matching is whitespace/comment insensitive but deliberately not
    type-equivalence based. Use this for callers that require the literal
    declaration header, not as the gate to Comparator. The declaration must
    also have the same fully qualified name, so wrapping a decoy in another
    namespace is rejected.
    """

    expected_declarations = _declarations(theorem_statement)
    expected = next(
        (declaration for declaration in expected_declarations
         if target_name is None or declaration.full_name == target_name),
        None,
    )
    if expected is None:
        return False
    required_header = target_theorem_header(theorem_statement[expected.start:])
    if not required_header:
        return False

    required_mask = mask_lean_comments_and_strings(required_header)
    required_compact = _compact(required_mask)
    if not required_compact:
        return False

    expected_name = expected.full_name

    masked = mask_lean_comments_and_strings(code)
    for declaration in _declarations(code, masked=masked):
        if declaration.full_name != expected_name:
            continue
        suffix = _compact(masked[declaration.start :])
        if suffix.startswith(required_compact + ":="):
            return True
    return False


def contains_target_candidate_declaration(
    code: str,
    theorem_statement: str,
    *,
    target_name: str | None = None,
) -> bool:
    """Identify a declaration to route to Comparator by its qualified name.

    The candidate may spell an equivalent type differently, or state an
    entirely different proposition. This scan establishes neither equivalence
    nor acceptance; Lean checking and source-bound Comparator evidence
    remain necessary. Namespaced shadows and helper-only code are excluded.
    """

    expected_declarations = _declarations(theorem_statement)
    expected = next(
        (declaration for declaration in expected_declarations
         if target_name is None or declaration.full_name == target_name),
        None,
    )
    if expected is None:
        return False
    expected_name = expected.full_name
    return any(
        declaration.full_name == expected_name
        for declaration in _declarations(code)
    )


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _declarations(
    code: str,
    *,
    masked: str | None = None,
) -> list[_Declaration]:
    masked = masked if masked is not None else mask_lean_comments_and_strings(code)
    blocks: list[tuple[str, str | None]] = []
    commands = list(_BLOCK_COMMAND_RE.finditer(masked))
    command_index = 0
    declarations: list[_Declaration] = []

    for match in _DECLARATION_RE.finditer(masked):
        while (
            command_index < len(commands)
            and commands[command_index].start() < match.start()
        ):
            _apply_block_command(blocks, commands[command_index])
            command_index += 1

        if _delimiter_depth(masked, match.start()) != 0:
            continue

        namespaces = [
            name
            for kind, name in blocks
            if kind == "namespace" and name
        ]
        declared_name = match.group("name")
        full_name = ".".join([*namespaces, declared_name])
        declarations.append(
            _Declaration(
                start=match.start("start"),
                full_name=full_name,
            )
        )
    return declarations


def _apply_block_command(
    blocks: list[tuple[str, str | None]],
    match: re.Match[str],
) -> None:
    if _delimiter_depth(match.string, match.start()) != 0:
        return
    command = match.group("command")
    tail = match.group("tail").strip()
    if command == "namespace":
        name = tail.split()[0] if tail else None
        blocks.append(("namespace", name))
    elif command == "section":
        name = tail.split()[0] if tail else None
        blocks.append(("section", name))
    elif blocks:
        blocks.pop()


def _delimiter_depth(text: str, stop: int) -> int:
    """Return bracket depth before ``stop`` in already-masked source."""

    depth = 0
    for char in text[:stop]:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth = max(0, depth - 1)
    return depth
