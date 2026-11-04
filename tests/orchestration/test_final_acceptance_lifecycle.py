"""Development deadlines end generation, then saved-source grading runs once."""

import asyncio
from dataclasses import replace
from functools import partial
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from litellm import ServiceUnavailableError

from dvp.cli.evaluate import build_parser
from dvp.factory import build_services
from dvp.orchestration import attempts
from dvp.verification.comparator import digest


transient_error = partial(ServiceUnavailableError, model="mock", llm_provider="openai")

STATEMENT = "theorem target : True := by sorry\n"
PROOF = "theorem target : True := by trivial\n"


def setup_lifecycle(tmp_path, monkeypatch, *, accepted=True, saved=PROOF,
                    final_delay=0.0, final_error=None, service_error=False, model_error=None):
    args = build_parser().parse_args([
        "--model", "mock", "--run-root", str(tmp_path / "runs"),
        "--target-workspace", str(tmp_path / "mathlib"),
        "--lean-retrieve", "false", "--trace-level", "full",
        "--attempts-per-task", "1", "--retries", "2", "--retry-delay", "0",
        "--max-turns", "200", "--max-tool-calls", "800",
    ])
    args.task_timeout = 0.08
    args.lean_timeout = 2
    args.target_workspace.mkdir()
    row = {"task_id": "deadline", "_dataset_index": 0, "statement": STATEMENT}
    events = []
    clients = []
    sinks = []
    services = []
    model_waiting = asyncio.Event()
    grading_started = asyncio.Event()

    class Sink:
        def __init__(self, path):
            self.closed = False
            self.records = []
            sinks.append(self)

        def record(self, event):
            assert not self.closed, "finalization wrote into a closed trajectory"
            self.records.append(event)

        def close(self):
            if not self.closed:
                events.append("sink_closed")
            self.closed = True

    async def grade(code, *, bundle, **kwargs):
        assert events[-1] == "model_cancelled"
        assert not sinks[-1].closed
        assert code == saved
        events.append("grading_started")
        grading_started.set()
        try:
            await asyncio.sleep(final_delay)
        except asyncio.CancelledError:
            events.append("grading_cancelled")
            raise
        if final_error:
            return {"success": False, "errors": [final_error], "command": ["comparator"]}
        events.append("grading_finished")
        return {
            "success": accepted, "errors": [] if accepted else ["proof rejected"],
            "backend": "leanprover/comparator",
            "candidate_sha256": digest(code), "original_source_sha256": digest(checker.statement),
            "source_bundle_sha256": bundle.sha256, "source_bundle_manifest": bundle.manifest(),
        }

    checker = SimpleNamespace(verify=AsyncMock(side_effect=grade))

    def make_services(**kwargs):
        checker.statement = kwargs["theorem_statement"]
        instance = replace(build_services(**kwargs), contract_verifier=checker)
        for verifier in (instance.tools.verifier, instance.tools.draft_verifier):
            verifier.verify_file = AsyncMock(return_value={
                "success": True, "errors": [], "lean_executed": True,
            })
        services.append(instance)
        return instance

    def make_client(config, *, routing_key):
        count = 0

        async def chat(**kwargs):
            nonlocal count
            count += 1
            if model_error is not None:
                raise model_error
            if service_error:
                raise transient_error("temporary model service unavailable")
            if count == 1:
                return {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "save", "type": "function", "function": {
                        "name": "file_write",
                        "arguments": json.dumps({"file_path": "Submission.lean", "content": saved}),
                    },
                }]}
            model_waiting.set()
            try:
                await asyncio.Event().wait()
            finally:
                events.append("model_cancelled")

        client = SimpleNamespace(chat=AsyncMock(side_effect=chat), routing_key=routing_key,
                                 generation_records=[])
        clients.append(client)
        return client

    monkeypatch.setattr(attempts, "BufferedJsonlSink", Sink)
    monkeypatch.setattr(attempts, "build_services", make_services)
    monkeypatch.setattr(attempts, "build_client", make_client)
    return SimpleNamespace(
        args=args, row=row, events=events, clients=clients, sinks=sinks, services=services,
        checker=checker, model_waiting=model_waiting, grading_started=grading_started,
    )


