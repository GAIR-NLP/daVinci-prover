"""Update verified facts and target-progress metadata after each outcome.

These functions are called only by the episode transition loop. They maintain
its single state object and do not execute tools or choose model actions."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dvp.protocol import ToolOutcome
from dvp.toolkits.lean_feedback import project_source_placeholders
from dvp.toolkits.surface import Dispatch
from dvp.verification.contracts import (
    contains_exact_target_declaration,
    contains_target_candidate_declaration,
    mask_lean_comments_and_strings,
)
from dvp.verification.diagnostics import select_primary_goal
from dvp.verification.lean import contains_placeholder

from . import candidate, completion
from .policy import (
    _LOOKUP_BACKENDS,
    _NO_LOGICAL_VERIFIER_EVIDENCE,
    verification_backend_enabled,
)
from .verifier_evidence import (
    _raw_draft_typechecked,
    _raw_source_placeholder_count,
    _raw_verification_profile,
    normalized_error_evidence,
    target_draft_hole_goals,
    target_unsolved_goals,
)

if TYPE_CHECKING:
    from .episode import ProverEpisode


def invalidate_saved_sources(episode: ProverEpisode, outcome: ToolOutcome) -> None:
    """Checked file facts describe old bytes after an unverified Lean save."""
    if outcome.backend_name not in {"file_write", "file_edit"}:
        return
    raw = outcome.raw
    if not raw.get("success") and not raw.get("file_saved") and not raw.get("rollback_failed"):
        return
    paths = [raw.get("file_path")]
    files = raw.get("files")
    if isinstance(files, list):
        paths.extend(item.get("file_path") for item in files if isinstance(item, dict))
    def canonical_path(path: str) -> str:
        try:
            return str(Path(episode.tools.workspace.resolve(path)).resolve())
        except (AttributeError, OSError, TypeError, ValueError):
            return str(Path(path))

    changed = {canonical_path(path) for path in paths if isinstance(path, str)}
    changed = {path for path in changed if Path(path).suffix == ".lean"}
    if not changed and not raw.get("rollback_failed"):
        return

    def affected(artifact: dict[str, Any]) -> bool:
        if raw.get("rollback_failed"):
            return True
        path = artifact.get("path")
        if isinstance(path, str) and canonical_path(path) in changed:
            return True
        manifest = artifact.get("source_bundle_manifest")
        modules = manifest.get("modules", []) if isinstance(manifest, dict) else []
        return any(
            isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and canonical_path(item["path"]) in changed
            for item in modules
        )

    episode._state.verified_artifacts = {
        key: artifact for key, artifact in episode._state.verified_artifacts.items()
        if not affected(artifact)
    }
    # An edited helper may be in the target's transitive import closure.
    # Historical counters/best frontier remain history; current-check fields
    # are refreshed only by an explicit or final automatic Lean check.
    episode._state.exact_target_verified = False
    episode._state.submission_ready = False
    episode._state.last_target_path = None
    episode._state.last_target_turn = None
    episode._state.last_target_error_kind = None
    episode._state.last_target_error = None
    episode._state.last_target_candidate_hash = None
    episode._state.last_target_source_placeholders = None


def harness_metadata(**updates: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "attempt_kind": "other",
        "exact_target": False,
        "contract_verified": False,
        "objective_progress": False,
        "target_candidate_changed": False,
        "strict_attempt": False,
        "new_evidence": True,
        "target_checked": False,
        "target_source_placeholders": None,
        "best_target_source_placeholders": None,
        "target_regression": False,
    }
    metadata.update(updates)
    return metadata


def annotate_outcome(
    episode: ProverEpisode,
    outcome: ToolOutcome,
    dispatch: Dispatch,
) -> ToolOutcome:
    backend = dispatch.backend_name
    code = candidate.outcome_code(episode, outcome)
    candidate_hash = candidate.source_fingerprint(code, outcome.raw)
    verification_attempt = bool(
        verification_backend_enabled(backend)
        and (
            outcome.verified is not None
            or _raw_verification_profile(outcome.raw) == "draft_allow_sorry"
        )
    )
    logical_verification_attempt = bool(
        verification_attempt and outcome.error_kind not in _NO_LOGICAL_VERIFIER_EVIDENCE
    )
    candidate_attempt = logical_verification_attempt
    permissive_draft = candidate.is_permissive_draft(outcome)
    strict_attempt = bool(logical_verification_attempt and not permissive_draft)
    target_name = getattr(episode.task_contract, "target_name", None)
    exact_target = bool(
        candidate_attempt
        and code
        and contains_exact_target_declaration(
            code, episode.theorem_statement, target_name=target_name,
        )
    )
    target_candidate = exact_target or bool(
        candidate_attempt
        and code
        # This is routing evidence only. Comparator checks the unchanged
        # original task and binds its verdict to the submitted sources.
        and contains_target_candidate_declaration(
            code, episode.theorem_statement, target_name=target_name,
        )
    )
    if backend in _LOOKUP_BACKENDS:
        attempt_kind = "lookup"
    elif candidate_attempt:
        # Keep the historical routing label for target work. ``exact_target``
        # below retains its stricter literal-header meaning.
        attempt_kind = "exact_target" if target_candidate else "auxiliary"
    else:
        attempt_kind = "other"

    changed = bool(
        target_candidate
        and candidate_hash
        and candidate_hash != episode._state.last_target_candidate_hash
    )
    current_placeholders = episode._state.last_target_source_placeholders
    best_placeholders = episode._state.best_target_source_placeholders
    target_checked = episode._state.last_target_turn is not None
    target_regression = False
    source_projection: dict[str, Any] | None = None
    if target_candidate:
        source_count = _raw_source_placeholder_count(outcome.raw)
        if source_count is None and code is not None:
            source_projection = project_source_placeholders(code)
            source_count = source_projection["total"]
        if source_count is not None:
            current_placeholders = source_count
            accepted_frontier = _raw_draft_typechecked(outcome.raw)
            if accepted_frontier:
                target_regression = bool(
                    changed
                    and best_placeholders is not None
                    and source_count > best_placeholders
                )
                best_placeholders = (
                    source_count
                    if best_placeholders is None
                    else min(best_placeholders, source_count)
                )
        target_checked = True
    new_evidence = not (
        outcome.valid
        and outcome.verified
        and outcome.artifact_id is not None
        and outcome.artifact_id in episode._state.verified_artifacts
    )
    harness = harness_metadata(
        attempt_kind=attempt_kind,
        exact_target=exact_target,
        target_candidate_changed=changed,
        strict_attempt=strict_attempt,
        new_evidence=new_evidence,
        target_checked=target_checked,
        target_source_placeholders=current_placeholders,
        best_target_source_placeholders=best_placeholders,
        target_regression=target_regression,
    )
    if target_candidate and not exact_target:
        # Only the newly routed case needs this extension. Existing exact,
        # and auxiliary evidence retain their prior shape.
        harness["target_candidate"] = True
    raw = dict(outcome.raw)
    if source_projection is not None:
        raw["source_placeholders"] = source_projection
    raw["_harness"] = harness
    return replace(outcome, raw=raw)


def update_facts(episode: ProverEpisode, outcome: ToolOutcome) -> None:
    raw = outcome.raw
    permissive_draft = candidate.is_permissive_draft(outcome)
    harness = raw.get("_harness") if isinstance(raw.get("_harness"), dict) else {}
    attempt_kind = str(harness.get("attempt_kind") or "other")
    strict_attempt = bool(harness.get("strict_attempt"))
    episode._state.last_attempt_kind = attempt_kind

    if attempt_kind == "lookup":
        episode._state.lookup_since_target += 1
        episode._state.lookup_since_strict += 1
    elif attempt_kind == "auxiliary":
        episode._state.helper_since_target += 1
    elif attempt_kind == "exact_target":
        # ``target_attempts`` means a strict target check that returned
        # logical verifier evidence, not a hole-bearing draft or a
        # rate-limit/timeout/transport failure.
        if strict_attempt:
            episode._state.target_attempts += 1
        # Every exact-target check executes and refreshes target work,
        # including a permissive draft. ``lookup_since_strict`` stays separate.
        episode._state.helper_since_target = 0
        episode._state.lookup_since_target = 0
        episode._state.last_target_turn = episode._state.turns
        episode._state.last_target_path = candidate.outcome_path(outcome)
        current_placeholders = harness.get("target_source_placeholders")
        best_placeholders = harness.get("best_target_source_placeholders")
        if (
            isinstance(current_placeholders, int)
            and not isinstance(current_placeholders, bool)
            and current_placeholders >= 0
        ):
            episode._state.last_target_source_placeholders = current_placeholders
        if (
            isinstance(best_placeholders, int)
            and not isinstance(best_placeholders, bool)
            and best_placeholders >= 0
        ):
            episode._state.best_target_source_placeholders = best_placeholders

        code = candidate.outcome_code(episode, outcome)
        candidate_hash = candidate.source_fingerprint(code, outcome.raw)
        if candidate_hash:
            episode._state.last_target_candidate_hash = candidate_hash

        target_error_kind, target_error_text = target_error(episode, outcome, code)
        episode._state.last_target_error_kind = target_error_kind
        episode._state.last_target_error = target_error_text
        if bool(harness.get("target_candidate_changed")):
            episode._state.last_progress_turn = episode._state.turns

    if strict_attempt:
        episode._state.strict_attempts += 1
        episode._state.last_strict_turn = episode._state.turns
        episode._state.lookup_since_strict = 0

    code = candidate.outcome_code(episode, outcome)
    reusable_auxiliary = not (
        attempt_kind == "auxiliary"
        and code
        and contains_placeholder(mask_lean_comments_and_strings(code))
    )
    if (
        outcome.valid
        and outcome.verified
        and outcome.artifact_id
        and reusable_auxiliary
    ):
        artifact = completion.verified_artifact_facts(
            episode,
            outcome,
            permissive_draft=permissive_draft,
        )
        episode._state.verified_artifacts[outcome.artifact_id] = {
            "artifact_id": outcome.artifact_id,
            "tool": outcome.name,
            "backend_tool": outcome.backend_name,
            **artifact,
        }

        exact_target = bool(artifact["exact_target"])
        submission_ready = bool(artifact["submission_ready"])
        if exact_target:
            episode._state.exact_target_verified = True
        if submission_ready:
            episode._state.submission_ready = True

        # Only exact-target progress advances the objective clock.  A
        # verified helper remains useful evidence, but must not hide a
        # stale target or erase its verifier error.
        if bool(harness.get("objective_progress")):
            episode._state.last_progress_turn = episode._state.turns

    if outcome.error_kind:
        episode._state.last_error_kind = outcome.error_kind
        episode._state.error_counts[outcome.error_kind] = (
            episode._state.error_counts.get(outcome.error_kind, 0) + 1
        )
    elif outcome.valid:
        episode._state.last_error_kind = None


def target_error(
    episode: ProverEpisode,
    outcome: ToolOutcome,
    code: str | None,
) -> tuple[str | None, str | None]:
    if outcome.terminal and outcome.verified:
        return None, None
    masked_code = mask_lean_comments_and_strings(code) if code else None
    kind = outcome.error_kind
    # Preserve the actionable compiler class even if the same candidate
    # also contains an unrelated hole.
    if kind in {
        "unknown_identifier",
        "type_mismatch",
        "unsolved_goals",
        "redundant_tactic",
    }:
        if kind == "unsolved_goals":
            primary_goal = select_primary_goal(
                target_unsolved_goals(outcome)
            )
            if primary_goal:
                return kind, primary_goal[:2000]
        evidence = normalized_error_evidence(outcome)[:2000]
        return kind, evidence or kind.replace("_", " ")
    if candidate.is_permissive_draft(outcome):
        primary_goal = select_primary_goal(
            target_draft_hole_goals(outcome)
        )
        if primary_goal:
            return "draft_hole", primary_goal[:2000]
    if masked_code and contains_placeholder(masked_code):
        return (
            "placeholder",
            "Exact target candidate contains sorry/admit or another placeholder.",
        )
    if not kind and outcome.verified is None:
        kind = "unverified_target"
    if not kind and outcome.verified:
        kind = "not_submission_ready"
    # This fact is repeated in every subsequent observation.  Keep enough
    # local Lean evidence to act on without inflating long trajectories;
    # the lossless outcome still contains the full backend response.
    evidence = normalized_error_evidence(outcome)[:2000]
    if kind and not evidence:
        evidence = kind.replace("_", " ")
    return kind, evidence or None
