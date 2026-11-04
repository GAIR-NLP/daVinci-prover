"""Imported scoped grammar must work without evaluating model declarations."""

import asyncio
from pathlib import Path
import shutil

import pytest

from dvp.verification.command_parser import LeanCommandParseError, parse_commands


WORKSPACE = Path(__file__).resolve().parents[2] / "runtime/mathlib4-4.35"
pytestmark = pytest.mark.skipif(
    shutil.which("lake") is None or not (WORKSPACE / "lean-toolchain").is_file(),
    reason="local Lean/Mathlib parser unavailable",
)
INNER_TARGET = "theorem target : ⟪x, y⟫_ℝ = ⟪x, y⟫_ℝ := by rfl\n"


def parse(source: str, **kwargs):
    return asyncio.run(parse_commands(source, WORKSPACE, timeout=3600, **kwargs))


@pytest.mark.parametrize("opening", [
    "open InnerProductSpace",
    "open scoped InnerProductSpace",
    "open NotElaboratedYet InnerProductSpace",
])
def test_imported_scoped_grammar_and_unknown_namespace(opening: str) -> None:
    commands = parse("import Mathlib\n" + opening + "\n" + INNER_TARGET)
    assert commands[-1].decl_name == "target"
    assert commands[-1].source == INNER_TARGET.rstrip()


def test_namespace_activates_its_imported_scoped_grammar() -> None:
    commands = parse("import Mathlib\nnamespace InnerProductSpace\n" + INNER_TARGET + "end InnerProductSpace\n")
    assert commands[-2].full_name == "InnerProductSpace.target"


def test_scope_is_restored_at_end_and_does_not_leak_between_requests() -> None:
    local = "import Mathlib\nsection\nopen scoped InnerProductSpace\n" + INNER_TARGET + "end\n"
    assert parse(local)
    with pytest.raises(LeanCommandParseError):
        parse(local + INNER_TARGET.replace("target", "outside"))
    with pytest.raises(LeanCommandParseError):
        parse("import Mathlib\n" + INNER_TARGET)


def test_scope_handling_does_not_evaluate_other_commands(tmp_path: Path) -> None:
    destination = tmp_path / "must_not_exist"
    source = (
        "import Mathlib\nnamespace BrandNew\n"
        f'#eval IO.FS.writeFile "{destination}" "executed"\n'
        "def impossible : False := by trivial\n"
        "instance : Inhabited False := ⟨impossible⟩\n"
        "open scoped InnerProductSpace\n" + INNER_TARGET + "end BrandNew\n"
    )
    assert parse(source)[-2].full_name == "BrandNew.target"
    assert not destination.exists()


def test_omitted_header_inherits_trusted_big_operators() -> None:
    opening = "open scoped BigOperators"
    target = "theorem target : (∑ i ∈ ({1, 2} : Finset Nat), i) = 3 := by norm_num\n"
    original = parse("import Mathlib\n" + opening + "\n" + target)
    omitted = parse(target, trusted_syntax=(opening,))
    assert original[-1].signature_fingerprint == omitted[-1].signature_fingerprint


def test_trusted_open_cannot_wrap_an_executable_command(tmp_path: Path) -> None:
    destination = tmp_path / "must_not_exist"
    payload = f'open Lean in\n#eval IO.FS.writeFile "{destination}" "executed"'
    with pytest.raises(LeanCommandParseError, match="non-notation/open"):
        parse("theorem target : True := by trivial", trusted_syntax=(payload,))
    assert not destination.exists()


def test_crlf_preserves_multiline_string_semantics_and_original_spans() -> None:
    original_lf = (
        'import Mathlib\nnamespace «名»\n'
        'def «文» : String := "甲\n乙"\n'
        'theorem «题» (α : Type) : True := by\n  trivial\nend «名»\n'
    )
    original_crlf = original_lf.replace("\n", "\r\n")
    lf_commands = parse(original_lf)
    crlf_commands = parse(original_crlf)
    assert len(lf_commands) == len(crlf_commands)
    for lf, crlf in zip(lf_commands, crlf_commands):
        assert lf.fingerprint == crlf.fingerprint
        assert original_crlf[crlf.start:crlf.end] == crlf.source
        assert crlf.source.replace("\r\n", "\n") == lf.source
        for prefix in ("signature", "value", "body"):
            lf_start, lf_end = getattr(lf, prefix + "_start"), getattr(lf, prefix + "_end")
            crlf_start, crlf_end = getattr(crlf, prefix + "_start"), getattr(crlf, prefix + "_end")
            if lf_start is not None and lf_end is not None:
                assert original_crlf[crlf_start:crlf_end].replace("\r\n", "\n") == original_lf[lf_start:lf_end]
