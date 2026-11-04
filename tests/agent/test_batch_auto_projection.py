"""Every automatic batch check keeps its own bounded model-visible evidence."""

from copy import deepcopy
import json

import pytest

from dvp.agent.result_projection import _compact


def serialized(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def check(path, *, success=False, error="unsolved goals\n⊢ True", **fields):
    return {"file_path": path, "verification": {
        "file_path": path, "success": success, "verification_profile": "strict",
        "errors": [] if success else [f"{path}:1:1: error: {error}"], **fields,
    }}


def batch(entries, *, primary=0):
    result = {
        "success": all(item["verification"]["success"] for item in entries),
        "file_saved": True, "edits_applied": len(entries), "lean_executed": True,
        "files": [{"file_path": item["file_path"], "bytes": 40} for item in entries],
        "verification_count": len(entries), "verifications": entries,
    }
    if entries:
        result.update(entries[primary])
        result["verification_profile"] = "strict"
    return result


def test_all_checks_are_filtered_without_mutating_raw_or_duplicating_primary():
    hidden = {
        "strict_verified": True, "draft_typechecked": False, "score": 1,
        "source_bundle_sha256": "hidden-bundle", "source_bundle_manifest": {"secret": True},
        "fixed_task_contract": {"secret": True}, "_harness_infrastructure_error": True,
        "command": ["lean", "/private/runtime"],
        "contract_verification": {"success": False, "candidate_sha256": "hidden-candidate"},
    }
    payload = batch([
        check("Submission.lean", success=True, **hidden),
        check("Submission/Helper.lean", **hidden),
    ], primary=1)
    payload.update(proof_code="private checked source snapshot", automatic_check_call_ids=["edit0", "edit1"])
    before = deepcopy(payload)

    result = _compact(payload, tool_name="file_edit")

    assert payload == before
    assert "proof_code" not in result and "automatic_check_call_ids" not in result
    assert "verification" not in result
    assert result["file_saved"] is True and result["success"] is False
    assert result["verification_count"] == result["verifications_shown"] == 2
    assert result["check_kind"] == "compilation"
    assert result["verifications_omitted"] == 0
    assert [item["file_path"] for item in result["verifications"]] == [
        "Submission.lean", "Submission/Helper.lean",
    ]
    for item in result["verifications"]:
        assert "verification_profile" not in item and "check_kind" not in item
        for key in hidden:
            if key != "contract_verification":
                assert key not in item["verification"]
        assert item["verification"]["contract_verification"] == {"success": False}
    text = serialized(result)
    assert text.count("unsolved goals") == 1
    assert "hidden-bundle" not in text and "hidden-candidate" not in text


def test_each_file_diagnostic_path_shortens_without_rewriting_goals_or_source():
    root = "/machine/runs/task/workspaces"
    quoted = f'"{root}/Submission/B.lean"'
    payload = batch([
        check(root + "/Submission/A.lean", error=f"unsolved goals\n⊢ {quoted} = {quoted}"),
        check(root + "/Submission/B.lean", source_code=quoted),
    ])
    payload["primary_diagnostic"] = {
        "file": root + "/Submission/A.lean", "primary_goal": quoted,
    }

    result = _compact(payload, tool_name="file_edit", path_aliases={root: ""})

    first, second = result["verifications"]
    assert first["file_path"] == "Submission/A.lean"
    assert first["verification"]["errors"][0].startswith("Submission/A.lean:1:1: error:")
    assert quoted in first["verification"]["errors"][0]
    assert first["primary_diagnostic"] == {"file": "Submission/A.lean", "primary_goal": quoted}
    assert second["file_path"] == "Submission/B.lean"
    assert second["verification"]["source_code"] == quoted


def test_secondary_diagnostics_survive_large_console_output():
    entries = [check(f"Submission/H{index}.lean", error=f"independent failure {index}",
                     output="compiler preamble\n" * 1000) for index in range(3)]
    payload = batch(entries)

    result = _compact(payload, tool_name="file_edit", max_chars=1800)

    assert len(serialized(result)) <= 1800
    assert result["verifications_shown"] == 3 and result["verifications_omitted"] == 0
    assert result["result_truncated"] is True
    for index, item in enumerate(result["verifications"]):
        assert item["file_path"] == f"Submission/H{index}.lean"
        assert f"independent failure {index}" in serialized(item)


@pytest.mark.parametrize("budget", [512, 900, 1800])
def test_small_budgets_keep_primary_failure_and_count_omitted_checks(budget):
    entries = [check(f"Submission/H{index}.lean", success=index != 17,
                     error="unique failure " + "context " * 1000) for index in range(30)]
    payload = batch(entries, primary=17)
    before = deepcopy(payload)

    result = _compact(payload, tool_name="file_edit", max_chars=budget)

    assert payload == before
    assert len(serialized(result)) <= budget
    assert result["success"] is False and result["file_saved"] is True
    assert result["verification_count"] == 30
    assert result["verifications_shown"] == len(result["verifications"])
    assert result["verifications_shown"] + result["verifications_omitted"] == 30
    assert result["verifications_omitted"] > 0
    primary = result["verifications"][0]
    assert primary["file_path"] == "Submission/H17.lean"
    assert result["check_kind"] == "compilation"
    assert serialized(result).count('"check_kind"') == 1
    assert "verification_profile" not in serialized(result)
    assert primary["verification"]["success"] is False
    assert primary["verification"]["errors"]
    if budget > 512:
        assert "unique failure" in serialized(primary)
    else:
        assert primary["verification"]["errors_truncated"] is True


def test_nonlean_batch_has_no_invented_check():
    payload = batch([])
    payload["files"] = [{"file_path": "Submission/notes.txt", "bytes": 9}]
    result = _compact(payload, tool_name="file_edit")
    assert result["file_saved"] and result["success"]
    assert result["verifications"] == []
    assert "check_kind" not in result
    assert result["verification_count"] == result["verifications_shown"] == result["verifications_omitted"] == 0


def test_existing_single_check_and_manual_projection_shapes_are_unchanged():
    manual = {"success": True, "files": [{"file_path": "Submission/A.lean", "bytes": 10}],
              "edits_applied": 1, "lean_executed": False, "stop": False}
    automatic = {"success": False, "file_path": "Submission.lean", "file_saved": True,
                 "verification_profile": "strict", "verification": {
                     "success": False, "verification_profile": "strict", "strict_verified": False,
                     "errors": ["unsolved goals"], "command": ["lean"]}, "stop": False}
    assert _compact(manual, tool_name="file_edit") == {key: value for key, value in manual.items() if key != "stop"}
    assert _compact(automatic, tool_name="file_edit") == {
        "success": False, "file_path": "Submission.lean", "file_saved": True,
        "check_kind": "compilation", "verification": {
            "success": False, "errors": ["unsolved goals"]},
    }
