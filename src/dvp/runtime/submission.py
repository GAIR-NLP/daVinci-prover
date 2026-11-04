"""Deterministic initial exact-target draft for a DVP episode."""

from __future__ import annotations

from pathlib import Path
from typing import Any


SUBMISSION_PATH = "Submission.lean"


def seed_submission(
    workspace: Any,
    theorem_statement: str,
) -> Path | None:
    """Write the supplied task verbatim to the submission entry.

    The seed is initial workspace state, not a tool action: this function does
    not call Lean, update episode state, or award progress.  A custom registry
    without Davinci's workspace capability is left unchanged.
    """

    if workspace is None:
        return None
    resolve = getattr(workspace, "resolve", None)
    if not callable(resolve):
        return None
    if not isinstance(theorem_statement, str):
        raise TypeError("theorem_statement must be a string")

    path = Path(resolve(SUBMISSION_PATH, for_write=True))
    path.parent.mkdir(parents=True, exist_ok=True)
    # write_bytes avoids newline conversion: comments, Unicode, CRLF, and a
    # trailing newline are preserved exactly as supplied by the task loader.
    path.write_bytes(theorem_statement.encode("utf-8"))
    return path
