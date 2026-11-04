"""Lean verification helpers for Davinci Prover."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

def mask_lean_comments_and_strings(code: str) -> str:
    """Mask Lean comments and string literals while preserving offsets.

    Lean block comments nest.  Keeping newlines and source length stable makes
    the result safe for both policy scans and diagnostics based on source
    ranges.  This is a conservative lexer, not a replacement for Lean's
    parser. Final proof acceptance is handled separately by Comparator.
    """

    chars = list(code)
    masked = list(code)
    index = 0
    block_depth = 0
    line_comment = False
    quote: str | None = None
    escaped = False

    def blank(position: int) -> None:
        if masked[position] not in "\r\n":
            masked[position] = " "

    while index < len(chars):
        current = chars[index]
        following = chars[index + 1] if index + 1 < len(chars) else ""

        if line_comment:
            blank(index)
            if current in "\r\n":
                line_comment = False
            index += 1
            continue

        if block_depth:
            if current == "/" and following == "-":
                blank(index)
                blank(index + 1)
                block_depth += 1
                index += 2
            elif current == "-" and following == "/":
                blank(index)
                blank(index + 1)
                block_depth -= 1
                index += 2
            else:
                blank(index)
                index += 1
            continue

        if quote is not None:
            blank(index)
            if escaped:
                escaped = False
            elif current == "\\":
                escaped = True
            elif current == quote:
                quote = None
            index += 1
            continue

        if current == "-" and following == "-":
            blank(index)
            blank(index + 1)
            line_comment = True
            index += 2
        elif current == "/" and following == "-":
            blank(index)
            blank(index + 1)
            block_depth = 1
            index += 2
        # Apostrophes are valid and common in Lean identifiers (``x'``).
        # A character literal cannot contain a declaration-sized decoy, so
        # only string literals need masking for these source checks.
        elif current == '"':
            blank(index)
            quote = current
            escaped = False
            index += 1
        else:
            index += 1

    return "".join(masked)


def strip_lean_comments(code: str) -> str:
    """Remove common Lean line/block comments, including nested block comments."""
    out: list[str] = []
    i = 0
    depth = 0
    while i < len(code):
        if depth == 0 and code.startswith("--", i):
            newline = code.find("\n", i)
            if newline == -1:
                break
            out.append("\n")
            i = newline + 1
        elif code.startswith("/-", i):
            depth += 1
            i += 2
        elif depth > 0 and code.startswith("-/", i):
            depth -= 1
            i += 2
        elif depth == 0:
            out.append(code[i])
            i += 1
        else:
            i += 1
    return "".join(out)


def contains_placeholder(code: str) -> bool:
    stripped = mask_lean_comments_and_strings(code)
    return re.search(r"\b(sorry|admit)\b", stripped) is not None


_THEOREM_DECL_PATTERN = re.compile(
    r"^\s*"
    r"(?:(?:private|protected|noncomputable|unsafe)\s+)*"
    r"(?:(?:theorem|lemma)\s+[^\s:({]+|example\b)"
)
_PROOF_ASSIGNMENT_PATTERN = re.compile(r":=\s*by\b")


def target_theorem_header(theorem_statement: str) -> str | None:
    """Return the target theorem declaration header, excluding imports and proof body."""
    lines = strip_lean_comments(theorem_statement).splitlines()
    for start, line in enumerate(lines):
        if not _THEOREM_DECL_PATTERN.match(line):
            continue

        declaration = "\n".join(lines[start:]).strip()
        # The theorem type itself may contain ``let x := ...``.  The final
        # ``:= by`` introduces the proof, so selecting the last such marker
        # avoids truncating the required statement at an internal let-binding.
        proof_markers = list(_PROOF_ASSIGNMENT_PATTERN.finditer(declaration))
        if proof_markers:
            return declaration[: proof_markers[-1].start()].strip()

        # This fallback supports declarations whose proof is not a tactic
        # block.  A proof assignment is conventionally the final ``:=`` in a
        # standalone benchmark declaration.
        assignment = declaration.rfind(":=")
        if assignment >= 0:
            return declaration[:assignment].strip()
        return declaration
    return None


@dataclass
class LeanVerificationConfig:
    target_workspace: Path | None
    scratch_workspace: Path
    timeout: float = 60.0
    # Select the frontend that reports hole goals; both modes are development
    # checks, and ordinary compiler warnings do not establish proof acceptance.
    allow_sorry: bool = False
    module_root: Path | None = None


_LOCAL_DIAGNOSTIC_HEADER = re.compile(
    r"^(?:(?:.+?:)?\d+:\d+:[ \t]*)?"
    r"(?P<severity>error|warning|info|information)"
    r"(?:\([^\r\n)]*\))?:[ \t]*",
    re.IGNORECASE,
)
_LOCAL_MESSAGE_LOCATION = re.compile(r"^(?:.+?:)?\d+:\d+:[ \t]*")


def _local_diagnostics(output: str) -> tuple[list[str], list[str]]:
    """Keep each compiler diagnostic together with its context and goals."""
    errors: list[str] = []
    warnings: list[str] = []
    severity: str | None = None
    lines: list[str] = []

    def flush() -> None:
        if severity == "error":
            errors.append("\n".join(lines).rstrip())
        elif severity == "warning":
            warnings.append("\n".join(lines).rstrip())

    for line in output.splitlines():
        header = _LOCAL_DIAGNOSTIC_HEADER.match(line)
        # Information messages (including position-only headers) end the
        # preceding diagnostic, but are retained only in the raw output.
        if header or _LOCAL_MESSAGE_LOCATION.match(line):
            flush()
            severity = header["severity"].lower() if header else None
            lines = [line]
        elif severity in {"error", "warning"}:
            lines.append(line)
    flush()
    return errors, warnings


class LeanVerifier:
    """Runs Lean against code snippets or editable submission files."""

    def __init__(self, config: LeanVerificationConfig) -> None:
        self.config = config

    def _working_dir(self) -> Path:
        return self.config.target_workspace or self.config.scratch_workspace

    def _lean_command(self, file_path: Path) -> list[str]:
        cwd = self._working_dir()
        has_lake = any((cwd / name).exists() for name in ("lakefile.lean", "lakefile.toml"))
        if has_lake and shutil.which("lake"):
            return ["lake", "env", "lean", str(file_path)]
        if shutil.which("lean"):
            return ["lean", str(file_path)]
        raise RuntimeError("Could not find `lake` or `lean` on PATH")

    @staticmethod
    def _format_message(message: dict[str, Any]) -> str:
        pos = message.get("pos") or {}
        line = pos.get("line")
        column = pos.get("column")
        location = f"{line}:{column}: " if line is not None and column is not None else ""
        severity = message.get("severity", "message")
        data = str(message.get("data", ""))
        return f"{location}{severity}: {data}"

    async def verify_code(self, code: str, *, max_output_chars: int = 12000) -> dict[str, Any]:
        temp_name = f".davinci_tmp_{uuid.uuid4().hex}.lean"
        temp_path = self.config.scratch_workspace / temp_name
        temp_path.write_text(code, encoding="utf-8")
        try:
            return await self.verify_file(temp_path, max_output_chars=max_output_chars)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    async def verify_file(self, file_path: Path, *, max_output_chars: int = 12000) -> dict[str, Any]:
        deadline = time.monotonic() + self.config.timeout
        code = file_path.read_text(encoding="utf-8")
        try:
            bundle = self._collect_bundle(code, file_path)
        except ValueError as error:
            return self._module_error(str(error))
        if bundle is not None and bundle.modules:
            return await self._verify_local_file(
                file_path, max_output_chars=max_output_chars, bundle=bundle, deadline=deadline,
            )
        return await self._verify_local_file(file_path, max_output_chars=max_output_chars)

    def _collect_bundle(self, code: str, entry_path: Path) -> Any:
        module_root = getattr(self.config, "module_root", None)
        if module_root is None:
            return None
        from dvp.verification.modules import collect_bundle
        return collect_bundle(
            code, module_root, entry_path=entry_path, allow_entrypoint_import=True,
        )

    @staticmethod
    def _module_error(message: str) -> dict[str, Any]:
        return {"success": False, "errors": [message], "warnings": [], "output": "",
                "command": None, "error_kind": "local_module_error"}

    async def _run_local_command(
        self, command: list[str],
    ) -> tuple[int, str, str] | None:
        """Collect complete output, returning None on timeout.

        Command construction and verification policy belong to the caller.
        Cancellation propagates after the process group is killed and reaped.
        """
        # Drafts and intermediate proofs use the same explicit runtime mode as
        # final acceptance. A sandbox failure never changes that mode.
        with tempfile.TemporaryDirectory(prefix="dvp-feedback-") as temporary:
            folder = Path(temporary)
            folder.chmod(0o755)
            translated = []
            source_paths = {}
            for argument in command:
                path = Path(argument)
                if path.suffix == ".lean" and path.is_file():
                    public = folder / (str(len(source_paths)) + ".lean")
                    shutil.copyfile(path, public)
                    public.chmod(0o444)
                    translated.append(str(public))
                    source_paths[str(public)] = str(path)
                else:
                    translated.append(argument)
            try:
                sandboxed = self._sandbox_command(translated, read_only=(folder,))
            except (OSError, ValueError, RuntimeError, KeyError) as error:
                return 125, "", f"DVP_SANDBOX_ERROR: {error}"
            proc = await asyncio.create_subprocess_exec(
                *sandboxed, cwd=str(self._working_dir()), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=self.config.timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.communicate()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                return None
            stdout, stderr = stdout_b.decode("utf-8", errors="replace"), stderr_b.decode("utf-8", errors="replace")
            for public, original in source_paths.items():
                stdout, stderr = stdout.replace(public, original), stderr.replace(public, original)
            return proc.returncode, stdout, stderr

    def _sandbox_command(
        self, command: list[str], *, read_only: tuple[Path, ...] = (),
        writable: tuple[Path, ...] = (), environment: dict[str, str] | None = None,
    ) -> list[str]:
        from dvp.verification.comparator import lean_search_path, runtime_for, sandbox_command
        workspace = self._working_dir().resolve()
        runtime = runtime_for(workspace)
        sandboxed = runtime.get("execution_mode", "sandboxed") == "sandboxed"
        environment = dict(environment) if environment is not None else None
        if not sandboxed:
            # The shared Mathlib checkout belongs to its installer. Compile
            # against its fixed artifacts directly instead of asking a child
            # with a different uid to resolve/update its Git dependencies.
            if command[:3] == ["lake", "env", "lean"]:
                command = ["lean", *command[3:]]
            environment = {"LEAN_PATH": lean_search_path(workspace), **(environment or {})}
        return sandbox_command(
            command, workspace=workspace, toolchain=Path(runtime["toolchain"]),
            read_only=read_only, writable=writable, environment=environment, sandboxed=sandboxed,
        )

    def _timeout_result(self, command: list[str]) -> dict[str, Any]:
        return {
            "success": False,
            "errors": [f"Lean verification timed out after {self.config.timeout:.1f}s"],
            "warnings": [],
            "output": "",
            "command": command,
        }

    async def _verify_local_file(
        self, file_path: Path, *, max_output_chars: int,
        bundle: Any = None, deadline: float | None = None,
    ) -> dict[str, Any]:
        """Run the installed Lean toolchain without an HTTP service.

        Drafts use Lean's frontend to retain elaborated hole goals. Other
        files use the ordinary compiler. Neither path establishes final proof
        acceptance; that is the separate Comparator's responsibility.
        """
        if getattr(self.config, "module_root", None) is not None:
            deadline = deadline if deadline is not None else time.monotonic() + self.config.timeout
            try:
                bundle = bundle or self._collect_bundle(file_path.read_text(encoding="utf-8"), file_path)
            except ValueError as error:
                return self._module_error(str(error))
            if bundle.modules:
                from dvp.verification.modular_feedback import verify_bundle
                return await verify_bundle(
                    self, bundle, entry_path=file_path, deadline=deadline,
                    max_output_chars=max_output_chars,
                )
        command = self._lean_command(file_path)
        if self.config.allow_sorry:
            helper = Path(__file__).with_name("resources") / "LocalDraftVerifier.lean"
            command = [*command[:-1], "--run", str(helper), str(file_path)]
        captured = await self._run_local_command(command)
        if captured is None:
            return self._timeout_result(command)
        returncode, stdout, stderr = captured

        if "DVP_SANDBOX_ERROR:" in stderr:
            return {"success": False, "errors": [stderr.strip()], "warnings": [],
                    "output": stdout + stderr, "command": command, "returncode": returncode,
                    "_harness_infrastructure_error": True}

        if self.config.allow_sorry:
            return self._local_draft_result(
                stdout, stderr, returncode=returncode, command=command,
                max_output_chars=max_output_chars,
            )
        return self._local_compile_result(
            stdout, stderr, returncode=returncode, command=command,
            max_output_chars=max_output_chars,
        )

    def _local_compile_result(
        self, stdout: str, stderr: str, *, returncode: int,
        command: list[str], max_output_chars: int,
    ) -> dict[str, Any]:
        output = (stdout + stderr).strip()

        errors, warnings = _local_diagnostics(stdout)
        stderr_errors, stderr_warnings = _local_diagnostics(stderr)
        errors.extend(stderr_errors)
        warnings.extend(stderr_warnings)
        if returncode != 0 and not errors and output:
            errors.append(output)
        if len(output) > max_output_chars:
            output = output[:max_output_chars] + "\n...<truncated>..."
        return {
            "success": returncode == 0,
            "returncode": returncode,
            "errors": errors,
            "warnings": warnings,
            "output": output,
            "command": command,
        }

    def _local_draft_result(
        self, stdout: str, stderr: str, *, returncode: int,
        command: list[str], max_output_chars: int,
    ) -> dict[str, Any]:
        """Read the final frontend record after any candidate-generated output."""
        marker = "DVP_LOCAL_DRAFT_JSON:"
        lines = stdout.rstrip().splitlines()
        response: dict[str, Any] | None = None
        if lines and lines[-1].startswith(marker):
            try:
                payload = json.loads(lines[-1][len(marker):])
                if (
                    isinstance(payload, dict)
                    and isinstance(payload.get("messages"), list)
                    and all(isinstance(item, dict) for item in payload["messages"])
                    and isinstance(payload.get("sorries"), list)
                    and all(isinstance(item, dict) for item in payload["sorries"])
                ):
                    response = payload
            except (ValueError, TypeError):
                pass
        if response is None or returncode != 0:
            output = (stdout + stderr).strip()
            return {
                "success": False,
                "returncode": returncode,
                "errors": ["Local Lean draft checker failed to return a valid result."],
                "warnings": [],
                "output": output[:max_output_chars],
                "command": command,
                "_harness_infrastructure_error": True,
            }
        errors = []
        warnings = []
        rendered = []
        for message in response["messages"]:
            text = self._format_message(message)
            rendered.append(text)
            if message.get("severity") == "error":
                errors.append(text)
            elif message.get("severity") == "warning":
                warnings.append(text)
        output = "\n".join([*lines[:-1], *rendered, stderr]).strip()
        if len(output) > max_output_chars:
            output = output[:max_output_chars] + "\n...<truncated>..."
        return {
            "success": not errors,
            "returncode": 1 if errors else 0,
            "errors": errors,
            "warnings": warnings,
            "output": output,
            "command": command,
            "response": response,
        }
