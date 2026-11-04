"""Suppress explicitly identified style lint only in model-visible Lean feedback."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping


_STYLE_LINTERS = frozenset({
    "unnecessarySimpa", "unusedSimpArgs", "unusedVariables", "unusedSectionVars",
    "unusedTactic", "style.haveILetI",
})
_LINTER_NOTE = re.compile(
    r"(?m)^Note: This linter can be disabled with "
    r"`set_option linter\.([A-Za-z0-9_.]+) false`\.?[ \t]*$"
)
_LINTER_CODE = re.compile(
    r"^(?:\S+\.lean:\d+:\d+:\s*)?warning\(linter\.([A-Za-z0-9_.]+)\):"
)
_IMPORTANT = re.compile(r"\b(?:sorry|admit|axiom|unsafe|native_decide)\b", re.I)
_CONSOLE_DIAGNOSTIC = re.compile(
    r"(?m)^\S+\.lean:\d+:\d+: (?:error|warning|info)(?:\([^\n)]*\))?:"
)


def _message(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, Mapping):
        value = item.get("message", item.get("data"))
        return value if isinstance(value, str) else ""
    return ""


def _style_warning(item: Any) -> bool:
    if isinstance(item, Mapping) and item.get("severity", "warning") != "warning":
        return False
    text = _message(item)
    markers = [*_LINTER_NOTE.findall(text), *_LINTER_CODE.findall(text)]
    if isinstance(item, Mapping) and isinstance(item.get("code"), str):
        code = item["code"]
        if code.startswith("linter."):
            markers.append(code.removeprefix("linter."))
    # A malformed adapter may concatenate independent messages in one warning.
    # Never discard such an entry, or a mislabeled error, as style lint.
    headers = list(_CONSOLE_DIAGNOSTIC.finditer(text))
    if len(headers) > 1 or any(": error" in header.group() for header in headers):
        return False
    return (bool(markers) and all(name in _STYLE_LINTERS for name in markers)
            and not _IMPORTANT.search(text))


def _console_forms(item: Any) -> list[str]:
    """Known exact renderings only; never match a bare message inside probe output."""
    text = _message(item).rstrip("\r\n")
    if not text:
        return []
    if isinstance(item, str) or _CONSOLE_DIAGNOSTIC.match(text):
        return [text]
    file = item.get("file", item.get("filename"))
    pos = item.get("pos", item.get("position", {}))
    if not isinstance(pos, Mapping):
        return []
    line, column = pos.get("line", item.get("line")), pos.get("column", item.get("column"))
    if isinstance(file, str) and isinstance(line, int) and isinstance(column, int):
        forms = [f"{file}:{line}:{column}: warning: {text}"]
        code = item.get("code")
        if isinstance(code, str) and code.startswith("linter."):
            forms.append(f"{file}:{line}:{column}: warning({code}): {text}")
        return forms
    return []


def project_verifier_warnings(payload: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Return a copy, omission counts, and original warnings for console deduplication.

    The allowlist is deliberately narrow. Unknown warnings, proof placeholders,
    errors, and goals remain unchanged. No history or cross-task state is used.
    """
    result = dict(payload)
    warnings = payload.get("warnings")
    console_forms: list[str] = []
    seen: set[str] = set()
    omitted = duplicates = 0
    if isinstance(warnings, list):
        kept = []
        for item in warnings:
            console_forms.extend(_console_forms(item))
            identity = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
            if identity in seen:
                duplicates += 1
                continue
            seen.add(identity)
            if _style_warning(item):
                omitted += 1
            else:
                kept.append(item)
        result["warnings"] = kept

    # Some adapters only supply console output. A complete, recognized linter
    # footer supplies an exact end boundary, preserving following #check/#eval
    # output. Never consume an error or unknown warning after the lint message.
    known = set(console_forms)
    console_only: set[str] = set()
    for key in ("output", "stdout", "stderr"):
        text = result.get(key)
        if not isinstance(text, str):
            continue
        starts = list(_CONSOLE_DIAGNOSTIC.finditer(text))
        spans = []
        for index, start in enumerate(starts):
            if ": warning" not in start.group():
                continue
            end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
            note = _LINTER_NOTE.search(text, start.start(), end)
            if note is None:
                continue
            block = text[start.start():note.end()]
            if _style_warning(block):
                spans.append((start.start(), note.end()))
                if block not in known:
                    console_only.add(block)
        for start, end in reversed(spans):
            text = text[:start] + text[end:]
        if spans:
            result[key] = text.strip("\r\n")
    omitted += len(console_only)
    for key, count in (("linter_warnings_omitted", omitted),
                       ("duplicate_warnings_omitted", duplicates)):
        if count:
            result[key] = result.get(key, 0) + count
    return result, console_forms
