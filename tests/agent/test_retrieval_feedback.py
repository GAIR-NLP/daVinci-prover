"""Model-visible retrieval must preserve callable declaration context."""

from copy import deepcopy
import json

import pytest

from dvp.agent.result_projection import _compact, _json


def declaration(index=0, *, size=0):
    return {
        "fullname": f"Sylow.sample_{index}",
        "signature": "theorem sample [Finite G] (h : p ∣ Nat.card G) : True :=",
        "variables": ["{G : Type u}", "[Group G]", "{p : ℕ}"] + [f"(h{i} : True)" for i in range(size)],
        "filename": "target/Mathlib/GroupTheory/Sylow.lean",
        "line_start": 123,
        "line_end": 129,
    }


def project(items, budget=12_000):
    result = _compact({"success": True, "results": items, "count": len(items)},
                      tool_name="lean_retrieve", max_chars=budget)
    assert len(_json(result)) <= budget
    return result


def context_of(result, hit):
    if "context_ref" in hit:
        reference = next(other for other in result["results"]
                         if other["rank"] == hit["context_ref"])
        assert reference["rank"] < hit["rank"]
        assert "context_ref" not in reference
        return reference["context"]
    return hit["context"]


def test_all_outer_binders_and_full_signature_survive_model_feedback():
    item = declaration(size=30)
    item["signature"] += " " + "α" * 4200
    result = project([item])
    assert result["results"] == [item]
    assert not result.get("result_truncated")


def test_more_than_ten_requested_hits_survive_when_they_fit():
    items = [declaration(i) for i in range(18)]
    assert project(items)["results"] == items


def test_budget_omits_whole_hits_instead_of_cutting_binders():
    items = [declaration(i, size=50) for i in range(10)]
    result = project(items, 2500)
    shown = result["results"]
    assert shown and len(shown) < len(items)
    assert result["results_shown"] == len(shown)
    assert result["results_omitted"] == len(items) - len(shown)
    for actual, original in zip(shown, items):
        assert actual["signature"] == original["signature"]
        assert actual["variables"] == original["variables"]


def test_oversized_declaration_has_explicit_omission_and_source_location():
    item = declaration(size=300)
    result = project([item], 512)
    assert result["results"] == []
    assert result["results_omitted"] == 1
    assert result["first_result_location"]["filename"] == item["filename"]
    assert "complete declaration" in result["message"]


def test_requested_proof_source_is_kept_when_it_fits():
    item = declaration()
    item["source_code"] = "theorem sample : True := by trivial"
    assert project([item])["results"][0] == item


def test_large_proof_source_never_replaces_complete_parameters():
    items = [declaration(i, size=20) for i in range(5)]
    for item in items:
        item["source_code"] = "by\n" + "  -- long proof\n" * 1000
        item["semantic"] = "A mathematical description" * 100
    result = project(items, 5000)
    assert result["results"]
    for actual, original in zip(result["results"], items):
        assert actual["variables"] == original["variables"]
        assert actual["signature"] == original["signature"]
        if "source_code" in actual:
            assert actual["source_code_truncated"]
            assert "...[omitted]..." in actual["source_code"]


@pytest.mark.parametrize("budget", [512, 1000, 2500, 12_000])
def test_escaped_unicode_parameters_respect_serialized_budget(budget):
    items = [declaration(i) for i in range(30)]
    for item in items:
        item["variables"] += ['(h : "α\\β" = "α\\β")'] * 10
    result = project(items, budget)
    json.loads(_json(result))
    for actual, original in zip(result["results"], items):
        assert actual["variables"] == original["variables"]


def test_transport_fields_do_not_reappear_with_lossless_feedback():
    item = declaration()
    result = _compact({"success": True, "results": [item],
                       "request_headers": {"authorization": "test-token"}},
                      tool_name="lean_retrieve")
    assert "request_headers" not in result
    assert result["results"][0]["variables"] == item["variables"]


