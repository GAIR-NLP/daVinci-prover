"""Ordered tool registration and dispatch; handlers live in focused modules."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from dvp.verification.lean import LeanVerificationConfig, LeanVerifier
from dvp.runtime.workspace import Workspace
from dvp.toolkits import files, search, lean, python
from dvp.runtime.python_execution import PythonExecutor
from dvp.toolkits.retrieval import execution as retrieval_execution
from dvp.toolkits.schemas import (
    Tool, TOOL_NAMES, build_tools, normalize_tool_name,
)
from dvp.toolkits.retrieval.execution import RetrievalSearch


class ToolRegistry:
    """Proving tools and optional sandboxed Python experiments."""

    def __init__(
        self,
        *,
        workspace: Workspace,
        verifier: LeanVerifier,
        task_contract: Any = None,
        retrieve_index: Path | None = None,
        retrieval_search: RetrievalSearch | None = None,
        python_executor: PythonExecutor | None = None,
    ) -> None:
        self.workspace = workspace
        self.python_executor = python_executor
        self.verifier = verifier
        self.task_contract = task_contract
        draft_config = LeanVerificationConfig(
            target_workspace=verifier.config.target_workspace,
            scratch_workspace=workspace.submission_dir,
            module_root=getattr(verifier.config, "module_root", None),
            timeout=verifier.config.timeout,
            allow_sorry=True,
        )
        self.draft_verifier = LeanVerifier(draft_config)
        self.retrieve_index = retrieve_index
        self._retrieval_search = retrieval_search
        self._deferred_auto_checks: dict[Path, None] | None = None
        self._tools: dict[str, Tool] = build_tools(self)

    def begin_automatic_check_batch(self) -> None:
        """Defer checks while the episode saves consecutive file-tool calls."""
        if self._deferred_auto_checks is not None:
            raise RuntimeError("An automatic check batch is already active")
        self._deferred_auto_checks = {}

    def pending_automatic_check_paths(self) -> tuple[Path, ...]:
        return tuple(self._deferred_auto_checks or ())

    def discard_automatic_check_batch(self) -> None:
        """Clear deferral even when the owning episode task is cancelled."""
        self._deferred_auto_checks = None

    async def finish_automatic_check_batch(
        self, result: dict[str, Any], *, on_check: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        paths = self.pending_automatic_check_paths()
        # Detach before awaiting: cancellation must never leave deferral enabled.
        self.discard_automatic_check_batch()
        try:
            return await files._verify_saved_batch(self, paths, result, on_check=on_check)
        except files.SavedFileVerificationError as exc:
            return exc.result

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        raw_name = name
        tool = self._tools.get(normalize_tool_name(name))
        if tool is None:
            return {
                "success": False,
                "error": f"Unknown tool: {raw_name}",
                "stop": False,
            }
        try:
            result = await tool.handler(arguments)
        except files.SavedFileVerificationError as exc:
            result = exc.result
        except Exception as exc:
            return {
                "success": False,
                "error": f"{type(exc).__name__}: {exc}",
                "stop": False,
            }
        result.setdefault("stop", False)
        return result

    async def preflight(self) -> None:
        """Validate optional execution services before the first model request."""
        if self.python_executor is not None:
            await python.preflight_executor(self.python_executor)

    def schemas(self) -> list[dict[str, Any]]:
        """Expose the ordered DVP tool schemas."""

        return [tool.schema() for tool in self._tools.values()]

    async def file_read(self, args: dict[str, Any]) -> dict[str, Any]:
        return await files.file_read(self, args)

    async def file_write(self, args: dict[str, Any]) -> dict[str, Any]:
        return await files.file_write(self, args)

    async def file_edit(self, args: dict[str, Any]) -> dict[str, Any]:
        return await files.file_edit(self, args)

    async def file_search(self, args: dict[str, Any]) -> dict[str, Any]:
        return await search.file_search(self, args)

    async def content_search(self, args: dict[str, Any]) -> dict[str, Any]:
        return await search.content_search(self, args)

    async def lean_verify(self, args: dict[str, Any]) -> dict[str, Any]:
        return await lean.lean_verify(self, args)

    async def python_execute(self, args: dict[str, Any]) -> dict[str, Any]:
        return await python.python_execute(self, args)

    async def lean_retrieve(self, args: dict[str, Any]) -> dict[str, Any]:
        return await retrieval_execution.lean_retrieve(self, args)
