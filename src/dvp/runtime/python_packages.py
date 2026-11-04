"""Discover the fixed mathematical package allowlist without importing it.

Only ordinary installed packages with adjacent distribution metadata are
accepted.  These fingerprints identify package entry points and metadata; they
are deliberately not a recursive integrity check of every installed file.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from email.parser import BytesParser
import hashlib
from importlib import metadata
from importlib.machinery import PathFinder
import json
from pathlib import Path, PurePosixPath
import re
import site
import sysconfig
from typing import Any


_PACKAGE_NAMES = ("numpy", "sympy", "mpmath")
_RECORD_KEYS = frozenset({
    "name", "version", "root", "extra_roots", "metadata_path",
    "metadata_sha256", "init_sha256",
})
_INSTALL_HELP = "Install them in this interpreter with: python -m pip install -e '.[python-experiments]'"


def _sha256(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _installation_paths() -> list[str]:
    """Use interpreter installation paths, never sys.path or user-site paths.

    site.getsitepackages() respects an activated virtual environment's policy:
    its base installation appears only when system site packages are enabled.
    """
    candidates = [
        sysconfig.get_path("purelib"), sysconfig.get_path("platlib"),
        *site.getsitepackages(),
    ]
    paths = []
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        if path.is_absolute() and path.is_dir():
            canonical = str(path.resolve(strict=True))
            if canonical not in paths:
                paths.append(canonical)
    return paths


def _installed_distribution(name: str, paths: Sequence[str]) -> metadata.Distribution:
    for install_root in paths:
        matches = [
            distribution for distribution in metadata.distributions(name=name, path=[install_root])
            if _normalized_name((distribution.metadata or {}).get("Name", "")) == name
        ]
        if len(matches) > 1:
            raise ValueError("multiple matching distributions exist in one installation directory")
        if matches:
            distribution = matches[0]
            if Path(distribution.locate_file("")).resolve(strict=True) != Path(install_root):
                raise ValueError("distribution metadata resolved outside its approved installation directory")
            return distribution
    raise metadata.PackageNotFoundError(name)


def _discover_package(name: str, paths: Sequence[str]) -> dict[str, Any]:
    distribution = _installed_distribution(name, paths)
    install_root = Path(distribution.locate_file("")).resolve(strict=True)
    spec = PathFinder.find_spec(name, [str(install_root)])
    if spec is None or spec.origin is None or spec.submodule_search_locations is None:
        raise ValueError("a regular installed package is required (namespace packages are unsupported)")

    origin = Path(spec.origin)
    root = origin.parent
    locations = list(spec.submodule_search_locations)
    if (
        not origin.is_absolute()
        or origin.name != "__init__.py"
        or root.name != name
        or len(locations) != 1
        or Path(locations[0]) != root
    ):
        raise ValueError("the import must resolve to its own named package directory")
    if root.is_symlink() or origin.is_symlink():
        raise ValueError("symlinked package roots or __init__.py files are unsupported")
    root = root.resolve(strict=True)
    origin = origin.resolve(strict=True)
    if not root.is_dir() or not origin.is_file():
        raise ValueError("the package directory and __init__.py must exist")

    files = distribution.files
    if files is None:
        raise ValueError("installed distribution file metadata is required")
    recorded = {PurePosixPath(str(entry)) for entry in files}
    init_entry = PurePosixPath(name, "__init__.py")
    if init_entry not in recorded:
        raise ValueError("the package entry point is absent from installed distribution metadata")
    installed_init = Path(distribution.locate_file(str(init_entry)))
    if (
        installed_init.is_symlink()
        or installed_init.parent.is_symlink()
        or installed_init.resolve(strict=True) != origin
    ):
        raise ValueError("the imported package differs from its installed distribution (editable installs are unsupported)")

    metadata_entries = [
        entry for entry in recorded
        if len(entry.parts) == 2
        and entry.name == "METADATA"
        and entry.parent.name.endswith(".dist-info")
    ]
    if len(metadata_entries) != 1:
        raise ValueError("one adjacent .dist-info/METADATA file is required")
    metadata_entry = metadata_entries[0]
    metadata_path = Path(distribution.locate_file(str(metadata_entry)))
    if metadata_path.is_symlink() or metadata_path.parent.is_symlink():
        raise ValueError("symlinked distribution metadata is unsupported")
    metadata_path = metadata_path.resolve(strict=True)
    if metadata_path.parent.parent != root.parent or not metadata_path.is_file():
        raise ValueError("package and distribution metadata must share an installation directory")
    if root.parent != install_root:
        raise ValueError("the package must be directly inside its installation directory")

    metadata_bytes = metadata_path.read_bytes()
    package_metadata = BytesParser().parsebytes(metadata_bytes)
    if _normalized_name(package_metadata.get("Name", "")) != name:
        raise ValueError("distribution metadata has an unexpected package name")
    version = distribution.version
    if not isinstance(version, str) or not version.strip():
        raise ValueError("distribution metadata has no version")
    if package_metadata.get("Version") != version:
        raise ValueError("distribution metadata has an unexpected package version")
    direct_url = metadata_path.parent / "direct_url.json"
    if direct_url.exists():
        if direct_url.is_symlink():
            raise ValueError("symlinked direct_url.json is unsupported")
        direct_info = json.loads(direct_url.read_text(encoding="utf-8"))
        if not isinstance(direct_info, dict) or direct_info.get("dir_info", {}).get("editable"):
            raise ValueError("editable package installations are unsupported")

    extra_roots = []
    if name == "numpy":
        bundled = root.parent / "numpy.libs"
        if bundled.exists() or bundled.is_symlink():
            if bundled.is_symlink() or not bundled.is_dir():
                raise ValueError("numpy.libs must be a regular sibling directory")
            if not any(len(entry.parts) > 1 and entry.parts[0] == "numpy.libs" for entry in recorded):
                raise ValueError("numpy.libs is not owned by the installed NumPy distribution")
            extra_roots.append(str(bundled.resolve(strict=True)))

    return {
        "name": name,
        "version": version,
        "root": str(root),
        "extra_roots": extra_roots,
        "metadata_path": str(metadata_path),
        "metadata_sha256": hashlib.sha256(metadata_bytes).hexdigest(),
        "init_sha256": _sha256(origin),
    }


def discover_packages() -> list[dict[str, Any]]:
    """Return JSON-compatible identities for NumPy, SymPy and mpmath.

    Discovery uses only the current interpreter's installation directories and
    their distribution metadata, ignoring sys.path/PYTHONPATH and the working
    directory. It never imports the packages, runs package startup code, or
    grants a complete site-packages directory.
    """
    records = []
    paths = _installation_paths()
    for name in _PACKAGE_NAMES:
        try:
            records.append(_discover_package(name, paths))
        except metadata.PackageNotFoundError as error:
            raise ValueError(f"Python experiments require {name}. {_INSTALL_HELP}") from error
        except (OSError, ValueError, TypeError, AttributeError, ImportError) as error:
            raise ValueError(f"Python experiments cannot use {name}: {error}. {_INSTALL_HELP}") from error
    return records


def validate_packages(records: Sequence[Mapping[str, Any]]) -> None:
    """Fail closed if the registered package paths or fingerprints changed."""
    if (
        not isinstance(records, (list, tuple))
        or len(records) != len(_PACKAGE_NAMES)
        or any(not isinstance(record, Mapping) or set(record) != _RECORD_KEYS for record in records)
        or [record["name"] for record in records] != list(_PACKAGE_NAMES)
    ):
        raise RuntimeError("Python package identity is invalid; create a new executor")
    try:
        current = discover_packages()
    except ValueError as error:
        raise RuntimeError(f"Python packages changed since executor registration: {error}") from error
    for previous, actual in zip(records, current):
        if dict(previous) != actual:
            raise RuntimeError(
                f"Python package {actual['name']} changed since executor registration; create a new executor"
            )
