"""Backend sets and structured-error markers for the episode contract."""

from __future__ import annotations

import re

from dvp.toolkits.schemas import OPTIONAL_TOOL_NAMES, TOOL_NAMES

_LEAN_BACKENDS = {
    "lean_verify",
}

_TOOL_NAME_SET = frozenset((*TOOL_NAMES, *OPTIONAL_TOOL_NAMES))

# Protocol/argument errors, not failed proofs or failed tool execution.
_FORMAT_ERROR_KINDS = frozenset({
    "response_format_error",
    "tool_parse_error",
    "capability_disabled",
    "unknown_tool",
    "invalid_arguments",
})

_LOOKUP_BACKENDS = {
    "lean_retrieve",
    "content_search",
    "file_search",
    "file_read",
}

_VERIFICATION_BACKENDS = {
    "lean_verify",
}


def verification_backend_enabled(backend: str) -> bool:
    """Recognize explicit Lean checks and checks attached to saved files."""
    return backend in _VERIFICATION_BACKENDS or backend in {"file_edit", "file_write"}

_NO_LOGICAL_VERIFIER_EVIDENCE = frozenset(
    {
        "infrastructure",
        "verifier_timeout",
        "tool_timeout",
        "invalid_arguments",
        "tool_parse_error",
        "output_truncated",
        "invalid_path",
        "unknown_tool",
        "capability_disabled",
        "tool_error",
        "lean_budget_exhausted",
        "tool_budget_exhausted",
    }
)

_LOCAL_TIMEOUT_PREFIX = "Lean verification timed out after "

_MISSING_LOCAL_VERIFIER = "RuntimeError: Could not find `lake` or `lean` on PATH"

_NAMED_AUXILIARY_DECLARATION_RE = re.compile(
    r"(?m)^[ \t]*(?:@\[[^\]]*\][ \t\r\n]*)*"
    r"(?:(?:private|protected|noncomputable|unsafe)[ \t\r\n]+)*"
    r"(?:lemma|theorem)[ \t\r\n]+\S+"
)
