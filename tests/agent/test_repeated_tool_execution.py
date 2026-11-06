"""Repeated actions execute afresh and consume the normal budgets."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from dvp.config import BudgetConfig, EpisodeConfig, RuntimeConfig
from dvp.factory import build_episode, build_services
from dvp.protocol import AssistantAction, ToolCall


THEOREM = "theorem target : True := by sorry"
HELPER = "theorem helper : True := by trivial"


@pytest.fixture
def make_episode(tmp_path, monkeypatch):
    target = tmp_path / "target"
    target.mkdir()
    services = build_services(
        theorem_statement=THEOREM,
        runtime=RuntimeConfig(target_workspace=target),
        run_dir=tmp_path / "run",
    )
    (services.workspace.submission_dir / "helper.lean").write_text(HELPER)
    for verifier in (services.verifier, services.tools.draft_verifier):
        for method in ("verify_code", "verify_file"):
            monkeypatch.setattr(verifier, method, AsyncMock(
                side_effect=AssertionError("unexpected real Lean call"),
            ))
    monkeypatch.setattr(services.contract_verifier, "verify", AsyncMock(
        side_effect=AssertionError("auxiliary checks must not complete the target"),
    ))

    def build(responses, **budget):
        backend = AsyncMock(side_effect=responses)
        monkeypatch.setattr(services.tools, "call", backend)
        episode = build_episode(
            theorem_statement=THEOREM,
            runtime=RuntimeConfig(),
            run_dir=services.workspace.root,
            services=services,
            config=EpisodeConfig(budget=BudgetConfig(**budget)),
        )
        return episode, backend

    return build


def repeat(episode, name, arguments, *, same_turn):
    async def run():
        calls = [ToolCall(str(i), name, dict(arguments)) for i in range(2)]
        if same_turn:
            return [await episode.step(AssistantAction.from_calls(calls))]
        return [
            await episode.step(AssistantAction.from_calls([call])) for call in calls
        ]

    return asyncio.run(run())


@pytest.mark.parametrize("same_turn", [True, False])
@pytest.mark.parametrize("name,arguments,payload_key,payloads", [
    ("lean_retrieve", {"lean_name": "True.intro"}, "results", [
        [{"fullname": "True.intro", "signature": "True.intro : True", "score": score}]
        for score in (0.8, 0.9)
    ]),
    ("content_search", {"path": "target/", "content_pattern": "theorem"}, "matches", [
        [{"file": f"target/{name}.lean", "line": 1, "text": f"theorem {name} : True"}]
        for name in ("first", "second")
    ]),
    ("file_search", {"directory_path": "target/", "pattern": "*.lean"}, "files", [
        ["target/First.lean"], ["target/Second.lean"],
    ]),
])
def test_repeated_lookup_returns_fresh_backend_result(
    make_episode, same_turn, name, arguments, payload_key, payloads,
):
    responses = [
        {"success": True, payload_key: payload} for payload in payloads
    ]
    episode, backend = make_episode(responses)
    transitions = repeat(episode, name, arguments, same_turn=same_turn)
    outcomes = [outcome for step in transitions for outcome in step.outcomes]
    assert backend.await_count == 2
    assert backend.await_args_list[0] == backend.await_args_list[1]
    assert [outcome.raw[payload_key] for outcome in outcomes] == payloads
    assert all(outcome.executed and outcome.valid for outcome in outcomes)
    assert transitions[-1].next_observation.state["tool_calls_used"] == 2
    assert transitions[-1].next_observation.state["lean_calls_used"] == 0
    assert [event["kind"] for step in transitions for event in step.events] == [
        "tool_execution", "tool_execution",
    ]


def strict_failure(message):
    return {
        "success": False,
        "verification_profile": "strict",
        "strict_verified": False,
        "source_placeholders": {"total": 0, "items": []},
        "errors": [message],
    }


@pytest.mark.parametrize("same_turn", [True, False])
@pytest.mark.parametrize("arguments", [
    {"code": HELPER}, {"file_path": "Submission/helper.lean"},
])
def test_repeated_strict_verification_runs_and_counts_each_attempt(
    make_episode, same_turn, arguments,
):
    responses = [strict_failure(f"unknown identifier 'missing{i}'") for i in range(2)]
    episode, backend = make_episode(responses)
    transitions = repeat(episode, "lean_verify", arguments, same_turn=same_turn)
    outcomes = [outcome for step in transitions for outcome in step.outcomes]
    assert backend.await_count == 2
    assert backend.await_args_list[0] == backend.await_args_list[1]
    assert [outcome.raw["errors"] for outcome in outcomes] == [
        response["errors"] for response in responses
    ]
    assert all(outcome.executed and outcome.verified is False for outcome in outcomes)
    state = transitions[-1].next_observation.state
    assert state["tool_calls_used"] == state["lean_calls_used"] == 2
    assert state["strict_attempts"] == 2
    assert not state["exact_target_verified"]


@pytest.mark.parametrize("budget,error_kind", [
    ({"max_lean_calls": 1}, "lean_budget_exhausted"),
    ({"max_tool_calls": 1}, "tool_budget_exhausted"),
])
def test_repeated_verification_cannot_bypass_budget(make_episode, budget, error_kind):
    episode, backend = make_episode([strict_failure("unknown identifier 'missing'")], **budget)
    transition = repeat(episode, "lean_verify", {"code": HELPER}, same_turn=True)[0]
    backend.assert_awaited_once()
    assert transition.outcomes[0].executed
    assert not transition.outcomes[1].executed
    assert transition.outcomes[1].error_kind == error_kind
    state = transition.next_observation.state
    assert state["tool_calls_used"] == state["lean_calls_used"] == 1
    assert transition.truncated and not transition.terminated


@pytest.mark.parametrize("same_turn", [True, False])
@pytest.mark.parametrize("draft", [True, False])
def test_repeated_target_diagnostics_keep_evidence(
    make_episode, same_turn, draft,
):
    candidate = THEOREM if draft else THEOREM.replace("sorry", "trivial")
    response = (
        {
            "success": True,
            "verification_profile": "draft_allow_sorry",
            "strict_verified": False,
            "draft_typechecked": True,
            "source_placeholders": {"total": 1, "items": []},
            "holes": {"total": 1, "items": [{"goal": "⊢ True"}]},
        }
        if draft
        else strict_failure("unsolved goals\n⊢ True")
    )
    episode, backend = make_episode([response, response])
    transitions = repeat(
        episode, "lean_verify", {"code": candidate}, same_turn=same_turn,
    )
    outcomes = [outcome for transition in transitions for outcome in transition.outcomes]
    assert backend.await_count == 2
    assert all(outcome.executed and not outcome.terminal for outcome in outcomes)
    state = transitions[-1].next_observation.state
    assert state["tool_calls_used"] == state["lean_calls_used"] == 2
    assert state["last_target_error"] == "True"
    assert state["last_target_error_kind"] == ("draft_hole" if draft else "unsolved_goals")
    assert all(not transition.terminated and not transition.truncated for transition in transitions)
    assert not {"last_outcome_signature_count", "last_error_signature_count", "last_target_error_count"} & state.keys()
    evidence_key = "holes" if draft else "errors"
    assert all(outcome.raw[evidence_key] == response[evidence_key] for outcome in outcomes)
