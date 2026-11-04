"""Automatic checks cover final bytes of consecutive saves, once per destination."""

import asyncio
import hashlib

import pytest

from dvp.config import BudgetConfig, EpisodeConfig
from dvp.agent import candidate
from test_format_error_limit import CANDIDATE, STATEMENT, action, make_episode, native_call


HELPER = "theorem helper : True := by trivial\n"


def setup(tmp_path, **budget):
    episode, verifier, comparator, sink = make_episode(tmp_path, config=EpisodeConfig(
        budget=BudgetConfig(**budget),
    ))
    verifier.verify_file.return_value = {"success": True, "errors": []}
    return episode, verifier, comparator, sink


def write(name, content, call_id):
    return native_call("file_write", {"file_path": name, "content": content}, call_id)


def edit(old, new, call_id, path="Submission.lean"):
    return native_call("file_edit", {"file_path": path, "old_str": old, "new_str": new}, call_id)


def run(episode, *calls):
    return asyncio.run(episode.step(action(*calls)))


def test_primary_source_belongs_to_checked_target_not_last_helper_write(tmp_path):
    episode, verifier, comparator, _ = setup(tmp_path)
    result = run(episode, write("Submission.lean", CANDIDATE, "target"),
                 write("Submission/H.lean", HELPER, "helper"))
    assert verifier.verify_file.await_count == 2
    assert result.outcomes[0].verified is None
    checked = result.outcomes[1]
    assert checked.call_id == "helper" and checked.raw["file_path"] == "Submission.lean"
    assert checked.raw["automatic_check_call_ids"] == ["target", "helper"]
    assert candidate.outcome_code(episode, checked) == CANDIDATE
    assert checked.artifact_id == "verified:sha256:" + hashlib.sha256(CANDIDATE.encode()).hexdigest()
    state = episode.observe().state
    assert state["target_attempts"] == state["strict_attempts"] == 1
    assert state["last_target_path"] == "Submission.lean"
    assert state["lean_calls_used"] == 2
    comparator.verify.assert_not_awaited()


@pytest.mark.parametrize("source", ["", " \n\t\n"])
def test_blank_primary_source_never_uses_last_savers_helper(tmp_path, source):
    episode, verifier, comparator, _ = setup(tmp_path)
    result = run(episode, write("Submission.lean", source, "target"),
                 write("Submission/H.lean", CANDIDATE, "helper"))
    checked = result.outcomes[-1]
    assert checked.raw["file_path"] == "Submission.lean"
    assert checked.raw["proof_code"] == source
    assert candidate.outcome_code(episode, checked) == source
    assert checked.artifact_id != "verified:sha256:" + hashlib.sha256(CANDIDATE.encode()).hexdigest()
    assert not episode.result().success
    comparator.verify.assert_not_awaited()


def test_failed_save_keeps_error_and_diagnostics_stay_on_successful_saver(tmp_path):
    episode, verifier, comparator, _ = setup(tmp_path)
    result = run(episode, write("Submission.lean", CANDIDATE, "saved"),
                 edit("missing text", "bad", "failed"))
    assert result.outcomes[0].verified
    assert result.outcomes[0].raw["verification_count"] == 1
    assert result.outcomes[1].error_kind == "tool_error"
    assert "old_str not found" in result.outcomes[1].feedback
    assert "verification" not in result.outcomes[1].raw
    assert episode.observe().state["error_counts"]["tool_error"] == 1
    verifier.verify_file.assert_awaited_once()
    comparator.verify.assert_not_awaited()


@pytest.mark.parametrize("barrier", ["file_read", "lean_verify"])
def test_nonedit_call_flushes_before_it_and_later_edits_start_new_group(tmp_path, barrier):
    episode, verifier, _, _ = setup(tmp_path)
    checked = []

    async def verify(path, **kwargs):
        checked.append(path.read_text())
        return {"success": "bad_tactic" not in checked[-1], "errors": []}

    verifier.verify_file.side_effect = verify
    intermediate = CANDIDATE.replace("trivial", "bad_tactic")
    result = run(episode, write("Submission.lean", intermediate, "first"),
                 native_call(barrier, {"file_path": "Submission.lean"}, "barrier"),
                 edit("bad_tactic", "trivial", "second"))
    assert checked == [intermediate] * (2 if barrier == "lean_verify" else 1) + [CANDIDATE]
    assert result.outcomes[0].verified is False
    assert result.outcomes[-1].verified is True
    if barrier == "file_read":
        assert "bad_tactic" in result.outcomes[1].raw["content"]


def test_lean_budget_reserves_unique_destinations_and_rejected_save_changes_nothing(tmp_path):
    episode, verifier, _, _ = setup(tmp_path, max_lean_calls=1)
    result = run(episode,
                 write("Submission.lean", CANDIDATE, "first"),
                 edit("trivial", "exact True.intro", "alias", "Submission.lean/."),
                 write("Submission/H.lean", HELPER, "over-budget"),
                 edit("exact True.intro", "trivial", "last"))
    assert [item.executed for item in result.outcomes] == [True, True, False, True]
    assert result.outcomes[2].error_kind == "lean_budget_exhausted"
    assert not episode.tools.workspace.resolve("Submission/H.lean").exists()
    assert result.outcomes[-1].raw["verification_count"] == 1
    assert episode.result().tool_calls == 3 and episode.result().lean_calls == 1
    assert result.truncated and result.reason == "lean_budget_exhausted"
    verifier.verify_file.assert_awaited_once()


