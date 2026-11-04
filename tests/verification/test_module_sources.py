"""Source closure identity and filesystem boundaries, independently of Lean."""
from pathlib import Path

import pytest

from dvp.verification.modules import CandidateBundle, LocalModule, collect_bundle, source_imports


def save(root, name, text):
    path = root / Path(*name.split(".")).with_suffix(".lean")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_header_comments_multiline_and_module_imports():
    source = '''/- import Submission.Decoy /- nested -/ -/
module
prelude
public import Mathlib
meta import
  Submission.Base
import all Submission.Top
import «Submission».Other
def message := "import Submission.Fake"
'''
    assert source_imports(source) == ("Mathlib", "Submission.Base", "Submission.Top", "Submission.Other")


def test_transitive_closure_is_immutable_and_excludes_unused_files(tmp_path):
    base = save(tmp_path, "Submission.Base", "import Mathlib\ndef base : Nat := 1\n")
    save(tmp_path, "Submission.Top", "import Submission.Base\ndef top : Nat := base + 1\n")
    save(tmp_path, "Submission.Unused", "this is not Lean")
    code = "import Submission.Top\nexample : top = 2 := rfl\n"
    bundle = collect_bundle(code, tmp_path)
    assert [m.name for m in bundle.modules] == ["Submission.Base", "Submission.Top"]
    original = bundle.sha256
    base.write_text("import Mathlib\ndef base : Nat := 2\n")
    assert bundle.sha256 == original
    assert collect_bundle(code, tmp_path).sha256 != original
    assert bundle.manifest()["modules"][0]["path"] == "Submission/Base.lean"


def test_missing_module_does_not_use_a_compiled_artifact(tmp_path):
    save(tmp_path, "Submission.Base", "import Mathlib\n").unlink()
    (tmp_path / "Submission/Base.olean").write_bytes(b"stale")
    with pytest.raises(ValueError, match="Cannot read local module"):
        collect_bundle("import Submission.Base", tmp_path)


@pytest.mark.parametrize("directory_link", [False, True])
def test_source_symlinks_are_rejected(tmp_path, directory_link):
    root, outside = tmp_path / "root", tmp_path / "outside"
    save(outside, "Submission.Base", "import Mathlib")
    root.mkdir()
    if directory_link:
        (root / "Submission").symlink_to(outside / "Submission", target_is_directory=True)
    else:
        (root / "Submission").mkdir()
        (root / "Submission/Base.lean").symlink_to(outside / "Submission/Base.lean")
    with pytest.raises(ValueError, match="symlinks"):
        collect_bundle("import Submission.Base", root)


def test_cycle_and_self_import_are_rejected(tmp_path):
    save(tmp_path, "Submission.A", "import Submission.B")
    save(tmp_path, "Submission.B", "import Submission.A")
    with pytest.raises(ValueError, match="cycle"):
        collect_bundle("import Submission.A", tmp_path)
    entry = save(tmp_path, "Submission.Self", "import Submission.Self")
    with pytest.raises(ValueError, match="cycle"):
        collect_bundle(entry.read_text(), tmp_path, entry_path=entry)


@pytest.mark.parametrize("name", ["Mathlib.Fake", "Submission", "Submission..Bad", "Submission.../bad", "Submission./tmp"])
def test_constructed_bundle_cannot_escape_local_namespaces(name):
    with pytest.raises(ValueError):
        LocalModule(name, "")


def test_manually_constructed_bundles_must_have_exact_ordered_closure():
    base = LocalModule("Submission.Base", "import Mathlib")
    top = LocalModule("Submission.Top", "import Submission.Base")
    for modules in ((top, base), (base, base), (top,), (base,)):
        with pytest.raises(ValueError):
            CandidateBundle("import Submission.Top", modules)
    with pytest.raises(ValueError, match="outside"):
        CandidateBundle("import Mathlib", (base,))


@pytest.mark.parametrize("name", ["Submission.«../Bad»", "Submission.«A.B»", "Submission.«/tmp/Bad»"])
def test_escaped_filesystem_separators_fail_closed(tmp_path, name):
    with pytest.raises(ValueError, match="Unsupported local"):
        collect_bundle(f"import {name}", tmp_path)


def test_inline_and_disk_sources_have_same_portable_identity(tmp_path):
    save(tmp_path, "Submission.Base", "import Mathlib")
    entry = save(tmp_path, "Submission.Probe", "import Submission.Base\n#check Nat")
    assert collect_bundle(entry.read_text(), tmp_path, entry_path=entry) == collect_bundle(entry.read_text(), tmp_path)


def test_root_entry_collects_only_submission_helpers(tmp_path):
    save(tmp_path, "Submission.Helpers", "theorem helper : True := by trivial\n")
    entry = tmp_path / "Submission.lean"
    entry.write_text("import Submission.Helpers\ntheorem target : True := helper\n")
    bundle = collect_bundle(entry.read_text(), tmp_path, entry_path=entry)
    assert [module.name for module in bundle.modules] == ["Submission.Helpers"]
    assert bundle.modules[0].relative_path == Path("Submission/Helpers.lean")


@pytest.mark.parametrize("through_helper", [False, True])
def test_root_entry_cannot_be_imported_directly_or_through_helper(tmp_path, through_helper):
    save(tmp_path, "Submission.Helpers", "import Submission\n")
    entry = tmp_path / "Submission.lean"
    entry.write_text("import Submission.Helpers\n" if through_helper else "import Submission\n")
    with pytest.raises(ValueError, match="entry file.*cannot be imported"):
        collect_bundle(entry.read_text(), tmp_path, entry_path=entry)


