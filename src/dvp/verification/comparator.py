"""Final acceptance by the upstream Lean comparator, with isolated build trees.

Local elaboration is feedback only. Every completion compares a fresh original
Challenge against a fresh Solution; no bespoke type probe or previous verdict
can substitute for comparator's kernel replay.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import tempfile
import uuid
from typing import Any

from dvp.runtime.paths import REPOSITORY_ROOT
from dvp.verification.lean import LeanVerifier, contains_placeholder
from dvp.verification.modules import CandidateBundle, collect_bundle
from dvp.verification.workspace_dependencies import dependency_path


BACKEND = "leanprover/comparator"
STANDARD_AXIOMS = ["propext", "Quot.sound", "Classical.choice"]
SUCCESS_LINE = "Your solution is okay!"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def runtime_for(workspace: Path) -> dict[str, Any]:
    """Resolve an explicitly installed, version-matched upstream runtime."""
    toolchain = (workspace / "lean-toolchain").read_text().strip()
    version = "4.35.0"
    if toolchain != f"leanprover/lean4:v{version}":
        raise RuntimeError(f"Comparator requires Lean {version}, found: {toolchain}")
    root = Path(os.environ.get("DVP_COMPARATOR_ROOT", REPOSITORY_ROOT / "runtime/comparator")).expanduser()
    manifest = root / version / "runtime.json"
    if not manifest.is_file():
        raise RuntimeError(
            f"Comparator for Lean {version} is not installed ({manifest}). "
            "Run scripts/setup_comparator_435.py; "
            "there is no fallback to the former DVP acceptance checks."
        )
    runtime = json.loads(manifest.read_text())
    if runtime.get("lean_version") != version:
        raise RuntimeError("Comparator runtime and workspace Lean versions differ")
    if runtime.get("kind") != "lake":
        raise RuntimeError("Comparator runtime must use Lean's bundled lake comparator")
    mode = runtime.get("execution_mode", "sandboxed")
    if mode not in {"sandboxed", "unsandboxed"}:
        raise RuntimeError(f"Unknown Comparator execution mode: {mode}")
    runtime["execution_mode"] = mode
    prefix = Path(runtime["toolchain"]).resolve()
    if not (prefix / "bin/lean").is_file() or not (prefix / "bin/lake").is_file():
        raise RuntimeError(f"Missing comparator Lean toolchain: {prefix}")
    tool_names = {"comparator": "lake", "lean4export": "leanexport", "leanchecker": "leanchecker"}
    registered_hashes = runtime.get("sha256")
    executable_keys = ("comparator", "lean4export", "leanchecker")
    if mode == "sandboxed":
        executable_keys += ("bwrap",)
    for key in executable_keys:
        path = Path(runtime[key]).resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise RuntimeError(f"Missing comparator executable {key}: {path}")
        if key in tool_names and path != (prefix / "bin" / tool_names[key]).resolve():
            raise RuntimeError(f"Comparator executable {key} is outside the selected toolchain: {path}")
        expected = registered_hashes.get(key) if isinstance(registered_hashes, dict) else None
        if not isinstance(expected, str) or re.fullmatch(r"[0-9a-fA-F]{64}", expected) is None:
            raise RuntimeError(
                f"Missing or invalid registered SHA256 for {key}. "
                "Rerun scripts/setup_comparator_435.py for the selected toolchain."
            )
        with path.open("rb") as handle:
            actual = hashlib.file_digest(handle, "sha256").hexdigest()
        if actual != expected.lower():
            raise RuntimeError(
                f"Comparator executable SHA256 mismatch for {key}: {path}. "
                "The executable changed since registration; rerun scripts/setup_comparator_435.py."
            )
        runtime[key] = str(path)
    runtime["toolchain"] = str(prefix)
    runtime["manifest"] = str(manifest.resolve())
    return runtime


def sandbox_command(command: list[str], *, workspace: Path, toolchain: Path,
                    writable: tuple[Path, ...] = (), read_only: tuple[Path, ...] = (),
                    environment: dict[str, str] | None = None,
                    sandboxed: bool = True) -> list[str]:
    """Use the installed execution mode for feedback and final acceptance."""
    script = Path(__file__).with_name("sandbox.py")
    args = [sys.executable, str(script), "--toolchain", str(toolchain), "--cwd", str(workspace)]
    if not sandboxed:
        args.append("--no-sandbox")
    for path in writable:
        args += ["--writable", str(path)]
    for path in read_only:
        args += ["--read-only", str(path)]
    for key, value in (environment or {}).items():
        args += ["--env", f"{key}={value}"]
    return [*args, "--", *command]


async def run_process(command: list[str], *, cwd: Path, timeout: float) -> tuple[int, str, str] | None:
    # The helper strips the inherited environment before executing any Lean.
    process = await asyncio.create_subprocess_exec(
        *command, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError) as error:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.communicate()
        if isinstance(error, asyncio.CancelledError):
            raise
        return None
    return process.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")


async def preflight_runtime(workspace: Path, *, timeout: float = 60) -> dict[str, Any]:
    """Check the selected runtime and execution mode before any model request."""
    workspace = Path(workspace).expanduser().resolve()
    runtime = runtime_for(workspace)
    command = sandbox_command(["lean", "--version"], workspace=workspace,
                              toolchain=Path(runtime["toolchain"]),
                              sandboxed=runtime["execution_mode"] == "sandboxed")
    captured = await run_process(command, cwd=workspace, timeout=timeout)
    if captured is None:
        raise RuntimeError(f"Comparator {runtime['execution_mode']} preflight timed out")
    status, stdout, stderr = captured
    if status != 0 or f"version {runtime['lean_version']}" not in stdout:
        raise RuntimeError(f"Comparator {runtime['execution_mode']}/toolchain preflight failed: {stdout}{stderr}")
    return runtime


def lean_search_path(workspace: Path) -> str:
    """Resolve the pinned, already-built workspace without running Lake or Git."""
    manifest = json.loads((workspace / "lake-manifest.json").read_text())
    paths = [workspace / ".lake/build/lib/lean"]
    for package in manifest.get("packages", []):
        name = package["name"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise ValueError("Unsupported dependency package name")
        dependency = dependency_path(workspace, manifest, package)
        paths.append(dependency / ".lake/build/lib/lean")
    if not paths[0].is_dir():
        raise RuntimeError(f"Mathlib is not built: {paths[0]}")
    return os.pathsep.join(str(path) for path in paths)


def write_project(folder: Path, workspace: Path, challenge: str, solution: str,
                  spec: dict[str, Any], *, bundle: CandidateBundle | None = None) -> None:
    """Compile against cached dependencies in a fresh per-attempt build tree."""
    if bundle is not None and bundle.entry_code != solution:
        raise ValueError("Build bundle entry does not match the prepared solution")
    (folder / "Challenge.lean").write_text(challenge, encoding="utf-8")
    (folder / "Solution.lean").write_text(solution, encoding="utf-8")
    modules = bundle.modules if bundle is not None else ()
    for module in modules:
        path = folder / module.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(module.source, encoding="utf-8")
    (folder / "lean-toolchain").write_bytes((workspace / "lean-toolchain").read_bytes())
    # Lake's normal import graph may try to regenerate dependency caches (or
    # ProofWidgets assets). Build the captured auxiliary graph explicitly from
    # source, in dependency order, without exposing it to the original challenge.
    manifest = json.loads((workspace / "lake-manifest.json").read_text())
    lean_path = lean_search_path(workspace)
    lakefile = """import Lake
