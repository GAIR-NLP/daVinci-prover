"""Real Lean parser regressions for complete command and signature identity."""

import asyncio
from pathlib import Path
import shutil

import pytest

from dvp.verification.command_parser import (
    LeanCommandParseError,
    parse_commands,
)


WORKSPACE = Path(__file__).resolve().parents[2] / "runtime/mathlib4-4.35"
pytestmark = pytest.mark.skipif(
    shutil.which("lake") is None or not (WORKSPACE / "lean-toolchain").is_file(),
    reason="local Lean/Mathlib parser unavailable",
)


def parse(source: str, **kwargs):
    return asyncio.run(parse_commands(source, WORKSPACE, **kwargs))


def test_same_line_commands_have_complete_spans() -> None:
    source = "import Mathlib\ndef a := 1 def b := 2\n"
    commands = parse(source)
    assert [command.source for command in commands] == [
        "import Mathlib", "def a := 1", "def b := 2",
    ]
    assert [command.decl_name for command in commands[1:]] == ["a", "b"]


def test_unicode_crlf_names_and_signature_spans() -> None:
    source = "import Mathlib\r\nnamespace «名»\r\ntheorem «题» (α : Type) : True := by trivial\r\nend «名»\r\n"
    command = parse(source)[2]
    assert command.full_name == "名.题"
    assert source[command.start:command.end] == command.source
    assert source[command.body_start:command.body_end] == "by trivial"
    assert source[command.signature_start:command.signature_end].rstrip() == "theorem «题» (α : Type) : True"


def test_doc_comments_and_lemma_theorem_synonym_have_same_identity() -> None:
    ordinary = parse("import Mathlib\ntheorem target : True := by trivial\n")[1]
    documented = parse("import Mathlib\n/-- Reworded description. -/ lemma target : True := by trivial\n")[1]
    assert documented.decl_kind == "lemma"
    assert documented.decl_name == "target"
    assert documented.fingerprint == ordinary.fingerprint
    assert documented.signature_fingerprint == ordinary.signature_fingerprint


def test_signature_preserves_values_inside_command_quotations() -> None:
    first = "import Mathlib\ntheorem target : (`(command| def x := 1)) = (`(command| def x := 1)) := by rfl\n"
    second = first.replace("def x := 1", "def x := 2", 1)
    assert parse(first)[1].signature_fingerprint != parse(second)[1].signature_fingerprint


def test_do_block_layout_changes_command_identity() -> None:
    original = """import Mathlib
def proposition : Prop := Id.run do
  if false then
    if true then
      return False
    else
      return True
  return False
"""
    changed = original.replace("    else\n      return True", "  else\n    return True")
    assert parse(original)[1].fingerprint != parse(changed)[1].fingerprint


def test_named_structure_axiom_and_instance_are_identified() -> None:
    source = """import Mathlib
structure Box where
  value : Nat
axiom source_fact : True
instance namedInstance : Inhabited Box := ⟨⟨0⟩⟩
"""
    commands = parse(source)[1:]
    assert [(c.decl_kind, c.decl_name) for c in commands] == [
        ("structure", "Box"), ("axiom", "source_fact"), ("instance", "namedInstance"),
    ]


@pytest.mark.parametrize("trusted_source", [False, True])
def test_parser_does_not_execute_eval(tmp_path: Path, trusted_source: bool) -> None:
    target = tmp_path / "must_not_exist"
    source = f'import Mathlib\n#eval IO.FS.writeFile "{target}" "executed"\n'
    assert parse(source, trusted_source=trusted_source)
    assert not target.exists()


def test_trusted_notation_environment_is_stable_and_does_not_leak() -> None:
    notation = 'notation "ℝ+" => PosReal'
    source = f"import Mathlib\n{notation}\ntheorem target (a : ℝ+) : a = a := by rfl\n"
    parsed_source = parse(source, trusted_source=True)
    assert parsed_source[1].source == notation
    fixed_source = parse(source, trusted_syntax=(notation,))
    candidate = parse(source.replace(notation + "\n", ""), trusted_syntax=(notation,))
    assert fixed_source[-1].signature_fingerprint == candidate[-1].signature_fingerprint
    with pytest.raises(LeanCommandParseError):
        parse(source.replace(notation + "\n", ""))


def test_trusted_syntax_payload_cannot_execute_non_notation(tmp_path: Path) -> None:
    target = tmp_path / "must_not_exist"
    payload = f'#eval IO.FS.writeFile "{target}" "executed"'
    with pytest.raises(LeanCommandParseError, match="non-notation"):
        parse("theorem target : True := by trivial", trusted_syntax=(payload,))
    assert not target.exists()
