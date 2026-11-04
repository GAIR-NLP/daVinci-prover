"""Bind final acceptance and progress to an immutable local source closure."""

import asyncio
from copy import deepcopy
import hashlib
from types import SimpleNamespace

import pytest

from dvp.verification.modules import collect_bundle
from test_format_error_limit import CANDIDATE, STATEMENT, action, make_episode, native_call


HELPER = "import Mathlib\ntheorem local_helper : True := by trivial\n"
CHANGED_HELPER = HELPER.replace("by trivial", "by exact True.intro")
ENTRY = "import Mathlib\nimport Submission.Helpers\n" + CANDIDATE


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def evidence(bundle):
    return {
        "source_bundle_sha256": bundle.sha256,
        "source_bundle_manifest": bundle.manifest(),
    }


def modular_episode(tmp_path, *, entry=ENTRY, helper=HELPER):
    episode, verifier, comparator, sink = make_episode(tmp_path)
    workspace = episode.tools.workspace
    verifier.config.module_root = workspace.workspaces_dir
    episode.reset()
    entry_path = workspace.submission_file
    helper_path = workspace.submission_dir / "Helpers.lean"
    entry_path.write_text(entry, encoding="utf-8")
    helper_path.write_text(helper, encoding="utf-8")

    def snapshot(code=None):
        return collect_bundle(
            entry_path.read_text() if code is None else code,
            workspace.workspaces_dir, entry_path=entry_path,
        )

    async def strict(path, **kwargs):
        bundle = collect_bundle(path.read_text(), workspace.workspaces_dir, entry_path=path)
        return {"success": True, "errors": [], **evidence(bundle)}

    async def accepted(code, *, bundle=None, **kwargs):
        return {
            "success": True, "errors": [], "backend": "leanprover/comparator",
            "candidate_sha256": digest(code), "original_source_sha256": digest(STATEMENT),
            **(evidence(bundle) if bundle is not None else {}),
        }

    verifier.verify_file.side_effect = strict
    comparator.verify.side_effect = accepted
    return episode, verifier, comparator, sink, entry_path, helper_path, snapshot


def check_entry():
    return action(native_call("lean_verify", {"file_path": "Submission.lean"}))


def test_modular_finalization_freezes_current_sources_once(tmp_path):
    episode, verifier, comparator, _, _, _, snapshot = modular_episode(tmp_path)
    expected = snapshot()
    checked = asyncio.run(episode.step(check_entry()))
    assert checked.outcomes[0].verified and not checked.terminated and checked.reward == 0
    comparator.verify.assert_not_awaited()
    episode.stop("model_finished")
    result = asyncio.run(episode.finalize())
    assert result.success and result.reason == "verified_exact_target"
    assert result.proof_code == ENTRY and result.lean_calls == 2
    comparator.verify.assert_awaited_once()
    assert comparator.verify.await_args.args == (ENTRY,)
    assert comparator.verify.await_args.kwargs["bundle"] == expected
    assert episode.observe().state["contract_lean_calls_used"] == 1
    assert asyncio.run(episode.finalize()) == result
    comparator.verify.assert_awaited_once()
    verifier.verify_file.assert_awaited_once()


@pytest.mark.parametrize("hole_evidence", [
    {"dependency_placeholders": {"total": 1}},
    {"sorries": [{"pos": {"line": 3, "column": 0}, "goal": "⊢ True"}]},
    {"warnings": ["declaration uses 'sorry'"]},
])
def test_compiled_target_with_elaborated_or_dependency_holes_stays_draft(tmp_path, hole_evidence):
    episode, verifier, comparator, _, _, _, _ = modular_episode(tmp_path)
    compile_file = verifier.verify_file.side_effect

    async def incomplete_compile(path, **kwargs):
        return {**await compile_file(path, **kwargs), **hole_evidence}

    verifier.verify_file.side_effect = incomplete_compile
    transition = asyncio.run(episode.step(check_entry()))
    assert transition.outcomes[0].valid and not transition.outcomes[0].verified
    assert transition.outcomes[0].raw["verification_profile"] == "draft_allow_sorry"
    assert not transition.terminated and transition.reward == 0
    assert not episode.observe().state["submission_ready"]
    comparator.verify.assert_not_awaited()


