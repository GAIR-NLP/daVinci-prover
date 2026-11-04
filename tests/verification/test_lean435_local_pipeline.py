"""Real Lean/Comparator integration; only the model replies are predetermined."""
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dvp.agent.driver import ProverDriver
from dvp.agent.episode import ProverEpisode
from dvp.config import BudgetConfig, EpisodeConfig
from dvp.factory import build_services
from dvp.verification.comparator import preflight_runtime
from dvp.verification.lean import LeanVerifier


REPO = Path(__file__).resolve().parents[2]
WORKSPACE = REPO / "runtime/mathlib4-4.35"
LOCAL_RUNTIME = REPO / "runtime/comparator-no-sandbox"
STATEMENT = "import Mathlib\ntheorem target : 1 + 1 = 2 := by sorry\n"


@pytest.fixture
def local_runtime(monkeypatch):
    if not (LOCAL_RUNTIME / "4.35.0/runtime.json").is_file() or not WORKSPACE.is_dir():
        pytest.skip("installed Lean 4.35 local Comparator runtime is unavailable")
    monkeypatch.setenv("DVP_COMPARATOR_ROOT", str(LOCAL_RUNTIME))
    runtime = asyncio.run(preflight_runtime(WORKSPACE))
    assert runtime["execution_mode"] == "unsandboxed"
    monkeypatch.setenv("PATH", runtime["toolchain"] + "/bin:" + os.environ["PATH"])
    return runtime


def response(code):
    calls = [{
        "id": "candidate", "type": "function", "function": {
            "name": "file_write", "arguments": json.dumps({
                "file_path": "Submission.lean", "content": code,
            }),
        },
    }]
    calls.append({
        "id": "check", "type": "function", "function": {
            "name": "lean_verify", "arguments": json.dumps({"file_path": "Submission.lean"}),
        },
    })
    calls.append({
        "id": "after", "type": "function", "function": {
            "name": "file_write", "arguments": json.dumps({
                "file_path": "Submission/after.txt", "content": "executed after compilation",
            }),
        },
    })
    return {"role": "assistant", "content": "", "tool_calls": calls}


@pytest.mark.parametrize("case", ["accepted", "repair", "early_finish", "rejected", "temporary_axiom", "overwritten"])
def test_real_local_pipeline_grades_only_after_rollout_ends(tmp_path, local_runtime, case):
    candidate = STATEMENT.replace("sorry", "norm_num")
    if case in {"rejected", "overwritten"}:
        candidate = STATEMENT.replace(
            "theorem target", "variable (extra : False)\ninclude extra\ntheorem target",
        ).replace("sorry", "exact False.elim extra")
    messages = []
    if case == "repair":
        messages.append(response(STATEMENT.replace("sorry", "exact True.intro")))
    elif case == "temporary_axiom":
        messages.append(response(STATEMENT.replace(
            "theorem target", "axiom assumption : 1 + 1 = 2\ntheorem target",
        ).replace("sorry", "exact assumption")))
    elif case == "overwritten":
        messages.append(response(STATEMENT.replace("sorry", "norm_num")))
    if case != "early_finish":
        messages.append(response(candidate))
    messages.append({"role": "assistant", "content": "The saved proof is ready."})
    client = SimpleNamespace(chat=AsyncMock(side_effect=[
        *messages, AssertionError("model called after final acceptance"),
    ]))
    services = build_services(
        theorem_statement=STATEMENT,
        runtime=SimpleNamespace(target_workspace=WORKSPACE, lean_timeout=300),
        run_dir=tmp_path / case,
    )
    original_verify = services.contract_verifier.verify
    services.contract_verifier.verify = AsyncMock(wraps=original_verify)
    episode = ProverEpisode(
        tools=services.tools, contract_verifier=services.contract_verifier,
        theorem_statement=STATEMENT, run_dir=services.workspace.root,
        config=EpisodeConfig(budget=BudgetConfig(max_turns=5)),
    )
    result = asyncio.run(ProverDriver(client, episode, STATEMENT, verbose=False).run())
    assert result.terminated and not result.aborted, result.to_dict()
    expected_success = case not in {"rejected", "overwritten", "early_finish"}
    writes = 0 if case == "early_finish" else 2 if case in {"repair", "temporary_axiom", "overwritten"} else 1
    assert result.success is expected_success, result.to_dict()
    assert result.reason == ("verified_exact_target" if expected_success else "comparator_rejected")
    assert result.rollout_reason == "model_finished"
    assert client.chat.await_count == writes + 1
    assert result.lean_calls == writes * 2 + 1
    assert result.tool_calls == writes * 3
    if writes:
        assert (services.workspace.submission_dir / "after.txt").read_text() == "executed after compilation"
    services.contract_verifier.verify.assert_awaited_once()
    assert services.contract_verifier.verify.await_args.args == (STATEMENT if case == "early_finish" else candidate,)
    evidence = result.final_tool_result["contract_verification"]
    assert evidence["comparator"]["runtime"]["execution_mode"] == "unsandboxed"
    assert "--inadvisably-no-sandbox" in evidence["command"]
    assert evidence["success"] is expected_success


