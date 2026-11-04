"""Lean tool handlers with draft/strict profile selection."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from dvp.toolkits.lean_feedback import (
    _MAX_LEAN_OUTPUT_CHARS, _contains_lean_hole, _annotate_verification,
    _lean_execution_status,
)

if TYPE_CHECKING:
    from dvp.toolkits.registry import ToolRegistry


async def lean_verify(registry: ToolRegistry, args: dict[str, Any]) -> dict[str, Any]:
    """Check inline code or a submission file with a derived profile."""

    code = args.get("code")
    file_path = args.get("file_path")
    if (
        not isinstance(code, (str, type(None)))
        or not isinstance(file_path, (str, type(None)))
    ):
        return {
            "success": False,
            "error": "code and file_path must be strings or null",
        }
    if (code is None) == (file_path is None):
        return {
            "success": False,
            "error": "Provide exactly one of code or file_path",
        }
    if isinstance(code, str) and not code.strip():
        return {
            "success": False,
            "error": "code must not be empty",
        }
    if isinstance(file_path, str) and not file_path.strip():
        return {
            "success": False,
            "error": "file_path must not be empty",
        }

    path: Path | None = None
    if file_path is not None:
        path = registry.workspace.resolve(file_path)
        allowed = (
            path.resolve() == registry.workspace.submission_file.resolve()
            or path.resolve().is_relative_to(registry.workspace.submission_dir.resolve())
        )
        if not allowed:
            return {
                "success": False,
                "error": (
                    "lean_verify file_path must be Submission.lean or under Submission/"
                ),
            }
        try:
            code = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {
                "success": False,
                "error": f"File not found: {file_path}",
            }
    assert isinstance(code, str)

    is_draft = _contains_lean_hole(code)
    verifier = registry.draft_verifier if is_draft else registry.verifier
    if path is None:
        verification = await verifier.verify_code(
            code,
            max_output_chars=_MAX_LEAN_OUTPUT_CHARS,
        )
    else:
        verification = await verifier.verify_file(
            path,
            max_output_chars=_MAX_LEAN_OUTPUT_CHARS,
        )
    annotated = _annotate_verification(
        verification,
        is_draft=is_draft,
        code=code,
    )
    annotated["lean_executed"] = _lean_execution_status(verification)
    if path is not None:
        annotated["file_path"] = str(file_path)
    return annotated
