"""Package discovery uses synthetic installations without importing packages."""
from __future__ import annotations

import copy
import importlib.machinery
import importlib.metadata
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dvp.runtime import python_packages


@pytest.fixture
def installation(tmp_path, monkeypatch):
    base = tmp_path / "site-packages"
    base.mkdir()
    distributions = {}
    specs = {}
    for name in ("numpy", "sympy", "mpmath"):
        root = base / name
        root.mkdir()
        init = root / "__init__.py"
        # Executing this file is a discovery bug, regardless of package name.
        init.write_text("raise AssertionError('package startup ran during discovery')\n")
        dist_info = base / f"{name}-1.2.3.dist-info"
        dist_info.mkdir()
        (dist_info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: 1.2.3\n"
        )
        entries = [f"{name}/__init__.py", f"{dist_info.name}/METADATA"]
        if name == "numpy":
            (base / "numpy.libs").mkdir()
            (base / "numpy.libs" / "libexample.so").write_bytes(b"synthetic library")
            entries.append("numpy.libs/libexample.so")
        (dist_info / "RECORD").write_text("".join(f"{entry},,\n" for entry in entries))
        distributions[name] = importlib.metadata.PathDistribution(dist_info)
        specs[name] = importlib.machinery.ModuleSpec(name, loader=None, origin=str(init), is_package=True)
        specs[name].submodule_search_locations = [str(root)]
    monkeypatch.setattr(python_packages, "_installation_paths", lambda: [str(base)])
    return base, distributions, specs


def test_discovery_is_narrow_json_serializable_and_does_not_import(installation):
    base, _, _ = installation
    records = python_packages.discover_packages()
    assert json.loads(json.dumps(records)) == records
    assert [record["name"] for record in records] == ["numpy", "sympy", "mpmath"]
    for record in records:
        assert record["root"] == str(base / record["name"])
        assert record["version"] == "1.2.3"
        assert len(record["metadata_sha256"]) == len(record["init_sha256"]) == 64
        assert str(base) not in [record["root"], *record["extra_roots"]]
    assert records[0]["extra_roots"] == [str(base / "numpy.libs")]
    assert records[1]["extra_roots"] == records[2]["extra_roots"] == []
    python_packages.validate_packages(records)


def test_discovery_ignores_sys_path_environment_and_current_directory(installation, tmp_path, monkeypatch):
    base, _, _ = installation
    poisoned = tmp_path / "untrusted"
    poisoned.mkdir()
    for name in ("numpy", "sympy", "mpmath"):
        (poisoned / f"{name}.py").write_text("raise AssertionError('untrusted code ran')\n")
        fake_info = poisoned / f"{name}-999.0.dist-info"
        fake_info.mkdir()
        (fake_info / "METADATA").write_text(f"Name: {name}\nVersion: 999.0\n")
    monkeypatch.syspath_prepend(str(poisoned))
    monkeypatch.setenv("PYTHONPATH", str(poisoned))
    monkeypatch.chdir(poisoned)
    records = python_packages.discover_packages()
    assert [record["version"] for record in records] == ["1.2.3"] * 3
    assert all(Path(record["root"]).parent == base for record in records)
    python_packages.validate_packages(records)


def test_installation_paths_use_interpreter_roots_and_skip_missing_paths(tmp_path, monkeypatch):
    purelib = tmp_path / "venv" / "site-packages"
    global_lib = tmp_path / "global" / "dist-packages"
    purelib.mkdir(parents=True)
    global_lib.mkdir(parents=True)
    monkeypatch.setattr(python_packages.sysconfig, "get_path", lambda key: str(purelib))
    monkeypatch.setattr(
        python_packages.site, "getsitepackages",
        lambda: [str(purelib), str(global_lib), str(tmp_path / "missing")],
    )
    monkeypatch.syspath_prepend(str(tmp_path / "untrusted"))
    assert python_packages._installation_paths() == [str(purelib), str(global_lib)]


def test_missing_dependency_has_actionable_install_instruction(installation):
    base, _, _ = installation
    (base / "sympy-1.2.3.dist-info" / "METADATA").unlink()
    with pytest.raises(ValueError, match=r"require sympy.*pip install -e '\.\[python-experiments\]'"):
        python_packages.discover_packages()