@pytest.mark.parametrize("valid_proof", [True, False], ids=["proved-helper", "added-axiom"])
def test_real_grouped_edits_check_final_sources_and_require_comparator(
    tmp_path, local_runtime, valid_proof,
):
    services = build_services(
        theorem_statement=STATEMENT,
        runtime=SimpleNamespace(target_workspace=WORKSPACE, lean_timeout=300),
        run_dir=tmp_path / "grouped",
    )
    helper_draft = "import Mathlib\ntheorem groupedHelper : 1 + 1 = 2 := by sorry\n"
    helper_final = (
        helper_draft.replace("sorry", "norm_num") if valid_proof
        else "import Mathlib\naxiom groupedHelper : 1 + 1 = 2\n"
    )
    candidate = STATEMENT.replace("import Mathlib", "import Mathlib\nimport Submission.Helper")
    candidate = candidate.replace("sorry", "exact groupedHelper")
    saved_sources = []
    verify_file = services.verifier.verify_file

    async def check_final_source(path, **kwargs):
        assert services.workspace.submission_file.read_text() == candidate
        assert (services.workspace.submission_dir / "Helper.lean").read_text() == helper_final
        saved_sources.append(path.name)
        return await verify_file(path, **kwargs)

    services.verifier.verify_file = AsyncMock(side_effect=check_final_source)
    services.contract_verifier.verify = AsyncMock(wraps=services.contract_verifier.verify)
    edits = [
        ("file_write", {"file_path": "Submission.lean", "content": candidate.replace("groupedHelper", "missingHelper")}),
        ("file_write", {"file_path": "Submission/Helper.lean", "content": helper_draft}),
        ("file_edit", {"file_path": "Submission/Helper.lean", "old_str": helper_draft, "new_str": helper_final}),
        ("file_edit", {"file_path": "Submission.lean", "old_str": "missingHelper", "new_str": "groupedHelper"}),
    ]
    client = SimpleNamespace(chat=AsyncMock(side_effect=[
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"edit{i}", "type": "function", "function": {
                "name": name, "arguments": json.dumps(args),
            }} for i, (name, args) in enumerate(edits)
        ]},
        {"role": "assistant", "content": "Finished."},
    ]))
    episode = ProverEpisode(
        tools=services.tools, contract_verifier=services.contract_verifier,
        theorem_statement=STATEMENT, run_dir=services.workspace.root,
        config=EpisodeConfig(),
    )
    driver = ProverDriver(client, episode, STATEMENT, verbose=False)
    result = asyncio.run(driver.run())

    assert saved_sources == ["Submission.lean", "Helper.lean"]
    assert result.tool_calls == 4 and result.lean_calls == 3
    assert result.rollout_reason == "model_finished"
    assert result.success is valid_proof, result.to_dict()
    assert result.reason == ("verified_exact_target" if valid_proof else "comparator_rejected")
    services.contract_verifier.verify.assert_awaited_once()
    assert services.contract_verifier.verify.await_args.args == (candidate,)
    results = [json.loads(message["content"]) for message in driver.messages if message["role"] == "tool"]
    assert len(results) == 4
    assert sum("verifications" in item for item in results) == 1
    assert results[-1]["verification_count"] == 2
    assert all("proof_code" not in item for item in results)


def test_real_local_draft_feedback_preserves_holes(tmp_path, local_runtime):
    services = build_services(
        theorem_statement=STATEMENT,
        runtime=SimpleNamespace(target_workspace=WORKSPACE, lean_timeout=300),
        run_dir=tmp_path / "draft",
    )
    draft = LeanVerifier(replace(services.verifier.config, allow_sorry=True))
    result = asyncio.run(draft.verify_code(STATEMENT))
    assert result["success"] and result["warnings"], result
    compiled = asyncio.run(services.verifier.verify_code(STATEMENT))
    assert compiled["success"] and compiled["warnings"], compiled


