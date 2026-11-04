"""Keep proof tasks on independent router sessions across worker retries."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from dvp.artifacts.conversation import build_conversation_artifact
from dvp.cli.evaluate import build_parser
from dvp.factory import build_client
from dvp.orchestration import attempts
from dvp.protocol import EpisodeResult
from dvp.tasks.dataset import safe_task_id
import dvp.llm_clients.client as llm


def test_factory_preserves_explicit_session_and_defaults_to_independent_clients():
    runtime = SimpleNamespace(model="mock", api_key="test-api-key")
    first, second = build_client(runtime), build_client(runtime)
    assert first.routing_key != second.routing_key
    assert build_client(runtime, routing_key="task-a").routing_key == "task-a"
    runtime.routing_key = "configured-session"
    assert build_client(runtime).routing_key == "configured-session"
    assert build_client(runtime, routing_key="override").routing_key == "override"


@pytest.mark.parametrize("root_option", ["--run-root", "--run-dir", None])
def test_task_retry_and_resume_keep_affinity_and_record_actual_header(tmp_path, monkeypatch, root_option):
    run_root = tmp_path / "run"
    root_args = [root_option, str(run_root)] if root_option else []
    args = build_parser().parse_args([
        "--model", "mock", "--python-execute", "false", *root_args,
        "--target-workspace", str(tmp_path), "--trace-level", "sft",
        "--retries", "1", "--retry-delay", "0",
    ])
    args.max_retries = 0
    args.api_key = "test-credential-for-affinity"
    requests = []
    clients = []
    episodes = []

    def handler(request):
        requests.append((request.headers["X-SMG-Routing-Key"], json.loads(request.content)))
        if len(requests) == 1:
            return httpx.Response(503, json={"error": "temporary service failure"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    original = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(llm, "AsyncClient", lambda **kw: original(transport=transport, **kw))
    monkeypatch.setattr(attempts, "build_services", lambda **kw: SimpleNamespace(
        tools=None, contract_verifier=None,
    ))
    def make_episode(**kwargs):
        episode = SimpleNamespace(
            config=kwargs["config"], episode_id="mock-episode", tool_schemas=lambda: [],
            finalize=AsyncMock(return_value=EpisodeResult(
                success=True, score=1.0, message="verified", episode_id="mock-episode",
                turns=1, tool_calls=0, lean_calls=1, terminated=True, truncated=False, aborted=False,
                reason="verified_exact_target", rollout_reason="model_finished",
            )),
        )
        episodes.append(episode)
        return episode

    monkeypatch.setattr(attempts, "build_episode", make_episode)

    class Driver:
        def __init__(self, *, client, **kwargs):
            self.client = client
            self.messages = [{"role": "user", "content": "prove the target"}]
            clients.append(client)

        async def run_rollout(self):
            message = await self.client.chat(messages=self.messages, tools=[])
            self.messages.append(message)

    monkeypatch.setattr(attempts, "ProverDriver", Driver)
    row = {"task_id": "task-a", "statement": "example : True := by trivial", "_dataset_index": 0}
    result = asyncio.run(attempts.run_task(row, args, run_root))
    assert result["runs_used"] == 2 and result["success"]
    assert len(clients) == 2 and clients[0] is not clients[1]
    episodes[0].finalize.assert_not_awaited()
    episodes[1].finalize.assert_awaited_once()
    key = requests[0][0]
    assert requests[1][0] == key
    for run in result["runs"]:
        assert run["endpoint"]["routing_key"] == key
        conversation = json.loads((Path(run["run_dir"]) / "conversation.json").read_text())
        assert conversation["routing"] == {"header": "X-SMG-Routing-Key", "routing_key": key}
        assert args.api_key not in json.dumps(conversation)
        assert args.api_key not in json.dumps(run)

    # A resumed attempt can use a new artifact directory without changing the
    # task's identity. A different task in this run must use another worker key.
    args.resume = True
    args.api_key = "a-different-test-credential"
    resumed = asyncio.run(attempts.run_attempt(
        row, args, run_root / "tasks" / safe_task_id(row) / "attempt_2_retry_0_resume_1",
        attempt=2, retry=0,
    ))
    other = asyncio.run(attempts.run_attempt(
        {**row, "task_id": "task-b", "_dataset_index": 1}, args,
        run_root / "tasks" / "task-b" / "attempt_1_retry_0", attempt=1, retry=0,
    ))
    assert resumed["endpoint"]["routing_key"] == requests[2][0] == key
    assert other["endpoint"]["routing_key"] == requests[3][0] != key
    assert all(payload == requests[0][1] for _, payload in requests)
    episodes[2].finalize.assert_awaited_once()
    episodes[3].finalize.assert_awaited_once()
    assert attempts._task_routing_key(row, run_root / ".." / "run") == key
    assert attempts._task_routing_key(row, tmp_path / "different-run") != key


def test_conversation_without_routing_aware_client_remains_supported():
    artifact = build_conversation_artifact(
        driver=SimpleNamespace(messages=[]),
        episode=SimpleNamespace(tool_schemas=lambda: []),
        client=SimpleNamespace(), result={},
    )
    assert artifact["routing"]["routing_key"] is None
    assert artifact["generation_metadata"]["available"] is False
