"""Signature boundaries and conservative source context for LeanExplore."""

import pytest

from dvp.toolkits.retrieval.explore_source import (
    declaration_context,
    declaration_evidence,
    declaration_signature,
)


def context_at(source: str, marker: str = "theorem target"):
    line = source[:source.index(marker)].count("\n") + 1
    return declaration_context(source, line)


def test_signature_ignores_nested_defaults_comments_strings_and_quoted_names():
    source = '''/- leading /- nested -/ comment -/
@[simp] theorem «where := theorem»
    (n : Nat := 2) (s : String := "where := by")
    (h : n = n := by rfl) : n = n := by
  rfl'''
    signature = declaration_signature(source)
    assert signature == '''@[simp] theorem «where := theorem»
    (n : Nat := 2) (s : String := "where := by")
    (h : n = n := by rfl) : n = n'''


def test_signature_keeps_more_than_forty_lines_and_all_binders():
    header = "theorem target\n" + "".join(f"    (n{i} : Nat)\n" for i in range(55)) + "    : True"
    assert declaration_signature(header + " := by trivial") == header


@pytest.mark.parametrize("source, expected", [
    ("structure Foo where\n  x : Nat", "structure Foo"),
    ("def f : Nat → Nat\n  | 0 => 1\n  | n + 1 => n", "def f : Nat → Nat"),
    ('def f : String := "where"', "def f : String"),
    ("axiom somewhere : True", "axiom somewhere : True"),
    ("def «a\"where» (s : String := \":=\") : String := s", 'def «a"where» (s : String := ":=") : String'),
])
def test_signature_body_forms(source, expected):
    assert declaration_signature(source) == expected


def test_generated_declaration_does_not_receive_its_generators_signature():
    source = '''@[to_additive]
lemma prod_attach (s : Finset α) (f : α → β) :
    ∏ x ∈ s.attach, f x = ∏ x ∈ s, f x := by simp'''
    evidence = declaration_evidence(source, "Finset.sum_attach")
    assert evidence["kind"] == "lemma"
    assert evidence["source_declaration"] == "prod_attach"
    assert evidence["signature"] is None
    assert evidence["signature_status"] == "name_mismatch"
    assert "generator" in evidence["signature_error"]
    assert "lean_verify" in evidence["signature_error"]
    assert "#check Finset.sum_attach" in evidence["signature_error"]


@pytest.mark.parametrize("source_name, fullname", [
    ("target", "Outer.Inner.target"),
    ("Prime.dvd_mul", "Nat.Prime.dvd_mul"),
    ("Nat.Prime.dvd_mul", "Nat.Prime.dvd_mul"),
    ("_root_.Nat.target", "Nat.target"),
    ("«target»", "Outer.target"),
    ("«name.with.dots»", "Outer.«name.with.dots»"),
    ("«where := theorem»", "Outer.«where := theorem»"),
    ('«name/--with"comments»', 'Outer.«name/--with"comments»'),
])
def test_source_header_matches_namespace_suffixes_and_quoted_names(source_name, fullname):
    header = f"theorem {source_name} (n : Nat := 2) : n = n"
    evidence = declaration_evidence(header + " := by rfl", fullname)
    assert evidence == {
        "kind": "theorem", "signature": header, "signature_status": "source_header",
        "source_declaration": source_name, "signature_error": None,
    }


@pytest.mark.parametrize("source_name, fullname", [
    ("Other.target", "Outer.target"),
    ("_root_.target", "Outer.target"),
    ("«Outer.target»", "Outer.target"),
    ("target", "Outer.not_target"),
])
def test_name_matching_uses_whole_identifier_components(source_name, fullname):
    evidence = declaration_evidence(f"theorem {source_name} : True := by trivial", fullname)
    assert evidence["signature_status"] == "name_mismatch"
    assert evidence["signature"] is None


@pytest.mark.parametrize("source_name, namespace, fullname", [
    ("target", "Outer.Inner", "Outer.Inner.target"),
    ("Prime.dvd_mul", "Nat", "Nat.Prime.dvd_mul"),
    ("Nat.Prime.dvd_mul", "", "Nat.Prime.dvd_mul"),
    ("target", "", "target"),
    ("_root_.Nat.target", "Different", "Nat.target"),
    ("target", "_root_.Outer", "Outer.target"),
    ("«name.with.dots»", "Outer.«namespace.with.dots»", "Outer.«namespace.with.dots».«name.with.dots»"),
])
def test_lexical_namespace_can_confirm_the_complete_declaration_name(source_name, namespace, fullname):
    header = f"theorem {source_name} : True"
    evidence = declaration_evidence(header + " := by trivial", fullname, namespace=namespace)
    assert evidence["signature_status"] == "source_header"
    assert evidence["signature"] == header


