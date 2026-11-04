"""Exercise saved helper reuse through the real tool and final acceptance loop."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dvp.agent.driver import ProverDriver
from dvp.agent.episode import ProverEpisode
from dvp.config import BudgetConfig, EpisodeConfig
from dvp.factory import build_services
from test_fixed_task_contract import contract_for
from test_lean435_local_pipeline import WORKSPACE, local_runtime


@pytest.mark.parametrize("fixed_context", [False, True])
def test_saved_module_reaches_trusted_completion(tmp_path, local_runtime, fixed_context):
    statement = "import Mathlib\ndef fixedValue : Nat := 2\ntheorem target : fixedValue = 2 := by sorry"
    helper = "import Mathlib\ntheorem module_identity (n : Nat) : n = n := rfl\n"
    code = statement.replace("import Mathlib", "import Mathlib\nimport Submission.Helpers").replace("sorry", "exact module_identity 2")

    def write(path, content):
        return {"role": "assistant", "content": "", "tool_calls": [{
            "id": path, "type": "function", "function": {
                "name": "file_write", "arguments": json.dumps({"file_path": path, "content": content}),
            },
        }]}

    services = build_services(
        theorem_statement=statement,
        runtime=SimpleNamespace(target_workspace=WORKSPACE, lean_timeout=300),
        run_dir=tmp_path / "run", task_contract=contract_for(statement) if fixed_context else None,
    )
    client = SimpleNamespace(chat=AsyncMock(side_effect=[
        write("Submission/Helpers.lean", helper), write("Submission.lean", code),
        {"role": "assistant", "content": "The saved proof is ready."},
        AssertionError("model called after acceptance"),
    ]))
    episode = ProverEpisode(
        tools=services.tools, contract_verifier=services.contract_verifier,
        theorem_statement=statement, run_dir=services.workspace.root,
        config=EpisodeConfig(budget=BudgetConfig(max_turns=4)),
    )
    result = asyncio.run(ProverDriver(client, episode, statement, verbose=False).run())
    assert result.success, result.to_dict()
    assert result.reason == "verified_exact_target"
    assert result.lean_calls == 3
    assert result.rollout_reason == "model_finished"
    assert client.chat.await_count == 3
    evidence = result.final_tool_result["contract_verification"]
    manifest = evidence["source_bundle_manifest"]
    assert manifest["modules"][0]["path"] == "Submission/Helpers.lean"
    assert evidence["source_bundle_sha256"] == manifest["sha256"]


def test_final_grading_uses_helper_overwritten_after_successful_development_check(tmp_path, local_runtime):
    statement = "import Mathlib\ntheorem target : True := by sorry\n"
    helper = "theorem helper : True := by trivial\n"
    forged_helper = "axiom helper : True\n"
    code = "import Mathlib\nimport Submission.Helpers\ntheorem target : True := helper\n"
    services = build_services(
        theorem_statement=statement,
        runtime=SimpleNamespace(target_workspace=WORKSPACE, lean_timeout=300),
        run_dir=tmp_path / "changed-helper",
    )
    original_verify = services.contract_verifier.verify
    services.contract_verifier.verify = AsyncMock(wraps=original_verify)

    def call(name, arguments, identifier):
        return {"id": identifier, "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments),
        }}

    message = {"role": "assistant", "content": "", "tool_calls": [
        call("file_write", {"file_path": "Submission/Helpers.lean", "content": helper}, "helper"),
        call("file_write", {"content": code}, "entry"),
        call("lean_verify", {"file_path": "Submission.lean"}, "check"),
        call("file_write", {"file_path": "Submission/Helpers.lean", "content": forged_helper}, "overwrite"),
    ]}
    client = SimpleNamespace(chat=AsyncMock(side_effect=[
        message, {"role": "assistant", "content": "Finished."},
        AssertionError("model called after final response"),
    ]))
    episode = ProverEpisode(
        tools=services.tools, contract_verifier=services.contract_verifier,
        theorem_statement=statement, run_dir=services.workspace.root,
        config=EpisodeConfig(budget=BudgetConfig(max_turns=3)),
    )
    result = asyncio.run(ProverDriver(client, episode, statement, verbose=False).run())
    assert not result.success and result.reason == "comparator_rejected", result.to_dict()
    assert result.rollout_reason == "model_finished"
    assert result.tool_calls == 4 and result.lean_calls == 5
    assert client.chat.await_count == 2
    services.contract_verifier.verify.assert_awaited_once()
    submitted = services.contract_verifier.verify.await_args.kwargs["bundle"]
    assert submitted.entry_code == code
    assert submitted.modules[0].source == forged_helper
    evidence = result.final_tool_result["contract_verification"]
    assert evidence["source_bundle_sha256"] == submitted.sha256