@pytest.mark.parametrize("change", ["entry_point", "metadata", "version", "missing"])
def test_validation_detects_changed_installation(installation, change):
    base, _, _ = installation
    records = python_packages.discover_packages()
    if change == "entry_point":
        (base / "sympy" / "__init__.py").write_text("changed = True\n")
    elif change == "metadata":
        metadata_path = Path(records[1]["metadata_path"])
        metadata_path.write_text(metadata_path.read_text() + "Summary: changed\n")
    elif change == "version":
        metadata_path = Path(records[1]["metadata_path"])
        metadata_path.write_text(metadata_path.read_text().replace("1.2.3", "1.2.4"))
    else:
        (base / "sympy" / "__init__.py").unlink()
    with pytest.raises(RuntimeError, match="changed since executor registration"):
        python_packages.validate_packages(records)


@pytest.mark.parametrize("change", ["namespace", "broad_root", "different_install"])
def test_rejects_unsafe_import_location(installation, tmp_path, monkeypatch, change):
    base, _, specs = installation
    spec = specs["sympy"]
    if change == "namespace":
        spec.origin = None
    elif change == "broad_root":
        spec.origin = str(base / "__init__.py")
        spec.submodule_search_locations = [str(base)]
    else:
        source = tmp_path / "source" / "sympy"
        source.mkdir(parents=True)
        (source / "__init__.py").write_text("source_tree = True\n")
        spec.origin = str(source / "__init__.py")
        spec.submodule_search_locations = [str(source)]
    monkeypatch.setattr(
        python_packages, "PathFinder",
        SimpleNamespace(find_spec=lambda name, paths: specs[name]),
    )
    with pytest.raises(ValueError, match="cannot use sympy"):
        python_packages.discover_packages()


@pytest.mark.parametrize("target", ["root", "entry_point", "metadata", "numpy_libs"])
def test_rejects_symlinked_trusted_paths(installation, tmp_path, target):
    base, _, _ = installation
    paths = {
        "root": base / "sympy",
        "entry_point": base / "sympy" / "__init__.py",
        "metadata": base / "sympy-1.2.3.dist-info" / "METADATA",
        "numpy_libs": base / "numpy.libs",
    }
    path = paths[target]
    moved = tmp_path / "moved"
    path.rename(moved)
    path.symlink_to(moved, target_is_directory=moved.is_dir())
    with pytest.raises(ValueError, match="symlink|regular sibling"):
        python_packages.discover_packages()


def test_rejects_editable_installation(installation):
    base, _, _ = installation
    (base / "sympy-1.2.3.dist-info" / "direct_url.json").write_text(
        json.dumps({"url": "file:///synthetic/source", "dir_info": {"editable": True}})
    )
    with pytest.raises(ValueError, match="editable"):
        python_packages.discover_packages()


def test_numpy_libs_must_belong_to_distribution(installation):
    base, _, _ = installation
    record = base / "numpy-1.2.3.dist-info" / "RECORD"
    record.write_text(record.read_text().replace("numpy.libs/libexample.so,,\n", ""))
    with pytest.raises(ValueError, match="not owned"):
        python_packages.discover_packages()


def test_numpy_installation_without_bundled_libraries_is_supported(installation):
    base, _, _ = installation
    (base / "numpy.libs" / "libexample.so").unlink()
    (base / "numpy.libs").rmdir()
    assert python_packages.discover_packages()[0]["extra_roots"] == []


@pytest.mark.parametrize("change", ["name", "extra_root", "root", "removed", "extra_field"])
def test_validation_rejects_tampered_identity(installation, change):
    base, _, _ = installation
    records = copy.deepcopy(python_packages.discover_packages())
    if change == "name":
        records[0]["name"] = "unapproved"
    elif change == "extra_root":
        records[0]["extra_roots"] = [str(base)]
    elif change == "root":
        records[0]["root"] = str(base)
    elif change == "removed":
        records.pop()
    else:
        records[0]["unexpected"] = True
    with pytest.raises(RuntimeError, match="identity is invalid|changed since executor registration"):
        python_packages.validate_packages(records)
