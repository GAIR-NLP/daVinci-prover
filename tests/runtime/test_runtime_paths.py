"""Project assets remain discoverable without an old compatibility package."""

from pathlib import Path

from dvp.runtime import paths


ROOT = Path(__file__).resolve().parents[2]


def test_source_checkout_is_found_from_an_unrelated_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("DVP_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)

    assert paths.project_root() == ROOT


def test_explicit_project_root_takes_precedence(monkeypatch, tmp_path):
    selected = tmp_path / "selected"
    selected.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DVP_PROJECT_ROOT", "selected")

    assert paths.project_root() == selected


def test_installed_package_finds_checkout_from_a_subdirectory(monkeypatch, tmp_path):
    checkout = tmp_path / "checkout"
    package = checkout / "src" / "dvp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (checkout / "pyproject.toml").write_text('[project]\nname = "dvp"\n')
    (checkout / "inputs").mkdir()
    working_dir = checkout / "analysis" / "nested"
    working_dir.mkdir(parents=True)
    installed = tmp_path / "site-packages" / "dvp" / "runtime" / "paths.py"
    monkeypatch.setattr(paths, "__file__", str(installed))
    monkeypatch.delenv("DVP_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(working_dir)

    assert paths.project_root() == checkout


def test_installed_package_outside_a_checkout_uses_current_directory(monkeypatch, tmp_path):
    installed = tmp_path / "site-packages" / "dvp" / "runtime" / "paths.py"
    working_dir = tmp_path / "work"
    working_dir.mkdir()
    monkeypatch.setattr(paths, "__file__", str(installed))
    monkeypatch.delenv("DVP_PROJECT_ROOT", raising=False)
    monkeypatch.chdir(working_dir)

    assert paths.project_root() == working_dir
