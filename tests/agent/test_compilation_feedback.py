"""Compilation feedback states its scope without implying an axiom audit."""

from copy import deepcopy

import pytest

from dvp.agent.result_projection import _compact, _json


def wrap(verification, mode):
    if mode == "lean_verify":
        return verification
    result = {"success": True, "file_saved": True, "file_path": "Submission.lean",
              "verification_profile": verification.get("verification_profile", "strict"),
              "verification": verification}
    if mode == "batch":
        result["verifications"] = [
            {"file_path": "Submission.lean", "verification": verification},
        ]
    return result


def project(payload, mode, budget=12000):
    return _compact(payload, tool_name="lean_verify" if mode == "lean_verify" else "file_write",
                    max_chars=budget)


@pytest.mark.parametrize("mode", ["lean_verify", "single", "batch"])
@pytest.mark.parametrize("profile", ["strict", "draft_allow_sorry"])
@pytest.mark.parametrize("budget", [512, 12000])
def test_check_kind_replaces_profiles_and_empty_placeholders_without_mutating_raw(mode, profile, budget):
    empty = {"total": 0, "items": [], "omitted": 0}
    payload = wrap({"success": True, "verification_profile": profile,
                    "holes": empty, "source_placeholders": empty,
                    "dependency_placeholders": empty, "errors": [],
                    "output": "compiler filler\n" * 3000}, mode)
    before = deepcopy(payload)
    result = project(payload, mode, budget)
    text = _json(result)
    assert result["check_kind"] == "compilation"
    assert text.count('"check_kind"') == 1
    assert all(key not in text for key in ["verification_profile", "holes", "source_placeholders",
                                           "dependency_placeholders", profile])
    assert payload == before
    assert len(text) <= budget
    assert project(result, mode, budget) == result


@pytest.mark.parametrize("mode", ["lean_verify", "single", "batch"])
def test_zero_entry_summary_cannot_erase_positive_nested_or_imported_goals(mode):
    holes = {"total": 1, "items": [{"goal": "n : Nat\n⊢ n = 0"}], "omitted": 0}
    deps = [{"module": "Submission.Empty", "total": 0},
            {"module": "Submission.Helper", "total": 2}]
    payload = wrap({"success": True, "holes": holes, "dependency_placeholders": deps,
                    "source_placeholders": {"total": 0, "items": []}}, mode)
    if mode != "lean_verify":
        payload["holes"] = {"total": 0, "items": []}
    result = project(payload, mode)
    evidence = result if mode == "lean_verify" else (
        result["verification"] if mode == "single" else result["verifications"][0]["verification"]
    )
    assert evidence["holes"] == holes
    assert evidence["dependency_placeholders"] == [deps[1]]
    assert "source_placeholders" not in evidence


REPORTS = [
    "'allowed' depends on axioms: [propext, Classical.choice, Quot.sound]",
    "'added' depends on axioms: [Submission.bad, sorryAx]",
    "'closed' does not depend on any axioms",
]


@pytest.mark.parametrize("mode", ["lean_verify", "single", "batch"])
def test_ordinary_axiom_output_is_preserved_once_as_natural_compiler_output(mode):
    output = "\n".join(REPORTS)
    payload = wrap({"success": True, "errors": [], "output": output, "stdout": output}, mode)
    before = deepcopy(payload)
    result = project(payload, mode)
    text = _json(result)
    assert "axiom_reports" not in text
    for report in REPORTS:
        assert text.count(report) == 1
    assert payload == before


@pytest.mark.parametrize("mode", ["lean_verify", "single", "batch"])
@pytest.mark.parametrize("budget", [512, 12000])
def test_axiom_reports_in_middle_of_oversized_console_survive_compaction(mode, budget):
    output = "compiler filler\n" * 3000 + "\n".join(REPORTS) + "\n" + "more filler\n" * 3000
    payload = wrap({"success": True, "errors": [], "output": output}, mode)
    before = deepcopy(payload)
    result = project(payload, mode, budget)
    text = _json(result)
    for report in REPORTS:
        assert text.count(report) == 1
    assert result["check_kind"] == "compilation"
    assert len(text) <= budget
    assert payload == before
    assert project(result, mode, budget) == result


def test_multiline_axiom_report_survives_console_compaction():
    report = "'foo\'' depends on axioms:\n[propext,\n Submission.newAxiom]"
    payload = {"success": True, "output": "filler\n" * 4000 + report + "\nfiller\n" * 4000}
    result = project(payload, "lean_verify", 512)
    assert result["axiom_reports"] == [report]


@pytest.mark.parametrize("prefix", ["4:0: info: ", "Submission.lean:4:0: info: "])
def test_compiler_info_locations_survive_console_compaction(prefix):
    report = prefix + REPORTS[1]
    payload = {"success": True, "output": "filler\n" * 4000 + report}
    result = project(payload, "lean_verify", 512)
    assert result["axiom_reports"] == [report]


@pytest.mark.parametrize("field", ["infos", "response"])
@pytest.mark.parametrize("report_first", [False, True])
def test_structured_axiom_info_survives_when_large_diagnostics_force_reduction(field, report_first):
    report = REPORTS[1]
    messages = [{"severity": "info", "data": "unrelated info " * 3000},
                {"severity": "info", "data": report}]
    if report_first:
        messages.reverse()
    payload = {"success": True, field: messages if field == "infos" else {"messages": messages}}
    result = project(payload, "lean_verify", 512)
    assert _json(result).count(report) == 1
    assert len(_json(result)) <= 512


def test_compacting_console_does_not_duplicate_an_existing_info_report():
    report = REPORTS[0]
    payload = {"success": True, "infos": [report], "output": "filler\n" * 4000 + report}
    result = project(payload, "lean_verify", 512)
    assert result["infos"] == [report]
    assert "axiom_reports" not in result
    assert _json(result).count(report) == 1


def test_axiom_reports_larger_than_budget_have_explicit_omission_counts():
    reports = [f"'t{i}' depends on axioms: [Submission.axiom{i}]" for i in range(30)]
    result = project({"success": True, "output": "\n".join(reports)}, "lean_verify", 512)
    assert result["axiom_reports_total"] == len(reports)
    assert result["axiom_reports_omitted"] + len(result["axiom_reports"]) == len(reports)
    assert result["axiom_reports_omitted"] > 0
    assert len(_json(result)) <= 512


def test_source_read_is_not_reinterpreted_as_compilation_feedback():
    payload = {"success": True, "content": "#print axioms strict\n-- holes = 0"}
    assert _compact(payload, tool_name="file_read") == payload
