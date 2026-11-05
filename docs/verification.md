# Lean Checks and Comparator Acceptance

[Back to documentation](README.md)

DVP uses ordinary Lean compilation during development and Lean **4.35.0**'s bundled `lake comparator` for final acceptance.

## Completion and termination

`lean_verify` and automatic checks return compiler diagnostics and proof-hole goals. A successful compilation does not finish the attempt or establish acceptance. Development supports local helper modules, `opaque`, `#eval`, and `#print` under the selected execution mode. To inspect a saved theorem's dependencies, check a probe with `import Submission` and `#print axioms <theorem name>`.

When the model finishes, reaches a development limit, or exhausts its recovery allowance, DVP checks the saved `Submission.lean` once with Comparator. The final chat reply is not graded. Final acceptance uses its own `--lean-timeout`, outside `--task-timeout` and the model/tool budgets, so total task time can exceed the development timeout. Infrastructure failures during rollout skip grading and retain their retry handling; cancellation stops the running work. See [completion rules](usage.md#model-completion-and-tool-format-errors).

For final acceptance, DVP:

1. Preserves the original task as `Challenge.lean` and the saved submission as `Solution.lean`.
2. Freezes imported `Submission.*` sources and rebuilds them in a fresh project. Missing sources, symlinks, and import cycles are rejected; old local `.olean` files and model-supplied Lake configuration are not used.
3. Runs Comparator to check statements, related definitions, original proof obligations, permitted axioms, and kernel validity.

The permitted axioms are `propext`, `Quot.sound`, and `Classical.choice`, plus axioms declared in the trusted original task. Candidates must preserve those original declarations and include the context needed to compile. Fixed-context metadata identifies obligations; it does not rewrite the original or complete the candidate.

Acceptance requires exit code `0` and `Your solution is okay!`. Accepted proofs score 1; rejected proofs score 0. A zero exit without the marker, a timeout, or a runtime failure is an infrastructure error. Final grading never restarts model generation or feeds its verdict back for repairs. Source files, module hashes, and results are saved in `comparator_artifacts/`.

## Installing the Lean 4.35 verifier

Complete [installation](installation.md), activate the Python environment, and run from the repository root. Default sandbox mode requires working bubblewrap / user namespaces and `libseccomp2`. No separate Comparator checkout is needed.

```bash
export DVP_ROOT="$PWD"
export DAVINCI_TARGET_WORKSPACE="${DAVINCI_TARGET_WORKSPACE:-$DVP_ROOT/runtime/mathlib4-4.35}"
export PATH="${ELAN_HOME:-$HOME/.elan}/bin:$PATH"
export ELAN_TOOLCHAIN="$(cat "$DAVINCI_TARGET_WORKSPACE/lean-toolchain")"
dvp_lean_bin="$(cd "$DAVINCI_TARGET_WORKSPACE" && elan which lean)"
export DVP_LEAN_TOOLCHAIN="$(dirname "$(dirname "$(readlink -f "$dvp_lean_bin")")")"
export DVP_COMPARATOR_ROOT="$DVP_ROOT/runtime/comparator"
python scripts/setup_comparator_435.py \
  --target-workspace "$DAVINCI_TARGET_WORKSPACE" \
  --toolchain "$DVP_LEAN_TOOLCHAIN" \
  --runtime-root "$DVP_COMPARATOR_ROOT"
```

Registration tests valid proofs, changed statements, `sorry`, and added axioms before writing `$DVP_COMPARATOR_ROOT/4.35.0/runtime.json`. Reports go to `analysis/comparator-setup`. Keep `DVP_COMPARATOR_ROOT` set when running proofs.

Runtime loading verifies the Lean version, toolchain paths, and registered SHA256 values of `lake`, `leanexport`, `leanchecker`, and the sandbox adapter when enabled. Missing or changed hashes stop execution. Register again after moving machines or paths, or intentionally updating these programs; hashes are never refreshed automatically.

To run without bubblewrap namespaces, select Landlock + seccomp instead:

```bash
export DVP_COMPARATOR_ROOT="$DVP_ROOT/runtime/comparator-no-sandbox"
python scripts/setup_comparator_435.py \
  --target-workspace "$DAVINCI_TARGET_WORKSPACE" \
  --toolchain "$DVP_LEAN_TOOLCHAIN" \
  --runtime-root "$DVP_COMPARATOR_ROOT" --no-sandbox
```

The existing `--no-sandbox` flag and runtime value `unsandboxed` now disable **namespaces only**. They require working Linux Landlock and `libseccomp2`; they do not permit unrestricted Lean execution.

Both modes restrict file-content reads to the trusted toolchain, pinned dependencies, and the current check's source and build files. Writes are limited to designated build and private temporary directories; network access is blocked. Each Comparator build, export, and kernel-check child gets its own restrictions, and candidate builds cannot read the parent's private export files. Keep other tasks and saved answers outside the trusted dependency directories.

These restrictions also cover development checks and `#eval`. Both modes clear inherited credentials and drop root subprocesses to UID/GID 65534; follow the [path permissions](installation.md#lean-and-mathlib). Landlock mode still shares process and IPC namespaces, and some filesystem metadata can remain visible. It protects file contents but does not provide complete isolation between hostile tenants.

Isolation failures stop execution without switching modes. Re-register after upgrading the sandbox adapter, and use a new run directory after changing runtime, source, or policy; see [resume rules](architecture.md#resuming-runs-and-upgrading-fixed-contexts). Save the selected runtime in your [local configuration](local-configuration.md).
