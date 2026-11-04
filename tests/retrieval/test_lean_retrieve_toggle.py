"""Exercise the retrieval capability switch without Lean or a model service."""

import asyncio
from copy import deepcopy
from dataclasses import asdict
import json
from unittest.mock import AsyncMock

import httpx
import pytest

import dvp.cli.prove as cli

from dvp.cli.evaluate import build_parser as build_eval_parser
from dvp.orchestration.manifest import build_run_manifest, validate_resume_manifest

import dvp.llm_clients.client as llm
from dvp.config import BudgetConfig, EpisodeConfig, RuntimeConfig
from dvp.agent.driver import ProverDriver
from dvp.factory import build_episode, build_services
from dvp.protocol import EpisodeResult
from dvp.llm_clients.parser import parse_assistant_action
from dvp.toolkits.schemas import TOOL_NAMES


THEOREM = "example : True := by sorry"


@pytest.fixture
def services(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    runtime = RuntimeConfig(target_workspace=target)
    search = AsyncMock(side_effect=AssertionError("retrieval backend must not run"))
    result = build_services(
        theorem_statement=THEOREM, runtime=runtime,
        run_dir=tmp_path / "run", retrieval_search=search,
    )
    for verifier in (result.verifier, result.tools.draft_verifier):
        for method in ("verify_code", "verify_file"):
            monkeypatch.setattr(verifier, method, AsyncMock(
                side_effect=AssertionError("real Lean must not run"),
            ))
    monkeypatch.setattr(result.contract_verifier, "verify", AsyncMock(
        side_effect=AssertionError("real contract Lean must not run"),
    ))
    return result


def make_episode(services, *, enabled=False, turns=8):
    return build_episode(
        theorem_statement=THEOREM, runtime=RuntimeConfig(),
        run_dir=services.workspace.root,
        services=services,
        config=EpisodeConfig(
            enable_lean_retrieve=enabled,
            budget=BudgetConfig(max_turns=turns, max_tool_calls=20, max_lean_calls=2),
        ),
    )


def test_episode_factory_reuses_the_existing_runtime_services(services, monkeypatch):
    def unexpected_build(**kwargs):
        raise AssertionError("prebuilt services must not be rebuilt")

    monkeypatch.setattr("dvp.factory.build_services", unexpected_build)
    episode = make_episode(services)
    assert episode.tools is services.tools
    assert episode.contract_verifier is services.contract_verifier


def native_call(name, arguments, call_id="call"):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments),
    }}


def action(name, arguments, turn=1):
    return parse_assistant_action({
        "role": "assistant", "content": "",
        "tool_calls": [native_call(name, arguments, f"turn-{turn}")],
    }, turn=turn)


def test_default_and_explicit_true_preserve_schemas_and_prompts(services):
    default = build_episode(
        theorem_statement=THEOREM, runtime=RuntimeConfig(),
        run_dir=services.workspace.root, services=services,
    )
    explicit = make_episode(services, enabled=True)
    assert EpisodeConfig().enable_lean_retrieve is True
    assert EpisodeConfig() == EpisodeConfig(enable_lean_retrieve=True)
    assert EpisodeConfig().config_id == EpisodeConfig(enable_lean_retrieve=True).config_id
    assert EpisodeConfig().config_id != EpisodeConfig(enable_lean_retrieve=False).config_id
    assert asdict(EpisodeConfig(enable_lean_retrieve=False))["enable_lean_retrieve"] is False
    assert json.dumps(default.tool_schemas()) == json.dumps(explicit.tool_schemas())
    default_messages = ProverDriver(None, default, THEOREM)._initial_messages()
    assert default_messages == ProverDriver(None, explicit, THEOREM)._initial_messages()
    assert "lean_retrieve" in default_messages[0]["content"]
    assert "lean_retrieve searches Lean declarations" in default_messages[0]["content"]
    assert {s["function"]["name"] for s in default.tool_schemas()} == set(TOOL_NAMES)


@pytest.mark.parametrize("invalid", [None, "false", "true", 0, 1])
def test_episode_switch_requires_boolean(invalid):
    with pytest.raises((TypeError, ValueError), match="enable_lean_retrieve"):
        EpisodeConfig(enable_lean_retrieve=invalid)


