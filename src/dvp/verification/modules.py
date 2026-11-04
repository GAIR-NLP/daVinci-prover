"""Immutable source snapshots for workspace-local Lean module dependencies.

Only Submission.* resolves from editable files by default. Development probes
may explicitly include the saved Submission entry as a dependency. Library imports keep
using the pinned runtime. No candidate build configuration or compiled artifact
is read here; consumers compile these exact source strings in a fresh tree.
"""
from __future__ import annotations

from dataclasses import InitVar, dataclass, field
import hashlib
import json
from pathlib import Path
import re


_PART = r"(?:«[^»\r\n]*»|[^\W\d][\w']*)"
_IDENT = rf"{_PART}(?:\.{_PART})*"
_IMPORT = re.compile(rf"\s*(?:public\s+)?(?:meta\s+)?import\s+(?:all\s+)?({_IDENT})")
_LOCAL_PART = re.compile(r"[^\W\d][\w']*\Z")
LOCAL_ROOTS = frozenset({"Submission"})


def _sha256(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def source_imports(code: str) -> tuple[str, ...]:
    """Read the ordinary Lean header, including multiline and public imports.

    This only discovers source dependencies; Lean remains the syntax authority.
    Unsupported/malformed headers fail during compilation against a clean path,
    never by falling back to an old local .olean.
    """
    from dvp.verification.lean import mask_lean_comments_and_strings
    text = mask_lean_comments_and_strings(code)
    position = 0
    for keyword in ("module", "prelude"):
        match = re.compile(rf"\s*{keyword}\b").match(text, position)
        if match:
            position = match.end()
    names = []
    while match := _IMPORT.match(text, position):
        parts = re.findall(_PART, match[1])
        parts = [part[1:-1] if part.startswith("«") else part for part in parts]
        # Escaped separators must not be interpreted as filesystem components.
        if parts and parts[0] in LOCAL_ROOTS and any(not _LOCAL_PART.fullmatch(p) for p in parts):
            raise ValueError(f"Unsupported local Lean module name: {match[1]}")
        names.append(".".join(parts))
        position = match.end()
    return tuple(names)


def is_local_module(name: str) -> bool:
    return name.split(".", 1)[0] in LOCAL_ROOTS


def local_imports(code: str, *, allow_entrypoint_import: bool = False) -> tuple[str, ...]:
    names = source_imports(code)
    for name in names:
        if name == "Submission" and not allow_entrypoint_import:
            raise ValueError("Submission.lean is the entry file and cannot be imported as a local helper")
    return tuple(name for name in names if is_local_module(name))


def _validate_name(name: str, *, allow_entrypoint: bool = False) -> None:
    if allow_entrypoint and name == "Submission":
        return
    parts = name.split(".")
    if len(parts) < 2 or parts[0] not in LOCAL_ROOTS or not all(_LOCAL_PART.fullmatch(p) for p in parts):
        raise ValueError(f"Local modules must be named Submission.<Name>: {name}")


@dataclass(frozen=True)
class LocalModule:
    name: str
    source: str
    allow_entrypoint: InitVar[bool] = False

    def __post_init__(self, allow_entrypoint: bool) -> None:
        _validate_name(self.name, allow_entrypoint=allow_entrypoint)
        if not isinstance(self.source, str):
            raise ValueError("Local module source must be text")

    @property
    def relative_path(self) -> Path:
        return Path(*self.name.split(".")).with_suffix(".lean")

    @property
    def sha256(self) -> str:
        return _sha256(self.source)


@dataclass(frozen=True)
class CandidateBundle:
    entry_code: str
    modules: tuple[LocalModule, ...] = ()
    # Development inspection only. Final source collection/reassembly uses the
    # default, which rejects any imported Submission entry.
    allow_entrypoint_import: bool = field(default=False, kw_only=True)

    def __post_init__(self) -> None:
        if not isinstance(self.entry_code, str) or not isinstance(self.modules, tuple):
            raise ValueError("Source bundles require text and an immutable module tuple")
        available: set[str] = set()
        for module in self.modules:
            if not isinstance(module, LocalModule) or module.name in available:
                raise ValueError("Source bundle contains an invalid or duplicate module")
            _validate_name(module.name, allow_entrypoint=self.allow_entrypoint_import)
            for dependency in local_imports(
                module.source, allow_entrypoint_import=self.allow_entrypoint_import,
            ):
                if dependency not in available:
                    raise ValueError(f"Missing, cyclic, or unordered local dependency: {module.name} imports {dependency}")
            available.add(module.name)
        wanted = set(local_imports(
            self.entry_code, allow_entrypoint_import=self.allow_entrypoint_import,
        ))
        by_name = {module.name: module for module in self.modules}
        pending = list(wanted)
        while pending:
            name = pending.pop()
            if name not in available:
                raise ValueError(f"Missing local module source: {name}")
            for dependency in local_imports(
                by_name[name].source, allow_entrypoint_import=self.allow_entrypoint_import,
            ):
                if dependency not in wanted:
                    wanted.add(dependency)
                    pending.append(dependency)
        if wanted != available:
            raise ValueError("Source bundle contains modules outside the entry's dependency closure")

    def _manifest_content(self) -> dict:
        return {
            "schema": "dvp.source-bundle.v1",
            **({"allow_entrypoint_import": True} if self.allow_entrypoint_import else {}),
            "entry_sha256": _sha256(self.entry_code),
            "modules": [
                {"name": module.name, "path": module.relative_path.as_posix(), "sha256": module.sha256}
                for module in sorted(self.modules, key=lambda item: item.name)
            ],
        }

    @property
    def sha256(self) -> str:
        return _sha256(json.dumps(self._manifest_content(), sort_keys=True, ensure_ascii=False, separators=(",", ":")))

    def manifest(self) -> dict:
        return {**self._manifest_content(), "sha256": self.sha256}


def collect_bundle(
    code: str, module_root: Path | None, *, entry_path: Path | None = None,
    allow_entrypoint_import: bool = False,
) -> CandidateBundle:
    """Freeze source dependencies, allowing root imports only for development.

    A probe can inspect the saved entry and its helpers through ``import
    Submission``. The probe remains the bundle entry; the saved entry becomes
    a separately compiled dependency. Default/final collection never permits
    this, and neither mode permits self-imports or dependency cycles.
    """
    imports = local_imports(code, allow_entrypoint_import=allow_entrypoint_import)
    if not imports:
        return CandidateBundle(code)
    if module_root is None:
        raise ValueError("Workspace-local module imports require a configured module root")
    root = Path(module_root).resolve()
    active: list[str] = []
    if entry_path is not None:
        try:
            relative = Path(entry_path).resolve().relative_to(root)
        except ValueError:
            pass  # Canonical fixed-context files are outside the editable root.
        else:
            name = ".".join(relative.with_suffix("").parts)
            if relative.suffix == ".lean" and is_local_module(name):
                active.append(name)
    complete: set[str] = set()
    modules: list[LocalModule] = []

    def visit(name: str) -> None:
        _validate_name(name, allow_entrypoint=allow_entrypoint_import)
        if name in active:
            raise ValueError("Local Lean import cycle: " + " -> ".join([*active, name]))
        if name in complete:
            return
        relative = Path(*name.split(".")).with_suffix(".lean")
        path = root
        for part in relative.parts:
            path = path / part
            if path.is_symlink():
                raise ValueError(f"Local module sources cannot be symlinks: {relative}")
        if not path.resolve().is_relative_to(root):
            raise ValueError(f"Local module escapes the editable workspace: {name}")
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ValueError(f"Cannot read local module {name} ({relative}): {error}") from error
        active.append(name)
        for dependency in local_imports(source, allow_entrypoint_import=allow_entrypoint_import):
            visit(dependency)
        active.pop()
        complete.add(name)
        modules.append(LocalModule(name, source, allow_entrypoint=allow_entrypoint_import))

    for name in imports:
        visit(name)
    return CandidateBundle(code, tuple(modules), allow_entrypoint_import="Submission" in complete)
