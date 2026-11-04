"""Persistent, syntax-only Lean command parsing for trusted-context assembly.

The worker loads a fixed complete Mathlib parser environment once. It never
elaborates candidate declarations/proofs, imports candidate modules, or registers
candidate syntax. Fixed Lean scope handlers only track namespaces and activate
the imported scoped grammar selected by ordinary open/open-scoped commands.
Unknown/dynamically introduced syntax fails closed with a parser diagnostic.
"""

from __future__ import annotations

import asyncio
import atexit
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from typing import Any


WORKER_SOURCE = Path(__file__).with_name("resources") / "CommandParserWorker.lean"
_PROTOCOL_VERSION = 1


class LeanCommandParseError(ValueError):
    """The fixed trusted parser could not parse the submitted source."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("Lean command parsing failed: " + "\n".join(errors))


class LeanCommandParserUnavailable(RuntimeError):
    """The owned parser worker failed to start or respond."""


@dataclass(frozen=True)
class LeanCommand:
    # All offsets index the original Python string, including Unicode/CRLF.
    start: int
    end: int
    source: str
    kind: str
    ast: Any
    fingerprint: str
    decl_kind: str | None = None
    decl_name: str | None = None
    decl_name_parts: tuple[str, ...] = ()
    full_name: str | None = None
    namespace: tuple[str, ...] = ()
    signature_start: int | None = None
    signature_end: int | None = None
    value_start: int | None = None
    value_end: int | None = None
    body_start: int | None = None
    body_end: int | None = None
    signature_ast: Any = None
    signature_fingerprint: str | None = None
    scope_kind: str | None = None
    scope_name: str | None = None


def parser_fingerprint() -> str:
    """Fingerprint both sides of the parser protocol for manifests/caches."""
    digest = hashlib.sha256()
    for path in (Path(__file__), WORKER_SOURCE):
        digest.update(path.name.encode("utf-8") + b"\0" + path.read_bytes())
    return digest.hexdigest()


def _ast_fingerprint(ast: Any) -> str:
    encoded = json.dumps(ast, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _render_name(parts: tuple[str, ...]) -> str:
    return ".".join(
        part if part and not any(c.isspace() or c in ".«»" for c in part)
        else f"«{part}»"
        for part in parts
    )


def _identifiers(ast: Any) -> list[tuple[str, ...]]:
    if not isinstance(ast, list) or not ast:
        return []
    if ast[0] == "ident":
        return [tuple(ast[1])]
    if ast[0] == "node":
        return [identifier for child in ast[2] for identifier in _identifiers(child)]
    return []


def _decode_commands(code: str, records: list[dict[str, Any]]) -> list[LeanCommand]:
    # Lean's input context applies String.crlfToLf, then reports UTF-8 byte
    # positions. Map those boundaries to code points in the original text.
    # Skip only the CR of CRLF, exactly as Lean does; a standalone CR remains
    # subject to the normal Lean parser diagnostic. Never replace CR by a
    # space: that would change multiline string literals and syntax identity.
    offsets = {0: 0}
    byte = 0
    for index, character in enumerate(code, 1):
        if character == "\r" and code[index:index + 1] == "\n":
            continue
        byte += len(character.encode("utf-8"))
        offsets[byte] = index

    def position(record: dict[str, Any], name: str) -> int | None:
        value = record.get(name + "_byte")
        if value is None:
            return None
        if value not in offsets:
            raise LeanCommandParserUnavailable(f"Invalid UTF-8 parser offset: {value}")
        return offsets[value]

    scopes: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
    namespace: tuple[str, ...] = ()
    commands = []
    for record in records:
        start, end = position(record, "start"), position(record, "end")
        if start is None or end is None or end < start:
            raise LeanCommandParserUnavailable("Missing or invalid command source range")
        ast = record["ast"]
        kind = record["kind"]
        name_parts = tuple(record.get("decl_name_parts") or ())
        full_parts = (
            name_parts[1:] if name_parts[:1] == ("_root_",)
            else namespace + name_parts
        )
        scope_kind = None
        scope_parts: tuple[str, ...] = ()
        short_kind = kind.rsplit(".", 1)[-1]
        if short_kind in {"namespace", "section", "end", "noncomputableSection"}:
            scope_kind = "section" if short_kind == "noncomputableSection" else short_kind
            identifiers = _identifiers(ast)
            scope_parts = identifiers[0] if identifiers else ()
        signature_ast = record.get("signature_ast")
        commands.append(LeanCommand(
            start=start, end=end, source=code[start:end], kind=kind,
            ast=ast, fingerprint=_ast_fingerprint(ast),
            decl_kind=(
                {"definition": "def"}.get(record["decl_kind"].rsplit(".", 1)[-1], record["decl_kind"].rsplit(".", 1)[-1])
                if record.get("decl_kind") else None
            ),
            decl_name=_render_name(name_parts) if name_parts else None,
            decl_name_parts=name_parts,
            full_name=_render_name(full_parts) if name_parts else None,
            namespace=namespace,
            signature_start=position(record, "signature_start"),
            signature_end=position(record, "signature_end"),
            value_start=position(record, "value_start"),
            value_end=position(record, "value_end"),
            body_start=position(record, "body_start"),
            body_end=position(record, "body_end"),
            signature_ast=signature_ast,
            signature_fingerprint=_ast_fingerprint(signature_ast) if signature_ast is not None else None,
            scope_kind=scope_kind,
            scope_name=_render_name(scope_parts) if scope_parts else None,
        ))
        if scope_kind in {"namespace", "section"}:
            scopes.append((scope_kind, scope_parts, namespace))
            if scope_kind == "namespace":
                namespace = scope_parts[1:] if scope_parts[:1] == ("_root_",) else namespace + scope_parts
        elif scope_kind == "end" and scopes:
            namespace = scopes.pop()[2]
    return commands


class _Worker:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.lock = threading.RLock()
        self.process: subprocess.Popen[bytes] | None = None
        self.stderr: Any = None
        self.buffer = bytearray()

    def close(self) -> None:
        with self.lock:
            process, self.process = self.process, None
            if process is not None:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                        process.wait(timeout=2)
                    except (ProcessLookupError, subprocess.TimeoutExpired):
                        if process.poll() is None:
                            os.killpg(process.pid, signal.SIGKILL)
                            process.wait(timeout=2)
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        stream.close()
            if self.stderr is not None:
                self.stderr.close()
                self.stderr = None
            self.buffer.clear()

    def _diagnostics(self) -> str:
        if self.stderr is None:
            return ""
        self.stderr.seek(0)
        return self.stderr.read(16000).decode("utf-8", errors="replace")

    def _read_response(self, deadline: float) -> dict[str, Any]:
        assert self.process is not None and self.process.stdout is not None
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LeanCommandParserUnavailable("Lean command parser timed out")
            ready, _, _ = select.select([self.process.stdout], [], [], remaining)
            if not ready:
                raise LeanCommandParserUnavailable("Lean command parser timed out")
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                raise LeanCommandParserUnavailable(
                    f"Lean command parser exited ({self.process.poll()}): " + self._diagnostics()
                    + self.buffer.decode("utf-8", errors="replace")
                )
            self.buffer.extend(chunk)
        line, _, tail = self.buffer.partition(b"\n")
        self.buffer = bytearray(tail)
        try:
            response = json.loads(line)
        except (ValueError, UnicodeDecodeError) as error:
            raise LeanCommandParserUnavailable(
                "Invalid Lean command parser response: " + line[:2000].decode("utf-8", errors="replace")
                + "\n" + self._diagnostics()
            ) from error
        if not isinstance(response, dict):
            raise LeanCommandParserUnavailable("Invalid Lean command parser response object")
        return response

    def _ensure_started(self, deadline: float) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        self.close()
        lake = os.environ.get("DAVINCI_LEAN_COMMAND_PARSER_LAKE") or shutil.which("lake")
        if lake is None:
            fallback = Path.home() / ".elan/bin/lake"
            lake = str(fallback) if fallback.is_file() else None
        if lake is None:
            raise LeanCommandParserUnavailable("lake executable unavailable for Lean command parser")
        self.stderr = tempfile.TemporaryFile(mode="w+b")
        try:
            self.process = subprocess.Popen(
                [lake, "env", "lean", "--run", str(WORKER_SOURCE)],
                cwd=self.workspace, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self.stderr, start_new_session=True, bufsize=0,
            )
            ready = self._read_response(deadline)
            if ready.get("ready") is not True or ready.get("protocol") != _PROTOCOL_VERSION:
                raise LeanCommandParserUnavailable(f"Unexpected Lean parser handshake: {ready}")
        except (OSError, LeanCommandParserUnavailable) as error:
            self.close()
            if isinstance(error, LeanCommandParserUnavailable):
                raise
            raise LeanCommandParserUnavailable(str(error)) from error

    def warmup(self, timeout: float) -> None:
        with self.lock:
            self._ensure_started(time.monotonic() + timeout)

    def parse(self, code: str, timeout: float, trusted_syntax: tuple[str, ...], trusted_source: bool) -> list[LeanCommand]:
        with self.lock:
            deadline = time.monotonic() + timeout
            self._ensure_started(deadline)
            assert self.process is not None and self.process.stdin is not None
            request = json.dumps({"source": code, "trusted_syntax": trusted_syntax,
                                  "trusted_source": trusted_source}, ensure_ascii=False).encode("utf-8") + b"\n"
            try:
                # FileIO.write may perform a partial write for large sources.
                view = memoryview(request)
                while view:
                    written = self.process.stdin.write(view)
                    if not written:
                        raise BrokenPipeError("Parser stdin closed")
                    view = view[written:]
                self.process.stdin.flush()
                response = self._read_response(deadline)
            except (OSError, LeanCommandParserUnavailable) as error:
                self.close()
                if isinstance(error, LeanCommandParserUnavailable):
                    raise
                raise LeanCommandParserUnavailable(str(error)) from error
            if response.get("success") is not True:
                raise LeanCommandParseError([str(x) for x in response.get("errors", ["unknown parser error"])])
            return _decode_commands(code, response.get("commands", []))


_WORKERS: dict[Path, _Worker] = {}
_WORKERS_LOCK = threading.Lock()
_CACHE: OrderedDict[tuple[Any, ...], tuple[LeanCommand, ...]] = OrderedDict()
_CACHE_LOCK = threading.Lock()


def _worker(workspace: Path) -> _Worker:
    workspace = workspace.expanduser().resolve()
    with _WORKERS_LOCK:
        return _WORKERS.setdefault(workspace, _Worker(workspace))


def _parse_cached(code: str, workspace: Path, timeout: float, trusted_syntax: tuple[str, ...], trusted_source: bool) -> list[LeanCommand]:
    key = (workspace.expanduser().resolve(), parser_fingerprint(), hashlib.sha256(code.encode("utf-8")).hexdigest(),
           trusted_syntax, trusted_source)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            _CACHE.move_to_end(key)
            return list(cached)
    commands = _worker(workspace).parse(code, timeout, trusted_syntax, trusted_source)
    with _CACHE_LOCK:
        _CACHE[key] = tuple(commands)
        while len(_CACHE) > 256:
            _CACHE.popitem(last=False)
    return commands


async def warmup_parser(target_workspace: Path, timeout: float = 3600) -> None:
    """Load the shared Mathlib syntax environment before starting episode timers."""
    await asyncio.to_thread(_worker(Path(target_workspace)).warmup, timeout)


async def parse_commands(
    code: str, target_workspace: Path, timeout: float = 3600,
    *, trusted_syntax: tuple[str, ...] = (), trusted_source: bool = False,
) -> list[LeanCommand]:
    """Parse commands without elaborating proofs or model-supplied declarations.

    ``trusted_source`` is reserved for immutable dataset source parsing; it
    registers source notation/mixfix syntax while ignoring other elaboration.
    ``trusted_syntax`` must contain only exact notation/mixfix and ordinary
    open commands obtained from that source. Source opens also supply scoped
    grammar when a candidate omits the copied header. Neither parameter may
    be populated from model tool arguments.
    """
    return await asyncio.to_thread(_parse_cached, code, Path(target_workspace), timeout,
                                   tuple(trusted_syntax), trusted_source)


def close_parser_workers() -> None:
    """Stop only parser subprocesses owned by this module."""
    with _WORKERS_LOCK:
        workers = list(_WORKERS.values())
        _WORKERS.clear()
    for worker in workers:
        worker.close()


atexit.register(close_parser_workers)
