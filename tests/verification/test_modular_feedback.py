"""Local feedback compiles frozen import closures, without reusing user oleans."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from dvp.verification import modular_feedback
from dvp.verification.lean import LeanVerificationConfig, LeanVerifier
from dvp.verification.modules import collect_bundle


BASE = "theorem base : True := by trivial\n"
STEP = "import Submission.Base\ntheorem step : True := base\n"
ENTRY = "import Submission.Step\ntheorem target : True := step\n"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "workspaces"
    (root / "Submission").mkdir(parents=True)
    (root / "Submission/Base.lean").write_text(BASE)
    (root / "Submission/Step.lean").write_text(STEP)
    entry = root / "Submission.lean"
    entry.write_text(ENTRY)
    checker = LeanVerifier(LeanVerificationConfig(
        target_workspace=tmp_path, scratch_workspace=root / "Submission",
        module_root=root, timeout=30,
    ))
    monkeypatch.setattr(modular_feedback, "lean_search_path", lambda _: "/pinned/lean")
    commands = []

    def sandbox(command, **options):
        commands.append((command, options))
        return command

    monkeypatch.setattr(checker, "_sandbox_command", sandbox)
    process = AsyncMock(return_value=(0, "", ""))
    monkeypatch.setattr(modular_feedback, "run_process", process)
    return checker, entry, root, commands, process


def test_snapshot_compiles_dependency_order_in_isolated_source_and_build_trees(setup):
    checker, entry, root, commands, process = setup
    sources = collect_bundle(ENTRY, root)
    seen = []

    async def compile(command, **options):
        path = Path(command[-1])
        seen.append(path.read_text())
        assert not path.stat().st_mode & 0o222
        if "-o" in command:
            destination = Path(command[command.index("-o") + 1])
            assert not destination.exists()
            assert not destination.parent.stat().st_mode & 0o002
            destination.write_text("new compiled artifact")
        # Editing a live helper while the first module runs must not change the snapshot.
        (root / "Submission/Step.lean").write_text("BROKEN CHANGED SOURCE")
        return 0, "", ""

    process.side_effect = compile
    result = asyncio.run(checker.verify_file(entry))

    assert result["success"], result
    assert seen == [BASE, STEP, ENTRY]
    assert result["compiled_modules"] == ["Submission.Base", "Submission.Step"]
    assert result["source_bundle_sha256"] == sources.sha256
    assert result["source_bundle_manifest"] == sources.manifest()
    assert result["dependency_placeholders"] == {"total": 0, "items": []}
    for _, options in commands:
        source, = options["read_only"]
        build, = options["writable"]
        assert source != build and source.parent == build.parent
        assert options["environment"] == {"LEAN_PATH": str(build) + ":/pinned/lean"}
    assert not commands[0][1]["read_only"][0].exists()


def test_each_check_builds_again_and_helper_edits_change_bundle_identity(setup):
    checker, entry, root, commands, process = setup
    first = asyncio.run(checker.verify_file(entry))
    (root / "Submission/Base.lean").write_text(BASE + "-- revised proof source\n")
    second = asyncio.run(checker.verify_file(entry))
    assert process.await_count == 6
    assert first["source_bundle_sha256"] != second["source_bundle_sha256"]
    assert commands[0][1]["writable"] != commands[3][1]["writable"]


@pytest.mark.parametrize("source", [
    "theorem base : True := by sorry\n",
    "axiom base : True\n",
    "@[simp] axiom base : True\n",
    "theorem base : True := by native_decide\n",
    "theorem base : True := sorryAx True true\n",
    "unsafe def value : Nat := 1\n",
    "run_cmd pure ()\n",
    "import Std\nimport Lean\n" + BASE,
    "opaque base : True := True.intro\n",
    'notation "truth" => True\ntheorem base : truth := by trivial\n',
])
def test_dependency_source_reaches_lean_without_semantic_prechecks(setup, source):
    checker, entry, root, _, process = setup
    (root / "Submission/Base.lean").write_text(source)
    seen = []

    async def compile(command, **options):
        seen.append(Path(command[-1]).read_text())
        return 0, "", ""

    process.side_effect = compile
    result = asyncio.run(checker.verify_file(entry))
    assert result["success"], result
    assert seen == [source, STEP, ENTRY]
    assert result["source_bundle_manifest"]["modules"]
    assert process.await_count == 3


@pytest.mark.parametrize("draft", [False, True])
def test_dependency_inspection_commands_compile_in_ordinary_and_draft_checks(setup, draft):
    checker, entry, root, _, process = setup
    checker.config.allow_sorry = draft
    helper = BASE + "#eval (20 + 22 : Nat)\n#print base\n"
    (root / "Submission/Base.lean").write_text(helper)
    seen = []

    async def compile(command, **options):
        source = Path(command[-1]).read_text()
        seen.append(source)
        if source == helper:
            return 0, "42\ntheorem base : True := True.intro\n", ""
        if source == ENTRY and draft:
            return 0, 'DVP_LOCAL_DRAFT_JSON:{"messages": [], "sorries": []}\n', ""
        return 0, "", ""

    process.side_effect = compile
    result = asyncio.run(checker.verify_file(entry))
    assert result["success"], result
    assert seen == [helper, STEP, ENTRY]
    assert result["compiled_modules"] == ["Submission.Base", "Submission.Step"]
    assert "42" in result["output"]
    assert "theorem base" in result["output"]


def test_draft_reports_dependency_holes_even_when_entry_has_none(setup):
    checker, entry, root, _, process = setup
    checker.config.allow_sorry = True
    (root / "Submission/Base.lean").write_text("theorem base : True := by sorry\n")
    process.side_effect = [
        (0, "", ""), (0, "", ""),
        (0, 'DVP_LOCAL_DRAFT_JSON:{"messages": [], "sorries": []}\n', ""),
    ]
    result = asyncio.run(checker.verify_file(entry))
    assert result["success"], result
    assert result["response"]["sorries"] == []
    assert result["dependency_placeholders"]["total"] == 1
    assert result["dependency_placeholders"]["items"][0]["module"] == "Submission.Base"


def test_dependency_error_stops_before_entry_and_preserves_full_mapped_diagnostic(setup):
    checker, entry, root, _, process = setup

    async def fail(command, **options):
        return 1, f"{command[-1]}:1:0: error: failed proof\nx : Nat\n⊢ x = 0\n", ""

    process.side_effect = fail
    result = asyncio.run(checker.verify_file(entry, max_output_chars=20))
    assert not result["success"]
    assert result["failed_module"] == "Submission.Base"
    assert result["errors"] == [f"{root}/Submission/Base.lean:1:0: error: failed proof\nx : Nat\n⊢ x = 0"]
    assert process.await_count == 1


def test_all_modules_share_one_timeout(setup, monkeypatch):
    checker, entry, _, _, process = setup
    clock = [100.0]
    monkeypatch.setattr(modular_feedback.time, "monotonic", lambda: clock[0])
    remaining = []

    async def compile(command, *, cwd, timeout):
        remaining.append(timeout)
        clock[0] += 16
        return 0, "", ""

    process.side_effect = compile
    result = asyncio.run(checker.verify_file(entry))
    assert remaining == [30, 14]
    assert not result["success"]
    assert "timed out" in result["errors"][0]


def test_no_imports_keep_single_file_execution_path(setup, monkeypatch):
    checker, entry, _, _, process = setup
    entry.write_text(BASE)
    legacy = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(checker, "_run_local_command", legacy)
    monkeypatch.setattr(checker, "_lean_command", lambda path: ["lean", str(path)])
    monkeypatch.setattr(checker, "_local_compile_result", lambda *args, **kwargs: {"success": True})
    legacy.return_value = (0, "", "")
    assert asyncio.run(checker.verify_file(entry))["success"]
    legacy.assert_awaited_once()
    process.assert_not_awaited()


def test_inline_code_uses_module_root(setup):
    checker, _, _, _, process = setup
    result = asyncio.run(checker.verify_code(ENTRY))
    assert result["success"], result
    assert process.await_count == 3


@pytest.mark.parametrize("disk_probe", [False, True])
def test_saved_entry_inspection_builds_frozen_root_before_probe(setup, disk_probe):
    checker, entry, root, commands, process = setup
    probe = "import Submission\n#print axioms target\n"
    expected = collect_bundle(probe, root, allow_entrypoint_import=True)
    seen = []

    async def compile(command, **options):
        path = Path(command[-1])
        seen.append((path.name, path.read_text()))
        if "-o" in command:
            destination = Path(command[command.index("-o") + 1])
            assert not destination.exists()
            destination.write_text("fresh artifact")
        if path.name == "Base.lean":
            # All sources, including the root entry, were frozen before Lean.
            entry.write_text("BROKEN LIVE ENTRY")
        if path.name == "__DvpEntry.lean":
            build, = commands[-1][1]["writable"]
            assert (build / "Submission.olean").is_file()
            return 0, "'target' depends on axioms: []\n", ""
        return 0, "", ""

    process.side_effect = compile
    if disk_probe:
        path = root / "Submission/Probe.lean"
        path.write_text(probe)
        result = asyncio.run(checker.verify_file(path))
    else:
        result = asyncio.run(checker.verify_code(probe))
    assert result["success"], result
    assert seen == [("Base.lean", BASE), ("Step.lean", STEP),
                    ("Submission.lean", ENTRY), ("__DvpEntry.lean", probe)]
    assert result["compiled_modules"] == ["Submission.Base", "Submission.Step", "Submission"]
    assert result["source_bundle_sha256"] == expected.sha256
    assert result["source_bundle_manifest"] == expected.manifest()
    assert "'target' depends on axioms: []" in result["output"]
    assert "contract_verification" not in result


def test_saved_entry_compile_failure_maps_to_root_and_prevents_probe_execution(setup):
    checker, entry, root, _, process = setup

    async def compile(command, **options):
        if Path(command[-1]).name == "Submission.lean":
            return 1, f"{command[-1]}:2:0: error: unknown identifier 'missing'\n", ""
        return 0, "", ""

    process.side_effect = compile
    result = asyncio.run(checker.verify_code("import Submission\n#print axioms target\n"))
    assert not result["success"]
    assert result["failed_module"] == "Submission"
    assert result["errors"] == [f"{entry}:2:0: error: unknown identifier 'missing'"]
    assert result["compiled_modules"] == ["Submission.Base", "Submission.Step"]
    assert process.await_count == 3


@pytest.mark.parametrize("inspect_root", [False, True])
def test_root_inspection_output_survives_warning_heavy_dependencies(setup, inspect_root):
    checker, _, root, _, process = setup
    info = "'target' depends on axioms: [hidden]"
    warning = f"{root}/Submission/Base.lean:1:0: warning: " + "w" * 400

    async def compile(command, **options):
        name = Path(command[-1]).name
        if name == "Base.lean":
            return 0, warning + "\n", ""
        if name == "__DvpEntry.lean":
            return 0, info + "\n", ""
        return 0, "", ""

    process.side_effect = compile
    imported = "Submission" if inspect_root else "Submission.Step"
    result = asyncio.run(checker.verify_code(f"import {imported}\n#print axioms target\n", max_output_chars=160))
    assert result["success"], result
    assert result["warnings"] == [warning]
    assert "<truncated>" in result["output"]
    if inspect_root:
        assert result["output"].startswith(info)
    else:
        assert result["output"].startswith(warning[:100])
        assert info not in result["output"]


def test_missing_module_is_feedback_without_starting_lean(setup):
    checker, entry, root, _, process = setup
    (root / "Submission/Base.lean").unlink()
    result = asyncio.run(checker.verify_file(entry))
    assert result["error_kind"] == "local_module_error"
    assert not result["success"]
    process.assert_not_awaited()


def test_development_compiles_original_candidate_without_contract_reassembly(setup):
    checker, entry, root, _, process = setup
    sources = collect_bundle(ENTRY, root)
    prepare = AsyncMock(side_effect=AssertionError("development must not reassemble the task"))
    checker.task_contract = SimpleNamespace(
        prepare_submission=prepare, metadata={"effective_statement_sha256": "statement"},
        target_name="target",
    )
    checker.artifact_dir = root.parent / "artifacts"
    seen = []

    async def compile(command, **options):
        seen.append(Path(command[-1]).read_text())
        return 0, "", ""

    process.side_effect = compile
    result = asyncio.run(checker.verify_file(entry))
    assert result["success"], result
    assert seen == [BASE, STEP, ENTRY]
    assert result["source_bundle_sha256"] == sources.sha256
    assert result["source_bundle_manifest"] == sources.manifest()
    prepare.assert_not_awaited()


@pytest.mark.parametrize(("name", "code"), [
    ("Submission.lean", "theorem helper : True := by trivial\n"),
    ("Submission/Other.lean", "theorem target : True := by trivial\n"),
    ("Submission/Other.lean", "def originalHelper : Nat := 1\n"),
    ("Submission/.davinci_tmp_123.lean", "theorem helper : True := by trivial\n"),
])
def test_all_development_paths_compile_the_supplied_source(setup, monkeypatch, name, code):
    checker, _, root, _, process = setup
    seen = []

    async def compile(path, **options):
        seen.append(path.read_text())
        return {"success": True}

    monkeypatch.setattr(checker, "_verify_local_file", compile)
    path = root / name
    path.write_text(code)
    result = asyncio.run(checker.verify_file(path))
    assert result["success"]
    assert seen == [code]
    assert "fixed_task_contract" not in result
    process.assert_not_awaited()


def test_real_lean_compiles_transitive_imports_and_reports_dependency_holes(tmp_path):
    from dvp.verification.comparator import runtime_for

    workspace = Path(__file__).resolve().parents[2] / "runtime/mathlib4-4.35"
    try:
        runtime_for(workspace)
    except (OSError, RuntimeError, KeyError) as error:
        pytest.skip(f"registered local Lean runtime unavailable: {error}")
    root = tmp_path / "workspaces"
    (root / "Submission").mkdir(parents=True)
    (root / "Submission/Base.lean").write_text(BASE + "#eval (20 + 22 : Nat)\n#print base\n")
    (root / "Submission/Step.lean").write_text(STEP)
    checker = LeanVerifier(LeanVerificationConfig(
        target_workspace=workspace, scratch_workspace=root / "Submission",
        module_root=root, timeout=120,
    ))
    result = asyncio.run(checker.verify_code(ENTRY))
    assert result["success"], result
    assert result["compiled_modules"] == ["Submission.Base", "Submission.Step"]
    assert "42" in result["output"]
    assert "theorem base" in result["output"]

    (root / "Submission/Base.lean").write_text("theorem base : True := by sorry\n")
    checker.config.allow_sorry = True
    result = asyncio.run(checker.verify_code(ENTRY))
    assert result["success"], result
    assert result["response"]["sorries"] == []
    assert result["dependency_placeholders"]["total"] == 1
    checker.config.allow_sorry = False
    result = asyncio.run(checker.verify_code(ENTRY))
    assert result["success"], result
    assert result["dependency_placeholders"]["total"] == 1
    assert any("sorry" in warning for warning in result["warnings"])
    # A real auxiliary source must not inherit definitions from a task contract.
    prepare = AsyncMock(side_effect=AssertionError("helper must compile independently"))
    checker.task_contract = SimpleNamespace(target_name="target", metadata={
        "proof_slots": [{"name": "target"}],
    }, prepare_submission=prepare)
    helper = root / "Submission/Aux.lean"
    helper.write_text("theorem helper : fixedValue = 1 := by rfl\n")
    result = asyncio.run(checker.verify_file(helper))
    assert not result["success"]
    assert "fixedValue" in "\n".join(result["errors"])
    helper.write_text("namespace Auxiliary\ntheorem helper : True := by trivial\nend Auxiliary\n")
    result = asyncio.run(checker.verify_file(helper))
    assert result["success"], result
    prepare.assert_not_awaited()