def test_disabled_surface_removes_retrieval_guidance_without_mutating_registry(services):
    original = deepcopy(services.tools.schemas())
    disabled = make_episode(services)
    schemas = disabled.tool_schemas()
    assert {s["function"]["name"] for s in schemas} == set(TOOL_NAMES) - {"lean_retrieve"}
    assert "lean_retrieve" not in json.dumps(schemas)
    assert "semantic Lean lookup" not in json.dumps(schemas)
    assert services.tools.schemas() == original
    enabled = make_episode(services, enabled=True)
    assert "lean_retrieve" in json.dumps(enabled.tool_schemas())
    messages = ProverDriver(None, disabled, THEOREM)._initial_messages()
    assert "lean_retrieve" not in json.dumps(messages)
    assert "retrieve an API fact" not in json.dumps(messages)
    assert "lean_verify" in json.dumps(messages)
    assert "file_edit" in {s["function"]["name"] for s in schemas}


def test_disabled_default_prompt_preserves_objective_text_verbatim(services):
    theorem = (
        "/- blocker you may retrieve an API fact, run one focused smaller probe, keep\n"
        "   editing the target directly, or switch strategy. Apply useful evidence back. -/\n"
        "example : True := by sorry"
    )
    messages = ProverDriver(None, make_episode(services), theorem)._initial_messages()
    assert f"```lean\n{theorem}\n```" in messages[1]["content"]
    prompt_without_objective = messages[1]["content"].replace(theorem, "")
    assert "retrieve an API fact" not in prompt_without_objective


@pytest.mark.parametrize("name", [
    "lean_retrieve", "functions.lean_retrieve", "to=functions.lean_retrieve",
    "functions.to=functions.functions.lean_retrieve",
    "  lean_retrieve  ", "lean_retrieve<|channel|>analysis",
])
def test_disabled_aliases_rejected_before_backend_or_budget(monkeypatch, services, name):
    episode = make_episode(services)
    backend = AsyncMock(side_effect=AssertionError("no registry dispatch allowed"))
    monkeypatch.setattr(services.tools, "call", backend)
    transition = asyncio.run(episode.step(action(name, {"lean_name": "Nat.add_comm"})))
    outcome = transition.outcomes[0]
    assert outcome.error_kind == "capability_disabled"
    assert not outcome.executed and not outcome.valid
    assert outcome.reward == 0
    assert "disabled" in outcome.feedback.lower()
    assert not transition.terminated and not transition.aborted
    assert transition.next_observation.state["turns_used"] == 1
    assert transition.next_observation.state["tool_calls_used"] == 0
    assert transition.next_observation.state["lean_calls_used"] == 0
    backend.assert_not_awaited()
    services.tools._retrieval_search.assert_not_awaited()


def test_enabled_retrieval_dispatches_and_preserves_lookup_hit(services):
    episode = make_episode(services, enabled=True)
    hit = {"fullname": "Nat.add_comm", "signature": "theorem add_comm (n m : Nat) : n + m = m + n"}
    backend = AsyncMock(return_value={"success": True, "results": [hit], "count": 1})
    services.tools._tools["lean_retrieve"].handler = backend
    transition = asyncio.run(episode.step(action("lean_retrieve", {"lean_name": "Nat.add_comm"})))
    backend.assert_awaited_once_with({"lean_name": "Nat.add_comm"})
    outcome = transition.outcomes[0]
    assert outcome.executed and outcome.valid
    assert outcome.raw["results"] == [hit]
    assert transition.next_observation.state["tool_calls_used"] == 1
    assert transition.next_observation.state["lean_calls_used"] == 0


