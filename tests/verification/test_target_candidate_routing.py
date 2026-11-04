"""Target routing recognizes names without claiming statement equivalence."""

import pytest

from dvp.verification.contracts import (
    contains_exact_target_declaration,
    contains_target_candidate_declaration,
)


STATEMENT = "theorem target (x : Nat) : x = x := by sorry"


@pytest.mark.parametrize("candidate", [
    "theorem target (n : Nat) : n = n := by rfl",
    "theorem target : ∀ n : Nat, n = n := by intro n; rfl",
    "lemma target (x : Nat) : x = x := by rfl",
    "theorem target : True := by trivial",
])
def test_same_name_routes_without_relaxing_literal_header_match(candidate):
    assert contains_target_candidate_declaration(candidate, STATEMENT)
    assert not contains_exact_target_declaration(candidate, STATEMENT)


def test_exact_header_match_keeps_whitespace_and_comment_tolerance():
    candidate = "theorem target /- binder -/ (x : Nat)\n : x = x := by rfl"
    assert contains_exact_target_declaration(candidate, STATEMENT)
    assert contains_target_candidate_declaration(candidate, STATEMENT)


@pytest.mark.parametrize("candidate", [
    "theorem helper (x : Nat) : x = x := by rfl",
    "namespace Shadow\ntheorem target (x : Nat) : x = x := by rfl\nend Shadow",
    "theorem Shadow.target (x : Nat) : x = x := by rfl",
    "/- theorem target : True := by trivial -/\ntheorem helper : True := by trivial",
    'def label : String := "theorem target : True := by trivial"',
    "def quoted := (\n  theorem target : True := by trivial\n)",
])
def test_helpers_shadows_and_masked_text_are_not_target_candidates(candidate):
    assert not contains_target_candidate_declaration(candidate, STATEMENT)


@pytest.mark.parametrize("candidate", [
    "namespace Scope\nlemma target : ∀ n : Nat, n = n := by intro n; rfl\nend Scope",
    "theorem Scope.target : ∀ n : Nat, n = n := by intro n; rfl",
])
def test_candidate_matches_the_fully_qualified_task_name(candidate):
    statement = f"namespace Scope\n{STATEMENT}\nend Scope"
    assert contains_target_candidate_declaration(candidate, statement)
    assert not contains_exact_target_declaration(candidate, statement)


def test_namespace_shadow_of_namespaced_task_is_not_a_candidate():
    statement = f"namespace Scope\n{STATEMENT}\nend Scope"
    candidate = f"namespace Other\nnamespace Scope\n{STATEMENT}\nend Scope\nend Other"
    assert not contains_target_candidate_declaration(candidate, statement)


def test_unidentified_task_cannot_route_any_candidate():
    assert not contains_target_candidate_declaration(STATEMENT, "example : True := by sorry")
