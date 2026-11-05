"""Resolve existing Lake dependency locations without materializing packages.

The manifest records more than a package name: path dependencies may live outside
``packagesDir``, and a Git package may occupy a subdirectory of its checkout.
These rules mirror Lake's ``PackageEntry.materialize`` location selection. They
do not search for similarly named caches or substitute a different checkout.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import re
from typing import Any


def dependency_roots(workspace: Path) -> tuple[Path, ...]:
    """Read only the pinned dependency locations, never their shared parent."""
    manifest_path = workspace / "lake-manifest.json"
    if not manifest_path.is_file():
        return ()
    manifest = json.loads(manifest_path.read_text())
    roots = []
    for package in manifest.get("packages", []):
        root = dependency_path(workspace, manifest, package)
        if root == Path("/") or root in workspace.resolve().parents:
            raise ValueError(f"Unsafe dependency read grant: {root}")
        if not root.is_dir():
            raise ValueError(f"Missing pinned dependency: {root}")
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def _path_value(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError(f"Invalid Lake dependency {field}")
    return Path(value)


def dependency_path(workspace: Path, manifest: Mapping[str, Any],
                    package: Mapping[str, Any]) -> Path:
    """Return the dependency directory declared by an existing Lake manifest.

    Relative ``dir`` values are relative to the workspace containing the
    manifest. With ``copy: true``, Lake uses the already materialized copy under
    ``packagesDir``. Git dependencies use ``packagesDir/name/subDir``. Missing
    package types retain the conventional layout used by older local fixtures.

    No build, checkout, network request, or fallback search is performed. A
    missing declared location must be repaired explicitly by the workspace
    owner; a neighbouring checkout is not evidence of dependency identity.
    """
    name = package.get("name")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError("Unsupported dependency package name")

    packages_value = manifest.get("packagesDir")
    packages_dir = _path_value(".lake/packages" if packages_value is None else packages_value,
                               "packagesDir")
    materialized = workspace / packages_dir / name
    kind = package.get("type", "git")
    if kind == "path":
        source = _path_value(package.get("dir"), "dir")
        copy = package.get("copy", False)
        if not isinstance(copy, bool):
            raise ValueError("Invalid Lake dependency copy flag")
        return (materialized if copy else workspace / source).resolve()
    if kind != "git":
        raise ValueError(f"Unsupported Lake dependency type: {kind!r}")

    subdir = package.get("subDir")
    if subdir is not None:
        relative = _path_value(subdir, "subDir")
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Lake dependency subDir must stay inside its checkout")
        materialized /= relative
    return materialized.resolve()
