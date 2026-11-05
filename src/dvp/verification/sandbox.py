"""Explicit sandboxed or local execution for Lean and upstream Comparator.

This helper is intentionally a separate process: privilege dropping, environment
clearing, and seccomp must never affect the Python evaluation worker itself.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import os
from pathlib import Path
import shutil
import sys
import tempfile

# This file is also launched directly, including by an isolated Python interpreter.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from lean_isolation import isolate, trusted_read_paths, validate_read_grant
else:
    from .lean_isolation import isolate, trusted_read_paths, validate_read_grant


SANDBOX_UID = 65534
_ENV_ALLOWED = {
    "LEAN_PATH", "LEAN_NUM_THREADS", "LANG", "LC_ALL",
}
_DENIED_SYSCALLS = (
    "socket", "socketpair", "ptrace", "process_vm_readv", "process_vm_writev",
    "process_madvise", "kcmp", "pidfd_getfd", "bpf", "perf_event_open",
    "mount", "umount2", "pivot_root", "unshare", "setns",
    "io_uring_setup", "io_uring_enter", "io_uring_register", "open_by_handle_at",
)


def _seccomp_filter() -> int:
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_rule_add.restype = ctypes.c_int
    lib.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.seccomp_export_bpf.restype = ctypes.c_int
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    ctx = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not ctx:
        raise RuntimeError("seccomp initialization failed")
    fd = os.memfd_create("dvp-seccomp", 0)
    try:
        for name in _DENIED_SYSCALLS:
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if number < 0 or lib.seccomp_rule_add(ctx, 0x00050000 | errno.EPERM, number, 0):
                raise RuntimeError(f"could not restrict syscall {name}")
        if lib.seccomp_export_bpf(ctx, fd):
            raise RuntimeError("seccomp BPF export failed")
        os.lseek(fd, 0, os.SEEK_SET)
        os.set_inheritable(fd, True)
        return fd
    except BaseException:
        os.close(fd)
        raise
    finally:
        lib.seccomp_release(ctx)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolchain", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--writable", type=Path, action="append", default=[])
    parser.add_argument("--read-only", type=Path, action="append", default=[])
    parser.add_argument("--env", action="append", default=[])
    parser.add_argument("--no-sandbox", action="store_true")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")
    status = run_isolated(
        command, toolchain=args.toolchain, cwd=args.cwd,
        read_only=tuple(args.read_only), writable=tuple(args.writable),
        environment=dict(_parse_environment(args.env)), sandboxed=not args.no_sandbox,
    )
    raise SystemExit(status)


def _parse_environment(items: list[str]) -> list[tuple[str, str]]:
    result = []
    for item in items:
        name, separator, value = item.partition("=")
        if not separator or name not in _ENV_ALLOWED:
            raise RuntimeError(f"unsupported sandbox environment variable: {name}")
        result.append((name, value))
    return result


def _validate_grants(cwd: Path, toolchain: Path, read_only: tuple[Path, ...],
                     writable: tuple[Path, ...]) -> None:
    for path in (*read_only, *writable):
        validate_read_grant(path)
    for path in writable:
        if path == cwd or path in cwd.parents or path.is_relative_to(toolchain) or path in toolchain.parents:
            raise RuntimeError(f"unsafe writable sandbox path: {path}")


def run_isolated(command: list[str], *, toolchain: Path, cwd: Path,
                 read_only: tuple[Path, ...] = (), writable: tuple[Path, ...] = (),
                 environment: dict[str, str] | None = None, sandboxed: bool = True,
                 read_cwd: bool = True) -> int:
    """Run one child with private temporary storage and a filesystem allowlist.

    The trusted supervisor retains its identity only to reap the child and clean
    temporary files. All model-controlled Lean executes after isolation setup.
    """
    toolchain, cwd = toolchain.resolve(strict=True), cwd.resolve(strict=True)
    if not (toolchain / "bin/lean").is_file() or not cwd.is_dir():
        raise RuntimeError("invalid Lean toolchain or working directory")
    read_only = tuple(dict.fromkeys(path.resolve(strict=True) for path in read_only))
    if read_cwd:
        read_only = tuple(dict.fromkeys((cwd, *read_only)))
    writable = tuple(path.resolve(strict=True) for path in writable)
    _validate_grants(cwd, toolchain, read_only, writable)
    bwrap = shutil.which("bwrap") if sandboxed else None
    if sandboxed and not bwrap:
        raise RuntimeError("bubblewrap is required; unsandboxed execution is disabled")
    with tempfile.TemporaryDirectory(prefix="dvp-lean-private-") as temporary:
        private = Path(temporary)
        private.chmod(0o700)
        if os.geteuid() == 0:
            os.chown(private, SANDBOX_UID, SANDBOX_UID)
        child_env = {
            "PATH": f"{toolchain}/bin:/usr/bin:/bin", "HOME": str(private), "TMPDIR": str(private),
            "LANG": "C.UTF-8", "LEAN_ABORT_ON_PANIC": "1", "LEAN_NUM_THREADS": "1",
            **(environment or {}),
        }
        # Never inherit a parent's export/temp directory into a candidate child.
        child_env.update(HOME=str(private), TMPDIR=str(private))
        pid = os.fork()
        if pid == 0:
            try:
                # Landlock cannot revoke access through an already-open fd.
                # In particular, never inherit a Comparator export handle.
                for descriptor in os.listdir("/proc/self/fd"):
                    if descriptor.isdigit() and int(descriptor) > 2:
                        try:
                            os.close(int(descriptor))
                        except OSError:
                            pass
                if sandboxed:
                    _exec_bwrap(bwrap, command, toolchain, cwd, read_only, writable, child_env)
                else:
                    _drop_privileges()
                    os.chdir(cwd)
                    isolate(toolchain, read_only, (*writable, private))
                    os.execvpe(command[0], command, child_env)
            except BaseException as exc:
                try:
                    os.write(2, f"DVP_SANDBOX_ERROR: {exc}\n".encode("utf-8", errors="replace"))
                finally:
                    os._exit(125)
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status) if os.WIFEXITED(status) else 128 + os.WTERMSIG(status)


def _exec_bwrap(bwrap: str, command: list[str], toolchain: Path, cwd: Path,
                read_only: tuple[Path, ...], writable: tuple[Path, ...],
                environment: dict[str, str]) -> None:
    # Start from an empty root. Read-only mounting the host / still exposes
    # other tasks' answers, so every accessible host path is listed explicitly.
    invocation = [bwrap, "--tmpfs", "/", "--dev", "/dev", "--tmpfs", "/tmp",
                  "--tmpfs", "/proc", "--dir", "/proc/self",
                  "--symlink", str(toolchain / "bin/lake"), "/proc/self/exe"]
    for pid in range(1, 2049):
        invocation += ["--symlink", "self", f"/proc/{pid}"]
    for path in dict.fromkeys((*trusted_read_paths(toolchain), *read_only)):
        invocation += ["--ro-bind", str(path), str(path)]
    for path in writable:
        invocation += ["--bind", str(path), str(path)]
    # Kernels may request the parent's export directory as cwd, but only their
    # exact export/config files are readable. Creating an empty cwd grants no data.
    invocation += ["--dir", str(cwd), "--chdir", str(cwd), "--remount-ro", "/proc",
                   "--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
                   "--clearenv", "--seccomp", str(_seccomp_filter())]
    environment = {**environment, "HOME": "/tmp", "TMPDIR": "/tmp"}
    for key, value in environment.items():
        invocation += ["--setenv", key, value]
    invocation += ["--", *command]
    _drop_privileges()
    os.execve(bwrap, invocation, {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})


def _drop_privileges() -> None:
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(SANDBOX_UID)
        os.setuid(SANDBOX_UID)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0):  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "could not set no_new_privs")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"DVP_SANDBOX_ERROR: {exc}", file=sys.stderr)
        raise SystemExit(125)
