"""Declared Lake locations must be preserved when assembling an offline project."""
import pytest

from dvp.verification.workspace_dependencies import dependency_path


def test_relative_path_dependency_uses_manifest_directory(tmp_path):
    workspace = tmp_path / "mathlib"
    source = tmp_path / "shared" / "batteries"
    source.mkdir(parents=True)
    package = {"name": "batteries", "type": "path", "dir": "../shared/batteries"}
    assert dependency_path(workspace, {"packagesDir": "cache"}, package) == source


def test_absolute_path_dependency_ignores_packages_directory(tmp_path):
    source = tmp_path / "cache" / "Qq"
    package = {"name": "Qq", "type": "path", "dir": str(source)}
    assert dependency_path(tmp_path / "mathlib", {}, package) == source


def test_copied_path_dependency_uses_materialized_copy(tmp_path):
    package = {"name": "batteries", "type": "path", "dir": "../source", "copy": True}
    assert dependency_path(tmp_path, {"packagesDir": "vendor"}, package) == tmp_path / "vendor/batteries"


def test_git_dependency_preserves_subdirectory(tmp_path):
    package = {"name": "tools", "type": "git", "subDir": "lean/package", "dir": "../ignored"}
    assert dependency_path(tmp_path, {"packagesDir": "vendor"}, package) == tmp_path / "vendor/tools/lean/package"


def test_absolute_packages_directory_is_preserved(tmp_path):
    package_root = tmp_path / "packages"
    assert dependency_path(tmp_path / "mathlib", {"packagesDir": str(package_root)},
                           {"name": "batteries", "type": "git"}) == package_root / "batteries"


def test_resolves_declared_symlink_without_searching_other_checkouts(tmp_path):
    workspace = tmp_path / "mathlib"
    parent = workspace / ".lake/packages"
    parent.mkdir(parents=True)
    source = tmp_path / "pinned"
    source.mkdir()
    (parent / "Qq").symlink_to(source, target_is_directory=True)
    assert dependency_path(workspace, {}, {"name": "Qq", "type": "git"}) == source


def test_missing_declared_cache_never_selects_sibling(tmp_path):
    workspace = tmp_path / "mathlib"
    sibling_cache = tmp_path / "batteries/.lake/build/lib/lean"
    sibling_cache.mkdir(parents=True)
    declared = workspace / ".lake/packages/batteries"
    assert dependency_path(workspace, {}, {"name": "batteries", "type": "git"}) == declared
    assert not declared.exists()


@pytest.mark.parametrize("manifest", [{}, {"packagesDir": None}])
def test_default_layout_without_materialization(tmp_path, manifest):
    assert dependency_path(tmp_path, manifest, {"name": "batteries"}) == tmp_path / ".lake/packages/batteries"
    assert not (tmp_path / ".lake").exists()


@pytest.mark.parametrize("package, error", [
    ({"name": "../other"}, "package name"),
    ({"name": "Qq", "type": "path"}, "dir"),
    ({"name": "Qq", "type": "path", "dir": "", "copy": False}, "dir"),
    ({"name": "Qq", "type": "path", "dir": "../Qq", "copy": "false"}, "copy"),
    ({"name": "Qq", "type": "git", "subDir": "../other"}, "inside its checkout"),
    ({"name": "Qq", "type": "git", "subDir": "/other"}, "inside its checkout"),
    ({"name": "Qq", "type": "archive"}, "dependency type"),
])
def test_rejects_ambiguous_or_escaping_locations(tmp_path, package, error):
    with pytest.raises(ValueError, match=error):
        dependency_path(tmp_path, {}, package)