@pytest.mark.parametrize("source_name, namespace, fullname", [
    ("target", "Other", "Outer.target"),
    ("target", "", "Outer.target"),
    ("Nat.target", "Nat", "Nat.target"),
    ("Prime.dvd_mul", "Int", "Nat.Prime.dvd_mul"),
    ("_root_.Nat.target", "Other", "Other.Nat.target"),
    ("target", "Outer.«Inner.Sub»", "Outer.Inner.Sub.target"),
])
def test_lexical_namespace_rejects_matching_suffix_from_a_different_scope(source_name, namespace, fullname):
    source = f"theorem {source_name} : True := by trivial"
    evidence = declaration_evidence(source, fullname, namespace=namespace)
    assert evidence["signature_status"] == "name_mismatch"
    assert evidence["signature"] is None
    assert evidence["source_declaration"] == source_name
    assert f"#check {fullname}" in evidence["signature_error"]
    if not source_name.startswith("_root_."):
        assert "Lexical source context" in evidence["signature_error"]


def test_invalid_lexical_namespace_does_not_claim_a_confirmed_signature():
    evidence = declaration_evidence("theorem target : True := by trivial", "Outer.target", namespace="Outer..Inner")
    assert evidence["signature_status"] == "unavailable"
    assert evidence["signature"] is None


def test_evidence_removes_nested_attributes_and_keeps_all_binders_and_defaults():
    header = (
        "lemma target\n"
        + "".join(f"    (n{i} : Nat := {i})\n" for i in range(55))
        + '    (s : String := "where := by")\n'
        + "    (h : n0 = n0 := by rfl) : n0 = n0"
    )
    source = '''/-- Documentation with a nested /- comment -/. -/
@[to_additive (attr := simp [foo, bar]) (reorder := 1 2)]
@[example_attr "text with ] and :="]
private noncomputable ''' + header + " := by rfl"
    evidence = declaration_evidence(source, "Outer.target")
    assert evidence["signature_status"] == "source_header"
    assert evidence["signature"] == header


@pytest.mark.parametrize("source", [
    "instance : Inhabited Nat := ⟨0⟩",
    "instance (α : Type) [Inhabited α] : Inhabited (List α) := ⟨[]⟩",
    "instance (priority := 100) : Inhabited Nat := ⟨0⟩",
    "instance _ : Inhabited Nat := ⟨0⟩",
])
def test_anonymous_instances_remain_hits_without_a_guessed_signature(source):
    evidence = declaration_evidence(source, "Nat.instInhabited")
    assert evidence["kind"] == "instance"
    assert evidence["source_declaration"] is None
    assert evidence["signature_status"] == "unavailable"
    assert evidence["signature"] is None
    assert "#check Nat.instInhabited" in evidence["signature_error"]


def test_named_instance_with_priority_can_be_identified():
    header = "instance (priority := 100) instFoo : Inhabited Nat"
    evidence = declaration_evidence(header + " := ⟨0⟩", "Foo.instFoo")
    assert evidence["kind"] == "instance"
    assert evidence["source_declaration"] == "instFoo"
    assert evidence["signature"] == header
    assert evidence["signature_status"] == "source_header"


@pytest.mark.parametrize("source", [
    None,
    "",
    "/-! " + "The algebraic hierarchy is documented here. " * 220 + "-/",
    'library_note "the algebraic hierarchy" /-- Long documentation. -/',
    'local notation "∑" => Finset.sum',
    'notation "target" => (0 : Nat)',
    '«not a declaration» theorem target : True := by trivial',
    '@[unfinished [attribute] theorem target : True := by trivial',
])
def test_non_declarations_and_missing_sources_do_not_become_signatures(source):
    evidence = declaration_evidence(source, "LibraryNote.the_algebraic_hierarchy")
    assert evidence["kind"] == "declaration"
    assert evidence["signature"] is None
    assert evidence["signature_status"] == "unavailable"
    assert evidence["source_declaration"] is None
    assert "#check LibraryNote.the_algebraic_hierarchy" in evidence["signature_error"]