def test_other_six_tools_still_dispatch_and_files_really_change(services):
    episode = make_episode(services)
    verifier = AsyncMock(return_value={
        "success": False, "lean_executed": True, "strict_verified": False,
        "error": "deliberate test diagnostic", "stop": False,
    })
    services.tools._tools["lean_verify"].handler = verifier
    text_search = AsyncMock(return_value={
        "success": True, "results": [{"file": "Submission/note.txt", "line": 1, "text": "beta"}],
        "count": 1,
    })
    services.tools._tools["content_search"].handler = text_search
    file_search = AsyncMock(return_value={"success": True, "matches": ["Submission/note.txt"], "count": 1})
    services.tools._tools["file_search"].handler = file_search
    calls = [
        ("file_write", {"file_path": "Submission/note.txt", "content": "alpha\n"}),
        ("file_edit", {"file_path": "Submission/note.txt", "old_str": "alpha", "new_str": "beta"}),
        ("file_read", {"file_path": "Submission/note.txt"}),
        ("file_search", {"directory_path": "Submission", "pattern": "*.txt"}),
        ("content_search", {"path": "Submission", "content_pattern": "beta"}),
        ("lean_verify", {"code": "example : True := by trivial"}),
    ]

    async def run():
        transitions = []
        for turn, (name, args) in enumerate(calls, 1):
            try:
                transition = await asyncio.wait_for(episode.step(action(name, args, turn)), 5)
            except TimeoutError:
                pytest.fail(f"local or mocked {name} unexpectedly stalled")
            transitions.append(transition)
        return transitions

    transitions = asyncio.run(run())
    assert all(t.outcomes[0].executed for t in transitions)
    assert all(t.outcomes[0].error_kind != "capability_disabled" for t in transitions)
    assert all(t.outcomes[0].valid for t in transitions[:5])
    assert (services.workspace.submission_dir / "note.txt").read_text() == "beta\n"
    assert "beta" in transitions[2].outcomes[0].feedback
    assert "note.txt" in transitions[3].outcomes[0].feedback
    assert "beta" in transitions[4].outcomes[0].feedback
    verifier.assert_awaited_once()
    text_search.assert_awaited_once()
    file_search.assert_awaited_once()
    services.tools._retrieval_search.assert_not_awaited()
    state = transitions[-1].next_observation.state
    assert state["tool_calls_used"] == 6 and state["lean_calls_used"] == 1


def test_driver_sends_disabled_surface_and_recovers_from_unadvertised_call(monkeypatch, services):
    episode = make_episode(services, turns=2)
    (services.workspace.submission_dir / "note.txt").write_text("still available\n")
    backend = AsyncMock(wraps=services.tools.call)
    monkeypatch.setattr(services.tools, "call", backend)
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        assert {s["function"]["name"] for s in payload["tools"]} == set(TOOL_NAMES) - {"lean_retrieve"}
        assert "lean_retrieve" not in json.dumps(payload["tools"])
        if len(requests) == 1:
            assert "lean_retrieve" not in json.dumps(payload["messages"])
            call = native_call("functions.lean_retrieve", {"lean_name": "Nat.add_comm"}, "blocked")
        else:
            assert not any(m["role"] in {"assistant", "tool"} for m in payload["messages"])
            feedback = payload["messages"][-1]
            assert feedback["role"] == "user"
            outcomes = json.JSONDecoder().raw_decode(feedback["content"].split("TOOL_RESULTS ", 1)[1])[0]
            assert outcomes[0]["tool_call_id"] == "blocked" and not outcomes[0]["executed"]
            result = outcomes[0]["result"]
            assert result["success"] is False
            assert "disabled" in result["error"].lower()
            call = native_call("file_read", {"file_path": "Submission/note.txt"}, "read")
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant", "content": "", "tool_calls": [call],
        }}]})

    original_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(llm, "AsyncClient", lambda **kw: original_client(transport=transport, **kw))
    client = llm.LiteLLMClient(
        model="mock", api_key=None, base_url="https://mock.invalid/v1", max_retries=0,
    )
    driver = ProverDriver(client, episode, THEOREM, verbose=False)
    result = asyncio.run(driver.run_rollout())
    assert len(requests) == 2 and result.reason == "max_turns"
    backend.assert_awaited_once()
    assert backend.await_args.args[0] == "file_read"
    assert "still available" in driver.messages[-1]["content"]
    assert result.tool_calls == 1 and result.lean_calls == 0
    services.tools._retrieval_search.assert_not_awaited()
    services.contract_verifier.verify.assert_not_awaited()


