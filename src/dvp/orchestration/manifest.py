"""Record and validate the immutable policy, environment, and source identity."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from dvp.artifacts.persistence import atomic_write_json
from dvp.factory import build_python_executor
from dvp.toolkits.retrieval.execution import resolved_retrieval_backend
from dvp.tasks.dataset import row_task_id
from .config import episode_config_from_args
from .checkpoints import _read_json_object
from .provenance import (
    _file_sha256, _source_git_sha, _package_version, _agent_source_identity,
    _trusted_source_identity, _command_parser_fingerprint,
    _fixed_context_preparation_fingerprint, _fixed_context_environments,
    _comparator_runtime_identity,
)


_EXECUTION_BASELINE_SCHEMA = "dvp.execution-baseline.v1"
# Only the public entrypoint and scheduler are runner provenance. Changes to
# attempts, scoring, checkpoints, or any execution source remain resume-locked.
_RESUME_RUNNER_MODULES = frozenset({"dvp.orchestration.evaluator", "dvp.cli.evaluate"})


def build_run_manifest(
    args: argparse.Namespace,
    *,
    dataset: Path,
    run_root: Path,
    rows: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Return the complete, secret-free policy/environment identity for a run."""

    episode_config = episode_config_from_args(args)
    python_executor = build_python_executor(args)
    selected_tasks = [
        {
            "task_id": row_task_id(row),
            "dataset_index": row.get("_dataset_index"),
        }
        for row in rows
    ]
    selection_payload = json.dumps(
        selected_tasks,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "schema_version": episode_config.schema_version,
        "config_id": episode_config.config_id,
        "source": {
            "git_sha": _source_git_sha(),
            "python": platform.python_version(),
            "httpx": _package_version("httpx"),
            "litellm": _package_version("litellm"),
            "openai": _package_version("openai"),
            "tenacity": _package_version("tenacity"),
            "agent": _agent_source_identity(),
            "trusted": _trusted_source_identity(),
        },
        "policy": {
            "version": episode_config.policy_version,
            **({"fixed_context": rows[0]["_fixed_contract_identity"], "command_parser_sha256": _command_parser_fingerprint(), "fixed_context_preparation_sha256": _fixed_context_preparation_fingerprint(), "fixed_context_environments": _fixed_context_environments(rows, args.target_workspace)} if rows and rows[0].get("_fixed_contract_identity") else {}),
        },
        "dataset": str(dataset),
        "dataset_sha256": _file_sha256(dataset),
        "split": args.split,
        "selection": {
            "count": len(selected_tasks),
            "sha256": hashlib.sha256(selection_payload).hexdigest(),
            "tasks": selected_tasks,
        },
        "run_root": str(run_root),
        "controller": "dvp",
        "model": args.model,
        "base_url": args.base_url,
        "sampling": {
            "temperature": args.temperature,
            "top_p": args.top_p,
            "presence_penalty": args.presence_penalty,
            "max_tokens": args.max_tokens,
            "reasoning_effort": args.reasoning_effort,
            "thinking": args.thinking,
            "chat_template_thinking": args.chat_template_thinking,
            "chat_template_reasoning_effort": args.chat_template_reasoning_effort,
            "stop_token_ids": args.stop_token_ids,
        },
        "episode": {
            "guidance": episode_config.guidance,
            "enable_lean_retrieve": episode_config.enable_lean_retrieve,
            "abort_on_infrastructure_error": (
                episode_config.abort_on_infrastructure_error
            ),
            "max_tool_result_chars": episode_config.max_tool_result_chars,
            "max_consecutive_format_errors": episode_config.max_consecutive_format_errors,
            "max_consecutive_output_truncations": episode_config.max_consecutive_output_truncations,
            "retain_truncated_thinking": episode_config.retain_truncated_thinking,
            "history_policy": episode_config.history_policy,
            "budget": asdict(episode_config.budget),
        },
        "services": {
            **({"python_execute": python_executor.identity} if python_executor is not None else {}),
            "comparator_runtimes": _comparator_runtime_identity(rows, args.target_workspace),
            "lean_timeout": args.lean_timeout,
            "llm_timeout": args.llm_timeout,
            "task_timeout": args.task_timeout,
            "retrieve_index": (
                str(args.retrieve_index.expanduser().resolve())
                if episode_config.enable_lean_retrieve and args.retrieve_index
                else None
            ),
            "retrieve_index_sha256": (
                _file_sha256(args.retrieve_index.expanduser().resolve())
                if episode_config.enable_lean_retrieve and args.retrieve_index
                else None
            ),
            "retrieve_backend": (
                resolved_retrieval_backend(args.retrieve_index)
                if episode_config.enable_lean_retrieve
                else "disabled"
            ),
            "target_workspace": (
                str(args.target_workspace.expanduser().resolve())
                if args.target_workspace
                else None
            ),
        },
        "evaluation": {
            "attempts_per_task": args.attempts_per_task,
            "retries": args.retries,
            "concurrency": args.concurrency,
        },
        "artifacts": {
            "trace_level": args.trace_level,
        },
    }


