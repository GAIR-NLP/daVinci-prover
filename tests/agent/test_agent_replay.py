"""Offline transcript parity and local-verifier failure classification.

Every subprocess replays fixed model/tool/verifier responses through the actual
parser, driver, episode, tool surface, and trajectory serializer. No Lean or LLM
service is started. Complete records and independent execution records are
checked for deterministic replay and explicit development/final-grading boundaries.
The v4 policy deliberately does not reinterpret v3 immediate-acceptance goldens.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

THEOREM = "-- replay preserves Unicode: α\r\ntheorem replay_target : True := by sorry\n"
PROOF = "-- replay preserves Unicode: α\r\ntheorem replay_target : True := by trivial\n"
HELPER = "theorem helper : True := by trivial"
DRAFT = "-- replay preserves Unicode: α\r\ntheorem replay_target : True := by\n  have h : True := by sorry\n  sorry\n"


def _call(name, arguments, call_id="call"):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


def _message(*calls, content="fixed model response", reasoning="sampled reasoning"):
    return {
        "role": "assistant",
        "content": content,
        "reasoning_content": reasoning,
        **({"tool_calls": list(calls)} if calls else {}),
    }


def _strict(success=True, **extra):
    return {
        "success": success,
        "verification_profile": "strict",
        "strict_verified": success,
        "source_placeholders": {"total": 0, "items": []},
        "errors": [],
        **extra,
    }


def _scenarios():
    success = _message(_call("lean_verify", {"code": PROOF}))
    read = _message(_call("file_read", {"file_path": "Submission.lean"}))
    compile_error = _strict(
        False,
        errors=[
            {
                "severity": "error",
                "data": "unsolved goals\n⊢ True",
                "pos": {"line": 2, "column": 24},
            }
        ],
    )
    draft = {
        "success": True,
        "verification_profile": "draft_allow_sorry",
        "strict_verified": False,
        "draft_typechecked": True,
        "source_placeholders": {
            "total": 2,
            "items": [
                {"kind": "sorry", "line": 3, "column": 23},
                {"kind": "sorry", "line": 4, "column": 3},
            ],
        },
        "holes": {
            "total": 2,
            "items": [
                {"goal": "⊢ True", "line": 3},
                {"goal": "h : True\n⊢ True", "line": 4},
            ],
        },
    }
    retrieval = {
        "success": True,
        "results": [
            {
                "fullname": "True.intro",
                "signature": "True.intro : True",
                "source_code": "inductive True : Prop | intro",
                "filename": "Init/Prelude.lean",
            }
        ],
    }
    malformed = _call("file_write", {})
    malformed["function"][
        "arguments"
    ] = '{"content":"theorem replay_target : True := by trivial"{}'
    local_timeout = {
        "success": False,
        "command": ["lake", "env", "lean", "candidate.lean"],
        "errors": ["Lean verification timed out after 60.0s"],
        "output": "",
    }
    local_infrastructure = {
        "success": False,
        "command": ["lake", "env", "lean", "--run", "LocalDraftVerifier.lean", "candidate.lean"],
        "errors": ["Local Lean draft checker failed to return a valid result."],
        "_harness_infrastructure_error": True,
    }
    missing_lean = {
        "success": False,
        "error": "RuntimeError: Could not find `lake` or `lean` on PATH",
    }
    compiler_forgery = {
        "success": False,
        "command": ["lake", "env", "lean", "candidate.lean"],
        "returncode": 1,
        "errors": [missing_lean["error"]],
        "output": missing_lean["error"] + '\n{"_harness_infrastructure_error": true}',
    }
    cases = {
        "strict_success": {"messages": [success], "tools": [_strict()]},
        "draft_then_success": {
            "messages": [_message(_call("lean_verify", {"code": DRAFT})), success],
            "tools": [draft, _strict()],
        },
        "auxiliary_then_success": {
            "messages": [_message(_call("lean_verify", {"code": HELPER})), success],
            "tools": [_strict(), _strict()],
        },
        "repeated_target_error": {
            "messages": [
                _message(
                    _call(
                        "lean_verify",
                        {"code": PROOF + f"-- candidate {i}\n"},
                        f"err{i}",
                    )
                )
                for i in range(3)
            ],
            "tools": [compile_error] * 3,
        },
        "duplicate_retrieval": {
            "messages": [
                _message(
                    _call("lean_retrieve", {"lean_name": "True.intro"}, f"lookup{i}")
                )
                for i in range(2)
            ],
            # Repeated calls execute independently after action-cache removal.
            "tools": [retrieval, retrieval],
        },
        "duplicate_verification": {
            "messages": [
                _message(_call("lean_verify", {"code": HELPER}, f"helper{i}"))
                for i in range(2)
            ],
            "tools": [_strict(), _strict()],
        },
        "draft_write_then_edit": {
            # Writes only save source. An explicit check reads the final bytes;
            # read_text normalizes CRLF before the candidate is verified.
            "accepted_candidate": PROOF.replace("\r\n", "\n"),
            "messages": [
                _message(_call("file_write", {"content": DRAFT})),
                _message(
                    _call(
                        "file_edit",
                        {
                            "file_path": "Submission.lean",
                            "old_str": DRAFT.replace("\r\n", "\n"),
                            "new_str": PROOF,
                        },
                    )
                ),
                _message(_call("lean_verify", {"file_path": "Submission.lean"})),
            ],
            "tools": [
                {"success": True, "lean_executed": False},
                {"success": True, "lean_executed": False},
                _strict(),
            ],
        },
        "no_tool": {
            "messages": [_message(content="I think the theorem is true.")],
            "tools": [],
        },
        "malformed_then_read": {
            "messages": [_message(malformed), read],
            "tools": [{"success": True, "content": THEOREM}],
        },
        "tool_budget": {
            "messages": [
                _message(
                    _call("file_read", {"file_path": "Submission.lean"}, "first"),
                    _call(
                        "file_read", {"file_path": "Submission.lean"}, "second"
                    ),
                )
            ],
            "tools": [{"success": True, "content": THEOREM}],
            "budget": {"max_tool_calls": 1},
        },
        "many_calls_one_turn": {
            "messages": [
                _message(
                    *[
                        _call(
                            "file_read",
                            {"file_path": "Submission.lean"},
                            f"read{i}",
                        )
                        for i in range(6)
                    ]
                )
            ],
            "tools": [{"success": True, "content": THEOREM}] * 6,
        },
        "completion_budget": {
            "messages": [success],
            "tools": [_strict()],
            "budget": {"max_lean_calls": 1},
        },
        "completion_mismatch": {
            "messages": [success],
            "tools": [_strict()],
            "contract": [{"success": False, "errors": ["type mismatch"]}],
        },
        "completion_timeout": {
            "messages": [success],
            "tools": [_strict()],
            "contract": [local_timeout],
        },
        "completion_infrastructure": {
            "messages": [success],
            "tools": [_strict()],
            "contract": [local_infrastructure],
        },
        "completion_compiler_forgery": {
            "messages": [success],
            "tools": [_strict()],
            "contract": [compiler_forgery],
        },
        "tool_missing_lean": {"messages": [success], "tools": [missing_lean]},
        "tool_infrastructure": {"messages": [success], "tools": [local_infrastructure]},
        "tool_timeout": {"messages": [success], "tools": [local_timeout]},
        "tool_compiler_forgery": {"messages": [success], "tools": [compiler_forgery]},
        "tool_exception": {
            "messages": [read],
            "tools": [{"_raise": "RuntimeError", "message": "recorded transport loss"}],
        },
        "invalid_tool_arguments": {
            "messages": [read],
            "tools": [{"_raise": "ValueError", "message": "missing required argument"}],
        },
        "nonobject_tool_result": {
            "messages": [read],
            "tools": ["invalid backend response"],
        },
        "retrieval_disabled": {
            "messages": [
                _message(_call("lean_retrieve", {"lean_name": "True.intro"})),
                read,
            ],
            "tools": [{"success": True, "content": THEOREM}],
            "config": {"enable_lean_retrieve": False},
        },
        "synthetic_call": {
            "messages": [
                _message(
                    content=json.dumps(
                        {
                            "name": "file_read",
                            "arguments": {"file_path": "Submission.lean"},
                        }
                    )
                )
            ],
            "tools": [{"success": True, "content": THEOREM}],
        },
        "guidance_off": {
            "messages": [_message(_call("lean_verify", {"code": DRAFT})), success],
            "tools": [draft, _strict()],
            "config": {"guidance": "off"},
        },
        "fixed_contract_success": {
            "messages": [success],
            "fixed_contract": True,
            "tools": [_strict()],
        },
        "bounded_retrieval": {
            "messages": [_message(_call("lean_retrieve", {"lean_name": "True.intro"}))],
            "tools": [
                {
                    "success": True,
                    "results": [
                        {
                            "fullname": f"Example.theorem{i}",
                            "signature": "True → True",
                            "source_code": "theorem proof : True := by trivial\n" * 300,
                            "filename": "Library.lean",
                        }
                        for i in range(20)
                    ],
                    "request_id": "transport-only",
                }
            ],
            "config": {"max_tool_result_chars": 512},
        },
        "bounded_verifier": {
            "messages": [_message(_call("lean_verify", {"code": PROOF}))],
            "tools": [
                _strict(
                    False,
                    output="compiler output\n" * 1000,
                    errors=[
                        {
                            "severity": "error",
                            "data": "unsolved goals\n" + "h : True\n" * 1000 + "⊢ True",
                        }
                    ],
                )
            ],
            "config": {"max_tool_result_chars": 512},
        },
    }
    # These controlled v4 success fixtures explicitly save their final source.
    # An inline lean_verify result alone must never become the submission.
    saved = {
        "strict_success", "draft_then_success", "auxiliary_then_success", "guidance_off",
        "fixed_contract_success", "completion_budget", "completion_mismatch",
        "completion_timeout", "completion_infrastructure", "completion_compiler_forgery",
    }
    for name in saved:
        plan = cases[name]
        plan["messages"] = deepcopy(plan["messages"])
        plan["messages"][-1]["tool_calls"].insert(0, _call(
            "file_write", {"file_path": "Submission.lean", "content": PROOF}, "save-final",
        ))
        plan["tools"].insert(-1, {"success": True, "lean_executed": False})
        plan["accepted_candidate"] = PROOF.replace("\r\n", "\n")
    for name in saved | {"draft_write_then_edit"}:
        cases[name]["expected_success"] = "contract" not in cases[name]
    cases["inline_probe_unsaved"] = {"messages": [success], "tools": [_strict()]}
    cases["model_finished_success"] = deepcopy(cases["strict_success"])
    cases["model_finished_success"]["messages"].append(_message(content="Done."))
    return cases


# Explicit behavior assertions below replace pre-v4 full-transcript hashes.
# No schema/policy/default metadata is rewritten to mimic an obsolete rollout.
EXECUTION_RECORD_KEYS = (
    "tool_calls", "contract_calls", "events", "rollout", "result", "lifecycle_errors", "workspace",
)


def _digest_source(source):
    return hashlib.sha256(source.encode()).hexdigest()


def _run_replay(scenario, *, config_defaults=None):
    """Executed in an isolated interpreter; class/module identities never mix."""

    def module(name):
        return importlib.import_module(f"dvp.{name}")

    config_module = module("config")
    driver_module = module("agent.driver")
    episode_module = module("agent.episode")
    trajectory_module = module("artifacts.trajectory")
    tools_module = module("toolkits.schemas")
    files_module = module("toolkits.files")
    plan = deepcopy(_scenarios()[scenario])

    class Workspace:
        def __init__(self, root):
            self.root = root

        def resolve(self, path, **_):
            return self.root / path

    class Registry:
        def __init__(self, root):
            self.workspace = Workspace(root)
            self.calls = []
            self.responses = list(plan["tools"])
            if plan.get("fixed_contract"):
                self.task_contract = SimpleNamespace(
                    contract_hash="recorded-contract-hash",
                    target_name="replay_target",
                    statement=THEOREM.strip(),
                    guidance="Recorded fixed-context contract guidance.",
                )

        def schemas(self):
            handlers = SimpleNamespace(**{
                name: self.call for name in tools_module.TOOL_NAMES
            })
            return [tool.schema() for tool in tools_module.build_tools(handlers).values()]

        async def call(self, name, arguments):
            self.calls.append([name, deepcopy(arguments)])
            if not self.responses:
                raise AssertionError("unexpected tool execution")
            raw = deepcopy(self.responses.pop(0))
            if isinstance(raw, dict) and "_raise" in raw:
                kind = {"ValueError": ValueError, "RuntimeError": RuntimeError}[
                    raw["_raise"]
                ]
                raise kind(raw["message"])
            if name in {"file_write", "file_edit"}:
                path = self.workspace.resolve(arguments["file_path"])
                path.parent.mkdir(parents=True, exist_ok=True)
                if name == "file_write":
                    content = arguments["content"]
                else:
                    content, error = files_module.preview_file_edit_content(
                        path.read_text(), arguments
                    )
                    if error:
                        raise AssertionError(error)
                path.write_text(content)
                raw["file_path"] = arguments["file_path"]
            return raw

    class Verifier:
        def __init__(self):
            self.calls = []
            self.responses = list(plan.get("contract", [{
                "success": bool(plan.get("expected_success")),
                "errors": [] if plan.get("expected_success") else ["saved submission has unfinished proof obligations"],
                "output": "recorded comparator check",
                "backend": "leanprover/comparator",
                "candidate_sha256": _digest_source(plan.get("accepted_candidate", PROOF)),
                "original_source_sha256": _digest_source(THEOREM.strip() if plan.get("fixed_contract") else THEOREM),
            }]))

        async def verify(self, code, *, bundle, **kwargs):
            self.calls.append({"code": code, "bundle": bundle.manifest(), **kwargs})
            if not self.responses:
                raise AssertionError("unexpected duplicate final grading")
            result = deepcopy(self.responses.pop(0))
            result.update(source_bundle_sha256=bundle.sha256, source_bundle_manifest=bundle.manifest())
            return result

    class Client:
        def __init__(self):
            self.responses = list(plan["messages"])
            self.requests = []

        async def chat(self, **kwargs):
            self.requests.append(deepcopy(kwargs))
            if not self.responses:
                raise AssertionError("unexpected model generation")
            return deepcopy(self.responses.pop(0))

    with tempfile.TemporaryDirectory() as temp:
        run_dir = Path(temp)
        registry = Registry(run_dir / "workspaces")
        verifier, client = Verifier(), Client()
        sink = trajectory_module.MemoryTrajectorySink()
        budget = config_module.BudgetConfig(
            max_turns=len(plan["messages"]), **plan.get("budget", {})
        )
        config = config_module.EpisodeConfig(
            budget=budget, **{**(config_defaults or {}), **plan.get("config", {})},
        )
        episode = episode_module.ProverEpisode(
            tools=registry,
            contract_verifier=verifier,
            theorem_statement=THEOREM,
            run_dir=run_dir,
            config=config,
            sink=sink,
            episode_id=f"replay-{scenario}",
        )
        driver = driver_module.ProverDriver(client, episode, THEOREM, verbose=False)
        async def run_phases():
            rollout = await driver.run_rollout()
            assert not verifier.calls, "development must not invoke Comparator"
            final = await episode.finalize()
            assert await episode.finalize() == final
            return rollout, final

        rollout, result = asyncio.run(run_phases())
        assert not registry.responses, "recorded tool response was not consumed"
        assert not client.responses, "recorded model response was not consumed"
        events = deepcopy(sink.events)
        for event in events:
            event.pop("time")
        # Lifecycle exceptions are observable behavior too.
        lifecycle = []
        for operation in (
            episode.reset,
            lambda: asyncio.run(
                episode.step(module("protocol").AssistantAction())
            ),
        ):
            try:
                operation()
            except Exception as exc:
                lifecycle.append([type(exc).__name__, str(exc)])
        return {
            "requests": client.requests,
            "messages": driver.messages,
            "tool_calls": registry.calls,
            "contract_calls": verifier.calls,
            "events": events,
            "rollout": rollout.to_dict(),
            "result": result.to_dict(),
            "lifecycle_errors": lifecycle,
            "workspace": {
                str(
                    path.relative_to(registry.workspace.root)
                ): path.read_bytes().decode()
                for path in sorted(registry.workspace.root.rglob("*"))
                if path.is_file()
            },
        }


def _subprocess_replay(scenario, pythonpath, *, config_defaults=None):
    command = [sys.executable, str(Path(__file__).resolve()), "--replay", scenario]
    if config_defaults is not None:
        command.extend(["--config-defaults", json.dumps(config_defaults)])
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PYTHONPATH": str(pythonpath)},
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _execution_behavior(record):
    """Exclude only the two changed defaults and their display/provenance fields.

    Every transition action, outcome, reward, budget, and termination field is
    retained, as are the actual tool/Comparator calls, results, and file bytes.
    Tool schemas are retained too; only model-visible guidance is excluded.
    """
    execution = deepcopy({key: record[key] for key in EXECUTION_RECORD_KEYS})
    for event in execution["events"]:
        payload = event["payload"]
        if event["event"] == "start":
            payload.pop("config_id")
            payload["config"].pop("guidance")
            payload["config"].pop("retain_truncated_thinking")
        for name in ("observation", "next_observation"):
            if name not in payload:
                continue
            observation = payload[name]
            observation.pop("guidance")
            observation["state"].pop("config_id")
            observation["state"].pop("retain_truncated_thinking")
    return execution


@pytest.mark.parametrize("scenario", list(_scenarios()))
def test_changed_history_defaults_preserve_fixed_response_execution(scenario):
    root = Path(__file__).resolve().parents[2]
    current = _subprocess_replay(scenario, root / "src")
    previous = _subprocess_replay(scenario, root / "src", config_defaults={
        "guidance": "soft", "retain_truncated_thinking": True,
    })
    assert len(current["requests"]) == len(previous["requests"])
    assert _execution_behavior(current) == _execution_behavior(previous)


@pytest.mark.parametrize("scenario", list(_scenarios()))
def test_v4_replay_is_deterministic_and_grades_only_saved_source(scenario):
    root = Path(__file__).resolve().parents[2]
    actual = _subprocess_replay(scenario, root / "src")
    repeated = _subprocess_replay(scenario, root / "src")
    assert actual == repeated
    start = actual["events"][0]["payload"]
    assert start["config"]["policy_version"] == "dvp.policy.comparator.v4"
    assert start["observation"]["schema_version"] == "dvp.episode.v12"
    rollout, result = actual["rollout"], actual["result"]
    assert not rollout["success"] and rollout["score"] == 0
    assert result["rollout_reason"] == rollout["reason"]
    assert result["tool_calls"] == rollout["tool_calls"] == len(actual["tool_calls"])
    transitions = [event for event in actual["events"] if event["event"] == "transition"]
    assert len(transitions) == len(_scenarios()[scenario]["messages"])
    for event in transitions:
        assert event["payload"]["reward"] == 0
        assert all(item["reward"] == 0 for item in event["payload"]["outcomes"])
        assert all("contract_verification" not in item["raw"] for item in event["payload"]["outcomes"])
    names = [event["event"] for event in actual["events"]]
    assert names.count("end") == 1 and names[-1] == "end"
    assert len(actual["lifecycle_errors"]) == 2
    if rollout["aborted"]:
        assert not actual["contract_calls"]
        assert names[-2] == "final_acceptance_skipped"
        assert result["lean_calls"] == rollout["lean_calls"]
    else:
        assert len(actual["contract_calls"]) == 1
        assert names[-2] == "final_acceptance"
        assert result["lean_calls"] == rollout["lean_calls"] + 1
        call = actual["contract_calls"][0]
        saved = actual["workspace"]["Submission.lean"].replace("\r\n", "\n")
        assert call["code"] == saved == result["proof_code"]
        assert call["bundle"]["entry_sha256"] == _digest_source(saved)
    assert result["success"] is bool(_scenarios()[scenario].get("expected_success"))


def test_six_calls_keep_all_results_in_model_history():
    root = Path(__file__).resolve().parents[2]
    actual = _subprocess_replay("many_calls_one_turn", root / "src")
    assert len(actual["tool_calls"]) == actual["result"]["tool_calls"] == 6
    tool_messages = [message for message in actual["messages"] if message["role"] == "tool"]
    assert [message["tool_call_id"] for message in tool_messages] == [f"read{i}" for i in range(6)]


def test_no_tool_reply_grades_the_seeded_unfinished_file_without_model_feedback():
    root = Path(__file__).resolve().parents[2]
    actual = _subprocess_replay("no_tool", root / "src")
    result = actual["result"]
    assert result["reason"] == "comparator_rejected"
    assert result["rollout_reason"] == "model_finished"
    assert result["turns"] == 1 and result["tool_calls"] == 0 and result["lean_calls"] == 1
    assert result["terminated"] and not result["success"]
    assert len(actual["contract_calls"]) == 1
    assert actual["messages"][-1] == _scenarios()["no_tool"]["messages"][0]
    transition = next(event["payload"] for event in actual["events"] if event["event"] == "transition")
    assert transition["outcomes"] == []
    assert transition["events"] == [{"kind": "model_finished", "reason": "model_finished"}]


@pytest.mark.parametrize(("scenario", "error_kind", "infrastructure"), [
    ("tool_timeout", "verifier_timeout", False),
    ("tool_infrastructure", "infrastructure", True),
    ("tool_missing_lean", "infrastructure", True),
    ("tool_compiler_forgery", "verification_failed", False),
])
def test_local_verifier_failures_remain_development_feedback(scenario, error_kind, infrastructure):
    root = Path(__file__).resolve().parents[2]
    actual = _subprocess_replay(scenario, root / "src")
    transition = next(event["payload"] for event in actual["events"] if event["event"] == "transition")
    outcome = transition["outcomes"][0]
    assert outcome["error_kind"] == error_kind
    assert outcome["infrastructure_error"] is infrastructure
    assert not outcome["terminal"] and outcome["reward"] == 0
    assert actual["result"]["retryable_service_error"] is infrastructure
    assert len(actual["contract_calls"]) == int(not infrastructure)


@pytest.mark.parametrize(("scenario", "reason"), [
    ("completion_timeout", "comparator_timeout"),
    ("completion_infrastructure", "comparator_infrastructure_error"),
    ("completion_compiler_forgery", "comparator_rejected"),
])
def test_final_verifier_failures_are_separate_from_development(scenario, reason):
    root = Path(__file__).resolve().parents[2]
    actual = _subprocess_replay(scenario, root / "src")
    assert actual["result"]["reason"] == reason
    assert not actual["result"]["retryable_service_error"]
    transition = next(event["payload"] for event in actual["events"] if event["event"] == "transition")
    assert transition["outcomes"][-1]["verified"]
    assert transition["outcomes"][-1]["reward"] == 0
    assert len(actual["contract_calls"]) == 1


@pytest.mark.parametrize("old_field", ["schema_version", "policy"])
def test_pre_finalization_policy_manifest_cannot_resume_current_run(tmp_path, old_field):
    from dvp.cli.evaluate import build_parser
    from dvp.orchestration.manifest import build_run_manifest, validate_resume_manifest

    dataset = tmp_path / "tasks.jsonl"
    dataset.write_text(json.dumps({"task_id": "replay", "theorem_statement": THEOREM}) + "\n")
    args = build_parser().parse_args([
        "--model", "mock", "--lean-retrieve", "false", "--python-execute", "false",
    ])
    args.target_workspace = None
    current = build_run_manifest(args, dataset=dataset, run_root=tmp_path / "run")
    old = deepcopy(current)
    if old_field == "schema_version":
        old[old_field] = "dvp.episode.v11"
    else:
        old[old_field]["version"] = "dvp.policy.comparator.v3"
    with pytest.raises(ValueError, match=old_field):
        validate_resume_manifest(old, current)


if __name__ == "__main__":
    if len(sys.argv) not in (3, 5) or sys.argv[1] != "--replay":
        raise SystemExit("usage: test_agent_replay.py --replay <scenario> [--config-defaults JSON]")
    defaults = None
    if len(sys.argv) == 5:
        if sys.argv[3] != "--config-defaults":
            raise SystemExit("expected --config-defaults JSON")
        defaults = json.loads(sys.argv[4])
    print(json.dumps(_run_replay(sys.argv[2], config_defaults=defaults), ensure_ascii=False))
