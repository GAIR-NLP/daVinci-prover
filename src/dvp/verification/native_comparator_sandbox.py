#!/usr/bin/python3
"""Adapt official Lake bubblewrap invocations to this container's empty procfs.

Real bubblewrap still isolates every child. Dependencies are already cached, so
even Lake's workspace-resolution phase stays offline. The seccomp filter also
blocks access to host process memory and further namespace creation.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys

from sandbox import _seccomp_filter


def main() -> None:
    bwrap = shutil.which("bwrap", path="/usr/bin:/bin")
    # Lake intentionally invokes this adapter through `env -i`. The selected
    # toolchain's PATH arrives as a bubblewrap argument, not in our environment.
    incoming = sys.argv[1:]
    child_path = ""
    for position in range(len(incoming) - 2):
        if incoming[position:position + 2] == ["--setenv", "PATH"]:
            child_path = incoming[position + 2]
    lake = shutil.which("lake", path=child_path)
    if not bwrap or not lake:
        raise RuntimeError("bubblewrap and the selected Lake executable are required")
    lake = str(Path(lake).resolve(strict=True))
    outgoing = []
    index = 0
    found_proc = False
    while index < len(incoming):
        item = incoming[index]
        if item == "--":
            outgoing.extend(incoming[index:])
            break
        if item == "--share-net":
            index += 1
            continue
        if item == "--proc":
            if incoming[index + 1] != "/proc":
                raise RuntimeError("unexpected proc mount")
            outgoing += ["--tmpfs", "/proc", "--dir", "/proc/self",
                         "--symlink", lake, "/proc/self/exe"]
            for pid in range(1, 2049):
                outgoing += ["--symlink", "self", f"/proc/{pid}"]
            outgoing += ["--remount-ro", "/proc"]
            found_proc = True
            index += 2
            continue
        outgoing.append(item)
        index += 1
    if not found_proc or "--unshare-all" not in outgoing or "--clearenv" not in outgoing:
        raise RuntimeError("unexpected native Comparator sandbox invocation")
    fd = _seccomp_filter()
    os.execve(bwrap, [bwrap, "--cap-drop", "ALL", "--seccomp", str(fd), *outgoing],
              {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, IndexError) as exc:
        print(f"DVP_SANDBOX_ERROR: {exc}", file=sys.stderr)
        raise SystemExit(125)