def validate_resume_manifest(
    existing: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    allow_endpoint_change: bool = False,
) -> None:
    """Reject resume across a different policy or environment identity."""

    identity_keys = (
        "schema_version",
        "config_id",
        "policy",
        "dataset",
        "dataset_sha256",
        "split",
        "selection",
        "controller",
        "model",
        "sampling",
        "episode",
        "services",
    )
    if not allow_endpoint_change:
        identity_keys = (*identity_keys, "base_url")
    _require_execution_metadata(existing)
    mismatched = [
        key
        for key in identity_keys
        if existing.get(key) != current.get(key)
    ]
    existing_source = dict(existing.get("source") or {})
    current_source = dict(current.get("source") or {})
    for key in ("python", "httpx", "litellm", "openai", "tenacity"):
        if (
            existing_source.get(key) != current_source.get(key)
        ):
            mismatched.append(f"source.{key}")
    existing_trusted = existing_source.get("trusted")
    current_trusted = current_source.get("trusted")
    if (
        isinstance(existing_trusted, Mapping)
        and existing_trusted.get("sha256")
        != (
            current_trusted.get("sha256")
            if isinstance(current_trusted, Mapping)
            else None
        )
    ):
        mismatched.append("source.trusted")
    existing_agent = existing_source.get("agent")
    current_agent = current_source.get("agent")
    if (
        isinstance(existing_agent, Mapping)
        and existing_agent.get("sha256")
        != (
            current_agent.get("sha256")
            if isinstance(current_agent, Mapping)
            else None
        )
        and not (
            isinstance(current_agent, Mapping)
            and _runner_only_source_change(existing_agent, current_agent)
        )
    ):
        mismatched.append("source.agent")
    if existing.get("artifacts") != current.get("artifacts"):
        mismatched.append("artifacts")
    existing_evaluation = dict(existing.get("evaluation") or {})
    current_evaluation = dict(current.get("evaluation") or {})
    # Worker count is an execution property, not part of an episode or sample.
    # Keeping it out of resume identity permits safe C20 -> C30 continuation.
    existing_evaluation.pop("concurrency", None)
    current_evaluation.pop("concurrency", None)
    if existing_evaluation != current_evaluation:
        mismatched.append("evaluation")
    if mismatched:
        raise ValueError(
            "Cannot resume with a different run profile; mismatched manifest "
            "field(s): "
            + ", ".join(mismatched)
        )