open Lake DSL
package dvp_comparison

def compileSource (pkg : Package) (name : String) (leanPath : String) : IO System.FilePath := do
  let out := pkg.buildDir / "lib" / "lean" / (name ++ ".olean")
  IO.FS.createDirAll out.parent.get!
  let result ← IO.Process.output {
    cmd := "lean"
    args := #["-o", out.toString, (pkg.dir / (name ++ ".lean")).toString]
    env := #[("LEAN_PATH", some leanPath)]
  }
  IO.print result.stdout
  IO.eprint result.stderr
  unless result.exitCode == 0 do
    throw <| IO.userError s!"Lean compilation failed: {result.exitCode}"
  return out

target Challenge pkg : System.FilePath := do
  let out ← compileSource pkg "Challenge" LEAN_PATH_LITERAL
  return Job.pure out

target Solution pkg : System.FilePath := do
  let leanPath := (pkg.buildDir / "lib" / "lean").toString ++ SEARCH_PATH_SEPARATOR ++ LEAN_PATH_LITERAL
  for name in (LOCAL_MODULES : Array String) do
    let _ ← compileSource pkg name leanPath
  let out ← compileSource pkg "Solution" leanPath
  return Job.pure out
""".replace("LEAN_PATH_LITERAL", json.dumps(lean_path, ensure_ascii=False)).replace(
        "SEARCH_PATH_SEPARATOR", json.dumps(os.pathsep),
    ).replace(
        "LOCAL_MODULES", "#[" + ", ".join(
            json.dumps(module.relative_path.with_suffix("").as_posix(), ensure_ascii=False)
            for module in modules
        ) + "]",
    )
    if manifest.get("name") != "mathlib":
        raise ValueError("Native Comparator requires a built Mathlib root workspace")
    lakefile = lakefile.replace(
        "package dvp_comparison\n",
        "package dvp_comparison\nrequire mathlib from " + json.dumps(str(workspace)) + "\n",
    )
    packages = [{
        "name": "mathlib", "scope": "", "type": "path", "dir": str(workspace),
        "inherited": False, "configFile": "lakefile.lean", "manifestFile": "lake-manifest.json",
    }]
    for package in manifest.get("packages", []):
        dependency = dependency_path(workspace, manifest, package)
        packages.append({
            "name": package["name"], "scope": package.get("scope", ""),
            "type": "path", "dir": str(dependency), "inherited": True,
            "configFile": package["configFile"], "manifestFile": package.get("manifestFile"),
        })
    (folder / "lakefile.lean").write_text(lakefile, encoding="utf-8")
    (folder / "lake-manifest.json").write_text(json.dumps({
        "version": manifest["version"], "packagesDir": ".lake/packages",
        "packages": packages, "name": "dvp_comparison", "lakeDir": ".lake",
    }, indent=2) + "\n")
    (folder / "comparator.json").write_text(json.dumps({
        "challenge_module": "Challenge", "solution_module": "Solution",
        "enable_nanoda": False, **spec,
    }, indent=2) + "\n")
    folder.chmod(0o755)
    for path in folder.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o444)
    build = folder / ".lake"
    build.mkdir(mode=0o755)
    if os.geteuid() == 0:
        os.chown(build, 65534, 65534)


def comparison_command(runtime: dict[str, Any], folder: Path) -> list[str]:
    # The trusted Lake parent runs without the outer sandbox: Lake itself
    # isolates each build/export child. A nested outer sandbox would prevent
    # its namespace setup. The adapter keeps those child sandboxes offline.
    command = [sys.executable, str(Path(__file__).with_name("native_comparator.py")),
               "--toolchain", runtime["toolchain"], "--cwd", str(folder)]
    if runtime.get("execution_mode", "sandboxed") == "unsandboxed":
        return [*command, "--inadvisably-no-sandbox"]
    return [*command, "--bwrap", runtime["bwrap"]]


class ComparatorVerifier:
    def __init__(self, verifier: LeanVerifier, theorem_statement: str, *,
                 task_contract: Any = None, artifact_dir: Path | None = None) -> None:
        self._verifier = verifier
        self.task_contract = task_contract
        self.statement = task_contract.statement if task_contract is not None else theorem_statement
        self.artifact_dir = artifact_dir or verifier.config.scratch_workspace.parent / "comparator_artifacts"

    async def _prepare(self, code: str) -> tuple[str, dict[str, Any]]:
        workspace = self._verifier.config.target_workspace
        timeout = self._verifier.config.timeout
        if self.task_contract is not None:
            # Fixed metadata identifies the original obligations and axioms;
            # it must not rewrite or constrain the candidate's Lean syntax.
            # Comparator checks the complete submitted file against the
            # unchanged original, just as it does for ordinary tasks.
            spec = await self.task_contract.comparator_spec(workspace, timeout=timeout)
            return code, spec
        from dvp.verification.parser_service import parse_commands
        from dvp.verification.contracts import _declarations
        from dvp.verification.lean import mask_lean_comments_and_strings
        declarations = _declarations(self.statement)
        if not declarations:
            raise ValueError("Original task has no named target theorem")
        target = declarations[0].full_name
        commands = await parse_commands(self.statement, workspace, timeout=timeout, trusted_source=True)
        names = [target]
        axioms = list(STANDARD_AXIOMS)
        for command in commands:
            if command.decl_kind == "axiom" and command.full_name:
                axioms.append(command.full_name)
            if contains_placeholder(mask_lean_comments_and_strings(command.source)):
                if command.decl_kind not in {"theorem", "lemma"} or not command.full_name:
                    raise ValueError("Comparator tasks must identify named theorem/lemma proof holes; definition holes are unsupported")
                names.append(command.full_name)
        return code, {"theorem_names": list(dict.fromkeys(names)), "definition_names": [],
                      "permitted_axioms": list(dict.fromkeys(axioms))}

    async def verify(self, code: str, *, bundle: CandidateBundle | None = None,
                     max_output_chars: int = 12_000) -> dict[str, Any]:
        identity = {"backend": BACKEND, "candidate_sha256": digest(code),
                    "original_source_sha256": digest(self.statement)}
        result: dict[str, Any] = {**identity, "success": False, "errors": [], "warnings": [], "output": "", "command": None}
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact = self.artifact_dir / uuid.uuid4().hex
        artifact.mkdir()
        (artifact / "candidate.lean").write_text(code, encoding="utf-8")
        (artifact / "Challenge.lean").write_text(self.statement, encoding="utf-8")
        try:
            if bundle is None:
                bundle = collect_bundle(code, getattr(self._verifier.config, "module_root", None))
            if bundle.entry_code != code:
                raise ValueError("Source bundle entry does not match the submitted candidate")
            result["source_bundle_sha256"] = bundle.sha256
            result["source_bundle_manifest"] = bundle.manifest()
            (artifact / "source_bundle.json").write_text(
                json.dumps(bundle.manifest(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            for module in bundle.modules:
                path = artifact / "modules" / module.relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(module.source, encoding="utf-8")
            workspace = Path(self._verifier.config.target_workspace).resolve()
            runtime = runtime_for(workspace)
            solution, spec = await self._prepare(code)
            (artifact / "Solution.lean").write_text(solution, encoding="utf-8")
            result["solution_sha256"] = digest(solution)
            solution_bundle = CandidateBundle(solution, bundle.modules)
            result["solution_bundle_sha256"] = solution_bundle.sha256
            result["comparator"] = {"runtime": runtime, "specification": spec, "artifact_dir": str(artifact)}
            # Temporary project is deliberately outside model-writable roots and
            # private pytest/run ancestors. No previous .olean is ever reused.
            with tempfile.TemporaryDirectory(prefix="dvp-comparator-") as temporary:
                folder = Path(temporary)
                # Both sides are the exact submitted sources. Fixed metadata
                # supplies obligations, never candidate source reconstruction.
                if solution_bundle.modules:
                    write_project(folder, workspace, self.statement, solution, spec,
                                  bundle=solution_bundle)
                else:
                    write_project(folder, workspace, self.statement, solution, spec)
                command = comparison_command(runtime, folder)
                result["command"] = command
                captured = await run_process(command, cwd=folder, timeout=self._verifier.config.timeout)
                for name in ("comparator.json", "lakefile.lean", "lake-manifest.json", "lean-toolchain"):
                    shutil.copyfile(folder / name, artifact / name)
                if captured is None:
                    result["errors"] = [f"Lean verification timed out after {self._verifier.config.timeout}s (comparator)."]
                else:
                    returncode, stdout, stderr = captured
                    output = (stdout + stderr).strip()
                    (artifact / "comparator.log").write_text(output, encoding="utf-8")
                    result.update(returncode=returncode, output=output[:max_output_chars])
                    result["success"] = returncode == 0 and SUCCESS_LINE in output
                    if returncode:
                        result["errors"] = [output[:max_output_chars] or f"Comparator exited with status {returncode}"]
                        if "DVP_SANDBOX_ERROR:" in output:
                            result["_harness_infrastructure_error"] = True
                    elif not result["success"]:
                        result["errors"] = ["Comparator exited successfully without its acceptance marker."]
                        result["_harness_infrastructure_error"] = True
        except ValueError as error:
            result["errors"] = [str(error)]
        except Exception as error:
            result.update(errors=[f"Comparator unavailable: {type(error).__name__}: {error}"],
                          _harness_infrastructure_error=True)
        (artifact / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result