@pytest.mark.parametrize("parser_factory", [cli.build_parser, build_eval_parser])
def test_cli_and_environment_boolean_precedence(monkeypatch, parser_factory):
    monkeypatch.delenv("DAVINCI_LEAN_RETRIEVE", raising=False)
    assert parser_factory().parse_args([]).lean_retrieve is True
    assert parser_factory().parse_args(["--lean-retrieve", "true"]).lean_retrieve is True
    assert parser_factory().parse_args(["--lean-retrieve", "false"]).lean_retrieve is False
    monkeypatch.setenv("DAVINCI_LEAN_RETRIEVE", "0")
    assert parser_factory().parse_args([]).lean_retrieve is False
    assert parser_factory().parse_args(["--lean-retrieve", "true"]).lean_retrieve is True
    monkeypatch.setenv("DAVINCI_LEAN_RETRIEVE", "true")
    assert parser_factory().parse_args(["--lean-retrieve", "0"]).lean_retrieve is False


@pytest.mark.parametrize("parser_factory", [cli.build_parser, build_eval_parser])
@pytest.mark.parametrize("invalid", ["auto", "none", "", "tru", "2"])
def test_invalid_cli_or_environment_boolean_is_rejected(monkeypatch, parser_factory, invalid):
    monkeypatch.delenv("DAVINCI_LEAN_RETRIEVE", raising=False)
    with pytest.raises(SystemExit):
        parser_factory().parse_args(["--lean-retrieve", invalid])
    monkeypatch.setenv("DAVINCI_LEAN_RETRIEVE", invalid)
    with pytest.raises(SystemExit):
        parser_factory().parse_args([])


@pytest.mark.parametrize("enabled", [False, True])
def test_standalone_cli_passes_switch_to_episode(monkeypatch, services, tmp_path, enabled):
    captured = []
    preflight = AsyncMock(return_value={})
    monkeypatch.setattr("dvp.verification.comparator.preflight_runtime", preflight)

    class Driver:
        def __init__(self, **kwargs):
            captured.append(kwargs["episode"].config.enable_lean_retrieve)

        async def run(self):
            return EpisodeResult(
                success=False, score=0.0, reason="mock_complete", message="mock result",
                episode_id="mock-episode", turns=1, tool_calls=0, lean_calls=0,
                terminated=True, truncated=False, aborted=False,
            )

    monkeypatch.setattr(cli, "build_services", lambda **kwargs: services)
    monkeypatch.setattr(cli, "build_client", lambda args: object())
    monkeypatch.setattr(cli, "ProverDriver", Driver)
    args = cli.build_parser().parse_args([
        "--theorem", THEOREM, "--model", "mock",
        "--run-dir", str(tmp_path / "standalone"), "--trace-level", "off",
        "--lean-retrieve", str(enabled).lower(),
    ])
    assert asyncio.run(cli.async_main(args)) == 1
    preflight.assert_awaited_once_with(
        args.target_workspace.resolve(), timeout=args.lean_timeout,
    )
    assert captured == [enabled]


def test_manifest_and_resume_record_capability_change(monkeypatch, tmp_path):
    monkeypatch.delenv("DAVINCI_LEAN_RETRIEVE", raising=False)
    monkeypatch.delenv("DAVINCI_RETRIEVE_BACKEND", raising=False)
    dataset = tmp_path / "tasks.jsonl"
    dataset.write_text('{}\n')

    def manifest(options):
        args = build_eval_parser().parse_args(["--model", "mock", *options])
        return build_run_manifest(args, dataset=dataset, run_root=tmp_path, rows=())

    default = manifest([])
    enabled = manifest(["--lean-retrieve", "true"])
    disabled = manifest(["--lean-retrieve", "false"])
    assert default == enabled
    assert enabled["episode"]["enable_lean_retrieve"] is True
    assert disabled["episode"]["enable_lean_retrieve"] is False
    assert enabled["services"]["retrieve_backend"] == "lean_explore"
    assert disabled["services"]["retrieve_backend"] == "disabled"
    validate_resume_manifest(deepcopy(disabled), disabled)
    for before, after in [(enabled, disabled), (disabled, enabled)]:
        with pytest.raises(ValueError, match="episode|config_id"):
            validate_resume_manifest(before, after, allow_endpoint_change=True)
