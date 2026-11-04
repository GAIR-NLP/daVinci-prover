#!/usr/bin/env python3
"""Register Lean 4.35's bundled Comparator after real tests in the selected mode."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

from dvp.verification.comparator import (
    STANDARD_AXIOMS, SUCCESS_LINE, comparison_command, run_process, sandbox_command, write_project,
)

REPO = Path(__file__).resolve().parents[1]


async def install(args) -> None:
    workspace = args.target_workspace.resolve(strict=True)
    toolchain = args.toolchain.resolve(strict=True)
    if (workspace / "lean-toolchain").read_text().strip() != "leanprover/lean4:v4.35.0":
        raise RuntimeError("A Lean 4.35.0 workspace is required")
    reported = subprocess.check_output([str(toolchain / "bin/lean"), "--version"], text=True)
    if "version 4.35.0" not in reported:
        raise RuntimeError(f"Unexpected Lean version: {reported}")
    adapter = REPO / "src/dvp/verification/native_comparator_sandbox.py"
    if not args.no_sandbox and not os.access(adapter, os.X_OK):
        raise RuntimeError(f"Sandbox adapter must be executable: {adapter}")
    runtime = {
        "kind": "lake", "lean_version": "4.35.0", "toolchain": str(toolchain),
        "execution_mode": "unsandboxed" if args.no_sandbox else "sandboxed",
        "upstream_commit": "eb6cafb7290e8bd5f2937522d8d804f58f02bafc",
        "upstream_repository": "https://github.com/leanprover/lean4",
        "comparator": str(toolchain / "bin/lake"),
        "lean4export": str(toolchain / "bin/leanexport"),
        "leanchecker": str(toolchain / "bin/leanchecker"),
    }
    if not args.no_sandbox:
        runtime["bwrap"] = str(adapter)
    output = args.report_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    command = sandbox_command(["lean", "--version"], workspace=workspace, toolchain=toolchain,
                              sandboxed=not args.no_sandbox)
    captured = await run_process(command, cwd=workspace, timeout=60)
    (output / "execution-preflight.json").write_text(json.dumps({
        "execution_mode": runtime["execution_mode"], "result": captured,
    }, indent=2) + "\n")
    if captured is None or captured[0] or "version 4.35.0" not in captured[1]:
        raise RuntimeError(
            f"Lean {runtime['execution_mode']} preflight failed: {captured}. "
            "No Comparator runtime was registered; verification remains fail-closed."
        )
    cases = {
        "valid": "theorem dvp_smoke : 1 + 1 = 2 := by rfl\n",
        "wrong_statement": "theorem dvp_smoke : True := by trivial\n",
        "sorry": "theorem dvp_smoke : 1 + 1 = 2 := by sorry\n",
        "new_axiom": "axiom bad : 1 + 1 = 2\ntheorem dvp_smoke : 1 + 1 = 2 := bad\n",
        "mathlib": "import Mathlib\ntheorem dvp_smoke : 1 + 1 = 2 := by norm_num\n",
    }
    checks = []
    for name, solution in cases.items():
        challenge = ("import Mathlib\n" if name == "mathlib" else "") + "theorem dvp_smoke : 1 + 1 = 2 := by sorry\n"
        spec = {"theorem_names": ["dvp_smoke"], "permitted_axioms": STANDARD_AXIOMS}
        with tempfile.TemporaryDirectory(prefix="dvp-native-setup-") as temp:
            folder = Path(temp)
            write_project(folder, workspace, challenge, solution, spec)
            captured = await run_process(comparison_command(runtime, folder),
                                         cwd=folder, timeout=300)
            if captured is None:
                raise RuntimeError(f"Comparator {name} timed out")
            status, stdout, stderr = captured
            log = stdout + stderr
            (output / f"comparator-{name}.log").write_text(log)
            accepted = status == 0 and SUCCESS_LINE in log
            rejection = ("theorem statement do not match" if name == "wrong_statement"
                         else "Illegal axiom detected")
            rejected = status == 1 and rejection in log and "DVP_SANDBOX_ERROR:" not in log
            passed = accepted if name in {"valid", "mathlib"} else rejected
            checks.append({"case": name, "exit_code": status, "passed": passed})
            if not passed:
                raise RuntimeError(f"Comparator {name} failed; see {output / f'comparator-{name}.log'}")
    runtime["smoke_checks"] = checks
    runtime["sha256"] = {
        key: hashlib.sha256(Path(runtime[key]).read_bytes()).hexdigest()
        for key in ("comparator", "lean4export", "leanchecker", "bwrap") if key in runtime
    }
    runtime_root = args.runtime_root or REPO / (
        "runtime/comparator-no-sandbox" if args.no_sandbox else "runtime/comparator")
    destination = runtime_root.resolve() / "4.35.0"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "runtime.json").write_text(json.dumps(runtime, indent=2) + "\n")
    print(f"Verified native Comparator registered: {destination / 'runtime.json'}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-workspace", type=Path, required=True)
    parser.add_argument("--toolchain", type=Path, required=True,
                        help="Lean 4.35 toolchain directory containing bin/lean and bin/lake")
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--no-sandbox", action="store_true",
                        help="Use Lean's --inadvisably-no-sandbox mode for local verification")
    parser.add_argument("--report-dir", type=Path, default=REPO / "analysis/comparator-setup")
    try:
        asyncio.run(install(parser.parse_args()))
    except (RuntimeError, OSError) as error:
        raise SystemExit(str(error))


if __name__ == "__main__":
    main()
