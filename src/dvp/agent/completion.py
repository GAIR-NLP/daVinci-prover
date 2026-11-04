"""Post-rollout grading of the saved submission by source-bound Comparator evidence."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dvp.protocol import ToolOutcome

from . import candidate, outcomes
from .verifier_evidence import (
    _structured_verifier_timeout,
    _trusted_verifier_infrastructure,
    source_bundle_evidence,
)

if TYPE_CHECKING:
    from .episode import ProverEpisode


def comparator_evidence(
    episode: ProverEpisode, outcome: ToolOutcome
) -> dict[str, Any]:
    """Bind successful upstream acceptance to this candidate and original task."""
    result = outcome.raw.get("contract_verification")
    code = candidate.outcome_code(episode, outcome)
    source = (
        episode.task_contract.statement
        if episode.task_contract is not None
        else episode._source_theorem_statement
    )
    bundle = source_bundle_evidence(outcome.raw)
    bundle_bound = bundle is None or bool(
        isinstance(result, dict)
        and isinstance(bundle.get("source_bundle_manifest"), dict)
        and isinstance(bundle.get("source_bundle_sha256"), str)
        and bundle["source_bundle_manifest"].get("sha256")
        == bundle["source_bundle_sha256"]
        and code is not None
        and bundle["source_bundle_manifest"].get("entry_sha256")
        == hashlib.sha256(code.encode("utf-8")).hexdigest()
        and all(result.get(key) == value for key, value in bundle.items())
    )
    if (
        isinstance(result, dict)
        and result.get("success") is True
        and not result.get("error")
        and not result.get("errors")
        and result.get("backend") == "leanprover/comparator"
        and code is not None
        and result.get("candidate_sha256")
        == hashlib.sha256(code.encode("utf-8")).hexdigest()
        and result.get("original_source_sha256")
        == hashlib.sha256(source.encode("utf-8")).hexdigest()
        and bundle_bound
    ):
        return result
    return {}


async def finalize_saved_submission(episode: ProverEpisode) -> dict[str, Any]:
    """Freeze and grade the saved entry, never an inline probe or old tool result."""
    from dvp.runtime.submission import SUBMISSION_PATH
    from dvp.verification.modules import collect_bundle

    final: dict[str, Any] = {
        "success": False, "score": 0.0, "stop": True,
        "file_path": SUBMISSION_PATH,
        "rollout_reason": episode._state.rollout_reason,
        "reason": "invalid_submission",
    }
    workspace = getattr(episode.tools, "workspace", None)
    root = getattr(workspace, "workspaces_dir", episode.run_dir / "workspaces")
    path = Path(root) / SUBMISSION_PATH
    try:
        if path.is_symlink():
            raise ValueError("Submission.lean must be a regular source file, not a symlink")
        code = path.read_text(encoding="utf-8")
        final["proof_code"] = code
        if not code.strip():
            raise ValueError("Submission.lean is empty")
        bundle = collect_bundle(code, root, entry_path=path)
    except (OSError, UnicodeError, ValueError) as exc:
        final["message"] = f"Cannot collect the saved submission: {exc}"
        final["errors"] = [final["message"]]
        return final

    final.update(source_bundle_sha256=bundle.sha256, source_bundle_manifest=bundle.manifest())
    episode._state.lean_calls += 1
    episode._state.contract_lean_calls += 1
    config = getattr(getattr(episode.tools, "verifier", None), "config", None)
    timeout = float(getattr(config, "timeout", 60.0))
    timed_out = False
    try:
        checked = await asyncio.wait_for(
            episode.contract_verifier.verify(
                code, bundle=bundle, max_output_chars=episode.config.max_tool_result_chars,
            ),
            timeout=timeout,
        )
    except TimeoutError:
        timed_out = True
        checked = {
            "success": False,
            "errors": [f"Final Comparator acceptance timed out after {timeout}s."],
        }
    except Exception as exc:
        checked = {
            "success": False, "_harness_infrastructure_error": True,
            "errors": [f"Comparator exception: {type(exc).__name__}: {exc}"],
        }
    if not isinstance(checked, dict):
        checked = {
            "success": False, "_harness_infrastructure_error": True,
            "errors": ["Comparator returned a non-object result."],
        }
    final["contract_verification"] = checked
    evidence = ToolOutcome(
        call_id="final_acceptance", name="comparator", backend_name="leanprover/comparator",
        arguments={"file_path": SUBMISSION_PATH}, executed=True, valid=True,
        verified=True, terminal=False, reward=0.0, raw=final,
    )
    accepted = bool(comparator_evidence(episode, evidence))
    if checked.get("success") is True and not accepted:
        checked = {
            **checked, "success": False, "_harness_infrastructure_error": True,
            "errors": ["Comparator returned missing or mismatched source-bound evidence."],
        }
        final["contract_verification"] = checked
    timed_out = timed_out or _structured_verifier_timeout(checked)
    infrastructure = timed_out or _trusted_verifier_infrastructure(checked)
    final.update(
        success=accepted, score=1.0 if accepted else 0.0,
        reason=("verified_exact_target" if accepted else "comparator_timeout" if timed_out
                else "comparator_infrastructure_error" if infrastructure else "comparator_rejected"),
        message=("Comparator accepted the saved submission." if accepted else outcomes.result_text(checked)),
    )
    if infrastructure:
        final["_harness_infrastructure_error"] = True
    return final


def verified_artifact_facts(
    episode: ProverEpisode,
    outcome: ToolOutcome,
    *,
    permissive_draft: bool,
) -> dict[str, Any]:
    code = candidate.outcome_code(episode, outcome)
    harness = outcome.raw.get("_harness")
    if not isinstance(harness, dict):
        harness = {}
    if permissive_draft:
        artifact_kind = "permissive_draft"
    elif outcome.verified and harness.get("attempt_kind") == "auxiliary":
        artifact_kind = "verified_fragment"
    else:
        artifact_kind = "helper_or_fragment"

    facts: dict[str, Any] = {
        "artifact_kind": artifact_kind,
        "exact_target": False,
        "submission_ready": False,
    }
    path = candidate.outcome_path(outcome)
    if path:
        facts["path"] = path
    if code:
        facts["content_sha256"] = hashlib.sha256(code.encode("utf-8")).hexdigest()
    bundle = source_bundle_evidence(outcome.raw)
    if bundle is not None:
        facts.update(bundle)
    return facts
