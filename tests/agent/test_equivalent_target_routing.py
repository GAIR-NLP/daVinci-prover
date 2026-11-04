"""A changed target header reaches final comparison after strict checking."""

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dvp.agent.episode import ProverEpisode
from dvp.config import EpisodeConfig
from dvp.protocol import AssistantAction, ToolCall
from dvp.runtime.workspace import Workspace
from dvp.toolkits.registry import ToolRegistry


STATEMENT = "theorem target (x : Nat) : x = x := by sorry\n"
EQUIVALENT = "theorem target : ∀ n : Nat, n = n := by intro n; rfl\n"
WRONG = "theorem target : True := by trivial\n"


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def accepted(code):
    return {
        "success": True, "errors": [], "backend": "leanprover/comparator",
        "candidate_sha256": digest(code),
        "original_source_sha256": digest(STATEMENT),
    }


def make_episode(tmp_path, response, *, local_response=None):
    workspace = Workspace(root=tmp_path / "run")
    workspace.setup()
    local_response = local_response or {"success": True, "errors": []}
    verifier = SimpleNamespace(
        config=SimpleNamespace(target_workspace=None, timeout=1),
        verify_code=AsyncMock(return_value=local_response),
        verify_file=AsyncMock(return_value=local_response),
    )
    tools = ToolRegistry(workspace=workspace, verifier=verifier)
    comparator = SimpleNamespace(verify=AsyncMock(return_value=response))

    async def bound_verdict(code, *, bundle, **kwargs):
        result = comparator.verify.return_value
        if isinstance(result, dict) and result.get("success") is True:
            return {**result, "source_bundle_sha256": bundle.sha256,
                    "source_bundle_manifest": bundle.manifest()}
        return result

    comparator.verify.side_effect = bound_verdict

    episode = ProverEpisode(
        tools=tools, contract_verifier=comparator, theorem_statement=STATEMENT,
        run_dir=workspace.root, config=EpisodeConfig(enable_lean_retrieve=False),
    )
    return episode, verifier, comparator


def verify_action(code):
    return AssistantAction.from_calls([
        ToolCall(id="candidate", name="lean_verify", arguments={"code": code}),
    ])


@pytest.mark.parametrize("entrypoint", ["lean_verify", "file_write", "file_edit", "final_response"])
def test_equivalent_header_is_graded_from_saved_source_after_exit(tmp_path, entrypoint):
    episode, verifier, comparator = make_episode(tmp_path, accepted(EQUIVALENT))
    episode.reset()
    if entrypoint == "lean_verify":
        episode.tools.workspace.submission_file.write_text(EQUIVALENT)
        checked = asyncio.run(episode.step(verify_action(EQUIVALENT)))
    elif entrypoint == "final_response":
        episode.tools.workspace.submission_file.write_text(EQUIVALENT)
        checked = None
    else:
        arguments = {"file_path": "Submission.lean"}
        if entrypoint == "file_write":
            arguments["content"] = EQUIVALENT
        else:
            arguments.update(old_str=STATEMENT, new_str=EQUIVALENT)
        checked = asyncio.run(episode.step(AssistantAction.from_calls([
            ToolCall(id="save", name=entrypoint, arguments=arguments),
        ])))
    comparator.verify.assert_not_awaited()
    if checked is not None:
        assert not checked.terminated and checked.reward == 0
    asyncio.run(episode.step(AssistantAction(content="Done.")))
    result = asyncio.run(episode.finalize())
    assert result.success and result.reason == "verified_exact_target"
    assert result.rollout_reason == "model_finished"
    assert result.lean_calls == (1 if entrypoint == "final_response" else 2)
    comparator.verify.assert_awaited_once()
    assert comparator.verify.await_args.args == (EQUIVALENT,)
    assert verifier.verify_code.await_count == int(entrypoint == "lean_verify")
    assert verifier.verify_file.await_count == int(entrypoint in {"file_write", "file_edit"})


def test_wrong_statement_compiles_for_feedback_then_comparator_rejects_final_file(tmp_path):
    episode, _, comparator = make_episode(tmp_path, {
        "success": False, "errors": ["target type does not match original task"],
    })
    episode.reset()
    episode.tools.workspace.submission_file.write_text(WRONG)
    checked = asyncio.run(episode.step(verify_action(WRONG)))
    assert checked.outcomes[0].verified and not checked.terminated
    comparator.verify.assert_not_awaited()
    episode.stop("model_finished")
    result = asyncio.run(episode.finalize())
    assert result.reason == "comparator_rejected" and not result.success
    assert result.score == 0 and result.proof_code == WRONG
    comparator.verify.assert_awaited_once()