@pytest.mark.parametrize("budget", [512, 12000])
def test_partial_retrieval_retains_its_error_and_truncation_status(budget):
    item = declaration()
    item["source_code"] = "long proof\n" * 2000
    message = "Search timed out; partial results only"
    result = _compact({"success": False, "error": message, "truncated": True, "results": [item]},
                      tool_name="lean_retrieve", max_chars=budget)
    assert result["success"] is False
    assert result["error"] == message
    assert result["truncated"] is True
    assert result["results_shown"] + result["results_omitted"] == 1
    assert len(_json(result)) <= budget


def explore_declaration(index=0):
    return {
        **declaration(index),
        "id": index + 1,
        "module": "Mathlib.GroupTheory.Sylow",
        "source_origin": "lean_explore",
        "source_repository": "https://github.com/leanprover-community/mathlib4",
        "source_revision": "a" * 40,
        "source_link": "https://github.com/leanprover-community/mathlib4/blob/"
        + "a" * 40 + "/Mathlib/GroupTheory/Sylow.lean#L123-L129",
        "context_status": "available",
        "context": {
            "namespace": "Sylow",
            "commands": ["variable {G : Type u} [Group G]", "open scoped Classical"],
        },
        "dependencies": [
            {"name": f"Nat.some_dependency_{i}", "id": 100 + i} for i in range(10)
        ],
        "docstring": "The original declaration documentation. " * 500,
        "semantic": "A generated mathematical explanation. " * 500,
        "source_code": "theorem sample : True := by\n" + "  -- proof step\n" * 1000,
    }


def test_budget_preserves_version_provenance_and_marks_rich_field_omissions():
    items = [explore_declaration(i) for i in range(3)]
    result = project(items, 4000)
    assert result["results"]
    assert result["sources"] == [{
        "repository": items[0]["source_repository"],
        "revision": items[0]["source_revision"],
    }]
    for actual in result["results"]:
        original = items[actual["rank"] - 1]
        for key in ("signature", "variables", "line_start", "line_end"):
            assert actual[key] == original[key]
        assert actual["source_id"] == 0
        assert actual["filename"] == original["filename"]
        assert actual["filename"].removeprefix("target/") == "Mathlib/GroupTheory/Sylow.lean"
        assert "source_path" not in actual
        assert context_of(result, actual)["commands"] == original["context"]["commands"]
        assert context_of(result, actual)["complete"] is False
        assert not {"dependencies", "dependencies_omitted", "docstring", "docstring_omitted"} & actual.keys()
        assert 150 <= len(actual["semantic"]) <= 200
        assert actual["semantic_truncated"] is True
        assert actual["semantic"].endswith("…[truncated]")
        assert len(actual["source_code"]) >= 256
        assert actual["source_code_truncated"] is True


def test_oversized_context_does_not_cut_binders_or_block_later_hits():
    item = explore_declaration()
    item["context"]["commands"] += [f"variable (h{i} : True)" for i in range(500)]
    later = explore_declaration(1)
    result = project([item, later], 1600)
    actual = result["results"][0]
    assert actual["rank"] == 2
    assert actual["fullname"] == later["fullname"]
    assert actual["context"]["commands"] == later["context"]["commands"]
    assert "context_status" not in actual
    assert actual["variables"] == later["variables"]
    assert actual["signature"] == later["signature"]
    assert result["results_omitted"] == 1


def test_default_results_exclude_dependencies_and_their_bookkeeping():
    item = declaration()
    item["dependencies"] = [
        {"id": i, "name": f"Nat.dependency_{i}", "description": "x" * 100}
        for i in range(30)
    ]
    result = project([item], 1600)
    actual = result["results"][0]
    assert not {"dependencies", "dependencies_total", "dependencies_omitted"} & actual.keys()
    assert actual["signature"] == item["signature"]
    assert len(item["dependencies"]) == 30
    assert not result.get("result_truncated")


def test_oversized_source_link_does_not_hide_usable_versioned_declaration():
    item = explore_declaration()
    item["source_link"] += "?detail=" + "x" * 5000
    result = project([item], 1600)
    actual = result["results"][0]
    assert "source_link" not in actual
    assert result["sources"][actual["source_id"]] == {
        "repository": item["source_repository"], "revision": item["source_revision"],
    }
    assert actual["signature"] == item["signature"]


