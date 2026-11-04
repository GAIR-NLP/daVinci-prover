"""Trusted, single-process bootstrap for local Python experiments on Linux.

Invoked by python_local.py using ``python -I -S -B``. No submitted code runs
until Landlock and a seccomp syscall allowlist have both been installed. The
setup pipe is closed before the program starts; stdout/stderr are never a
control channel. Only the standard library and explicitly registered NumPy,
SymPy and mpmath packages are readable; network, subprocesses, threads and
arbitrary package installation remain unavailable.
"""
from __future__ import annotations

import argparse
import collections
import ctypes
import decimal
import errno
import fcntl
import fractions
import functools
import importlib.util
import itertools
import json
import math
import os
from pathlib import Path
import random
import resource
import signal
import stat
import statistics
import sys
import sysconfig
import types


POLICY_VERSION = "landlock-seccomp-python-v2"
MAX_CODE_BYTES = 65_536
FILE_SIZE_BYTES = 16 * 1024 * 1024
MAX_OPEN_FILES = 64
_PACKAGE_NAMES = frozenset({"numpy", "sympy", "mpmath"})
_MATH_ENV = {
    "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1", "MKL_NUM_THREADS": "1",
    "BLIS_NUM_THREADS": "1", "VECLIB_MAXIMUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1",
    "SYMPY_GROUND_TYPES": "python", "MPMATH_NOGMPY": "1",
}

# ABI 1 filesystem rights. REFER and TRUNCATE are added when supported.
_FS_EXECUTE = 1 << 0
_FS_WRITE_FILE = 1 << 1
_FS_READ_FILE = 1 << 2
_FS_READ_DIR = 1 << 3
_FS_REMOVE_DIR = 1 << 4
_FS_REMOVE_FILE = 1 << 5
_FS_MAKE_DIR = 1 << 7
_FS_MAKE_REG = 1 << 8
_FS_REFER = 1 << 13
_FS_TRUNCATE = 1 << 14
_FS_IOCTL_DEV = 1 << 15
_FS_RESOLVE_UNIX = 1 << 16
_SCMP_ALLOW = 0x7FFF0000
_SCMP_ERRNO = 0x00050000 | errno.EPERM
_SCMP_CMP_EQ = 4
_SCMP_CMP_MASKED_EQ = 7


