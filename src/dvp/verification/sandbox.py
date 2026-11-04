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


TOOLCHAIN_ALIAS = "/tmp/dvp-toolchain"
SANDBOX_UID = 65534
_ENV_ALLOWED = {
    "LEAN_PATH", "LEAN_NUM_THREADS", "LANG", "LC_ALL",
}
_DENIED_SYSCALLS = (
    "socket", "socketpair", "ptrace", "process_vm_readv", "process_vm_writev",
    "process_madvise", "kcmp", "pidfd_getfd", "bpf", "perf_event_open",
    "mount", "umount2", "pivot_root", "unshare", "setns",
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
    toolchain, cwd = args.toolchain.resolve(strict=True), args.cwd.resolve(strict=True)
    if not (toolchain / "bin/lean").is_file() or not cwd.is_dir():
        raise RuntimeError("invalid Lean toolchain or working directory")
    environment = {
        "PATH": str(toolchain if args.no_sandbox else TOOLCHAIN_ALIAS) + "/bin:/usr/bin:/bin",
        "HOME": "/tmp", "TMPDIR": "/tmp", "LANG": "C.UTF-8",
        "LEAN_ABORT_ON_PANIC": "1", "LEAN_NUM_THREADS": "1",
    }
    for item in args.env:
        name, separator, value = item.partition("=")
        if not separator or name not in _ENV_ALLOWED:
            raise RuntimeError(f"unsupported sandbox environment variable: {name}")
        environment[name] = value
    if args.no_sandbox:
        # An installed runtime selects this mode explicitly. Keep credentials
        # out of the child environment and avoid running Lean as host root.
        _drop_privileges()
        os.chdir(cwd)
        os.execvpe(command[0], command, environment)
        return
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise RuntimeError("bubblewrap is required; unsandboxed execution is disabled")
    writable = [path.resolve(strict=True) for path in args.writable]
    # A task may write its own build or scratch directory only. The root, source
    # project, and dependency tree are never granted broadly writable access.
    for path in writable:
        if path == cwd or path == Path("/") or path == toolchain or path in cwd.parents:
            raise RuntimeError(f"unsafe writable sandbox path: {path}")
    fd = _seccomp_filter()
    invocation = [
        bwrap, "--ro-bind", "/", "/", "--dev", "/dev",
        "--tmpfs", "/tmp", "--tmpfs", "/run", "--tmpfs", "/proc",
        "--dir", "/proc/self", "--symlink", TOOLCHAIN_ALIAS + "/bin/lake", "/proc/self/exe",
    ]
    # This container cannot mount a new procfs. Lean resolves its executable via
    # /proc/<pid>/exe; expose only that symlink, never the host process filesystem.
    for pid in range(1, 2049):
        invocation += ["--symlink", "self", f"/proc/{pid}"]
    invocation += [
        "--remount-ro", "/proc", "--unshare-all", "--die-with-parent", "--new-session",
        "--cap-drop", "ALL", "--ro-bind", str(toolchain), TOOLCHAIN_ALIAS,
        "--ro-bind", str(cwd), str(cwd),
    ]
    for path in args.read_only:
        path = path.resolve(strict=True)
        invocation += ["--ro-bind", str(path), str(path)]
    for path in writable:
        invocation += ["--bind", str(path), str(path)]
    invocation += ["--chdir", str(cwd), "--clearenv", "--seccomp", str(fd)]
    for name, value in environment.items():
        invocation += ["--setenv", name, value.replace(str(toolchain), TOOLCHAIN_ALIAS)]
    command = [part.replace(str(toolchain), TOOLCHAIN_ALIAS) for part in command]
    invocation += ["--", *command]
    # Drop the *host* identity before bubblewrap creates its user namespace.
    # Merely using bubblewrap --uid would still map the host root identity.
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