@pytest.mark.parametrize("mutation", ["helper", "transitive_helper", "remove_imports", "missing_development_evidence"])
def test_finalization_uses_saved_closure_instead_of_stale_development_evidence(tmp_path, mutation):
    episode, verifier, comparator, _, entry_path, helper_path, snapshot = modular_episode(tmp_path)
    base_path = helper_path.with_name("Base.lean")
    if mutation == "transitive_helper":
        base_path.write_text("theorem base_helper : True := by trivial\n")
        helper_path.write_text("import Submission.Base\n" + HELPER)
    old = snapshot()
    verifier.verify_file.side_effect = None
    verifier.verify_file.return_value = {
        "success": True, "errors": [],
        **({} if mutation == "missing_development_evidence" else evidence(old)),
    }
    checked = asyncio.run(episode.step(check_entry()))
    assert not checked.terminated
    if mutation == "helper":
        helper_path.write_text(CHANGED_HELPER)
    elif mutation == "transitive_helper":
        base_path.write_text("theorem base_helper : True := by exact True.intro\n")
    elif mutation == "remove_imports":
        entry_path.write_text(CANDIDATE)
    expected = snapshot()
    episode.stop("max_turns", truncated=True)
    result = asyncio.run(episode.finalize())
    assert result.success and result.rollout_reason == "max_turns"
    assert result.proof_code == entry_path.read_text()
    comparator.verify.assert_awaited_once()
    actual = comparator.verify.await_args.kwargs.get("bundle")
    if expected.modules:
        assert actual == expected
    assert comparator.verify.await_args.args == (entry_path.read_text(),)
    verifier.verify_file.assert_awaited_once()


@pytest.mark.parametrize("tamper", ["hash", "manifest", "missing", "entry_hash"])
def test_finalization_rejects_unbound_comparator_bundle(tmp_path, tamper):
    episode, _, comparator, _, _, _, _ = modular_episode(tmp_path)
    accepted = comparator.verify.side_effect

    async def mismatched(code, **kwargs):
        result = await accepted(code, **kwargs)
        if tamper == "hash":
            result["source_bundle_sha256"] = "0" * 64
        elif tamper == "manifest":
            result["source_bundle_manifest"]["modules"][0]["sha256"] = "0" * 64
        elif tamper == "entry_hash":
            result["candidate_sha256"] = "0" * 64
        else:
            result.pop("source_bundle_sha256")
            result.pop("source_bundle_manifest")
        return result

    comparator.verify.side_effect = mismatched
    episode.stop("model_finished")
    result = asyncio.run(episode.finalize())
    assert result.aborted and result.reason == "comparator_infrastructure_error"
    assert result.score == 0 and not result.success
    comparator.verify.assert_awaited_once()
    assert asyncio.run(episode.finalize()) == result
    comparator.verify.assert_awaited_once()


def test_changed_helper_is_a_changed_target_candidate_even_with_identical_entry(tmp_path):
    episode, verifier, comparator, _, entry_path, helper_path, snapshot = modular_episode(tmp_path)

    async def failing_check(path, **kwargs):
        return {"success": False, "errors": ["unsolved goals"], **evidence(snapshot())}

    verifier.verify_file.side_effect = failing_check

    async def run():
        first = await episode.step(check_entry())
        before = episode.observe().state["last_target_candidate_hash"]
        helper_path.write_text(CHANGED_HELPER, encoding="utf-8")
        second = await episode.step(check_entry())
        assert first.outcomes[0].artifact_id != second.outcomes[0].artifact_id
        assert second.outcomes[0].raw["_harness"]["target_candidate_changed"]
        assert episode.observe().state["last_target_candidate_hash"] != before

    asyncio.run(run())

    assert entry_path.read_text() == ENTRY
    assert episode.observe().state["last_target_candidate_hash"] == snapshot().sha256
    comparator.verify.assert_not_awaited()


def test_auxiliary_artifact_identity_includes_imported_helper_sources(tmp_path):
    entry = "import Mathlib\nimport Submission.Helpers\nexample : True := local_helper\n"
    episode, _, comparator, _, _, helper_path, snapshot = modular_episode(tmp_path, entry=entry)

    async def run():
        first = await episode.step(check_entry())
        helper_path.write_text(CHANGED_HELPER, encoding="utf-8")
        second = await episode.step(check_entry())
        assert first.outcomes[0].verified and second.outcomes[0].verified
        assert first.outcomes[0].artifact_id != second.outcomes[0].artifact_id
        assert second.outcomes[0].raw["_harness"]["new_evidence"]

    asyncio.run(run())

    assert len(episode._state.verified_artifacts) == 2
    assert f"verified:sha256:{snapshot().sha256}" in episode._state.verified_artifacts
    assert not episode.observe().state["submission_ready"]
    comparator.verify.assert_not_awaited()


def test_target_in_imported_module_does_not_make_an_entry_candidate(tmp_path):
    episode, _, comparator, _, _, _, _ = modular_episode(
        tmp_path, entry="import Mathlib\nimport Submission.Helpers\n", helper=CANDIDATE,
    )

    checked = asyncio.run(episode.step(check_entry()))

    assert not checked.terminated and checked.reward == 0
    assert checked.outcomes[0].verified
    assert checked.outcomes[0].raw["_harness"]["attempt_kind"] == "auxiliary"
    assert not checked.outcomes[0].raw["_harness"].get("target_candidate")
    comparator.verify.assert_not_awaited()