def test_explore_normalizes_small_results_without_mutating_raw_evidence():
    item = explore_declaration()
    item.update(semantic="Every Sylow subgroup has prime power order.", docstring="")
    item.pop("source_code")
    item["context"].update(
        note="Long repeated lexical-context note. " * 30,
        scopes=[{"kind": "namespace", "name": "Sylow"}],
        method="source_scan", complete=False,
    )
    original = deepcopy(item)
    result = project([item])
    actual = result["results"][0]
    assert item == original
    assert actual["semantic"] == item["semantic"]
    assert not result.get("result_truncated")
    assert "incomplete lexical" in result["context_note"]
    assert "elaborated Lean type" in result["context_note"]
    assert not {"id", "name", "module", "source_path", "source_link"} & actual.keys()
    assert actual["filename"] == item["filename"]
    assert "Check local revision" in result["source_note"]
    assert not {"note", "scopes", "method"} & actual["context"].keys()
    assert not {"dependencies", "dependencies_omitted"} & actual.keys()


def test_explore_keeps_one_description_and_preserves_real_errors():
    item = explore_declaration()
    item.pop("source_code")
    item.update(semantic="A complete useful statement.", docstring="A complete useful statement.",
                signature_error=None, source_error="", context_error=None)
    before = deepcopy(item)
    result = project([item])
    actual = result["results"][0]
    assert actual["semantic"] == item["semantic"]
    assert actual["signature"] == item["signature"]
    assert actual["context"]["commands"] == item["context"]["commands"]
    assert not {"docstring", "docstring_omitted", "signature_error", "source_error", "context_error"} & actual.keys()
    assert item == before

    item["docstring"] = "A distinct source note."
    item["source_error"] = "Version-matched source unavailable."
    actual = project([item])["results"][0]
    assert "docstring" not in actual
    assert "docstring_omitted" not in actual
    assert actual["source_error"] == item["source_error"]


@pytest.mark.parametrize("budget", [512, 1000, 1600, 4000, 12_000])
def test_explore_serialized_budget_and_parameter_completeness(budget):
    items = [explore_declaration(i) for i in range(12)]
    for item in items:
        item["variables"] += ['(h : "α\\β" = "α\\β")'] * 4
        item["context"]["commands"] += ["variable {α : Type u} [Group α]"]
    result = project(items, budget)
    json.loads(_json(result))
    assert result["results_shown"] + result["results_omitted"] == len(items)
    for actual in result["results"]:
        original = items[actual["rank"] - 1]
        assert actual["signature"] == original["signature"]
        assert actual["variables"] == original["variables"]
        assert context_of(result, actual)["commands"] == original["context"]["commands"]
        assert 150 <= len(actual["semantic"]) <= 200
        assert actual["source_code_truncated"] is True


def test_explore_keeps_descriptions_short_even_with_spare_budget():
    items = [explore_declaration(i) for i in range(20)]
    for item in items:
        item.pop("source_code")
    result = project(items, 4000)
    assert 1 < len(result["results"]) < len(items)
    for hit in result["results"]:
        assert 150 <= len(hit["semantic"]) <= 200
        assert hit["semantic_truncated"] is True
    small = project(items[:1])
    assert len(_json(small)) < 2000
    assert small["results"][0]["semantic"] == result["results"][0]["semantic"]


def test_explore_skips_oversized_library_note_and_preserves_later_signature():
    note = explore_declaration()
    note["signature"] = "library_note \"" + "documentation " * 5000 + '"'
    ordinary = explore_declaration(1)
    result = project([note, ordinary], 4000)
    assert [hit["fullname"] for hit in result["results"]] == [ordinary["fullname"]]
    assert result["results"][0]["signature"] == ordinary["signature"]
    assert result["results_omitted"] == 1


