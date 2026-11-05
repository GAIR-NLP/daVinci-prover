"""Filesystem and syscall restrictions inherited by local Lean subprocesses.

Only trusted launchers call this module, before executing any candidate code.
Landlock restricts file contents, not all pathname metadata such as stat().
"""
from __future__ import annotations

import ctypes
import errno
import os
from pathlib import Path
import re
import resource
import shutil
import subprocess
import sys


POLICY_VERSION = "landlock-seccomp-lean-v1"
_READ_FILE = 1 << 2
_READ_DIR = 1 << 3
_EXECUTE = 1
_WRITE_FILE = 1 << 1
_TRUNCATE = 1 << 14
_BROAD_ROOTS = {
    Path(name) for name in (
        "/", "/tmp", "/var/tmp", "/home", "/root", "/inspire", "/opt",
        "/usr", "/lib", "/lib64", "/proc", "/run", "/dev",
    )
}
_PROGRAM_NAMES = ("which", "git", "env", "sh")
_DENIED = (
    "socket", "socketpair", "connect", "bind", "listen", "accept", "accept4",
    "ptrace", "process_vm_readv", "process_vm_writev", "process_madvise",
    "kcmp", "pidfd_getfd", "bpf", "perf_event_open", "open_by_handle_at",
    "name_to_handle_at", "io_uring_setup", "io_uring_enter", "io_uring_register",
    "truncate", "chmod", "fchmod", "fchmodat", "fchmodat2",
    "chown", "fchown", "lchown", "fchownat", "add_key", "request_key", "keyctl",
)
_NAMESPACE_SYSCALLS = (
    "mount", "umount2", "pivot_root", "unshare", "setns", "fsopen", "fsconfig",
    "fsmount", "move_mount", "open_tree", "mount_setattr",
)


def validate_read_grant(path: Path) -> None:
    """Reject a host-wide filesystem grant in either isolation backend."""
    if path.resolve(strict=True) in _BROAD_ROOTS:
        raise RuntimeError(f"Refusing broad Lean read grant: {path}")


class _Ruleset(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathRule(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class _Comparison(ctypes.Structure):
    _fields_ = [
        ("arg", ctypes.c_uint32), ("op", ctypes.c_uint32),
        ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64),
    ]


def _programs(toolchain: Path) -> tuple[Path, ...]:
    programs = [path for path in (toolchain / "bin").iterdir()
                if path.is_file() and os.access(path, os.X_OK)]
    for name in _PROGRAM_NAMES:
        path = shutil.which(name, path="/usr/bin:/bin")
        if path is None:
            raise RuntimeError(f"Lean isolation requires {name}")
        programs.append(Path(path))
    # `which` is commonly a script with this absolute interpreter path.
    if Path("/bin/sh").is_file():
        programs.append(Path("/bin/sh"))
    return tuple(dict.fromkeys(programs))


def trusted_read_paths(toolchain: Path) -> tuple[Path, ...]:
    """Exact runtime mounts, retaining aliases needed in an empty mount namespace.

    ldd examines only administrator-selected toolchain/system executables. It is
    never run on a candidate file or a path inferred from candidate arguments.
    """
    toolchain = toolchain.resolve(strict=True)
    if toolchain in _BROAD_ROOTS or not (toolchain / "bin/lean").is_file():
        raise RuntimeError("Invalid isolated Lean toolchain")
    programs = _programs(toolchain)
    paths = {toolchain}
    elf_programs = []
    for program in programs:
        paths.update((program, program.resolve(strict=True)))
        with program.open("rb") as handle:
            if handle.read(4) == b"\x7fELF":
                elf_programs.append(program)
    ldd = shutil.which("ldd", path="/usr/bin:/bin")
    if not ldd:
        raise RuntimeError("Lean isolation requires ldd to identify runtime libraries")
    captured = subprocess.run(
        [ldd, *(str(path) for path in elf_programs)],
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30,
    )
    output = captured.stdout + captured.stderr
    if "=> not found" in output:
        raise RuntimeError("A trusted Lean runtime shared library is missing")
    for line in output.splitlines():
        match = re.match(r"\s*(?:\S+\s+=>\s+)?(/\S+)\s+\(0x[0-9a-fA-F]+\)", line)
        if match:
            path = Path(os.path.abspath(match.group(1)))
            paths.update((path, path.resolve(strict=True)))
    for name in ("/etc/ld.so.cache", "/usr/lib/locale/C.utf8", "/usr/lib/locale/C.UTF-8"):
        path = Path(name)
        if path.exists():
            paths.update((path, path.resolve(strict=True)))
    # The toolchain root already grants/mounts its own executables and libraries.
    return tuple(sorted((path for path in paths
                         if path == toolchain or not path.is_relative_to(toolchain)), key=str))


def _seccomp_library() -> ctypes.CDLL:
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint,
        ctypes.POINTER(_Comparison),
    ]
    lib.seccomp_rule_add_array.restype = ctypes.c_int
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_load.restype = ctypes.c_int
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.restype = None
    return lib


