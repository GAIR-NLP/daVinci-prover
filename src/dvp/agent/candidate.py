"""Resolve candidate source and workspace paths for evidence checks."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dvp.protocol import ToolOutcome
from dvp.toolkits.surface import Dispatch

from .verifier_evidence import _raw_verification_profile, source_bundle_evidence

if TYPE_CHECKING:
    from .episode import ProverEpisode


def dispatch_path(dispatch: Dispatch) -> str:
    arguments = dispatch.backend_arguments
    value = (
        arguments.get("target_path")
        or arguments.get("file_path")
        or arguments.get("path")
        or ""
    )
    return str(value)


def dispatch_candidate_code(episode: ProverEpisode, dispatch: Dispatch) -> str | None:
    arguments = dispatch.backend_arguments
    for key in ("code", "content"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    path = dispatch_path(dispatch)
    return read_workspace_path(episode, path) if path else None


def outcome_path(outcome: ToolOutcome) -> str | None:
    raw = outcome.raw
    arguments = outcome.arguments
    value = (
        raw.get("file_path")
        or (
            arguments.get("target_path")
            if outcome.backend_name == "file_edit"
            else None
        )
        or arguments.get("file_path")
        or arguments.get("path")
    )
    return str(value) if value else None


def read_outcome_path(episode: ProverEpisode, path: str | None) -> str | None:
    if not path:
        return None

    return read_workspace_path(episode, path)


def read_workspace_path(episode: ProverEpisode, path: str) -> str | None:
    workspace = getattr(episode.tools, "workspace", None)
    resolve = getattr(workspace, "resolve", None)
    if callable(resolve):
        try:
            resolved = Path(resolve(path))
            if resolved.is_file():
                return resolved.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError, TypeError, ValueError):
            pass

    try:
        base = (episode.run_dir / "workspaces").resolve()
        resolved = (base / path).resolve()
        if resolved.is_relative_to(base) and resolved.is_file():
            return resolved.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, TypeError, ValueError):
        pass
    return None


def outcome_code(episode: ProverEpisode, outcome: ToolOutcome) -> str | None:
    raw = outcome.raw
    arguments = outcome.arguments
    proof_code = raw.get("proof_code")
    if "proof_code" in raw:
        # A grouped check can belong to a different file than its saving call.
        # Empty or unreadable primary source must not fall back to that call.
        return proof_code if isinstance(proof_code, str) else None

    operation = str(arguments.get("operation") or "")
    if outcome.backend_name != "file_edit" and operation != "replace":
        for key in ("code", "content"):
            value = arguments.get(key)
            if isinstance(value, str) and value.strip():
                return value

    return read_outcome_path(episode, outcome_path(outcome))


def source_fingerprint(code: str | None, raw: dict[str, Any]) -> str | None:
    """Identify the checked closure while retaining legacy single-file hashes."""
    entry_hash = hashlib.sha256(code.encode("utf-8")).hexdigest() if code else None
    evidence = source_bundle_evidence(raw)
    if evidence is not None:
        digest = evidence.get("source_bundle_sha256")
        manifest = evidence.get("source_bundle_manifest")
        if (
            isinstance(digest, str) and len(digest) == 64
            and all(char in "0123456789abcdef" for char in digest)
            and isinstance(manifest, dict)
            and manifest.get("sha256") == digest
            and manifest.get("entry_sha256") == entry_hash
        ):
            return digest
    return entry_hash


def is_permissive_draft(outcome: ToolOutcome) -> bool:
    """Separate hole-bearing elaboration from strict Lean evidence."""

    profile = _raw_verification_profile(outcome.raw)
    # Every V10 verifier-capable tool returns an explicit derived profile.
    # Do not infer one from arbitrary file contents: reading the seeded
    # solution or writing non-Lean text containing the word ``sorry`` is
    # not evidence that Lean elaborated a draft.
    return profile == "draft_allow_sorry"