@pytest.mark.parametrize("case", ["valid", "added_axiom", "sorry", "changed_statement"])
def test_saved_target_axiom_inspection_never_replaces_final_acceptance(tmp_path, local_runtime, case):
    statement = "import Mathlib\nnamespace Result\ntheorem target : True := by sorry\nend Result\n"
    helper = {
        "added_axiom": "import Mathlib\naxiom Aux.support : True\n",
        "sorry": "import Mathlib\ntheorem Aux.support : True := by sorry\n",
    }.get(case, "import Mathlib\ntheorem Aux.support : True := by trivial\n")
    candidate = statement.replace("import Mathlib", "import Mathlib\nimport Submission.Helpers")
    candidate = candidate.replace("sorry", "exact Aux.support")
    if case == "changed_statement":
        candidate = candidate.replace("target : True", "target : 1 = 1").replace("exact Aux.support", "rfl")
    probe = "import Submission\n#print axioms Result.target\n"
    services = build_services(
        theorem_statement=statement,
        runtime=SimpleNamespace(target_workspace=WORKSPACE, lean_timeout=300),
        run_dir=tmp_path / case,
    )
    services.contract_verifier.verify = AsyncMock(wraps=services.contract_verifier.verify)
    calls = [
        ("file_write", {"file_path": "Submission/Helpers.lean", "content": helper}),
        ("file_write", {"file_path": "Submission.lean", "content": candidate}),
        ("lean_verify", {"code": probe}),
    ]
    requests = 0
    observed = {}
    probe_handler = services.tools._tools["lean_verify"].handler

    async def inspect_dependencies(arguments):
        observed["target_attempts_before_probe"] = episode.observe().state["target_attempts"]
        return await probe_handler(arguments)

    services.tools._tools["lean_verify"].handler = inspect_dependencies

    async def chat(*args, **kwargs):
        nonlocal requests
        requests += 1
        if requests == 1:
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"call{i}", "type": "function", "function": {
                    "name": name, "arguments": json.dumps(arguments),
                }} for i, (name, arguments) in enumerate(calls)
            ]}
        assert requests == 2
        services.contract_verifier.verify.assert_not_awaited()
        assert not episode.result().success
        assert not episode._state.exact_target_verified and not episode._state.submission_ready
        assert episode.observe().state["target_attempts"] == observed["target_attempts_before_probe"]
        observed["raw"] = episode._state.last_outcomes[-1].raw
        return {"role": "assistant", "content": "Finished."}

    episode = ProverEpisode(
        tools=services.tools, contract_verifier=services.contract_verifier,
        theorem_statement=statement, run_dir=services.workspace.root,
        config=EpisodeConfig(),
    )
    driver = ProverDriver(SimpleNamespace(chat=AsyncMock(side_effect=chat)), episode, statement, verbose=False)
    result = asyncio.run(driver.run())
    raw = observed["raw"]
    assert raw["success"], raw
    manifest = raw["source_bundle_manifest"]
    assert manifest["entry_sha256"] == hashlib.sha256(probe.encode()).hexdigest()
    assert manifest["allow_entrypoint_import"] is True
    modules = {item["name"]: item for item in manifest["modules"]}
    assert set(modules) == {"Submission", "Submission.Helpers"}
    assert modules["Submission"]["sha256"] == hashlib.sha256(candidate.encode()).hexdigest()
    feedback = json.loads([m for m in driver.messages if m["role"] == "tool"][-1]["content"])
    assert feedback["success"] and feedback["check_kind"] == "compilation"
    rendered = json.dumps(feedback)
    assert "verification_profile" not in rendered and "strict_verified" not in rendered
    assert "Result.target" in rendered
    if case == "added_axiom":
        assert "Aux.support" in rendered
    elif case == "sorry":
        assert "sorryAx" in rendered
        assert feedback["dependency_placeholders"]["total"] == 1
    else:
        assert "does not depend on any axioms" in rendered
    assert result.success is (case == "valid"), result.to_dict()
    assert result.rollout_reason == "model_finished"
    assert result.tool_calls == 3 and result.lean_calls == 4
    services.contract_verifier.verify.assert_awaited_once()
    assert services.contract_verifier.verify.await_args.args == (candidate,)
    final_bundle = services.contract_verifier.verify.await_args.kwargs["bundle"]
    assert not final_bundle.allow_entrypoint_import
    assert [module.name for module in final_bundle.modules] == ["Submission.Helpers"]


def test_elaborated_sorry_warning_keeps_a_compiled_candidate_unfinished(tmp_path, local_runtime):
    services = build_services(
        theorem_statement="theorem target : True := by sorry\n",
        runtime=SimpleNamespace(target_workspace=WORKSPACE, lean_timeout=120),
        run_dir=tmp_path / "implicit-hole",
    )
    result = asyncio.run(services.tools.lean_verify({
        "code": "theorem target : True := @sorryAx True false\n",
    }))
    assert result["success"] and result["lean_executed"], result
    assert result["source_placeholders"]["total"] == 0
    assert result["warnings"], result
    assert result["verification_profile"] == "draft_allow_sorry"
    assert not result["strict_verified"]