@pytest.mark.parametrize("source", [
    "theorem target (n : Nat : n = n := by rfl",
    "theorem target-nope : True := by trivial",
    "axiom target : True\naxiom unrelated : False",
    "axiom target : True\n#check Nat",
])
def test_incomplete_or_ambiguous_header_does_not_claim_reliable_signature(source):
    evidence = declaration_evidence(source, "target")
    assert evidence["signature_status"] == "unavailable"
    assert evidence["signature"] is None


def test_context_retains_only_active_nested_scopes_and_multiline_variables():
    source = '''universe u
open scoped BigOperators
namespace Outer
variable {α : Type u}
section Closed
variable (discarded : Nat)
open Discarded
end Closed
section
variable
  [Group α]
  (x : α)
include x
namespace Inner
local notation "one" => (1 : α)
set_option maxRecDepth 2000
theorem target : x = x := by rfl
end Inner
end
end Outer
'''
    context = context_at(source)
    assert context["namespace"] == "Outer.Inner"
    assert context["commands"] == [
        "universe u", "open scoped BigOperators", "variable {α : Type u}",
        "variable\n  [Group α]\n  (x : α)", "include x",
        'local notation "one" => (1 : α)', "set_option maxRecDepth 2000",
    ]
    assert context["scopes"] == [
        {"kind": "namespace", "name": "Outer"},
        {"kind": "section", "name": ""},
        {"kind": "namespace", "name": "Inner"},
    ]
    assert context["method"] == "source_scan"
    assert context["complete"] is False
    assert "not an elaborated Lean type" in context["note"]
    assert not context["warnings"]


def test_fake_commands_in_comments_strings_quotes_and_tactics_do_not_leak():
    source = '''namespace Real
/- namespace Fake
variable (bad : Nat)
/- end Real -/
-/
def text := "namespace Fake\\nvariable (bad : Nat)"
def quoted := `(command| variable (alsoBad : Nat))
theorem previous : True := by
  set_option maxRecDepth 1 in
    trivial
-- variable (bad : Nat)
variable (good : Nat)
theorem target : good = good := by rfl
'''
    context = context_at(source)
    assert context["namespace"] == "Real"
    assert context["commands"] == ["variable (good : Nat)"]


def test_named_and_anonymous_scopes_close_and_quoted_namespace_survives():
    source = '''namespace «where and end»
section Named
variable (gone : Nat)
end Named
section
variable (alsoGone : Nat)
end
variable (kept : Nat)
omit kept
theorem target : kept = kept := by rfl
'''
    context = context_at(source)
    assert context["namespace"] == "«where and end»"
    assert context["commands"] == ["variable (kept : Nat)", "omit kept"]


def test_multiline_binders_need_not_be_indented_and_delimiters_can_span_lines():
    source = '''variable
{α : Type}
(f :
  α → α)
theorem target : True := by trivial
'''
    assert context_at(source)["commands"] == ["variable\n{α : Type}\n(f :\n  α → α)"]


@pytest.mark.parametrize("section", [
    "public section", "private section", "noncomputable section",
    "@[expose] public section", "public noncomputable section",
])
def test_prefixed_sections_remove_closed_variables_and_preserve_active_commands(section):
    source = f'''namespace Outer
{section} Closed
variable (discarded : Nat)
end Closed
{section}
variable (kept : Nat)
theorem target : kept = kept := by rfl
end
end Outer
'''
    context = context_at(source)
    assert context["namespace"] == "Outer"
    assert context["commands"] == [section, "variable (kept : Nat)"]
    assert context["scopes"] == [
        {"kind": "namespace", "name": "Outer"},
        {"kind": "section", "name": ""},
    ]
    assert not context["warnings"]


def test_command_local_context_and_unmatched_ends_are_explicitly_incomplete():
    source = '''end Missing
open Foo in
theorem previous : True := by trivial
theorem target : True := by trivial
'''
    context = context_at(source)
    assert context["commands"] == []
    assert any("Unmatched end" in warning for warning in context["warnings"])
    assert any("Command-local" in warning for warning in context["warnings"])


@pytest.mark.parametrize("line", [0, -1, 4, True, "1"])
def test_invalid_source_locations_fail_explicitly(line):
    with pytest.raises(ValueError):
        declaration_context("theorem target : True := by trivial\n", line)
