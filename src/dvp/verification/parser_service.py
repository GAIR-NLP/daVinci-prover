"""Host-local, process-independent reuse of the owned syntax-only Lean worker.

The transport never evaluates candidate code. Each parse still uses the original
worker's fresh trusted baseline, source syntax checks, AST schema, and offsets.
Client shutdown never terminates the shared daemon. Explicit administration:

    dvp parser warmup --workspace PATH
    dvp parser status --workspace PATH
    dvp parser stop --workspace PATH
"""
from __future__ import annotations

import argparse
import asyncio
import copy
from dataclasses import asdict
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import socketserver
import stat
import struct
import subprocess
import sys
import threading
import time
from typing import Any
import uuid

from dvp.verification.command_parser import (
    LeanCommand,
    LeanCommandParseError,
    LeanCommandParserUnavailable,
    _Worker,
    parser_fingerprint,
)


_PROTOCOL = 1
_MAX_MESSAGE = 64 * 1024 * 1024
_ENVIRONMENTS: dict[Path, dict[str, Any]] = {}
_ENVIRONMENT_LOCK = threading.RLock()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def service_fingerprint() -> str:
    """Transport changes create a new service without changing parser semantics."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _file_identity(path: Path, *, content: bool = True) -> dict[str, Any]:
    path = path.resolve()
    if not path.is_file():
        return {"path": str(path), "missing": True}
    details = path.stat()
    result: dict[str, Any] = {"path": str(path), "size": details.st_size}
    if content:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        result["sha256"] = digest.hexdigest()
    else:
        result.update(mtime_ns=details.st_mtime_ns, ctime_ns=details.st_ctime_ns)
    return result


def _git_identity(root: Path) -> dict[str, Any]:
    head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                          capture_output=True, timeout=30, check=False)
    if head.returncode:
        return {"available": False}
    # Include tracked dirty sources, independently of the committed revision.
    dirty = subprocess.run(
        ["git", "-C", str(root), "diff", "--no-ext-diff", "--binary", "HEAD",
         "--", "*.lean", "lakefile.toml", "lake-manifest.json", "lean-toolchain"],
        capture_output=True, timeout=60, check=False,
    )
    if dirty.returncode:
        raise LeanCommandParserUnavailable("Cannot fingerprint tracked Lean source changes")
    return {"head": head.stdout.decode().strip(),
            "tracked_source_diff_sha256": hashlib.sha256(dirty.stdout).hexdigest()}


def environment_identity(target_workspace: Path, *, refresh: bool = False) -> dict[str, Any]:
    """Stable JSON identity, computed once per workspace per client process.

    Normal Lake builds update the umbrella module artifacts and traces; tracked
    source edits, toolchain changes, manifests and an explicit deployment
    revision also invalidate reuse. Set DAVINCI_LEAN_ENVIRONMENT_REVISION after
    manually replacing leaf build artifacts without rebuilding their umbrella.
    ``refresh=True`` is available to long-lived administrative callers.
    """
    workspace = Path(target_workspace).expanduser().resolve()
    with _ENVIRONMENT_LOCK:
        if not refresh and workspace in _ENVIRONMENTS:
            return copy.deepcopy(_ENVIRONMENTS[workspace])
        lake = os.environ.get("DAVINCI_LEAN_COMMAND_PARSER_LAKE") or shutil.which("lake")
        if not lake:
            fallback = Path.home() / ".elan/bin/lake"
            lake = str(fallback) if fallback.is_file() else None
        if not lake:
            raise LeanCommandParserUnavailable("lake executable unavailable for parser environment identity")
        probe = 'import json,os,shutil; print(json.dumps({"lean":shutil.which("lean"),"lean_path":os.environ.get("LEAN_PATH","")}))'
        try:
            result = subprocess.run([lake, "env", sys.executable, "-c", probe], cwd=workspace,
                                    capture_output=True, text=True, timeout=60, check=False)
            if result.returncode:
                raise LeanCommandParserUnavailable("Cannot resolve the lake parser environment: " + result.stderr[-2000:])
            resolved = json.loads(result.stdout)
            lean = Path(resolved["lean"]).resolve()
            version = subprocess.run([str(lean), "--version"], capture_output=True,
                                     text=True, timeout=30, check=False)
            if version.returncode:
                raise LeanCommandParserUnavailable("Cannot read the actual Lean compiler version")
            roots = [Path(part).resolve() if Path(part).is_absolute() else (workspace / part).resolve()
                     for part in resolved["lean_path"].split(os.pathsep) if part]
            roots.append(lean.parent.parent / "lib/lean")
            roots = list(dict.fromkeys(roots))
            artifacts = []
            packages: dict[str, Any] = {}
            for root in roots:
                if not root.is_dir():
                    artifacts.append({"root": str(root), "missing": True})
                    continue
                files = sorted(p for p in root.iterdir() if p.is_file() and (
                    ".olean" in p.name or p.suffix in {".trace", ".so", ".dylib", ".dll"}))
                artifacts.append({"root": str(root), "files": [
                    _file_identity(p, content=p.suffix not in {".so", ".dylib", ".dll"}) for p in files]})
                if root.parts[-4:] == (".lake", "build", "lib", "lean"):
                    package = root.parents[3]
                    packages[str(package)] = _git_identity(package)
            identity = {
                "schema": "lean-parser-environment-v1",
                "workspace": str(workspace),
                "lean": _file_identity(lean),
                "lean_version": version.stdout.strip(),
                "lake": _file_identity(Path(lake)),
                "lake_override": os.environ.get("DAVINCI_LEAN_COMMAND_PARSER_LAKE", ""),
                "lean_path": resolved["lean_path"],
                "lean_toolchain": _file_identity(workspace / "lean-toolchain"),
                "lake_manifest": _file_identity(workspace / "lake-manifest.json"),
                "packages": packages,
                "umbrella_artifacts": artifacts,
                "deployment_revision": os.environ.get("DAVINCI_LEAN_ENVIRONMENT_REVISION", ""),
            }
        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
            raise LeanCommandParserUnavailable(f"Cannot identify the parser environment: {error}") from error
        _ENVIRONMENTS[workspace] = identity
        return copy.deepcopy(identity)


def _identity(workspace: Path) -> dict[str, Any]:
    return {"protocol": _PROTOCOL, "parser_sha256": parser_fingerprint(),
            "service_sha256": service_fingerprint(),
            "environment": environment_identity(workspace)}


def _private_root() -> Path:
    root = Path("/tmp") / f"davinci-lean-parser-{os.getuid()}"
    root.mkdir(mode=0o700, exist_ok=True)
    details = root.lstat()
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.getuid() or details.st_mode & 0o077:
        raise LeanCommandParserUnavailable(f"Parser socket directory must be owned by this uid with mode 0700: {root}")
    return root


def _paths(identity: dict[str, Any]) -> tuple[Path, Path, Path, Path]:
    key = _digest({"host": socket.gethostname(), "uid": os.getuid(), "identity": identity})[:40]
    root = _private_root()
    return tuple(root / (key + suffix) for suffix in (".sock", ".lock", ".json", ".log"))  # type: ignore[return-value]


def _owned_file(path: Path, expected_type: str) -> None:
    details = path.lstat()
    valid_type = stat.S_ISSOCK(details.st_mode) if expected_type == "socket" else stat.S_ISREG(details.st_mode)
    if not valid_type or details.st_uid != os.getuid():
        raise LeanCommandParserUnavailable(f"Refusing non-owned parser {expected_type}: {path}")


def _check_peer(connection: socket.socket) -> None:
    if hasattr(socket, "SO_PEERCRED"):
        _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if uid != os.getuid():
            raise LeanCommandParserUnavailable("Parser socket peer uid does not match")


def _receive(connection: socket.socket) -> dict[str, Any]:
    chunks = bytearray()
    while b"\n" not in chunks:
        block = connection.recv(65536)
        if not block:
            raise LeanCommandParserUnavailable("Parser service connection closed before responding")
        chunks.extend(block)
        if len(chunks) > _MAX_MESSAGE:
            raise LeanCommandParserUnavailable("Parser service message exceeds the transport limit")
    line, _, trailing = chunks.partition(b"\n")
    if trailing:
        raise LeanCommandParserUnavailable("Parser service accepts one request per connection")
    try:
        result = json.loads(line)
    except (ValueError, UnicodeDecodeError) as error:
        raise LeanCommandParserUnavailable("Invalid parser service JSON") from error
    if not isinstance(result, dict):
        raise LeanCommandParserUnavailable("Invalid parser service response object")
    return result


def _request_existing(path: Path, identity: dict[str, Any], operation: str,
                      deadline: float, **payload: Any) -> dict[str, Any]:
    request_id = uuid.uuid4().hex
    request = {"request_id": request_id, "identity_sha256": _digest(identity),
               "operation": operation, "deadline": deadline, **payload}
    source_sha256 = hashlib.sha256(payload.get("source", "").encode()).hexdigest()
    request["source_sha256"] = source_sha256
    if path.exists():
        _owned_file(path, "socket")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LeanCommandParserUnavailable("Parser service request timed out")
        connection.settimeout(remaining)
        connection.connect(str(path))
        _check_peer(connection)
        connection.sendall((_json(request) + "\n").encode())
        response = _receive(connection)
    if (response.get("request_id") != request_id or response.get("identity_sha256") != _digest(identity)
            or response.get("source_sha256") != source_sha256):
        raise LeanCommandParserUnavailable("Parser service response identity does not match this request")
    if response.get("success") is not True:
        if response.get("error_kind") == "parse":
            raise LeanCommandParseError(response.get("errors", ["Unknown parser error"]))
        raise LeanCommandParserUnavailable(response.get("error", "Parser service unavailable"))
    return response


def _acquire_lock(path: Path, deadline: float) -> int:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        _owned_file(path, "file")
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return descriptor
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LeanCommandParserUnavailable("Timed out waiting for the parser service startup lock")
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    except BaseException:
        os.close(descriptor)
        raise


def _launch(workspace: Path, identity: dict[str, Any], log_path: Path) -> subprocess.Popen[bytes]:
    descriptor = os.open(log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        _owned_file(log_path, "file")
        environment = dict(os.environ)
        from dvp.runtime.paths import PACKAGE_IMPORT_ROOT
        harness = str(PACKAGE_IMPORT_ROOT)
        environment["PYTHONPATH"] = harness + (os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else "")
        return subprocess.Popen(
            [sys.executable, "-m", "dvp.verification.parser_service", "serve",
             "--workspace", str(workspace), "--identity-sha256", _digest(identity)],
            cwd=workspace, env=environment, stdin=subprocess.DEVNULL,
            stdout=descriptor, stderr=descriptor, start_new_session=True, close_fds=True,
        )
    finally:
        os.close(descriptor)


def _ensure_daemon(workspace: Path, identity: dict[str, Any], deadline: float) -> tuple[Path, bool]:
    path, lock_path, _, log_path = _paths(identity)
    try:
        _request_existing(path, identity, "status", deadline)
        return path, False
    except (FileNotFoundError, ConnectionRefusedError):
        pass
    lock = _acquire_lock(lock_path, deadline)
    try:
        try:
            _request_existing(path, identity, "status", deadline)
            return path, False
        except (FileNotFoundError, ConnectionRefusedError):
            if path.exists():
                _owned_file(path, "socket")
                path.unlink()
        process = _launch(workspace, identity, log_path)
        while time.monotonic() < deadline:
            try:
                _request_existing(path, identity, "status", deadline)
                return path, True
            except (FileNotFoundError, ConnectionRefusedError):
                if process.poll() is not None:
                    raise LeanCommandParserUnavailable(f"Parser service exited at startup; see {log_path}")
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        raise LeanCommandParserUnavailable(f"Parser service startup timed out; see {log_path}")
    finally:
        os.close(lock)


def _call(workspace: Path, operation: str, timeout: float, **payload: Any) -> dict[str, Any]:
    workspace = Path(workspace).expanduser().resolve()
    deadline = time.monotonic() + timeout
    try:
        identity = _identity(workspace)
        path, started = _ensure_daemon(workspace, identity, deadline)
        response = _request_existing(path, identity, operation, deadline, **payload)
        response.update(reused=not started, daemon_started=started, socket=str(path))
        return response
    except (OSError, TimeoutError) as error:
        raise LeanCommandParserUnavailable(f"Parser service infrastructure failure: {error}") from error


async def warmup_parser(target_workspace: Path, timeout: float = 3600) -> dict[str, Any]:
    """Warm a new daemon, or check the already-loaded shared worker before LLM work."""
    return await asyncio.to_thread(_call, Path(target_workspace), "warmup", timeout)


async def parse_commands(code: str, target_workspace: Path, timeout: float = 3600,
                         *, trusted_syntax: tuple[str, ...] = (), trusted_source: bool = False) -> list[LeanCommand]:
    """Same AST contract as lean_command_parser.parse_commands, shared across processes."""
    response = await asyncio.to_thread(_call, Path(target_workspace), "parse", timeout,
                                      source=code, trusted_syntax=list(trusted_syntax), trusted_source=trusted_source)
    result = []
    for item in response["commands"]:
        item["decl_name_parts"] = tuple(item.get("decl_name_parts", ()))
        item["namespace"] = tuple(item.get("namespace", ()))
        command = LeanCommand(**item)
        if command.source != code[command.start:command.end]:
            raise LeanCommandParserUnavailable("Parser service returned inconsistent original source spans")
        result.append(command)
    return result


class _Handler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: _ParserServer = self.server  # type: ignore[assignment]
        response: dict[str, Any] = {"identity_sha256": server.identity_sha256, "success": False}
        try:
            _check_peer(self.request)
            self.request.settimeout(30)
            request = _receive(self.request)
            response.update(request_id=request.get("request_id"), source_sha256=request.get("source_sha256"))
            if request.get("identity_sha256") != server.identity_sha256:
                raise LeanCommandParserUnavailable("Parser service is running a different frozen environment")
            operation = request.get("operation")
            if operation not in {"status", "warmup", "parse", "stop"}:
                raise LeanCommandParserUnavailable("Unsupported parser service operation")
            if operation == "status":
                response.update(server.status())
            elif operation == "stop":
                response["stopping"] = True
            else:
                deadline = float(request["deadline"])
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not server.operation_lock.acquire(timeout=remaining):
                    raise LeanCommandParserUnavailable("Parser service request expired while waiting for the worker")
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LeanCommandParserUnavailable("Parser service request expired before execution")
                    process = getattr(server.worker, "process", None)
                    worker_was_ready = server.warmed and process is not None and process.poll() is None
                    if not worker_was_ready:
                        server.warmed = False
                    if operation == "warmup":
                        server.worker.warmup(remaining)
                    else:
                        source = request.get("source")
                        syntax = request.get("trusted_syntax", [])
                        if not isinstance(source, str) or not isinstance(syntax, list) or not all(isinstance(x, str) for x in syntax):
                            raise LeanCommandParserUnavailable("Invalid parser service source/syntax payload")
                        if hashlib.sha256(source.encode()).hexdigest() != request.get("source_sha256"):
                            raise LeanCommandParserUnavailable("Parser service source digest mismatch")
                        commands = server.worker.parse(source, remaining, tuple(syntax), request.get("trusted_source") is True)
                        response["commands"] = [asdict(command) for command in commands]
                    server.warmed = True
                    if operation == "warmup":
                        response.update(server.status(), worker_reused=worker_was_ready)
                finally:
                    server.operation_lock.release()
            response["success"] = True
        except LeanCommandParseError as error:
            response.update(error_kind="parse", errors=error.errors)
        except Exception as error:
            response.update(error_kind="infrastructure", error=str(error))
        try:
            self.request.sendall((_json(response) + "\n").encode())
        except OSError:
            pass  # A timed-out client owns a different connection from the next request.
        if response.get("stopping"):
            threading.Thread(target=server.shutdown, daemon=True).start()


class _ParserServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 128

    def __init__(self, path: Path, identity: dict[str, Any], worker: Any):
        self.identity = copy.deepcopy(identity)
        self.identity_sha256 = _digest(identity)
        self.worker = worker
        self.operation_lock = threading.Lock()
        self.warmed = False
        self.started_utc = datetime.now(timezone.utc).isoformat()
        super().__init__(str(path), _Handler)
        os.chmod(path, 0o600)

    def status(self) -> dict[str, Any]:
        process = getattr(self.worker, "process", None)
        return {"pid": os.getpid(), "started_utc": self.started_utc,
                "warmed": self.warmed and process is not None and process.poll() is None,
                "identity": self.identity}


def _serve(workspace: Path, expected_identity_sha256: str | None = None) -> None:
    identity = _identity(workspace)  # Freeze this snapshot before loading Lean.
    if expected_identity_sha256 and _digest(identity) != expected_identity_sha256:
        raise LeanCommandParserUnavailable("Parser environment changed during daemon startup")
    path, _, metadata, _ = _paths(identity)
    worker = _Worker(workspace)
    server = _ParserServer(path, identity, worker)
    details = path.lstat()
    socket_inode = details.st_ino
    descriptor = os.open(metadata, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(server.status(), handle, ensure_ascii=False)
    def interrupted(_signal: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
        process = worker.process
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        worker.close()
        try:
            if path.lstat().st_ino == socket_inode:
                path.unlink()
                metadata.unlink(missing_ok=True)
        except FileNotFoundError:
            pass


def service_status(target_workspace: Path, *, timeout: float = 10) -> dict[str, Any]:
    """Inspect this exact service identity without starting it."""
    identity = _identity(Path(target_workspace).expanduser().resolve())
    path, _, _, log = _paths(identity)
    try:
        response = _request_existing(path, identity, "status", time.monotonic() + timeout)
        return {"running": True, "socket": str(path), "log": str(log), **response}
    except (FileNotFoundError, ConnectionRefusedError):
        return {"running": False, "socket": str(path), "log": str(log), "identity": identity}


def stop_service(target_workspace: Path, *, timeout: float = 30) -> dict[str, Any]:
    """Stop only the daemon for this uid/host/workspace and exact current identity."""
    identity = _identity(Path(target_workspace).expanduser().resolve())
    path, lock_path, _, _ = _paths(identity)
    deadline = time.monotonic() + timeout
    lock = _acquire_lock(lock_path, deadline)
    try:
        try:
            return _request_existing(path, identity, "stop", deadline)
        except (FileNotFoundError, ConnectionRefusedError):
            return {"running": False}
    finally:
        os.close(lock)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("warmup", "status", "stop", "serve"))
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--timeout", default=3600, type=float)
    parser.add_argument("--identity-sha256", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    workspace = args.workspace.expanduser().resolve()
    if args.operation == "serve":
        _serve(workspace, args.identity_sha256)
    elif args.operation == "warmup":
        print(_json(asyncio.run(warmup_parser(workspace, args.timeout))))
    elif args.operation == "status":
        print(_json(service_status(workspace, timeout=args.timeout)))
    else:
        print(_json(stop_service(workspace, timeout=args.timeout)))


if __name__ == "__main__":
    main()
