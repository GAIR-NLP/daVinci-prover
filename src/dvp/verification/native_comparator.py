"""Launch the trusted native Comparator parent with a clean, unprivileged identity."""
from __future__ import annotations

import argparse
import ctypes
import os
from pathlib import Path
import sys


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
    environment = {
        "PATH": f"{toolchain}/bin:/usr/bin:/bin", "HOME": "/tmp", "TMPDIR": "/tmp",
        "LANG": "C.UTF-8", "LEAN_ABORT_ON_PANIC": "1", "LEAN_NUM_THREADS": "1",
    }
    command = [str(lake), "comparator", "--config", str(cwd / "comparator.json")]
    if args.inadvisably_no_sandbox:
        command.append("--inadvisably-no-sandbox")
    else:
        if args.bwrap is None:
            raise RuntimeError("native Comparator requires a sandbox adapter")
        adapter = args.bwrap.resolve(strict=True)
        if not os.access(adapter, os.X_OK):
            raise RuntimeError("native Comparator sandbox adapter is not executable")
        environment["COMPARATOR_BWRAP"] = str(adapter)
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(65534)
        os.setuid(65534)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0):
        raise OSError(ctypes.get_errno(), "could not set no_new_privs")
    os.chdir(cwd)
    os.execve(lake, command, environment)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"DVP_SANDBOX_ERROR: {exc}", file=sys.stderr)
        raise SystemExit(125)
