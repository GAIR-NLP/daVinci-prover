"""Keep exhausted transient model faults out of proof-scoring denominators."""

import asyncio
from functools import partial
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from litellm import AuthenticationError, ServiceUnavailableError

from dvp.cli.evaluate import build_parser
from dvp.orchestration import attempts, results
from dvp.protocol import EpisodeResult


transient_error = partial(ServiceUnavailableError, model="mock", llm_provider="openai")
auth_error = partial(AuthenticationError, model="mock", llm_provider="openai")


def setup_attempts(tmp_path, monkeypatch, outcomes):
    args = build_parser().parse_args([
        "--model", "mock", "--run-root", str(tmp_path / "run"),
        "--target-workspace", str(tmp_path), "--trace-level", "sft",
        "--attempts-per-task", "1", "--retries", "2", "--retry-delay", "0",
    ])
    row = {
        "task_id": "auth-recovery", "_dataset_index": 0,
        "statement": "example : True := by trivial",
    }
    pending = iter(outcomes)
    clients = []
    monkeypatch.setattr(attempts, "build_services", lambda **kw: SimpleNamespace(
        tools=None, contract_verifier=None,
    ))
    monkeypatch.setattr(attempts, "build_episode", lambda **kw: SimpleNamespace(
        config=kw["config"], episode_id="mock-episode", tool_schemas=lambda: [],
    ))

    class Driver:
        def __init__(self, *, client, episode, **kwargs):
            clients.append(client)
            self.episode = episode
            self.messages = [{"role": "user", "content": "prove the target"}]

        async def run_rollout(self):
            outcome = next(pending)
            if isinstance(outcome, Exception):
                raise outcome
            result = EpisodeResult(
                message="mock result", episode_id="mock-episode", turns=1,
                tool_calls=0, lean_calls=0, terminated=True, truncated=False, aborted=False,
                **outcome,
            )
            self.episode.finalize = AsyncMock(return_value=result)
            return result

    monkeypatch.setattr(attempts, "ProverDriver", Driver)
    return args, row, clients


def summary(args, record):
    return results.build_progress_summary(
        args=args, manifest={"config_id": "test", "schema_version": "test"},
        dataset=args.dataset, run_root=args.run_root, selected=1, records=[record],
    )


@pytest.mark.parametrize("message", [
    "transient upstream dependency failure",
    "upstream temporarily overloaded",
])
def test_typed_fault_marks_real_attempt_unscored_and_persists_reason(
    tmp_path, monkeypatch, message,
):
    # The generic message deliberately matches none of the legacy text markers.
    args, row, _ = setup_attempts(tmp_path, monkeypatch, [transient_error(message)])
    run_dir = args.run_root / "tasks" / "task" / "attempt_1_retry_0"
    record = asyncio.run(attempts.run_attempt(row, args, run_dir, attempt=1, retry=0))

    assert record["status"] == "infrastructure_error"
    assert not record["valid_for_scoring"] and not record["success"]
    assert record["result"]["aborted"]
    assert record["result"]["reason"] == "infrastructure_error"
    assert record["error"].startswith("ServiceUnavailableError:") and message in record["error"]
    saved = json.loads((run_dir / "attempt_result.json").read_text())
    assert saved == record
    conversation = json.loads((run_dir / "conversation.json").read_text())
    assert conversation["metadata"]["status"] == "infrastructure_error"
    assert conversation["result"]["reason"] == "infrastructure_error"


def test_task_retries_typed_fault_without_consuming_proof_sample(tmp_path, monkeypatch):
    args, row, clients = setup_attempts(tmp_path, monkeypatch, [
        transient_error("transient upstream dependency failure"),
        {"success": True, "score": 1.0, "reason": "verified_exact_target"},
    ])
    record = asyncio.run(attempts.run_task(row, args, args.run_root))

    assert record["status"] == "passed" and record["valid_for_scoring"]
    assert record["runs_used"] == 2 and record["attempts_used"] == 1
    assert record["infrastructure_retries"] == 1
    assert record["first_attempt_success"]
    assert [(r["attempt"], r["retry"]) for r in record["runs"]] == [(1, 0), (1, 1)]
    assert [r["valid_for_scoring"] for r in record["runs"]] == [False, True]
    assert len(clients) == 2 and clients[0] is not clients[1]
    assert clients[0].routing_key == clients[1].routing_key
    progress = summary(args, record)
    assert progress["completed"] == progress["scored"] == progress["passed"] == 1
    assert progress["infrastructure_failed"] == 0 and progress["pass@1"] == 1.0


def test_exhausted_task_retries_remain_infrastructure_not_proof_failure(tmp_path, monkeypatch):
    args, row, clients = setup_attempts(tmp_path, monkeypatch, [
        transient_error("transient upstream dependency failure") for _ in range(3)
    ])
    record = asyncio.run(attempts.run_task(row, args, args.run_root))

    assert record["status"] == "infrastructure_error"
    assert not record["valid_for_scoring"] and not record["success"]
    assert record["runs_used"] == 3 and record["attempts_used"] == 0
    assert record["infrastructure_retries"] == 3
    assert [r["retry"] for r in record["runs"]] == [0, 1, 2]
    assert all(not r["valid_for_scoring"] for r in record["runs"])
    assert len({client.routing_key for client in clients}) == 1
    saved = json.loads((Path(record["run_dir"]) / "result.json").read_text())
    assert saved == record
    progress = summary(args, record)
    assert progress["completed"] == progress["infrastructure_failed"] == 1
    assert progress["scored"] == progress["passed"] == 0
    assert progress["pass@1"] is None


@pytest.mark.parametrize("message", [
    "Invalid API key",
    "Permission denied for this model",
    "Invalid key; service unavailable for this account",
])
def test_permanent_auth_error_keeps_existing_nonretrying_behavior(tmp_path, monkeypatch, message):
    args, row, clients = setup_attempts(tmp_path, monkeypatch, [auth_error(message)])
    record = asyncio.run(attempts.run_task(row, args, args.run_root))

    assert record["status"] == "failed" and record["valid_for_scoring"]
    assert record["runs_used"] == record["attempts_used"] == len(clients) == 1
    assert record["infrastructure_retries"] == 0
    assert record["result"]["reason"] == "exception"
    assert not record["result"]["aborted"]
    progress = summary(args, record)
    assert progress["scored"] == 1 and progress["infrastructure_failed"] == 0