def test_task_timeout_grades_saved_proof_outside_generation_deadline(tmp_path, monkeypatch):
    case = setup_lifecycle(tmp_path, monkeypatch, final_delay=0.16)
    record = asyncio.run(attempts.run_task(case.row, case.args, case.args.run_root))
    assert record["success"] and record["valid_for_scoring"], record
    assert record["runs_used"] == len(case.clients) == case.checker.verify.await_count == 1
    assert case.clients[0].chat.await_count == 2
    assert case.events == ["model_cancelled", "grading_started", "grading_finished", "sink_closed"]
    assert record["result"]["rollout_reason"] == "task_timeout"
    assert record["result"]["tool_calls"] == 1
    assert record["result"]["lean_calls"] == 2
    assert record["result"]["reason"] == "verified_exact_target"
    assert case.services[0].workspace.submission_file.read_text() == PROOF
    records = case.sinks[0].records
    names = [event["event"] for event in records]
    assert names.count("rollout_end") == 1
    assert names.index("rollout_end") < names.index("final_acceptance") < names.index("end")
    stopped = next(event for event in records if event["event"] == "rollout_end")
    assert stopped["payload"]["reason"] == "task_timeout"
    assert stopped["payload"]["lean_calls"] == 1


def test_task_timeout_with_saved_draft_is_scored_without_model_retry(tmp_path, monkeypatch):
    case = setup_lifecycle(tmp_path, monkeypatch, accepted=False, saved=STATEMENT)
    record = asyncio.run(attempts.run_task(case.row, case.args, case.args.run_root))
    assert not record["success"] and record["valid_for_scoring"], record
    assert record["status"] == "failed"
    assert record["runs_used"] == len(case.clients) == case.checker.verify.await_count == 1
    assert record["result"]["rollout_reason"] == "task_timeout"
    assert record["result"]["reason"] == "comparator_rejected"


def test_final_acceptance_timeout_never_restarts_generation(tmp_path, monkeypatch):
    case = setup_lifecycle(
        tmp_path, monkeypatch, final_error="Lean verification timed out after 2s (comparator).",
    )
    record = asyncio.run(attempts.run_task(case.row, case.args, case.args.run_root))
    assert not record["success"] and not record["valid_for_scoring"], record
    assert record["status"] == "infrastructure_error"
    assert record["result"]["reason"] == "comparator_timeout"
    assert record["runs_used"] == len(case.clients) == case.checker.verify.await_count == 1


def test_model_infrastructure_abort_skips_grading_and_keeps_retries(tmp_path, monkeypatch):
    case = setup_lifecycle(tmp_path, monkeypatch, service_error=True)
    record = asyncio.run(attempts.run_task(case.row, case.args, case.args.run_root))
    assert record["status"] == "infrastructure_error" and not record["valid_for_scoring"]
    assert len(case.clients) == record["runs_used"] == 3
    case.checker.verify.assert_not_awaited()
    assert all(sink.closed for sink in case.sinks)


def test_model_timeout_before_task_deadline_remains_an_infrastructure_fault(tmp_path, monkeypatch):
    case = setup_lifecycle(tmp_path, monkeypatch, model_error=TimeoutError("backend request timed out"))
    case.args.task_timeout = 60
    record = asyncio.run(attempts.run_task(case.row, case.args, case.args.run_root))
    assert record["status"] == "infrastructure_error" and not record["valid_for_scoring"]
    assert record["result"]["reason"] == "infrastructure_error"
    assert record["runs_used"] == len(case.clients) == 3
    case.checker.verify.assert_not_awaited()


@pytest.mark.parametrize("during_grading", [False, True])
def test_external_cancellation_never_leaves_background_grading(
    tmp_path, monkeypatch, during_grading,
):
    case = setup_lifecycle(tmp_path, monkeypatch, final_delay=60.0)
    if not during_grading:
        case.args.task_timeout = 60

    async def cancel():
        task = asyncio.create_task(attempts.run_attempt(
            case.row, case.args, case.args.run_root / "attempt", attempt=1, retry=0,
        ))
        await asyncio.wait_for(
            (case.grading_started if during_grading else case.model_waiting).wait(), timeout=2,
        )
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert all(sink.closed for sink in case.sinks)
        assert "grading_finished" not in case.events
        if during_grading:
            assert "grading_cancelled" in case.events
        else:
            case.checker.verify.assert_not_awaited()

    asyncio.run(cancel())