@pytest.mark.parametrize("old_name", ["scratch.Helpers", "sketch.Helpers"])
def test_old_namespaces_never_resolve_as_local_sources(tmp_path, old_name):
    save(tmp_path, old_name, "theorem helper : True := by trivial\n")
    code = f"import {old_name}\ntheorem target : True := helper\n"
    assert collect_bundle(code, tmp_path).modules == ()
    with pytest.raises(ValueError, match="Submission"):
        LocalModule(old_name, "")


def test_development_probe_snapshots_saved_root_and_transitive_helpers(tmp_path):
    base = save(tmp_path, "Submission.Base", "theorem base : True := by trivial\n")
    saved = save(tmp_path, "Submission", "import Submission.Base\ntheorem target : True := base\n")
    probe = "import Submission\n#print axioms target\n"
    bundle = collect_bundle(probe, tmp_path, allow_entrypoint_import=True)
    assert bundle.entry_code == probe
    assert [(m.name, m.relative_path) for m in bundle.modules] == [
        ("Submission.Base", Path("Submission/Base.lean")),
        ("Submission", Path("Submission.lean")),
    ]
    assert bundle.modules[-1].source == saved.read_text()
    assert bundle.manifest()["allow_entrypoint_import"] is True
    assert bundle.manifest()["entry_sha256"] != bundle.modules[-1].sha256
    original = bundle.sha256
    base.write_text("axiom base : True\n")
    assert bundle.sha256 == original
    assert collect_bundle(probe, tmp_path, allow_entrypoint_import=True).sha256 != original
    saved.write_text("theorem target : True := by trivial\n")
    latest = collect_bundle(probe, tmp_path, allow_entrypoint_import=True)
    assert [m.name for m in latest.modules] == ["Submission"]
    assert latest.sha256 != original
    # Development bundles cannot be reused as final bundles by default.
    with pytest.raises(ValueError, match="Submission"):
        CandidateBundle(bundle.entry_code, bundle.modules)
    with pytest.raises(ValueError, match="entry file.*cannot be imported"):
        collect_bundle(probe, tmp_path)


def test_development_opt_in_does_not_change_ordinary_bundle_identity(tmp_path):
    save(tmp_path, "Submission.Helper", "theorem helper : True := by trivial\n")
    code = "import Submission.Helper\nexample : True := helper\n"
    ordinary = collect_bundle(code, tmp_path)
    development = collect_bundle(code, tmp_path, allow_entrypoint_import=True)
    assert development == ordinary
    assert "allow_entrypoint_import" not in development.manifest()


@pytest.mark.parametrize("entry_name", ["Submission.Probe", None])
def test_root_probe_requires_current_root_source_not_olean(tmp_path, entry_name):
    (tmp_path / "Submission.olean").write_bytes(b"stale compiled entry")
    code = "import Submission\n#print axioms target\n"
    path = save(tmp_path, entry_name, code) if entry_name else None
    with pytest.raises(ValueError, match=r"Cannot read local module Submission \(Submission.lean\)"):
        collect_bundle(code, tmp_path, entry_path=path, allow_entrypoint_import=True)


@pytest.mark.parametrize("case", ["root-self", "root-helper-root", "helper-root-helper", "probe-cycle"])
def test_development_root_import_preserves_cycle_checks(tmp_path, case):
    helper = save(tmp_path, "Submission.Helper", "import Submission\n")
    root = save(tmp_path, "Submission", "import Submission.Helper\n")
    if case == "root-self":
        root.write_text("import Submission\n")
        path = root
    elif case == "root-helper-root":
        path = root
    elif case == "helper-root-helper":
        path = helper
    else:
        path = save(tmp_path, "Submission.Probe", "import Submission\n#print axioms target\n")
    with pytest.raises(ValueError, match="cycle"):
        collect_bundle(path.read_text(), tmp_path, entry_path=path, allow_entrypoint_import=True)


@pytest.mark.parametrize("link", ["root", "helper", "helper-directory"])
def test_root_probe_rejects_symlinks_at_every_dependency_level(tmp_path, link):
    root, outside = tmp_path / "workspaces", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    helper = save(root, "Submission.Helper", "theorem helper : True := by trivial\n")
    entry = save(root, "Submission", "import Submission.Helper\ntheorem target : True := helper\n")
    if link == "root":
        target = outside / "Entry.lean"
        target.write_text(entry.read_text())
        entry.unlink()
        entry.symlink_to(target)
    elif link == "helper":
        target = outside / "Helper.lean"
        target.write_text(helper.read_text())
        helper.unlink()
        helper.symlink_to(target)
    else:
        (root / "Submission").rename(outside / "Submission")
        (root / "Submission").symlink_to(outside / "Submission", target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        collect_bundle("import Submission", root, allow_entrypoint_import=True)


def test_manual_development_bundle_requires_ordered_complete_root_closure():
    helper = LocalModule("Submission.Helper", "theorem helper : True := by trivial\n")
    root = LocalModule("Submission", "import Submission.Helper\n", allow_entrypoint=True)
    for modules in ((root, helper), (root,), (helper, root, root)):
        with pytest.raises(ValueError):
            CandidateBundle("import Submission", modules, allow_entrypoint_import=True)
    cyclic_root = LocalModule("Submission", "import Submission\n", allow_entrypoint=True)
    with pytest.raises(ValueError, match="cyclic"):
        CandidateBundle("import Submission", (cyclic_root,), allow_entrypoint_import=True)