@pytest.mark.parametrize("status", ["name_mismatch", "unavailable"])
def test_explore_keeps_signature_identity_warnings_and_generator_role(status):
    item = explore_declaration()
    item.update(
        signature_status=status,
        source_declaration="sample",
        signature_error="Source header names sample; use #check Sylow.sample_0 to obtain its type.",
        source_code_role="generator",
    )
    if status != "source_header":
        item["signature"] = None
    actual = project([item], 1800)["results"][0]
    for key in ("signature_status", "signature_error", "source_declaration", "source_code_role", "signature"):
        assert actual[key] == item[key]
    assert "#check" in actual["signature_error"]
    if status == "name_mismatch":
        assert "semantic" not in actual
        assert "docstring" not in actual


def test_explore_source_ids_resolve_each_distinct_revision():
    items = [explore_declaration(i) for i in range(3)]
    items[1]["source_revision"] = "b" * 40
    result = project(items, 12_000)
    assert len(result["sources"]) == 2
    for hit in result["results"]:
        original = items[hit["rank"] - 1]
        assert result["sources"][hit["source_id"]]["revision"] == original["source_revision"]
        assert hit["line_start"] == original["line_start"]


def test_explore_keeps_distinct_local_and_repository_paths():
    item = explore_declaration()
    item["filename"] = "/workspace/custom_checkout/Sylow.lean"
    item["source_path"] = "Mathlib/GroupTheory/Sylow.lean"
    actual = project([item])["results"][0]
    assert actual["filename"] == item["filename"]
    assert actual["source_path"] == item["source_path"]


def test_explore_falls_back_to_docstring_for_natural_language_explanation():
    item = explore_declaration()
    item["semantic"] = ""
    actual = project([item], 1600)["results"][0]
    assert actual["docstring"].startswith("The original declaration documentation.")
    assert actual["docstring_truncated"] is True


def test_explore_context_warning_counts_remain_explicit():
    item = explore_declaration()
    item["context"]["warnings"] = [f"Unreconstructed command-local scope at line {i}." for i in range(5)]
    actual = project([item], 2000)["results"][0]
    assert actual["context"]["warnings"] == item["context"]["warnings"][:1]
    assert actual["context"]["warnings_omitted"] == 4


def test_explore_partial_failure_retains_error_with_bounded_metadata():
    result = _compact(
        {"success": False, "retrieval_backend": "lean_explore", "error": "timeout " * 2000,
         "truncated": True, "results": [explore_declaration()]},
        tool_name="lean_retrieve", max_chars=512,
    )
    assert len(_json(result)) <= 512
    assert result["success"] is False
    assert result["error"].startswith("timeout")
    assert result["error_truncated"] is True
    assert result["truncated"] is True


@pytest.mark.parametrize("field", ["semantic", "docstring", "source_code"])
def test_explore_preserves_upstream_truncation_of_short_text(field):
    item = explore_declaration()
    item.update(semantic="", docstring="", dependencies=[])
    item.pop("source_code")
    item[field] = "Evidence was already shortened by the source."
    item[field + "_truncated"] = True
    result = project([item])
    actual = result["results"][0]
    assert actual[field] == item[field]
    assert actual[field + "_truncated"] is True
    assert result["result_truncated"] is True


@pytest.mark.parametrize("field", ["source_code"])
@pytest.mark.parametrize("upstream_truncated", [False, True])
def test_requested_proof_source_uses_spare_budget_and_original_truncation_marker(field, upstream_truncated):
    item = explore_declaration()
    item.update(semantic="", docstring="", dependencies=[])
    item.pop("source_code")
    item[field] = "Long evidence restored after initial budget allocation. " * 60
    item[field + "_truncated"] = upstream_truncated
    result = project([item])
    actual = result["results"][0]
    assert actual[field] == item[field]
    assert actual[field + "_truncated"] is upstream_truncated
    assert bool(result.get("result_truncated")) is upstream_truncated


@pytest.mark.parametrize("field", ["semantic", "docstring"])
@pytest.mark.parametrize("upstream_truncated", [False, True])
def test_description_never_restores_long_prose_to_fill_spare_budget(field, upstream_truncated):
    item = explore_declaration()
    item.update(semantic="", docstring="", dependencies=[])
    item.pop("source_code")
    item[field] = "A long explanation with otherwise useful mathematical details. " * 60
    item[field + "_truncated"] = upstream_truncated
    result = project([item])
    actual = result["results"][0]
    assert len(actual[field]) <= 200
    assert actual[field + "_truncated"] is True
    assert len(_json(result)) < 2000


