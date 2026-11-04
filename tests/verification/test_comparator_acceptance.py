"""Saved-source finalization requires fresh Comparator evidence after rollout stop."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dvp.agent.episode import ProverEpisode
from dvp.config import BudgetConfig, EpisodeConfig
from dvp.factory import build_services
from dvp.verification.comparator import digest
from dvp.verification.modules import CandidateBundle


STATEMENT = "\n theorem target : True := by sorry\n"
CANDIDATE = "theorem target : True := by trivial"


def accepted_result(*, source=STATEMENT, code=CANDIDATE):
    bundle = CandidateBundle(code)
    return {
        "success": True, "errors": [], "backend": "leanprover/comparator",
        "candidate_sha256": digest(code), "original_source_sha256": digest(source),
        "source_bundle_sha256": bundle.sha256, "source_bundle_manifest": bundle.manifest(),
    }


def make_episode(tmp_path, result, *, fixed=False, budget=None, used=1, saved=CANDIDATE):
    target = tmp_path / "mathlib"
    target.mkdir()
    contract = (SimpleNamespace(statement=STATEMENT.strip(), target_name="target", contract_hash="fixed")
                if fixed else None)
    services = build_services(
        theorem_statement=STATEMENT,
        runtime=SimpleNamespace(target_workspace=target, lean_timeout=0.2),
        run_dir=tmp_path / "run", task_contract=contract,
    )
    checker = SimpleNamespace(verify=AsyncMock(return_value=result))
    episode = ProverEpisode(
        tools=services.tools, contract_verifier=checker, theorem_statement=STATEMENT,
        run_dir=services.workspace.root,
        config=EpisodeConfig(enable_lean_retrieve=False, budget=BudgetConfig(max_lean_calls=budget)),
    )
    episode.reset()
    services.workspace.submission_file.write_text(saved)
    episode._state.lean_calls = used
    return episode


def finish(episode, *, reason="model_finished", truncated=False):
    episode.stop(reason, truncated=truncated)
    return asyncio.run(episode.finalize())


@pytest.mark.parametrize("fixed", [False, True])
def test_finalization_grades_actual_saved_source_once(tmp_path, fixed):
    episode = make_episode(
        tmp_path, accepted_result(source=STATEMENT.strip() if fixed else STATEMENT), fixed=fixed,
    )
    result = finish(episode)
    episode.contract_verifier.verify.assert_awaited_once_with(
        CANDIDATE, bundle=CandidateBundle(CANDIDATE), max_output_chars=episode.config.max_tool_result_chars,
    )
    assert result.success and result.score == 1.0
    assert result.reason == "verified_exact_target" and result.rollout_reason == "model_finished"
    assert result.lean_calls == 2 and result.tool_calls == 0
    assert episode._state.contract_lean_calls == 1


@pytest.mark.parametrize("field,value", [
    ("backend", "legacy-verifier"),
    ("candidate_sha256", digest("a different proof")),
    ("original_source_sha256", digest(STATEMENT.strip())),
    ("original_source_sha256", None),
    ("source_bundle_sha256", "0" * 64),
    ("source_bundle_manifest", None),
])
def test_missing_or_stale_comparator_evidence_cannot_grant_reward(tmp_path, field, value):
    evidence = accepted_result()
    evidence[field] = value
    result = finish(make_episode(tmp_path, evidence))
    assert not result.success and result.score == 0.0
    assert result.aborted and result.reason == "comparator_infrastructure_error"


@pytest.mark.parametrize("fixed", [False, True])
def test_failed_comparator_has_no_development_evidence_fallback(tmp_path, fixed):
    episode = make_episode(
        tmp_path, {"success": False, "errors": ["modified original definition"]}, fixed=fixed,
    )
    episode._state.exact_target_verified = True
    episode._state.submission_ready = True
    result = finish(episode)
    assert result.terminated and not result.aborted
    assert result.score == 0.0 and not result.success
    assert result.reason == "comparator_rejected"
    assert not episode._state.exact_target_verified


@pytest.mark.parametrize("limit,reason", [(1, "lean_budget_exhausted"), (2, "turn_budget_exhausted")])
def test_exhausted_development_budget_does_not_skip_finalization(tmp_path, limit, reason):
    episode = make_episode(tmp_path, accepted_result(), budget=limit, used=limit)
    if reason == "turn_budget_exhausted":
        episode._state.turns = episode.config.budget.max_turns
    result = finish(episode, reason=reason, truncated=True)
    episode.contract_verifier.verify.assert_awaited_once()
    assert result.success and result.lean_calls == limit + 1
    assert result.tool_calls == 0 and result.rollout_reason == reason


def test_changed_saved_candidate_cannot_reuse_old_acceptance(tmp_path):
    episode = make_episode(tmp_path, accepted_result(), saved=CANDIDATE + "\n-- changed source")
    result = finish(episode)
    assert not result.success and result.aborted
    assert result.reason == "comparator_infrastructure_error"


def test_unavailable_comparator_fails_closed(tmp_path):
    episode = make_episode(tmp_path, None)
    episode.contract_verifier.verify.side_effect = FileNotFoundError("comparator not installed")
    result = finish(episode)
    assert not result.success and result.score == 0.0
    assert result.aborted and result.reason == "comparator_infrastructure_error"


@pytest.mark.parametrize("response", [
    "invalid comparator response", {"success": True},
    {**accepted_result(), "errors": ["rejected axiom"]},
])
def test_invalid_or_contradictory_verdict_cannot_grant_reward(tmp_path, response):
    result = finish(make_episode(tmp_path, response))
    assert not result.success and result.score == 0.0


def test_comparator_timeout_is_not_a_logical_rejection(tmp_path):
    episode = make_episode(tmp_path, {
        "success": False, "errors": ["Lean verification timed out after 1s"],
        "command": ["comparator"],
    })
    result = finish(episode)
    assert result.aborted and not result.success
    assert result.reason == "comparator_timeout" and result.lean_calls == 2


def test_saved_draft_reaches_comparator_without_a_development_precheck(tmp_path):
    episode = make_episode(tmp_path, {"success": False, "errors": ["sorryAx"]}, saved=STATEMENT)
    episode.tools.verifier.verify_file = AsyncMock(side_effect=AssertionError("unexpected precheck"))
    result = finish(episode)
    assert not result.success and result.reason == "comparator_rejected"
    assert episode.contract_verifier.verify.await_args.args == (STATEMENT,)
    episode.tools.verifier.verify_file.assert_not_awaited()


def test_missing_saved_submission_does_not_use_an_old_proof(tmp_path):
    episode = make_episode(tmp_path, accepted_result())
    episode.tools.workspace.submission_file.unlink()
    result = finish(episode)
    assert not result.success and result.reason == "invalid_submission"
    episode.contract_verifier.verify.assert_not_awaited()


def test_finalization_cannot_run_before_rollout_stops(tmp_path):
    episode = make_episode(tmp_path, accepted_result())
    with pytest.raises(RuntimeError, match="Stop the rollout"):
        asyncio.run(episode.finalize())
    episode.contract_verifier.verify.assert_not_awaited()


def test_concurrent_finalization_requests_share_one_verdict(tmp_path):
    episode = make_episode(tmp_path, accepted_result())
    episode.stop("model_finished")

    async def finalize_twice():
        first, second = await asyncio.gather(episode.finalize(), episode.finalize())
        assert first is second
        assert first.success

    asyncio.run(finalize_twice())
    episode.contract_verifier.verify.assert_awaited_once()


@pytest.mark.parametrize("previous_policy", [
    "legacy.policy.v1", "dvp.policy.comparator.v1", "dvp.policy.comparator.v3",
])
def test_comparator_policy_cannot_resume_previous_acceptance_semantics(tmp_path, previous_policy):
    from dvp.cli.evaluate import build_parser
    from dvp.orchestration.manifest import build_run_manifest, validate_resume_manifest

    dataset = tmp_path / "tasks.jsonl"
    dataset.write_text('{"task_id":"task","statement":"theorem t : True := by sorry"}\n')
    args = build_parser().parse_args([
        "--dataset", str(dataset), "--model", "mock",
        "--lean-retrieve", "false", "--run-root", str(tmp_path / "run"),
    ])
    current = build_run_manifest(args, dataset=dataset, run_root=args.run_root)
    previous = deepcopy(current)
    previous["policy"]["version"] = previous_policy
    previous["config_id"] = replace(
        EpisodeConfig(), policy_version=previous["policy"]["version"],
    ).config_id
    with pytest.raises(ValueError, match="config_id|policy"):
        validate_resume_manifest(previous, current, allow_endpoint_change=True)