def _syscall(libc: ctypes.CDLL, number: int, *args) -> int:
    result = libc.syscall(ctypes.c_long(number), *args)
    if result < 0:
        raise OSError(ctypes.get_errno(), "Lean Landlock setup failed")
    return int(result)


def _landlock(libc: ctypes.CDLL, seccomp: ctypes.CDLL, rules: dict[Path, int]) -> None:
    numbers = [int(seccomp.seccomp_syscall_resolve_name(name)) for name in (
        b"landlock_create_ruleset", b"landlock_add_rule", b"landlock_restrict_self",
    )]
    if any(number < 0 for number in numbers):
        raise RuntimeError("libseccomp cannot resolve Landlock syscalls")
    create, add, restrict = numbers
    abi = _syscall(libc, create, ctypes.c_void_p(), ctypes.c_size_t(0), ctypes.c_uint(1))
    handled = (1 << 13) - 1
    if abi >= 2:
        handled |= 1 << 13
    if abi >= 3:
        handled |= _TRUNCATE
    if abi >= 5:
        handled |= 1 << 15
    attributes = _Ruleset(handled)
    ruleset = _syscall(libc, create, ctypes.byref(attributes),
                       ctypes.c_size_t(ctypes.sizeof(attributes)), ctypes.c_uint(0))
    try:
        for path, rights in rules.items():
            descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = _PathRule(rights & handled, descriptor)
                _syscall(libc, add, ctypes.c_int(ruleset), ctypes.c_int(1),
                         ctypes.byref(rule), ctypes.c_uint(0))
            finally:
                os.close(descriptor)
        _syscall(libc, restrict, ctypes.c_int(ruleset), ctypes.c_uint(0))
    finally:
        os.close(ruleset)


def _seccomp(lib: ctypes.CDLL) -> None:
    required = {
        "openat", "openat2", "clone", "clone3", "socket", "socketpair",
        "ptrace", "process_vm_readv", "process_vm_writev", "pidfd_getfd",
        "open_by_handle_at", "io_uring_setup", "mount", "unshare", "setns",
    }
    if os.uname().machine == "x86_64":
        required.add("open")
    unresolved = sorted(name for name in required
                        if lib.seccomp_syscall_resolve_name(name.encode("ascii")) < 0)
    if unresolved:
        raise RuntimeError(f"libseccomp cannot resolve required Lean syscalls: {', '.join(unresolved)}")
    context = lib.seccomp_init(0x7FFF0000)  # Default allow; Lean needs threads and child processes.
    if not context:
        raise RuntimeError("Could not initialize Lean seccomp policy")

    def deny(name: str, *comparisons: _Comparison, error: int = errno.EPERM) -> None:
        number = lib.seccomp_syscall_resolve_name(name.encode("ascii"))
        if number < 0:
            return  # This syscall does not exist on this architecture.
        array = (_Comparison * len(comparisons))(*comparisons)
        if lib.seccomp_rule_add_array(context, 0x00050000 | error, number, len(array), array):
            raise RuntimeError(f"Could not restrict Lean syscall {name}")

    try:
        for name in _DENIED:
            deny(name)
        # Before Landlock ABI 3, opening a readable file with O_RDONLY|O_TRUNC
        # can truncate it despite lacking a write grant. Require a real writable
        # access mode for truncating opens, including Linux's special mode 3.
        for name, argument in (("open", 1), ("openat", 2)):
            for mode in (os.O_RDONLY, os.O_ACCMODE):
                deny(name, _Comparison(argument, 7, os.O_TRUNC | os.O_ACCMODE,
                                       os.O_TRUNC | mode))
        # openat2 stores flags behind a pointer that seccomp cannot inspect.
        # ftruncate is safe on newly opened writable fds and needed by Lake;
        # inherited fds are closed before this policy is installed.
        deny("openat2", error=errno.ENOSYS)
        for name in _NAMESPACE_SYSCALLS:
            deny(name)
        # clone3's pointed-to flags cannot be inspected by classic seccomp.
        # ENOSYS lets libc use clone for ordinary threads/subprocesses.
        deny("clone3", error=errno.ENOSYS)
        for flag in (0x00020000, 0x02000000, 0x04000000, 0x08000000,
                     0x10000000, 0x20000000, 0x40000000):
            deny("clone", _Comparison(0, 7, flag, flag))
        if lib.seccomp_load(context):
            raise RuntimeError("Could not install Lean seccomp policy")
    finally:
        lib.seccomp_release(context)


