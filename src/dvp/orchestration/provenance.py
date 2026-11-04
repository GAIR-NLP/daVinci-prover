"""Fingerprint the complete installed execution sources and Lean resources."""

from __future__ import annotations

import hashlib
import importlib.metadata
import re
import subprocess
from pathlib import Path
from typing import Any, Mapping, Sequence
from dvp.tasks.dataset import row_target_workspace


def _file_sha256(path: Path) -> str | None:
    """Hash an input file without loading it into the manifest or memory."""

    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _source_git_sha() -> str | None:
    """Return the source checkout revision without making Git a runtime dependency."""

    source_root = Path(__file__).resolve().parent.parent
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=source_root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = result.stdout.strip().lower()
    if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", sha):
        return sha
    return None


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _command_parser_fingerprint() -> str:
    from dvp.verification.command_parser import parser_fingerprint
    return parser_fingerprint()


def _fixed_context_preparation_fingerprint() -> str:
    from dvp.verification.fixed_context import preparation_fingerprint
    return preparation_fingerprint()


def _fixed_context_environments(rows: Sequence[Mapping[str, Any]], fallback: Path | None) -> dict[str, str]:
    from dvp.verification.context_cache import environment_fingerprint
    result = {}
    for row in rows:
        if not row.get("_fixed_task_contract"):
            continue
        target = row_target_workspace(row, fallback)
        if target is None:
            raise ValueError("A target workspace is required for fixed-context verification.")
        key = str(target.expanduser().resolve())
        if key not in result:
            result[key] = row.get("_fixed_context_environment_sha256") or environment_fingerprint(target)
    return result


def _comparator_runtime_identity(
    rows: Sequence[Mapping[str, Any]], fallback: Path | None,
) -> dict[str, dict[str, str | None] | None]:
    """Bind resume to each workspace's installed upstream runtime manifest.

    Runtime availability is checked separately before execution. Recording an
    unavailable runtime as None keeps offline manifest inspection possible and
    still distinguishes it from a subsequently installed runtime.
    """
    from dvp.verification.comparator import runtime_for

    targets = {row_target_workspace(row, fallback) for row in rows}
    if not rows and fallback is not None:
        targets.add(fallback.expanduser().resolve())
    result = {}
    for target in sorted(path for path in targets if path is not None):
        key = str(target)
        try:
            runtime = runtime_for(target)
        except (OSError, RuntimeError, ValueError, KeyError):
            result[key] = None
            continue
        manifest = Path(runtime["manifest"])
        result[key] = {"manifest": str(manifest), "sha256": _file_sha256(manifest)}
    return result


_SOURCE_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_TRUSTED_SOURCE_DIRS = frozenset({
    "verification", "runtime", "toolkits", "llm_clients", "resources",
})


def _source_tree_identity(*, trusted_only: bool = False) -> dict[str, Any]:
    """Hash implementation files, including nested modules and Lean resources.

    Enumerating the package keeps new execution helpers inside the identity by
    default. The module keys are package-relative; moving the installation does
    not itself alter the aggregate, while renames and changed bytes do.
    """

    files: list[dict[str, Any]] = []
    aggregate = hashlib.sha256()
    for path in sorted(_SOURCE_PACKAGE_ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in {".py", ".lean"}:
            continue
        relative = path.relative_to(_SOURCE_PACKAGE_ROOT)
        if trusted_only and relative.parts[0] not in _TRUSTED_SOURCE_DIRS:
            continue
        if path.suffix == ".py":
            parts = list(relative.with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            module = ".".join(("dvp", *parts))
        else:
            module = "dvp.resource:" + relative.as_posix()
        digest = _file_sha256(path)
        files.append({"module": module, "path": str(path.resolve()), "sha256": digest})
        aggregate.update(module.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(str(digest or "missing").encode("ascii"))
        aggregate.update(b"\0")
    return {"sha256": aggregate.hexdigest(), "files": files}


def _agent_source_identity() -> dict[str, Any]:
    return _source_tree_identity()


def _trusted_source_identity() -> dict[str, Any]:
    """Fingerprint the trusted execution sources and Lean resources."""

    return _source_tree_identity(trusted_only=True)
