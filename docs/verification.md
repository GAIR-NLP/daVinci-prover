# Lean Checks and Comparator Acceptance

[Back to documentation](README.md)

DVP uses the official `lake comparator` bundled with **Lean 4.35.0**. A mismatch between the workspace and Comparator runtime stops execution before any model request. Tasks must also be compatible with the selected Lean / Mathlib environment.

## Completion and termination

Development and final acceptance are separate phases. `lean_verify`, automatic checks after file edits, and inline probes return Lean diagnostics and proof-hole goals. They never invoke Comparator, submit a proof, or end development, even when a complete target compiles successfully. The model can continue checking and editing all saved files.

DVP stops development when the model returns a visible final reply without tools, reaches a configured development limit, or exhausts the empty-response, format-error, or truncation recovery allowance. It then checks the saved `Submission.lean` and its imported local sources once with Comparator. The final chat reply is not graded. There is no completion-repair prompt and no requirement for prior successful compilation: unfinished saved files also reach final acceptance and are judged by Comparator. Missing or unreadable submission sources cannot earn acceptance. Model or tool infrastructure failures keep their existing retry handling and skip final acceptance; external cancellation propagates without starting a background check.

Final acceptance runs outside the development `--task-timeout` and uses its own `--lean-timeout`. It does not consume model turns or tool calls and cannot be skipped because a development budget is exhausted. `lean_calls` includes this final check, so it can exceed `--max-lean-calls` by one. A task timeout ends development rather than causing an infrastructure retry; a valid saved proof can still pass final acceptance. The task's total wall time can therefore exceed `--task-timeout` by the final check and cleanup time.

Development uses ordinary Lean language rules. Helpers may use `opaque`, notation, elaborator programs, and imports available in the pinned environment or collected `Submission.*` modules. Incomplete proofs can compile with warnings; local proof-hole checks report their remaining goals. Compilation does not establish statement equality or valid axiom dependencies. `#eval` and `#print` are available and return compiler output; they do not establish acceptance.

For a final development self-check, use `lean_verify` with `import Submission` followed by `#print axioms <fully-qualified target theorem name>`. The probe builds the current saved entry and helper source closure afresh. Importing the entry is allowed only for development checks; final acceptance still treats `Submission.lean` as the entry and rejects self-imports and cycles. Probe evidence identifies the probe and each imported source separately; it cannot replace the saved entry or establish acceptance. Model-visible checks use `check_kind: "compilation"`, omit internal verification profiles and empty hole summaries, and retain actual errors, remaining goals, and printed axiom dependencies.

Consecutive file writes and edits in one response save in order, then each distinct changed Lean destination is checked once before another tool runs or the response ends. Non-Lean writes do not compile. The model can also call `lean_verify` explicitly, including to recheck the entry after changing only its imported helpers. All these checks remain development feedback.

Each final check builds a fresh project with the unchanged original task as `Challenge.lean` and the unchanged saved entry as `Solution.lean`. Fixed-context metadata identifies the original target, all original auxiliary proof holes, and original trusted axioms; it does not reconstruct or filter candidate source. Comparator compares statements and related definitions, checks permitted axioms, and rechecks proofs with Lean's kernel. The standard permitted axioms remain `propext`, `Quot.sound`, and `Classical.choice`, plus axioms explicitly present in the trusted original task. Changed statements, unproved original obligations, and proofs depending on disallowed axioms cannot pass.

Imports under `Submission.*` resolve to editable source files. DVP freezes their transitive source closure, records a manifest and hashes, and compiles dependencies before the entry. Challenge compilation sees only pinned library dependencies; Solution also sees the freshly built local modules. No model-supplied Lake configuration or old local `.olean` is used. Missing sources, symlinked modules, and cycles fail source collection. Final evidence binds the original task, raw entry, and complete source bundle. Previously successful development checks do not replace a fresh final verdict.