def test_total_tool_budget_exhaustion_still_flushes_saved_edits(tmp_path):
    episode, verifier, _, _ = setup(tmp_path, max_tool_calls=2)
    result = run(episode, write("Submission.lean", CANDIDATE, "first"),
                 edit("trivial", "exact True.intro", "second"),
                 edit("exact True.intro", "bad", "rejected"))
    assert result.outcomes[1].verified
    assert result.outcomes[2].error_kind == "tool_budget_exhausted"
    assert result.truncated and result.reason == "tool_budget_exhausted"
    assert episode.result().tool_calls == 2 and episode.result().lean_calls == 1
    assert "exact True.intro" in episode.tools.workspace.submission_file.read_text()
    verifier.verify_file.assert_awaited_once()


def test_nonlean_last_saver_does_not_change_primary_source_identity(tmp_path):
    episode, verifier, _, _ = setup(tmp_path)
    result = run(episode, write("Submission.lean", CANDIDATE, "proof"),
                 write("Submission/notes.txt", "notes", "notes"))
    assert result.outcomes[-1].raw["file_path"] == "Submission.lean"
    assert candidate.outcome_code(episode, result.outcomes[-1]) == CANDIDATE
    verifier.verify_file.assert_awaited_once()


def test_fatal_save_infrastructure_skips_pending_checks_and_later_calls(tmp_path):
    episode, verifier, _, _ = setup(tmp_path)
    dispatch = episode.tools.call

    async def fail_save(name, args):
        if args.get("file_path") == "Submission/H.lean":
            return {"success": False, "error": "storage unavailable", "_harness_infrastructure_error": True}
        return await dispatch(name, args)

    episode.tools.call = fail_save
    result = run(episode, write("Submission.lean", CANDIDATE, "saved"),
                 write("Submission/H.lean", HELPER, "failed"),
                 native_call("file_read", {"file_path": "Submission.lean"}, "read"))
    assert result.aborted
    assert result.outcomes[0].raw["automatic_check_skipped"] == "infrastructure_error"
    assert result.outcomes[1].infrastructure_error
    assert result.outcomes[2].error_kind == "episode_finished"
    assert episode.tools._deferred_auto_checks is None
    assert episode.tools.workspace.submission_file.read_text() == CANDIDATE
    verifier.verify_file.assert_not_awaited()


def test_verifier_infrastructure_keeps_all_saves_and_stops_following_read(tmp_path):
    episode, verifier, _, _ = setup(tmp_path)
    verifier.verify_file.side_effect = [RuntimeError("offline"), {"success": True, "errors": []}]
    result = run(episode, write("Submission.lean", CANDIDATE, "target"),
                 write("Submission/H.lean", HELPER, "helper"),
                 native_call("file_read", {"file_path": "Submission.lean"}, "read"))
    assert result.aborted and result.outcomes[1].infrastructure_error
    assert result.outcomes[2].error_kind == "episode_finished"
    assert result.outcomes[1].raw["verification_count"] == 2
    assert episode.result().lean_calls == 2
    assert episode.tools._deferred_auto_checks is None
    assert episode.tools.workspace.resolve("Submission/H.lean").read_text() == HELPER


def test_cancellation_mid_flush_charges_attempts_and_clears_deferral(tmp_path):
    episode, verifier, _, _ = setup(tmp_path)

    async def scenario():
        entered_second = asyncio.Event()
        call_count = 0

        async def verify(path, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                entered_second.set()
                await asyncio.Event().wait()
            return {"success": True, "errors": []}

        verifier.verify_file.side_effect = verify
        task = asyncio.create_task(episode.step(action(
            write("Submission.lean", CANDIDATE, "target"),
            write("Submission/H.lean", HELPER, "helper"),
        )))
        await asyncio.wait_for(entered_second.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert episode.result().lean_calls == 2
        assert episode.tools._deferred_auto_checks is None
        assert not episode.observe().state["verified_artifacts"]
        # Outside an action, a standalone save is checked immediately.
        standalone = await episode.tools.file_write({"file_path": "Submission/H.lean", "content": HELPER})
        assert standalone["verification_count"] == 1
        assert call_count == 3

    asyncio.run(scenario())




def test_group_success_still_requires_final_comparator(tmp_path):
    episode, verifier, comparator, _ = setup(tmp_path)
    result = run(episode, write("Submission.lean", CANDIDATE.replace("trivial", "bad"), "first"),
                 edit("bad", "trivial", "second"))
    assert not result.terminated and not episode.result().success
    assert episode.result().tool_calls == 2 and episode.result().lean_calls == 1
    comparator.verify.assert_not_awaited()
    episode.stop("model_finished")
    assert asyncio.run(episode.finalize()).success
    comparator.verify.assert_awaited_once()
    assert comparator.verify.await_args.args == (CANDIDATE,)
