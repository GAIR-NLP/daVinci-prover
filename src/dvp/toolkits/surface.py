"""Capability-filtered access to Davinci's compatible tool surface."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from dvp.toolkits.schemas import normalize_tool_name, visible_schemas
from dvp.runtime.submission import SUBMISSION_PATH


@dataclass(frozen=True)
class Dispatch:
    """A compatibility action normalized for the trusted DVP registry."""

    backend_name: str
    backend_arguments: dict[str, Any]


class CompatibilitySurface:
    """Preserve Davinci schemas and calls while hiding disabled capabilities."""

    def __init__(self, registry: Any, *, enable_lean_retrieve: bool = True) -> None:
        self.registry = registry
        self.enable_lean_retrieve = enable_lean_retrieve

    def schemas(self) -> list[dict[str, Any]]:
        return visible_schemas(
            self.registry.schemas(), enable_lean_retrieve=self.enable_lean_retrieve,
        )

    @staticmethod
    def resolve(name: str, arguments: dict[str, Any]) -> Dispatch:
        backend_name = canonical_tool_name(name)
        normalized = dict(arguments)
        batch_edit = backend_name == "file_edit" and "edits" in normalized
        if backend_name in {"file_write", "file_edit"} and not batch_edit and not normalized.get("file_path"):
            normalized["file_path"] = (
                normalized.pop("path", None)
                or SUBMISSION_PATH
            )
        for key in ("line_range", "old_line_range"):
            if key in normalized:
                normalized[key] = _normalize_line_range(normalized[key])
        if batch_edit and isinstance(normalized["edits"], list):
            normalized["edits"] = [
                {
                    **edit,
                    **({"old_line_range": _normalize_line_range(edit["old_line_range"])}
                       if "old_line_range" in edit else {}),
                }
                if isinstance(edit, dict) else edit
                for edit in normalized["edits"]
            ]
        return Dispatch(backend_name, normalized)


def _normalize_line_range(value: Any) -> Any:
    """Repair common scalar/string range calls without changing valid arrays."""

    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return [value, value + 1]
    if isinstance(value, str):
        parts = [
            item.strip()
            for item in re.split(r"\s*(?:,|:|\.\.)\s*", value.strip())
            if item.strip()
        ]
        if 1 <= len(parts) <= 2 and all(
            re.fullmatch(r"-?\d+", item) for item in parts
        ):
            numbers = [int(item) for item in parts]
            return (
                [numbers[0], numbers[0] + 1]
                if len(numbers) == 1
                else numbers
            )
    return value


def canonical_tool_name(name: str | None) -> str:
    """Match the backend registry's historical tool-name normalization.

    Capability checks happen before backend dispatch, so aliases must be
    canonicalized at this boundary rather than only inside ToolRegistry.call.
    """
    return normalize_tool_name(name)