class _Ruleset(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathRule(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class _Comparison(ctypes.Structure):
    _fields_ = [
        ("arg", ctypes.c_uint), ("op", ctypes.c_int),
        ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64),
    ]


class _CapabilityHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapabilityData(ctypes.Structure):
    _fields_ = [
        ("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32),
        ("inheritable", ctypes.c_uint32),
    ]


def _libc() -> ctypes.CDLL:
    lib = ctypes.CDLL(None, use_errno=True)
    lib.syscall.restype = ctypes.c_long
    lib.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                          ctypes.c_ulong, ctypes.c_ulong]
    lib.prctl.restype = ctypes.c_int
    lib.capset.argtypes = [ctypes.POINTER(_CapabilityHeader),
                           ctypes.POINTER(_CapabilityData)]
    lib.capset.restype = ctypes.c_int
    return lib


def _seccomp_library() -> ctypes.CDLL:
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
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


def _syscall(lib: ctypes.CDLL, number: int, *args: object) -> int:
    result = int(lib.syscall(ctypes.c_long(number), *args))
    if result < 0:
        raise OSError(ctypes.get_errno(), "Python sandbox setup syscall failed")
    return result


def _set_privilege_limits(lib: ctypes.CDLL) -> None:
    # Drop all effective, permitted and inheritable capabilities, including for
    # a root-launched evaluator. No subsequent exec is permitted by seccomp.
    header = _CapabilityHeader(0x20080522, 0)
    data = (_CapabilityData * 2)()
    if lib.capset(ctypes.byref(header), data):
        raise OSError(ctypes.get_errno(), "Could not drop Python worker capabilities")
    for option, argument in ((38, 1), (4, 0)):  # NO_NEW_PRIVS, DUMPABLE
        if lib.prctl(option, argument, 0, 0, 0):
            raise OSError(ctypes.get_errno(), "Could not restrict Python worker privileges")
    parent = os.getppid()
    if lib.prctl(1, signal.SIGKILL, 0, 0, 0):  # PARENT_DEATH_SIGNAL
        raise OSError(ctypes.get_errno(), "Could not install Python worker cleanup")
    if os.getppid() != parent:
        raise RuntimeError("Python worker parent exited during startup")


def _runtime_rules() -> dict[Path, int]:
    """Only stdlib contents and already mapped interpreter libraries are readable.

    Granting a stdlib root recursively would also expose site-packages. Instead
    grant each direct stdlib child, skipping package installation directories
    and symlinks. A nested symlink still has to resolve to a permitted target.
    """
    result: dict[Path, int] = {}
    for key in ("stdlib", "platstdlib"):
        root = Path(sysconfig.get_path(key)).resolve(strict=True)
        if root == Path("/") or not root.name.startswith("python"):
            raise RuntimeError("Cannot identify a narrow Python standard-library root")
        result[root] = result.get(root, 0) | _FS_READ_DIR
        for child in root.iterdir():
            if child.name in {"site-packages", "dist-packages"} or child.is_symlink():
                continue
            rights = _FS_READ_FILE | (_FS_READ_DIR if child.is_dir() else 0)
            result[child] = result.get(child, 0) | rights
    for value in sys.path:
        path = Path(value)
        if path.suffix == ".zip" and path.is_file():
            result[path.resolve(strict=True)] = _FS_READ_FILE
    # Common numerical stdlib extensions were imported before isolation. Their
    # shared dependencies need no broad /usr/lib permission or loader cache.
    with open("/proc/self/maps", encoding="utf-8") as handle:
        for line in handle:
            fields = line.rstrip("\n").split(None, 5)
            if len(fields) != 6 or not fields[5].startswith("/"):
                continue
            path = Path(fields[5])
            if not path.is_file():
                raise RuntimeError("Python runtime contains an unavailable mapped library")
            result[path.resolve(strict=True)] = _FS_READ_FILE
    for name in ("/dev/null", "/dev/urandom"):
        path = Path(name).resolve(strict=True)
        result[path] = _FS_READ_FILE | (_FS_WRITE_FILE if name == "/dev/null" else 0)
    return result


class _ApprovedPackages:
    """Locate only registered top-level packages, without adding site-packages.

    Submodules use their package's normal __path__. Nothing runs .pth files or
    searches the experiment directory for top-level modules.
    """

    def __init__(self, roots: dict[str, Path]) -> None:
        self.roots = roots

    def find_spec(self, fullname, path=None, target=None):
        root = self.roots.get(fullname)
        if root is None:
            return None
        return importlib.util.spec_from_file_location(
            fullname, root / "__init__.py", submodule_search_locations=[str(root)],
        )


def _package_rules(raw: str) -> tuple[dict[Path, int], _ApprovedPackages]:
    records = json.loads(raw)
    if not isinstance(records, list) or len(records) != len(_PACKAGE_NAMES):
        raise ValueError("Expected the registered NumPy, SymPy and mpmath packages")
    roots: dict[str, Path] = {}
    rules: dict[Path, int] = {}
    for item in records:
        if not isinstance(item, dict) or item.get("name") not in _PACKAGE_NAMES:
            raise ValueError("Unapproved Python package")
        name = item["name"]
        root = Path(item["root"])
        if (name in roots or not root.is_absolute() or root.is_symlink()
                or root.name != name or not root.is_dir()
                or not (root / "__init__.py").is_file()
                or (root / "__init__.py").is_symlink()):
            raise ValueError("Invalid Python package directory")
        root = root.resolve(strict=True)
        roots[name] = root
        rules[root] = _FS_READ_FILE | _FS_READ_DIR
        extras = item.get("extra_roots", [])
        if not isinstance(extras, list) or len(extras) > 1:
            raise ValueError("Invalid Python package library directories")
        for value in extras:
            extra = Path(value)
            if (name != "numpy" or extra != root.parent / "numpy.libs"
                    or extra.is_symlink() or not extra.is_dir()):
                raise ValueError("Unapproved Python package library directory")
            rules[extra.resolve(strict=True)] = _FS_READ_FILE | _FS_READ_DIR
    return rules, _ApprovedPackages(roots)


def _install_landlock(lib: ctypes.CDLL, seccomp: ctypes.CDLL,
                      workspace: Path, readonly: dict[Path, int]) -> int:
    # Resolve syscall numbers through libseccomp rather than assuming an ABI's
    # hard-coded syscall table. Unsupported architectures fail closed.
    names = (b"landlock_create_ruleset", b"landlock_add_rule", b"landlock_restrict_self")
    create, add, restrict = [int(seccomp.seccomp_syscall_resolve_name(name)) for name in names]
    if min(create, add, restrict) < 0:
        raise RuntimeError("libseccomp cannot resolve Landlock syscalls")
    abi = _syscall(lib, create, ctypes.c_void_p(), ctypes.c_size_t(0), ctypes.c_uint(1))
    if abi < 1:
        raise RuntimeError("Landlock is unavailable")
    handled = (1 << 13) - 1
    if abi >= 2:
        handled |= _FS_REFER
    if abi >= 3:
        handled |= _FS_TRUNCATE
    if abi >= 5:
        handled |= _FS_IOCTL_DEV
    if abi >= 9:
        handled |= _FS_RESOLVE_UNIX
    attributes = _Ruleset(handled)
    ruleset = _syscall(lib, create, ctypes.byref(attributes),
                       ctypes.c_size_t(ctypes.sizeof(attributes)), ctypes.c_uint(0))
    try:
        writable = (_FS_READ_FILE | _FS_READ_DIR | _FS_WRITE_FILE |
                    _FS_REMOVE_FILE | _FS_REMOVE_DIR | _FS_MAKE_DIR | _FS_MAKE_REG)
        if abi >= 3:
            writable |= _FS_TRUNCATE
        rules = dict(readonly)
        rules[workspace] = writable
        for path, rights in rules.items():
            descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rule = _PathRule(rights, descriptor)
                _syscall(lib, add, ctypes.c_int(ruleset), ctypes.c_int(1),
                         ctypes.byref(rule), ctypes.c_uint(0))
            finally:
                os.close(descriptor)
        _syscall(lib, restrict, ctypes.c_int(ruleset), ctypes.c_uint(0))
    finally:
        os.close(ruleset)
    return abi


# Unlisted syscalls return EPERM. In particular there is no network, IPC,
# process/thread creation, exec, ptrace, io_uring, chmod/chown/xattr, mount,
# device ioctl, arbitrary truncate, or ability to raise resource limits.
_ALLOW_SYSCALLS = (
    "read", "write", "readv", "writev", "pread64", "pwrite64", "close", "lseek",
    "fstat", "newfstatat", "stat", "lstat", "statx", "getdents64",
    "mmap", "mprotect", "munmap", "mremap", "madvise", "brk", "futex",
    "clock_gettime", "gettimeofday", "time", "clock_nanosleep", "nanosleep",
    "getrusage", "times", "getpid", "gettid", "getuid", "geteuid", "getgid", "getegid", "uname",
    "rt_sigaction", "rt_sigprocmask", "rt_sigreturn", "sigaltstack", "sched_yield",
    "getrandom", "getcwd", "readlink", "readlinkat", "getrlimit",
    "dup", "dup2", "dup3", "fsync", "fdatasync", "ftruncate",
    "mkdir", "mkdirat", "unlink", "unlinkat", "rmdir",
    "poll", "ppoll", "select", "pselect6", "exit", "exit_group",
)


def _install_seccomp(lib: ctypes.CDLL) -> None:
    context = lib.seccomp_init(_SCMP_ERRNO)
    if not context:
        raise RuntimeError("Could not initialize Python seccomp policy")

    def allow(name: str, *comparisons: _Comparison) -> None:
        number = int(lib.seccomp_syscall_resolve_name(name.encode("ascii")))
        if number < 0:
            # A syscall absent from this architecture remains denied; its
            # architecture's standard alternative is listed separately.
            return
        array = (_Comparison * len(comparisons))(*comparisons)
        if lib.seccomp_rule_add_array(context, _SCMP_ALLOW, number, len(array), array):
            raise RuntimeError(f"Could not restrict Python syscall {name}")

    try:
        for name in _ALLOW_SYSCALLS:
            allow(name)
        # Landlock ABI 1/2 does not mediate truncate. A read-only open with
        # O_TRUNC must therefore be denied even for readable runtime files.
        # Write-capable opens are mediated by Landlock WRITE_FILE. ftruncate
        # requires a writable fd, and no host-file fds survive bootstrap.
        # openat2 is deliberately absent: its pointed-to flags cannot be
        # inspected by a classic seccomp filter.
        for name, argument in (("open", 1), ("openat", 2)):
            allow(name, _Comparison(argument, _SCMP_CMP_MASKED_EQ,
                                     os.O_ACCMODE | os.O_TRUNC, os.O_RDONLY))
            for mode in (os.O_WRONLY, os.O_RDWR):
                allow(name, _Comparison(argument, _SCMP_CMP_MASKED_EQ,
                                         os.O_ACCMODE, mode))
        for command in (fcntl.F_DUPFD, fcntl.F_GETFD, fcntl.F_SETFD,
                        fcntl.F_GETFL, fcntl.F_SETFL, fcntl.F_DUPFD_CLOEXEC):
            allow("fcntl", _Comparison(1, _SCMP_CMP_EQ, command, 0))
        # CPython uses these descriptor-only ioctls for custom file openers.
        # They change close-on-exec flags, never invoke device operations.
        for command in (0x5450, 0x5451):  # FIONCLEX, FIOCLEX on supported Linux ABIs
            allow("ioctl", _Comparison(1, _SCMP_CMP_EQ, command, 0))
        allow("prlimit64", _Comparison(0, _SCMP_CMP_EQ, 0, 0),
              _Comparison(2, _SCMP_CMP_EQ, 0, 0))
        if lib.seccomp_load(context):
            raise RuntimeError("Could not install Python seccomp policy")
    finally:
        lib.seccomp_release(context)


def _close_extra_descriptors(status_fd: int) -> None:
    for item in os.listdir("/proc/self/fd"):
        descriptor = int(item)
        if descriptor in {0, 1, 2, status_fd}:
            continue
        try:
            os.close(descriptor)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise


def _resource_limits(timeout: float, memory_bytes: int) -> None:
    cpu = max(1, math.ceil(timeout))
    for kind, limit in (
        (resource.RLIMIT_AS, memory_bytes),
        (resource.RLIMIT_CPU, cpu),
        (resource.RLIMIT_FSIZE, FILE_SIZE_BYTES),
        (resource.RLIMIT_NOFILE, MAX_OPEN_FILES),
        (resource.RLIMIT_CORE, 0),
    ):
        old_soft, old_hard = resource.getrlimit(kind)
        if old_hard != resource.RLIM_INFINITY:
            limit = min(limit, old_hard)
        resource.setrlimit(kind, (limit, limit))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--memory-bytes", type=int, required=True)
    parser.add_argument("--status-fd", type=int, required=True)
    parser.add_argument("--packages-json", required=True)
    args = parser.parse_args()
    status_fd = args.status_fd
    ready = False
    phase = "validating startup"
    try:
        if (sys.platform != "linux" or os.uname().machine not in {"x86_64", "aarch64"}
                or ctypes.sizeof(ctypes.c_void_p) != 8):
            raise RuntimeError("Python sandbox requires 64-bit Linux x86_64 or aarch64")
        if not sys.flags.isolated or not sys.flags.no_site or not sys.dont_write_bytecode:
            raise RuntimeError("Python sandbox requires an isolated -I -S -B interpreter")
        if status_fd < 3 or not stat.S_ISFIFO(os.fstat(status_fd).st_mode):
            raise ValueError("Python sandbox requires a dedicated setup pipe")
        if any(not stat.S_ISFIFO(os.fstat(fd).st_mode) for fd in (0, 1, 2)):
            raise ValueError("Python sandbox standard streams must be supervisor pipes")
        if not math.isfinite(args.timeout) or args.timeout <= 0 or args.memory_bytes <= 0:
            raise ValueError("Invalid Python sandbox resource limits")
        if args.workspace.is_symlink():
            raise ValueError("Python sandbox workspace must not be a symlink")
        workspace = args.workspace.resolve(strict=True)
        if not workspace.is_dir() or workspace == Path("/"):
            raise ValueError("Invalid Python sandbox workspace")
        source_bytes = sys.stdin.buffer.read(MAX_CODE_BYTES + 1)
        if len(source_bytes) > MAX_CODE_BYTES:
            raise ValueError("Python code exceeds its input limit")
        source = source_bytes.decode("utf-8")
        phase = "loading runtime libraries"
        libc, seccomp = _libc(), _seccomp_library()
        # NumPy wheels may depend on the system C++/GCC runtimes. Load these
        # trusted libraries now so _runtime_rules grants their exact mapped
        # files, without exposing /usr/lib or the dynamic loader cache.
        numeric_libraries = [
            ctypes.CDLL(name)
            for name in ("libstdc++.so.6", "libgcc_s.so.1", "libpthread.so.0", "libz.so.1")
        ]
        phase = "locating the standard library"
        readonly = _runtime_rules()
        package_rules, package_finder = _package_rules(args.packages_json)
        readonly.update(package_rules)
        for runtime_path in readonly:
            if (runtime_path == workspace or runtime_path.is_relative_to(workspace)
                    or (runtime_path.is_dir() and workspace.is_relative_to(runtime_path))):
                raise ValueError("Python workspace overlaps its trusted runtime")
        phase = "configuring the process"
        _close_extra_descriptors(status_fd)
        os.chdir(workspace)
        os.umask(0o077)
        os.environ.clear()
        os.environ.update({"HOME": str(workspace), "TMPDIR": str(workspace),
                           "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PATH": ""})
        os.environ.update(_MATH_ENV)
        _resource_limits(args.timeout, args.memory_bytes)
        phase = "dropping privileges"
        _set_privilege_limits(libc)
        phase = "installing Landlock"
        _install_landlock(libc, seccomp, workspace, readonly)
        phase = "installing seccomp"
        _install_seccomp(seccomp)
        sys.meta_path.insert(0, package_finder)
        phase = "acknowledging startup"
        os.write(status_fd, b"OK\n")
        os.close(status_fd)
        ready = True
    except BaseException as error:
        if not ready:
            try:
                os.write(status_fd, b"ERROR\n")
                os.close(status_fd)
            except OSError:
                pass
        # No user code has executed. Keep the control protocol fixed while
        # making deployment failures (kernel support, libraries, permissions)
        # actionable in the supervisor's captured diagnostics.
        detail = f"Python sandbox failed while {phase}: {type(error).__name__}: {error}"
        try:
            os.write(2, (detail.replace("\n", " ")[:1200] + "\n").encode("utf-8", errors="replace"))
        except OSError:
            pass
        raise SystemExit(125)
    # The old __main__ module contains trusted setup helpers, but no authority
    # survives policy installation. Give user code ordinary script semantics.
    module = types.ModuleType("__main__")
    module.__file__ = "<python_execute>"
    sys.modules["__main__"] = module
    sys.argv = ["<python_execute>"]
    exec(compile(source, "<python_execute>", "exec"), module.__dict__)


if __name__ == "__main__":
    main()
