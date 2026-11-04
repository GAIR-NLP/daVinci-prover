"""Style lint is quiet in chat while proof diagnostics and raw evidence survive."""

from copy import deepcopy
import json

import pytest

from dvp.agent.result_projection import _compact


def lint(name="unusedVariables", body="Variable name `h` is not explicitly referenced."):
    return (f"Submission/Helper.lean:3:2: warning: {body}\n\n"
            f"Note: This linter can be disabled with `set_option linter.{name} false`")


def render(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@pytest.mark.parametrize("mode", ["lean_verify", "file_write", "batch_auto"])
def test_all_verification_entrypoints_filter_lint_without_losing_errors_or_holes(mode):
    warning = lint()
    important = "Submission.lean:8:0: warning: declaration uses `sorry`"
    error = "Submission.lean:9:2: error: unsolved goals\nx : Nat\n⊢ x = 0"
    holes = {"total": 1, "items": [{"line": 8, "goal": "⊢ True"}]}
    verification = {"success": False, "errors": [error], "warnings": [warning, important],
                    "holes": holes, "output": warning + "\n" + error + "\n" + important}
    if mode == "lean_verify":
        payload = verification
    else:
        payload = {"success": True, "file_path": "Submission.lean", "verification": verification}
        if mode == "batch_auto":
            payload["verifications"] = [
                {"file_path": "Submission.lean", "verification": verification},
                {"file_path": "Submission/Helper.lean", "verification": {
                    "success": True, "errors": [], "warnings": [warning], "stdout": warning}},
            ]
    before = deepcopy(payload)
    tool = "file_edit" if mode == "batch_auto" else mode
    result = _compact(payload, tool_name=tool)
    if mode == "batch_auto":
        evidence = result["verifications"][0]["verification"]
        assert result["verifications"][1]["verification"]["linter_warnings_omitted"] == 1
    else:
        evidence = result if mode == "lean_verify" else result["verification"]
    assert evidence["errors"] == [error]
    assert evidence["holes"] == holes
    assert evidence["warnings"] == [important]
    assert evidence["linter_warnings_omitted"] == 1
    assert "unusedVariables" not in render(result)
    assert payload == before
    assert _compact(result, tool_name=tool) == result


@pytest.mark.parametrize("name", ["unnecessarySimpa", "unusedSimpArgs", "unusedVariables",
                                  "unusedSectionVars", "unusedTactic", "style.haveILetI"])
def test_only_explicitly_identified_allowlisted_lint_is_omitted(name):
    payload = {"success": True, "errors": [], "warnings": [lint(name)]}
    result = _compact(payload, tool_name="lean_verify")
    assert result == {"success": True, "errors": [], "linter_warnings_omitted": 1,
                      "check_kind": "compilation"}


def test_unknown_or_correctness_warnings_and_every_error_are_preserved():
    warnings = [
        "Submission.lean:1:0: warning: unused variable",  # No explicit linter identity.
        lint("unrecognizedLinter"),
        lint(body="declaration uses `sorry`"), lint(body="declaration uses admit"),
        lint(body="unsafe declaration"), lint(body="unexpected axiom"),
        lint(body="native_decide used"),
        {"severity": "error", "data": lint()},
    ]
    payload = {"success": False, "warnings": warnings, "errors": [lint(), "unknown error"],
               "holes": {"total": 2, "items": [{"goal": "⊢ False"}]}}
    result = _compact(payload, tool_name="lean_verify")
    assert result == {**payload, "check_kind": "compilation"}


def test_duplicate_warnings_are_counted_once_per_check_without_cross_call_state():
    important = "Submission.lean:5:0: warning: declaration uses `sorry`"
    payload = {"success": True, "warnings": [lint(), lint(), important, important],
               "stdout": lint() + "\n" + important, "stderr": important}
    before = deepcopy(payload)
    result = _compact(payload, tool_name="lean_verify")
    assert result == {"success": True, "warnings": [important],
                      "linter_warnings_omitted": 1, "duplicate_warnings_omitted": 2,
                      "check_kind": "compilation"}
    assert _compact(payload, tool_name="lean_verify") == result
    assert payload == before


def test_structured_warning_console_copies_do_not_restore_suppressed_lint():
    text = lint().split(": warning: ", 1)[1]
    warning = {"file": "Submission/Helper.lean", "pos": {"line": 3, "column": 2},
               "severity": "warning", "data": text}
    payload = {"success": True, "warnings": [warning], "output": lint()}
    result = _compact(payload, tool_name="lean_verify")
    assert result == {"success": True, "linter_warnings_omitted": 1,
                      "check_kind": "compilation"}


def test_structured_linter_code_and_formatted_console_copy():
    warning = {"file": "Submission.lean", "pos": {"line": 2, "column": 3},
               "severity": "warning", "code": "linter.unusedVariables", "data": "Unused h"}
    payload = {"success": True, "warnings": [warning],
               "stdout": "Submission.lean:2:3: warning(linter.unusedVariables): Unused h"}
    assert _compact(payload, tool_name="lean_verify") == {
        "success": True, "linter_warnings_omitted": 1, "check_kind": "compilation",
    }


def test_mentions_in_errors_unknown_warnings_and_quoted_console_are_not_lint():
    error = "Submission.lean:1:0: error: unknown option linter.unusedVariables"
    unknown = "Submission.lean:2:0: warning: Cannot load warning(linter.unusedVariables): metadata"
    quoted = 'Printed diagnostic: "' + lint() + '"'
    payload = {"success": False, "errors": [error], "warnings": [unknown], "output": quoted}
    assert _compact(payload, tool_name="lean_verify") == {**payload, "check_kind": "compilation"}


def test_mixed_diagnostic_warning_entry_and_mislabeled_error_are_preserved():
    mixed = lint() + "\nSubmission.lean:5:0: error: missing theorem"
    mislabeled = lint().replace(": warning:", ": error:", 1)
    payload = {"success": False, "warnings": [mixed, mislabeled]}
    assert _compact(payload, tool_name="lean_verify") == {**payload, "check_kind": "compilation"}


def test_console_only_lint_has_a_complete_boundary_and_keeps_probe_output():
    important = "Submission.lean:9:2: warning: declaration uses `sorry`"
    error = "Submission.lean:10:2: error: unsolved goals\n⊢ False"
    output = "\n".join(["Nat : Type", lint(), "Bool : Type", important, error])
    payload = {"success": False, "output": output, "stdout": lint(), "errors": [error]}
    result = _compact(payload, tool_name="lean_verify")
    assert result["errors"] == [error]
    assert result["linter_warnings_omitted"] == 1
    assert "unusedVariables" not in render(result)
    assert all(s in result["output"] for s in ["Nat : Type", "Bool : Type", important])
    assert not result.get("stdout")


@pytest.mark.parametrize("budget", [512, 1200])
def test_omission_counts_survive_bounded_feedback_alongside_errors(budget):
    payload = {"success": False, "errors": ["Submission.lean:1:0: error: unknown identifier `foo`"],
               "warnings": [lint()], "output": "Nat : Type\n" * 4000}
    result = _compact(payload, tool_name="lean_verify", max_chars=budget)
    assert result["errors"] == payload["errors"]
    assert result["linter_warnings_omitted"] == 1
    assert len(render(result)) <= budget


@pytest.mark.parametrize("tool", ["file_read", "content_search"])
def test_source_and_search_text_are_never_treated_as_verifier_warnings(tool):
    payload = {"success": True, "content": lint(), "warnings": [lint()]}
    assert _compact(payload, tool_name=tool) == payload