def isolate(toolchain: Path, read_only: tuple[Path, ...] = (),
            writable: tuple[Path, ...] = ()) -> None:
    """Restrict the current process; privileges/environment are handled by its launcher.

    Restrictions survive exec and apply to children. Failure is fatal to the
    launcher; it must never execute the candidate after a setup error.
    """
    if (sys.platform != "linux" or os.uname().machine not in {"x86_64", "aarch64"}
            or ctypes.sizeof(ctypes.c_void_p) != 8):
        raise RuntimeError("Lean isolation requires 64-bit Linux x86_64 or aarch64")
    toolchain = toolchain.resolve(strict=True)
    runtime = trusted_read_paths(toolchain)
    runtime_files = {path.resolve(strict=True) for path in runtime if path.is_file()}
    rules: dict[Path, int] = {}
    for path in (*runtime, *read_only):
        path = path.resolve(strict=True)
        validate_read_grant(path)
        rights = _READ_FILE | (_READ_DIR if path.is_dir() else 0)
        # execve also opens the ELF interpreter; its exact shared-library path
        # needs EXECUTE even though it is not a command on PATH.
        if path == toolchain or path in runtime_files:
            rights |= _EXECUTE
        rules[path] = rules.get(path, 0) | rights
    for path in writable:
        path = path.resolve(strict=True)
        if path in _BROAD_ROOTS or not path.is_dir():
            raise RuntimeError(f"Refusing broad or non-directory Lean write grant: {path}")
        if path.is_relative_to(toolchain) or toolchain.is_relative_to(path):
            raise RuntimeError("Lean toolchain must remain read-only")
        if any(trusted == path or trusted.is_relative_to(path) for trusted in runtime):
            raise RuntimeError("Lean runtime files must remain read-only")
        rights = (_READ_FILE | _READ_DIR | _WRITE_FILE | _TRUNCATE |
                  (1 << 4) | (1 << 5) | (1 << 7) | (1 << 8) | (1 << 12))
        rules[path] = rules.get(path, 0) | rights
    for name in ("/dev/null", "/dev/zero", "/dev/urandom", "/dev/random"):
        path = Path(name)
        if path.exists():
            rules[path.resolve(strict=True)] = _READ_FILE | (_WRITE_FILE if name == "/dev/null" else 0)
    seccomp = _seccomp_library()
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    # A previously opened descriptor bypasses pathname-based Landlock checks.
    # Launchers reserve only stdin/stdout/stderr for candidate processes.
    descriptor_limit = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    os.closerange(3, 1_048_576 if descriptor_limit == resource.RLIM_INFINITY else descriptor_limit)
    if libc.prctl(38, 1, 0, 0, 0):
        raise OSError(ctypes.get_errno(), "Could not set Lean no_new_privs")
    _landlock(libc, seccomp, rules)
    _seccomp(seccomp)