Candidates must include the context they need to compile, directly or through collected modules, and preserve original axiom declarations even when unused: Comparator exports permitted axioms from both sides. Older fixed-context submissions that omitted necessary imports, definitions, or scopes are no longer completed automatically. Reverification uses the current raw-source behavior. Historical artifacts and scores are not rewritten.

Sandbox mode covers development and final acceptance. Candidate code cannot modify shared Mathlib or the original task and may write only to the check's private build directory. Explicit unsandboxed mode provides no such isolation. Sandbox failures never trigger an automatic switch to unsandboxed execution.

Acceptance requires exit code `0` and Comparator's `Your solution is okay!` marker. A zero exit without the marker is an infrastructure error. Acceptance scores 1; rejection scores 0. Final acceptance timeouts and infrastructure errors are recorded separately from proof failures and do not restart model generation. Its verdict is never sent back to the model for another repair within the same attempt. Results, `Challenge.lean`, `candidate.lean`, `Solution.lean`, local modules, and `source_bundle.json` are saved in `comparator_artifacts/`.

The acceptance policy is `dvp.policy.comparator.v4`, with episode schema `dvp.episode.v12`. Use a new run directory after upgrading; source fingerprints prevent resuming old execution semantics. With `--attempts-per-task` greater than 1, independently configured proof attempts retain pass@k semantics. See the [fixed-context upgrade and resume rules](architecture.md#resuming-runs-and-upgrading-fixed-contexts).

## Installing the Lean 4.35 verifier

The verifier is bundled with the toolchain; no separate Comparator checkout or Go installation is required. Complete the Python and Lean / Mathlib steps in the [installation guide](installation.md), then run the following from the repository root with the Python environment activated. The default sandbox mode requires working bubblewrap / user namespaces and `libseccomp2`.

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

The setup script writes `$DVP_COMPARATOR_ROOT/4.35.0/runtime.json` only after real acceptance tests: correct arithmetic and Mathlib proofs must pass; changed statements, `sorry`, and added axioms must fail. The runtime file contains absolute paths, so repeat registration after moving machines or relocating the repository or toolchain. Keep `DVP_COMPARATOR_ROOT` pointed at the selected directory when proving or evaluating.

Runtime loading, including startup preflight, checks the registered SHA256 values
of `lake`, `leanexport`, `leanchecker`, and the sandbox adapter when enabled. The
registered compiler tools must resolve inside the selected toolchain. Missing
hashes or changed executables stop verification; rerun the registration command
after an intentional toolchain or adapter update. DVP never refreshes hashes
automatically or falls back to a different verification mode.

To explicitly select unsandboxed mode, use the same configuration with a separate runtime directory:

```bash
export DVP_COMPARATOR_ROOT="$DVP_ROOT/runtime/comparator-no-sandbox"
python scripts/setup_comparator_435.py \
  --target-workspace "$DAVINCI_TARGET_WORKSPACE" \
  --toolchain "$DVP_LEAN_TOOLCHAIN" \
  --runtime-root "$DVP_COMPARATOR_ROOT" --no-sandbox
```

This runtime sets `execution_mode=unsandboxed` and uses the official `--inadvisably-no-sandbox` option. The selected mode applies to drafts, ordinary Lean compilation, and final Comparator acceptance. Subprocesses still clear inherited credential variables and drop to UID/GID 65534 when launched as root; see the [path permission requirements](installation.md#lean-and-mathlib). Unsandboxed mode does not provide the isolation or trust guarantees of sandbox mode. Run manifests bind the selected runtime, so runs cannot resume across modes; sandbox failures never trigger an automatic mode switch.

`--toolchain` is required: select the accessible toolchain discovered above or provide its absolute path. Reports default to `analysis/comparator-setup`; use `--runtime-root` and `--report-dir` to choose other local directories. Save the selected runtime path in your [local configuration](local-configuration.md).

Previous local deployment checks accepted valid and Mathlib proofs, and rejected changed statements, `sorry`, and added axioms. A simple task generated by a real model also passed ordinary checking and final acceptance. These checks are not a full Fate-H benchmark.
