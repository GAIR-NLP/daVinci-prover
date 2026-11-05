"""Launch the trusted Comparator parent; isolate every build/export/check child."""
from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path
import shlex
import sys
import tempfile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolchain", type=Path, required=True)
    parser.add_argument("--bwrap", type=Path)
    parser.add_argument("--inadvisably-no-sandbox", action="store_true")
    parser.add_argument("--cwd", type=Path, required=True)
    args = parser.parse_args()
    toolchain = args.toolchain.resolve(strict=True)
    cwd = args.cwd.resolve(strict=True)
    lake = toolchain / "bin/lake"
    if not os.access(lake, os.X_OK):
        raise RuntimeError("native Comparator executable is missing")
    # The legacy no-sandbox runtime disables namespaces only. It still uses the
    # official child's sandbox hook with mandatory Landlock and seccomp.
    adapter = (Path(__file__).with_name("native_comparator_sandbox.py")
               if args.inadvisably_no_sandbox else args.bwrap)
    if adapter is None or not adapter.is_file():
        raise RuntimeError("native Comparator requires a sandbox adapter")
    adapter = adapter.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="dvp-comparator-private-") as temporary:
        private = Path(temporary)
        private.chmod(0o755)
        exports = private / "exports"
        exports.mkdir(mode=0o700)
        if os.geteuid() == 0:
            os.chown(exports, 65534, 65534)
        invocation = [sys.executable, "-I", "-S", "-B", str(adapter),
                      "--toolchain", str(toolchain), "--project", str(cwd)]
        if args.inadvisably_no_sandbox:
            invocation.append("--no-bwrap")
        wrapper = private / "sandbox-child"
        wrapper.write_text("#!/bin/sh\nexec " + shlex.join([*invocation, "--"]) + ' "$@"\n')
        wrapper.chmod(0o555)
        environment = {
            "PATH": f"{toolchain}/bin:/usr/bin:/bin", "HOME": str(exports), "TMPDIR": str(exports),
            "LANG": "C.UTF-8", "LEAN_ABORT_ON_PANIC": "1", "LEAN_NUM_THREADS": "1",
            "COMPARATOR_BWRAP": str(wrapper),
        }
        command = [str(lake), "comparator", "--config", str(cwd / "comparator.json")]
        pid = os.fork()
        if pid == 0:
            try:
                if os.geteuid() == 0:
                    os.setgroups([])
                    os.setgid(65534)
                    os.setuid(65534)
                libc = ctypes.CDLL(None, use_errno=True)
                if libc.prctl(38, 1, 0, 0, 0):
                    raise OSError(ctypes.get_errno(), "could not set no_new_privs")
                os.chdir(cwd)
                # Only the trusted Lake parent runs here. It executes all
                # potentially untrusted elaboration via sandbox-child.
                os.execve(lake, command, environment)
            except BaseException as exc:
                try:
                    os.write(2, f"DVP_SANDBOX_ERROR: {exc}\n".encode("utf-8", errors="replace"))
                finally:
                    os._exit(125)
        _, status = os.waitpid(pid, 0)
        raise SystemExit(os.waitstatus_to_exitcode(status) if os.WIFEXITED(status)
                         else 128 + os.WTERMSIG(status))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"DVP_SANDBOX_ERROR: {exc}", file=sys.stderr)
        raise SystemExit(125)