def _module_digest_map(identity: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return a strict module-to-digest map from a source identity."""

    files = identity.get("files")
    if not isinstance(files, list) or not files:
        return None
    digests: dict[str, Any] = {}
    for item in files:
        if not isinstance(item, Mapping):
            return None
        module = item.get("module")
        if not isinstance(module, str) or module in digests:
            return None
        digests[module] = item.get("sha256")
    return digests


def _runner_only_source_change(
    existing: Mapping[str, Any],
    current: Mapping[str, Any],
) -> bool:
    """Allow source drift only in explicitly runner-only modules."""

    existing_digests = _module_digest_map(existing)
    current_digests = _module_digest_map(current)
    if existing_digests is None or current_digests is None:
        return False
    if existing_digests.keys() != current_digests.keys():
        return False
    changed = {
        module
        for module, digest in existing_digests.items()
        if current_digests[module] != digest
    }
    return bool(changed) and changed <= _RESUME_RUNNER_MODULES


def _execution_identity(manifest: Mapping[str, Any]) -> dict[str, Any]:
    source = dict(manifest.get("source") or {})
    agent_source = dict(source.get("agent") or {})
    trusted_source = dict(source.get("trusted") or {})
    return {
        "schema_version": manifest.get("schema_version"),
        "config_id": manifest.get("config_id"),
        "policy": manifest.get("policy"),
        "python": source.get("python"),
        "httpx": source.get("httpx"),
        "litellm": source.get("litellm"),
        "openai": source.get("openai"),
        "tenacity": source.get("tenacity"),
        "agent_sha256": agent_source.get("sha256"),
        "trusted_sha256": trusted_source.get("sha256"),
        "artifacts": manifest.get("artifacts"),
    }


def _execution_identity_allows_runner_change(
    existing: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    original_manifest: Mapping[str, Any],
    current_manifest: Mapping[str, Any],
) -> bool:
    """Recognize a baseline change confined to runner-only source files."""

    existing_without_runner = dict(existing)
    current_without_runner = dict(current)
    existing_agent_sha = existing_without_runner.pop("agent_sha256", None)
    current_agent_sha = current_without_runner.pop("agent_sha256", None)
    if existing_without_runner != current_without_runner:
        return False
    if existing_agent_sha == current_agent_sha:
        return False
    original_source = dict(original_manifest.get("source") or {})
    current_source = dict(current_manifest.get("source") or {})
    original_agent = original_source.get("agent")
    current_agent = current_source.get("agent")
    return bool(
        isinstance(original_agent, Mapping)
        and isinstance(current_agent, Mapping)
        and original_agent.get("sha256") == existing_agent_sha
        and current_agent.get("sha256") == current_agent_sha
        and _runner_only_source_change(original_agent, current_agent)
    )


def _require_execution_metadata(manifest: Mapping[str, Any]) -> None:
    """Reject manifests that cannot establish the execution and artifact identity."""

    source = manifest.get("source")
    artifacts = manifest.get("artifacts")
    if (
        not isinstance(source, Mapping)
        or any(
            not isinstance(source.get(component), Mapping)
            or not source[component].get("sha256")
            for component in ("agent", "trusted")
        )
        or not isinstance(artifacts, Mapping)
        or artifacts.get("trace_level") not in {"off", "sft", "full"}
    ):
        raise ValueError(
            "Cannot resume without complete source and artifact metadata; "
            "use a new run directory"
        )


def ensure_execution_baseline(
    run_root: Path,
    *,
    current_manifest: Mapping[str, Any],
    original_manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Create or validate the immutable non-runner execution fingerprint."""

    _require_execution_metadata(original_manifest)
    _require_execution_metadata(current_manifest)
    path = run_root / "execution_baseline.json"
    identity = _execution_identity(current_manifest)
    if path.exists():
        existing = _read_json_object(path, label="execution baseline")
        if (
            existing.get("schema_version") != _EXECUTION_BASELINE_SCHEMA
            or not (
                existing.get("identity") == identity
                or (
                    isinstance(existing.get("identity"), Mapping)
                    and _execution_identity_allows_runner_change(
                        existing["identity"],
                        identity,
                        original_manifest=original_manifest,
                        current_manifest=current_manifest,
                    )
                )
            )
        ):
            raise ValueError(
                "Cannot resume with different agent/execution source or "
                "artifact contract; execution_baseline.json does not match"
            )
        return existing

    original_source = dict(original_manifest.get("source") or {})
    payload = {
        "schema_version": _EXECUTION_BASELINE_SCHEMA,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "identity": identity,
        "original_manifest_source": original_source,
    }
    atomic_write_json(path, payload)
    return payload
