"""Inspect the saved entry with real Lean while preserving final grading rules."""

import asyncio
from types import SimpleNamespace

import pytest

from dvp.factory import build_services
from dvp.verification.lean import LeanVerificationConfig, LeanVerifier
from dvp.verification.modules import collect_bundle
from test_lean435_local_pipeline import WORKSPACE, local_runtime


def checker_for(tmp_path):
    root = tmp_path / "workspaces"
    (root / "Submission").mkdir(parents=True)
    checker = LeanVerifier(LeanVerificationConfig(
        target_workspace=WORKSPACE, scratch_workspace=root / "Submission",
        module_root=root, timeout=120,
    ))
    return checker, root


@pytest.mark.parametrize("namespaced", [False, True])
@pytest.mark.parametrize("disk_probe", [False, True])
def test_real_probe_prints_axioms_of_current_saved_target(tmp_path, local_runtime, namespaced, disk_probe):
    checker, root = checker_for(tmp_path)
    (root / "Submission/Helper.lean").write_text("theorem helper : True := by trivial\n")
    body = "theorem target : True := helper\n"
    if namespaced:
        body = "namespace Inner\n" + body + "end Inner\n"
    saved = "import Submission.Helper\n" + body
    (root / "Submission.lean").write_text(saved)
    name = "Inner.target" if namespaced else "target"
    probe = f"import Submission\n#print axioms {name}\n"
    if disk_probe:
        path = root / "Submission/Probe.lean"
        path.write_text(probe)
        result = asyncio.run(checker.verify_file(path))
    else:
        result = asyncio.run(checker.verify_code(probe))
    assert result["success"], result
    assert f"'{name}' does not depend on any axioms" in result["output"], result
    assert result["compiled_modules"] == ["Submission.Helper", "Submission"]
    assert result["source_bundle_manifest"] == collect_bundle(
        probe, root, allow_entrypoint_import=True,
    ).manifest()
    assert "contract_verification" not in result
    assert (root / "Submission.lean").read_text() == saved


@pytest.mark.parametrize("helper,axiom,holes", [
    ("axiom hidden : True\n", "hidden", 0),
    ("theorem hidden : True := by sorry\n", "sorryAx", 1),
])
def test_real_probe_reports_untrusted_axioms_through_saved_entry_closure(
    tmp_path, local_runtime, helper, axiom, holes,
):
    checker, root = checker_for(tmp_path)
    (root / "Submission/Helper.lean").write_text(helper)
    (root / "Submission/Middle.lean").write_text(
        "import Submission.Helper\ntheorem middle : True := hidden\n"
    )
    (root / "Submission.lean").write_text("import Submission.Middle\ntheorem target : True := middle\n")
    result = asyncio.run(checker.verify_code("import Submission\n#print axioms target\n"))
    assert result["success"], result  # Compilation is development feedback.
    assert f"'target' depends on axioms: [{axiom}]" in result["output"], result
    assert result["dependency_placeholders"]["total"] == holes
    assert result["compiled_modules"] == ["Submission.Helper", "Submission.Middle", "Submission"]
    assert "contract_verification" not in result


def test_real_probe_rebuilds_changed_helper_and_root_instead_of_reusing_artifacts(tmp_path, local_runtime):
    checker, root = checker_for(tmp_path)
    helper = root / "Submission/Helper.lean"
    helper.write_text("def value : Nat := 1\n")
    saved = root / "Submission.lean"
    saved.write_text("import Submission.Helper\ntheorem target : value = 1 := by rfl\n")
    probe = "import Submission\n#print axioms target\n"
    first = asyncio.run(checker.verify_code(probe))
    assert first["success"], first
    # Workspace artifacts are never inputs, even when their names match.
    (root / "Submission.olean").write_bytes(b"stale root artifact")
    (root / "Submission/Helper.olean").write_bytes(b"stale helper artifact")
    helper.write_text("def value : Nat := 2\n")
    broken = asyncio.run(checker.verify_code(probe))
    assert not broken["success"]
    assert broken["failed_module"] == "Submission"
    assert broken["source_bundle_sha256"] != first["source_bundle_sha256"]
    assert any(str(saved) in error for error in broken["errors"])
    saved.write_text("import Submission.Helper\ntheorem target : value = 2 := by rfl\n")
    repaired = asyncio.run(checker.verify_code(probe))
    assert repaired["success"], repaired
    assert repaired["source_bundle_sha256"] not in {
        first["source_bundle_sha256"], broken["source_bundle_sha256"],
    }


def test_comparator_does_not_accept_development_root_import_bundle(tmp_path, local_runtime):
    statement = "theorem target : True := by sorry\n"
    services = build_services(
        theorem_statement=statement,
        runtime=SimpleNamespace(target_workspace=WORKSPACE, lean_timeout=120),
        run_dir=tmp_path / "run",
    )
    services.workspace.submission_file.write_text(statement.replace("sorry", "trivial"))
    probe = "import Submission\n#print axioms target\n"
    bundle = collect_bundle(probe, services.workspace.workspaces_dir, allow_entrypoint_import=True)
    result = asyncio.run(services.contract_verifier.verify(probe, bundle=bundle))
    assert not result["success"], result
    assert result["command"] is None
    assert any("Local modules must be named Submission.<Name>: Submission" in error for error in result["errors"])
    assert not result.get("_harness_infrastructure_error")