@pytest.mark.parametrize("field", ["semantic", "docstring", "source_code"])
def test_explore_partial_text_overrides_upstream_false_truncation_marker(field):
    item = explore_declaration()
    item.update(semantic="", docstring="", dependencies=[])
    item.pop("source_code")
    item[field] = "This evidence needs a bounded excerpt. " * 1000
    item[field + "_truncated"] = False
    result = project([item], 2000)
    actual = result["results"][0]
    assert actual[field] != item[field]
    assert actual[field + "_truncated"] is True
    assert result["result_truncated"] is True


def test_context_deduplication_preserves_each_complete_scope_and_is_response_local():
    items = [explore_declaration(i) for i in range(4)]
    for item in items:
        item.pop("source_code")
        item["semantic"] = "A short mathematical description."
        item["context"]["commands"] += [
            "variable {α : Type u} [Ring α]",
            'local notation "R" => α',
        ]
    items[2]["context"]["commands"].append("variable [Finite α]")
    before = deepcopy(items)
    result = project(items)
    assert "context_ref" not in result["results"][0]
    assert result["results"][1]["context_ref"] == 1
    assert "context_ref" not in result["results"][2]
    assert result["results"][3]["context_ref"] == 1
    for hit, original in zip(result["results"], items):
        assert context_of(result, hit)["commands"] == original["context"]["commands"]
        assert context_of(result, hit)["namespace"] == original["context"]["namespace"]
    assert items == before
    assert "context" in project(items[1:2])["results"][0]


def test_unavailable_context_and_mismatched_signature_remain_explicit():
    item = explore_declaration()
    item.pop("source_code")
    item.pop("context")
    item.update(
        fullname="Set.Finite.map_sInf_of_monotoneOn",
        signature=None,
        signature_status="name_mismatch",
        source_declaration="Set.Finite.map_sSup_of_monotoneOn",
        signature_error="Source is a generator; use #check Set.Finite.map_sInf_of_monotoneOn.",
        context_status="unavailable",
        context_error="Source belongs to another declaration.",
        semantic="Supremum preservation is not the requested infimum theorem.",
    )
    actual = project([item])["results"][0]
    for key in ("signature", "signature_status", "source_declaration", "signature_error",
                "context_status", "context_error"):
        assert actual[key] == item[key]
    assert not {"semantic", "docstring", "context", "context_ref"} & actual.keys()


def test_valid_header_does_not_repeat_normal_status_and_declaration_identity():
    item = explore_declaration()
    item.pop("source_code")
    item.update(signature_status="source_header", source_declaration="sample")
    actual = project([item])["results"][0]
    assert actual["signature"] == item["signature"]
    assert actual["fullname"] == item["fullname"]
    assert not {"signature_status", "source_declaration", "context_status"} & actual.keys()


def test_non_explore_context_is_atomic_when_optional_evidence_exceeds_budget():
    item = declaration()
    item["context"] = {"commands": [f"variable (h{i} : True)" for i in range(200)]}
    result = project([item], 1000)
    assert result["results"] == []
    assert result["results_omitted"] == 1


def test_non_explore_description_is_concise_without_mutating_source_fields():
    item = declaration()
    item.update(semantic="A useful mathematical explanation. " * 100,
                docstring="An additional description.")
    before = deepcopy(item)
    result = project([item])
    actual = result["results"][0]
    assert len(actual["semantic"]) <= 200
    assert actual["semantic_truncated"] is True
    assert "docstring" not in actual
    assert item == before


def test_non_explore_bounded_result_keeps_shortened_description_marker():
    items = [declaration(i) for i in range(6)]
    for item in items:
        item["semantic"] = "A useful mathematical explanation. " * 100
    result = project(items, 2200)
    descriptions = [hit for hit in result["results"] if "semantic" in hit]
    assert descriptions
    for hit in descriptions:
        assert len(hit["semantic"]) <= 200
        assert hit["semantic_truncated"] is True