@pytest.mark.parametrize("module_import", ["Challenge", "Solution", "Lean"])
def test_compiled_auxiliary_imports_are_not_rejected_by_agent_source_policy(tmp_path, module_import):
    entry = f"import {module_import}\nimport Submission.Helpers\nexample : True := local_helper\n"
    episode, _, comparator, _, _, _, _ = modular_episode(tmp_path, entry=entry)

    # The compiler resolves imports. Auxiliary checks cannot earn target credit.
    checked = asyncio.run(episode.step(check_entry()))

    assert checked.outcomes[0].verified and not checked.terminated
    assert len(episode._state.verified_artifacts) == 1
    assert not episode.result().success and checked.reward == 0
    comparator.verify.assert_not_awaited()


def test_finalization_without_any_development_check_passes_saved_modules(tmp_path):
    episode, verifier, comparator, _, _, _, snapshot = modular_episode(tmp_path)
    expected = snapshot()
    episode.stop("model_finished")
    result = asyncio.run(episode.finalize())
    assert result.success and result.tool_calls == 0 and result.lean_calls == 1
    verifier.verify_file.assert_not_awaited()
    assert comparator.verify.await_args.kwargs["bundle"] == expected


def test_missing_helper_cannot_reuse_an_earlier_successful_compilation(tmp_path):
    episode, verifier, comparator, _, _, helper_path, _ = modular_episode(tmp_path)
    checked = asyncio.run(episode.step(check_entry()))
    assert checked.outcomes[0].verified
    helper_path.unlink()
    episode.stop("model_finished")
    result = asyncio.run(episode.finalize())
    assert not result.success and result.score == 0
    comparator.verify.assert_not_awaited()
    verifier.verify_file.assert_awaited_once()


def tool_step(episode, name, arguments):
    return asyncio.run(episode.step(action(native_call(name, arguments))))


@pytest.mark.parametrize("helper_alias", [
    "Submission/Helpers.lean", "Submission/./Helpers.lean", "Submission/unused/../Helpers.lean",
])
def test_batch_save_invalidates_helper_evidence_and_final_checks_current_bundle(tmp_path, helper_alias):
    episode, verifier, comparator, _, entry_path, helper_path, snapshot = modular_episode(tmp_path)
    checked = tool_step(episode, "lean_verify", {"file_path": "Submission/Helpers.lean"})
    assert checked.outcomes[0].verified and episode.observe().state["verified_artifacts"]
    saved = tool_step(episode, "file_edit", {"edits": [
        {"file_path": helper_alias, "old_str": "by trivial", "new_str": "by exact True.intro"},
        {"file_path": "Submission.lean", "old_str": "by trivial", "new_str": "by exact local_helper"},
    ]})
    assert saved.outcomes[0].valid and saved.outcomes[0].verified
    assert saved.outcomes[0].raw["verification_count"] == 2
    assert helper_path.read_text() == CHANGED_HELPER
    assert entry_path.read_text() == ENTRY.replace("by trivial", "by exact local_helper")
    assert verifier.verify_file.await_count == 3
    comparator.verify.assert_not_awaited()

    expected = snapshot()
    final = asyncio.run(episode.step(action(content="The saved proof is complete.", finish_reason="stop")))
    assert final.terminated and not episode.result().success
    assert asyncio.run(episode.finalize()).success
    assert episode.result().tool_calls == 2
    assert episode.result().lean_calls == 4
    assert verifier.verify_file.await_count == 3
    assert comparator.verify.await_args.kwargs["bundle"] == expected
    assert comparator.verify.await_args.args == (entry_path.read_text(),)



def test_helper_edit_invalidates_an_inline_checked_dependency_closure(tmp_path):
    episode, verifier, comparator, _, _, _, _ = modular_episode(tmp_path)
    code = "import Mathlib\nimport Submission.Helpers\nexample : True := local_helper\n"
    bundle = collect_bundle(code, episode.tools.workspace.workspaces_dir)
    verifier.verify_code.return_value = {"success": True, "errors": [], **evidence(bundle)}
    checked = tool_step(episode, "lean_verify", {"code": code})
    assert checked.outcomes[0].verified and len(episode._state.verified_artifacts) == 1
    previous = set(episode._state.verified_artifacts)
    assert "path" not in next(iter(episode._state.verified_artifacts.values()))

    saved = tool_step(episode, "file_edit", {
        "file_path": "Submission/unused/../Helpers.lean",
        "old_str": "by trivial", "new_str": "by exact True.intro",
    })
    assert saved.outcomes[0].valid
    assert previous.isdisjoint(episode._state.verified_artifacts)
    assert saved.outcomes[0].verified
    assert episode.result().lean_calls == 2
    comparator.verify.assert_not_awaited()
