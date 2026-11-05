#!/usr/bin/python3
"""Apply DVP's read allowlist to every upstream Comparator child invocation."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

# Upstream launches this through env -i; no editable-install import is required.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from sandbox import run_isolated
from workspace_dependencies import dependency_roots


def parse_invocation(incoming: list[str]) -> tuple[list[str], Path, tuple[Path, ...], tuple[Path, ...], dict[str, str]]:
    """Accept the documented Lake sandbox arguments; never grant its host root."""
    readable, writable, environment, flags = [], [], {}, set()
    cwd = None
    command = []
    index = 0
    while index < len(incoming):
        item = incoming[index]
        if item == "--":
            command = incoming[index + 1:]
            break
        if item in {"--ro-bind", "--bind"}:
            source, destination = incoming[index + 1:index + 3]
            if source != destination:
                raise RuntimeError("Comparator bind source and destination must match")
            if source == "/":
                if item != "--ro-bind":
                    raise RuntimeError("Comparator may not write the filesystem root")
            else:
                (readable if item == "--ro-bind" else writable).append(Path(source))
            index += 3
        elif item == "--setenv":
            key, value = incoming[index + 1:index + 3]
            if key not in {"PATH", "HOME", "TMPDIR", "LEAN_PATH", "LEAN_ABORT_ON_PANIC",
                           "LEAN_NUM_THREADS", "LAKE_CHECK_EXPORT", "LANG", "LC_ALL"}:
                raise RuntimeError(f"Unexpected Comparator environment variable: {key}")
            environment[key] = value
            index += 3
        elif item == "--chdir":
            cwd = Path(incoming[index + 1])
            index += 2
        elif item in {"--tmpfs", "--dir", "--dev", "--proc"}:
            # The child gets a fresh temporary directory or empty namespace,
            # rather than allowing access to these host directories.
            index += 2
        elif item in {"--clearenv", "--unshare-all", "--die-with-parent", "--new-session", "--share-net"}:
            flags.add(item)
            index += 1
        else:
            raise RuntimeError(f"Unexpected Comparator sandbox argument: {item}")
    if not command or cwd is None or not {"--clearenv", "--unshare-all"} <= flags:
        raise RuntimeError("unexpected native Comparator sandbox invocation")
    return command, cwd, tuple(readable), tuple(writable), environment


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toolchain", type=Path, required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--no-bwrap", action="store_true")
    parser.add_argument("invocation", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    incoming = args.invocation[1:] if args.invocation[:1] == ["--"] else args.invocation
    command, cwd, readable, writable, environment = parse_invocation(incoming)
    status = run_isolated(
        command, toolchain=args.toolchain, cwd=cwd,
        read_only=(*readable, *dependency_roots(args.project.resolve(strict=True))),
        writable=writable, environment=environment, sandboxed=not args.no_bwrap,
        read_cwd=False,
    )
    raise SystemExit(status)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, IndexError) as exc:
        print(f"DVP_SANDBOX_ERROR: {exc}", file=sys.stderr)
        raise SystemExit(125)