@pytest.mark.parametrize("field,value", [
    ("candidate_sha256", "different candidate"),
    ("original_source_sha256", "different source"),
    ("backend", "ordinary Lean checker"),
])
def test_changed_header_still_requires_source_bound_comparator_evidence(tmp_path, field, value):
    episode, _, comparator = make_episode(tmp_path, {**accepted(EQUIVALENT), field: value})
    episode.reset()
    episode.tools.workspace.submission_file.write_text(EQUIVALENT)
    episode.stop("model_finished")
    result = asyncio.run(episode.finalize())
    comparator.verify.assert_awaited_once()
    assert not result.success and result.reason == "comparator_infrastructure_error"


@pytest.mark.parametrize("code", [
    "theorem helper : True := by trivial\n",
    "namespace Shadow\n" + EQUIVALENT + "end Shadow\n",
])
def test_helper_and_namespace_shadow_remain_auxiliary_during_development(tmp_path, code):
    episode, _, comparator = make_episode(tmp_path, accepted(code))
    transition = asyncio.run(episode.step(verify_action(code)))
    comparator.verify.assert_not_awaited()
    assert not transition.terminated and not episode.result().success
    assert transition.outcomes[0].raw["_harness"]["attempt_kind"] == "auxiliary"
    assert transition.next_observation.state["target_attempts"] == 0


@pytest.mark.parametrize("local_response", [
    {"success": False, "errors": ["unsolved goals"]},
    {"success": True, "errors": []},
])
def test_development_compile_result_does_not_trigger_or_veto_final_comparison(tmp_path, local_response):
    episode, verifier, comparator = make_episode(tmp_path, accepted(EQUIVALENT), local_response=local_response)
    episode.reset()
    episode.tools.workspace.submission_file.write_text(EQUIVALENT)
    checked = asyncio.run(episode.step(verify_action(EQUIVALENT)))
    assert not checked.terminated
    comparator.verify.assert_not_awaited()
    episode.stop("model_finished")
    assert asyncio.run(episode.finalize()).success
    verifier.verify_code.assert_awaited_once()
    comparator.verify.assert_awaited_once()


@pytest.mark.parametrize("prefix", [
    "axiom experimental_assumption : True\n",
    "opaque experimental_value : Nat := 1\n",
    'local notation "ExperimentNat" => Nat\n',
])
@pytest.mark.parametrize("comparator_accepts", [True, False])
def test_ordinary_lean_source_is_judged_only_by_final_comparator(tmp_path, prefix, comparator_accepts):
    code = prefix + EQUIVALENT
    response = accepted(code) if comparator_accepts else {
        "success": False, "errors": ["candidate depends on an unapproved axiom"],
    }
    episode, verifier, comparator = make_episode(tmp_path, response)
    episode.reset()
    episode.tools.workspace.submission_file.write_text(code)
    checked = asyncio.run(episode.step(verify_action(code)))
    assert checked.outcomes[0].verified and not checked.terminated
    comparator.verify.assert_not_awaited()
    episode.stop("model_finished")
    result = asyncio.run(episode.finalize())
    verifier.verify_code.assert_awaited_once()
    comparator.verify.assert_awaited_once()
    assert comparator.verify.await_args.args == (code,)
    assert result.success is comparator_accepts
    assert result.score == float(comparator_accepts)


@pytest.mark.parametrize("candidate_name,target_work", [
    ("Expected.helper", False), ("Shadow.target", False),
    ("target", False), ("Expected.target", True),
])
def test_fixed_context_development_routes_named_target_not_first_helper(tmp_path, candidate_name, target_work):
    original = (
        "namespace Expected\ntheorem helper : True := by trivial\n"
        "theorem target (x : Nat) : x = x := by sorry\nend Expected\n"
    )
    code = f"theorem {candidate_name} : True := by trivial\n"
    episode, _, comparator = make_episode(tmp_path, accepted(code))
    episode.theorem_statement = original
    episode.task_contract = SimpleNamespace(statement=original, target_name="Expected.target")
    transition = asyncio.run(episode.step(verify_action(code)))
    comparator.verify.assert_not_awaited()
    assert (transition.outcomes[0].raw["_harness"]["attempt_kind"] == "exact_target") is target_work
    assert not transition.terminated and transition.reward == 0


def test_fixed_context_saved_target_uses_original_source_bound_verdict(tmp_path):
    original = (
        "namespace Expected\ntheorem helper : True := by trivial\n"
        "theorem target (x : Nat) : x = x := by sorry\nend Expected\n"
    )
    code = original.replace("by sorry", "by rfl")
    episode, verifier, comparator = make_episode(tmp_path, {
        "success": True, "errors": [], "backend": "leanprover/comparator",
        "candidate_sha256": digest(code), "original_source_sha256": digest(original),
    })
    episode.theorem_statement = original
    episode.task_contract = SimpleNamespace(statement=original, target_name="Expected.target")
    episode.reset()
    episode.tools.workspace.submission_file.write_text(code)
    episode.stop("max_turns", truncated=True)
    assert asyncio.run(episode.finalize()).success
    verifier.verify_file.assert_not_awaited()
    comparator.verify.assert_awaited_once()
