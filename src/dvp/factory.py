"""Construction helpers for the unified inference workspace and verifier."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dvp.verification.lean import LeanVerificationConfig, LeanVerifier
from dvp.runtime.workspace import Workspace

from dvp.verification.comparator import ComparatorVerifier
from dvp.toolkits.registry import ToolRegistry

if TYPE_CHECKING:
    from dvp.llm_clients.client import LiteLLMClient


@dataclass(frozen=True)
class RuntimeServices:
    """Concrete services used by the inference drivers."""

    workspace: Workspace
    verifier: LeanVerifier
    tools: ToolRegistry
    contract_verifier: ComparatorVerifier


def build_python_executor(args: Any) -> Any:
    """Share one local executor and startup probe across an evaluation run."""
    if not getattr(args, "python_execute", False):
        return None
    from dvp.runtime.python_local import LocalPythonExecutor

    settings = (
        float(getattr(args, "python_timeout", 30.0)),
        int(getattr(args, "python_max_output_bytes", 12000)),
        int(getattr(args, "python_memory_mb", 1024)) * 1024 * 1024,
    )
    cached = getattr(args, "_dvp_python_executor", None)
    if cached is None or cached[0] != settings:
        executor = LocalPythonExecutor(
            timeout=settings[0], max_output_bytes=settings[1], memory_bytes=settings[2],
        )
        args._dvp_python_executor = (settings, executor)
    return args._dvp_python_executor[1]


async def preflight_python_executor(args: Any) -> Any:
    """Check isolation before scheduling tasks or contacting the model endpoint."""
    try:
        executor = build_python_executor(args)
        if executor is not None:
            from dvp.toolkits.python import preflight_executor
            await preflight_executor(executor)
        return executor
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        raise SystemExit(f"Python sandbox preflight failed: {exc}") from exc


def build_services(
    *,
    theorem_statement: str,
    runtime: Any,
    run_dir: str | Path | None = None,
    retrieval_search: Any = None,
    task_contract: Any = None,
) -> RuntimeServices:
    """Build the shared proving services from a ``RuntimeConfig``-like object.

    ``runtime`` is deliberately duck-typed so launchers may deserialize config
    into a plain namespace. Development checks compile the saved source;
    only Comparator can establish final acceptance. An optional Python executor
    is passed through to the registry, which uses its trusted preflight and
    execution interface; no unsandboxed fallback is constructed here.
    """

    resolved_run_dir = (
        _as_resolved_path(run_dir)
        if run_dir not in (None, "")
        else _required_path(runtime, "run_dir")
    )
    target_workspace = _optional_path(runtime, "target_workspace")
    workspace = Workspace(
        root=resolved_run_dir,
        target_workspace=target_workspace,
    )
    workspace.setup()

    verifier = LeanVerifier(
        LeanVerificationConfig(
            target_workspace=workspace.target,
            scratch_workspace=workspace.submission_dir,
            module_root=workspace.workspaces_dir,
            timeout=float(getattr(runtime, "lean_timeout", 60.0)),
            # Hole-bearing checks use the registry's frontend for goal feedback.
            # Both paths compile ordinary Lean; Comparator owns acceptance.
            allow_sorry=False,
        ),
    )

    tools = ToolRegistry(
        workspace=workspace,
        verifier=verifier,
        task_contract=task_contract,
        retrieve_index=_optional_path(runtime, "retrieve_index"),
        retrieval_search=retrieval_search,
        python_executor=getattr(runtime, "python_executor", None),
    )
    return RuntimeServices(
        workspace=workspace,
        verifier=verifier,
        tools=tools,
        contract_verifier=ComparatorVerifier(
            verifier, theorem_statement, task_contract=task_contract,
            artifact_dir=workspace.root / "comparator_artifacts",
        ),
    )


def build_episode(
    *,
    theorem_statement: str,
    runtime: Any,
    run_dir: str | Path,
    config: Any = None,
    sink: Any = None,
    services: RuntimeServices | None = None,
) -> Any:
    """Construct one inference episode from the unified services."""

    from dvp.agent.episode import ProverEpisode

    if services is None:
        services = build_services(
            theorem_statement=theorem_statement,
            runtime=runtime,
            run_dir=run_dir,
        )
    values: dict[str, Any] = {
        "tools": services.tools,
        "contract_verifier": services.contract_verifier,
        "theorem_statement": theorem_statement,
        "run_dir": _as_resolved_path(run_dir),
    }
    if config is not None:
        values["config"] = config
    if sink is not None:
        values["sink"] = sink
    return ProverEpisode(**values)


def build_client(
    runtime: Any, *, routing_key: str | None = None
) -> LiteLLMClient:
    """Build the LiteLLM adapter used by the standalone drivers."""

    from dvp.llm_clients.client import LiteLLMClient

    model = str(getattr(runtime, "model", "") or "")
    if not model:
        raise ValueError("runtime.model is required")
    stop_token_ids = getattr(runtime, "stop_token_ids", None)
    return LiteLLMClient(
        model=model,
        api_key=getattr(runtime, "api_key", None),
        base_url=getattr(runtime, "base_url", None),
        routing_key=(
            routing_key if routing_key is not None else getattr(runtime, "routing_key", None)
        ),
        timeout=float(getattr(runtime, "llm_timeout", 1200.0)),
        temperature=getattr(runtime, "temperature", None),
        top_p=getattr(runtime, "top_p", None),
        presence_penalty=getattr(runtime, "presence_penalty", None),
        max_tokens=getattr(runtime, "max_tokens", None),
        reasoning_effort=getattr(runtime, "reasoning_effort", None),
        thinking=getattr(runtime, "thinking", None),
        chat_template_thinking=getattr(runtime, "chat_template_thinking", None),
        chat_template_reasoning_effort=getattr(
            runtime,
            "chat_template_reasoning_effort",
            None,
        ),
        stop_token_ids=list(stop_token_ids) if stop_token_ids else None,
        trust_env=getattr(runtime, "trust_env", None),
        max_retries=int(getattr(runtime, "max_retries", 3)),
    )


def _required_path(value: Any, field: str) -> Path:
    path = _optional_path(value, field)
    if path is None:
        raise ValueError(f"runtime.{field} is required")
    return path


def _optional_path(value: Any, field: str) -> Path | None:
    raw = getattr(value, field, None)
    if raw in (None, ""):
        return None
    return _as_resolved_path(raw)


def _as_resolved_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()
