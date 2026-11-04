# Running and Troubleshooting

[Back to documentation](README.md)

Run commands from the repository root with your Python environment activated.
Complete [installation](installation.md) and [Comparator registration](verification.md) first.

## Quick Start

For a local or hosted OpenAI-compatible endpoint, set:

```bash
export DVP_ROOT="$PWD"
export PATH="${ELAN_HOME:-$HOME/.elan}/bin:$PATH"
export DAVINCI_MODEL=your-served-model-name
export OPENAI_BASE_URL=http://127.0.0.1:8000/v1
export OPENAI_API_KEY=your-api-key
export DAVINCI_TARGET_WORKSPACE="${DAVINCI_TARGET_WORKSPACE:-$DVP_ROOT/runtime/mathlib4-4.35}"
export DVP_COMPARATOR_ROOT="${DVP_COMPARATOR_ROOT:-$DVP_ROOT/runtime/comparator}"
export DAVINCI_LLM_TRUST_ENV=0
```

Use `EMPTY` only for endpoints without authentication. Set `DAVINCI_LLM_TRUST_ENV=1`
if this endpoint needs system proxy settings. Save reusable settings in
[`.local/proof.env.sh`](local-configuration.md).

DVP calls the LiteLLM SDK directly. A bare model name uses the OpenAI-compatible
route. For a provider's official API, use its LiteLLM identifier, such as
`--model deepseek/deepseek-reasoner`, unset `OPENAI_BASE_URL`, and omit `--base-url`.
Provide that provider's key with `--api-key`, or unset `OPENAI_API_KEY` and use
the provider's credential environment variables.
Only set sampling and thinking options supported by that service; unsupported
parameters are not silently dropped. Native providers use LiteLLM's proxy settings.

**Prove one theorem:**

```bash
dvp prove \
  --theorem $'import Mathlib\ntheorem demo (n : Nat) : n + 0 = n := by sorry' \
  --lean-retrieve false \
  --max-turns 20 --max-tokens 16384 --lean-timeout 120 \
  --trace-level full \
  --run-dir "$DVP_ROOT/runs/demo_$(date +%Y%m%d_%H%M%S)"
```

**Evaluate FATE-H:**

```bash
dvp eval \
  --dataset benchmarks/fate_h_lean435.jsonl --split all \
  --lean-retrieve false --concurrency 20 \
  --max-turns 100 --max-tool-calls 200 \
  --lean-timeout 600 --llm-timeout 1200 --task-timeout 7200 \
  --attempts-per-task 1 --retries 2 --retry-delay 5 --trace-level full \
  --run-root "$DVP_ROOT/runs/fate_h_$(date +%Y%m%d_%H%M%S)"
```

Add `--limit 1 --concurrency 1` for a smoke run. Other inputs include
`benchmarks/fate_x_lean435.jsonl` and `benchmarks/putnam200.jsonl`.
Use `dvp prove --theorem-file problem.lean` for a single problem stored in a file.

Enable [retrieval](#selecting-retrieval) with `--lean-retrieve true`, or
[Python experiments](python-experiments.md) with `--python-execute true`.
Resume with the original command and output directory plus `--resume`.
Changed code, dependencies, datasets, or execution settings require a new run;
see [resume rules](architecture.md#resuming-runs-and-upgrading-fixed-contexts).
See [run artifacts](architecture.md#tools-and-run-artifacts) for results and traces,
and `dvp prove --help` / `dvp eval --help` for all options.

### DeepSeek comparison run

For the previous `deepseek-v4.1-flash` comparison, sampling was explicitly set to
`--temperature 1.0 --top-p 0.95 --reasoning-effort high --thinking enabled
--chat-template-thinking true --max-tokens 32000`. These settings are service-specific.
For uncapped requests, omit `--max-tokens` and unset `DAVINCI_MAX_TOKENS`.
Keep model, endpoint, dataset, and budgets fixed when comparing harness changes.

### Editing and automatic checks

Consecutive `file_edit` / `file_write` calls save in order, then each distinct
changed Lean file is checked once before another tool runs or the response ends.
Diagnostics are attached to the last successful save. Non-Lean files do not compile.

`file_edit` accepts one edit or an `edits` array across files. A batch validates
all edits before writing; an invalid call leaves its files unchanged. Earlier
successful calls remain saved. Compiler errors also leave saved changes in place.
Tool and Lean-check budgets still apply.

## Modular Proofs

Keep the target in `Submission.lean` and helpers under `Submission/`:

```lean
-- Submission/Helpers.lean
import Mathlib
theorem add_zero_helper (n : Nat) : n + 0 = n := by simp
```

```lean
-- Submission.lean
import Submission.Helpers
theorem demo (n : Nat) : n + 0 = n := by exact add_zero_helper n
```

Paths determine import names: `Submission/Counting/Base.lean` is
`import Submission.Counting.Base`. Each module needs its own imports; cycles are errors.
After changing only helpers, explicitly check the entry with
`lean_verify({"file_path": "Submission.lean"})`.

To inspect the saved theorem's axiom dependencies, call `lean_verify` with:

```json
{"code": "import Submission\n#print axioms demo\n"}
```

Lean feedback reports compilation results and remaining goals. Compilation can
succeed with proof holes or new axioms; only [Comparator](verification.md) accepts
the final proof. It rebuilds the saved entry and imported helpers from source.

## Selecting Retrieval

Retrieval uses local LeanExplore. After preparing the [assets](leanexplore.md), run:

```bash
source examples/leanexplore-local.env.sh
python scripts/check_leanexplore_local.py
```

Use `--lean-retrieve true` to enable it or `--lean-retrieve false` to run without
retrieval. The template preserves your proof and model settings.

## Model Completion and Tool Format Errors

| Model response | DVP behavior |
| --- | --- |
| Valid tool calls | Execute in order and return results; Lean errors remain normal tool results |
| `stop` with visible text and no tools | End development and grade the saved submission |
| Thinking-only or empty `stop` | Remind once; another before a valid tool executes ends development |
| Unknown tool or malformed arguments | Return a correction; default limit: 3 consecutive format-error responses |
| `length` with incomplete output | Request another turn; default limit: 5 consecutive truncations |

A complete valid call can execute even when the finish reason is `length`.
Valid tool execution resets the empty-response reminder; complete valid tool
responses reset the format/truncation counters. Set
`--max-consecutive-format-errors` or `--max-consecutive-output-truncations` to `0`
to disable that guard. Ordinary turn, tool, and time limits remain active.

With default `--history-policy current`, normal tool exchanges retain reasoning.
Thinking-only replies and malformed calls are archived but omitted from subsequent
history; corrections report any valid calls that already executed. DVP cannot
recover tool calls discarded by the model service. Extra workflow guidance is
off by default; no per-turn countdown is injected.

After normal completion or a development limit, Comparator grades saved files
once, outside `--task-timeout`, using a separate `--lean-timeout`. Its verdict
does not trigger another model turn. Model/tool infrastructure aborts skip grading;
final grading failures do not restart generation. See [verification](verification.md).

### Output truncation recovery

Default `--retain-truncated-thinking false` keeps visible partial text and omits
unfinished reasoning. Setting it to `true` retains reasoning unless it contains
an extreme repeated-character span. Incomplete calls are never executed.
Recovery uses a new request, not token-level continuation. Full traces preserve
the original output; `conversation.json.rejected_responses` records filtered replies.

### Legacy history comparison

`--history-policy legacy` retains normalized assistant replies, including
truncated thinking and rejected calls, and overrides `--retain-truncated-thinking`.
It does not change current completion rules or restore older tool behavior.
Use a new run directory for comparisons.

## Distributed Inference and Prefix Caching

DVP sends a stable per-task `X-SMG-Routing-Key` across turns, retries, and compatible
resumes. The service must support that header and enable prefix caching.

`conversation.json` records routing and per-call usage under `generation_metadata.calls`.
Missing usage stays unknown; native-provider values may be LiteLLM estimates.
Proof requests are non-streaming, so their recorded duration includes retries
and is not time to first token. These records are not sent back to the model.

## Model API Errors

The client retries transport failures and transient HTTP errors up to three times
with 1/2/4-second delays; nested SDK retries are disabled. Authentication,
permission, invalid-parameter, and context-window errors are not repaired or retried.
DVP does not automatically compress history or reduce `max_tokens`.

`--retries` controls separate whole-attempt infrastructure retries;
`--attempts-per-task` controls proof attempts. Exhausted infrastructure failures
are unscored. `success_rate_selected` includes all selected tasks; `pass@k` uses
valid samples. Start a new run after changing client code or SDK versions.

## Custom Datasets

Each JSONL row needs `task_id` and `theorem_statement`. Fixed-context datasets also
need their contract sidecars. Copy both when moving machines, and test with
`--limit 1 --concurrency 1`. See the included [benchmarks](../benchmarks/).
Original MathOlympiadBench, ProofNet, and DeepSeek-ProverBench inputs are retained
without a claim of compatibility with Lean 4.35; changing a workspace does not port a task.

## Tool Timeouts

| Tool | Default | Adjustment |
| --- | --- | --- |
| Python | 30 seconds | `--python-timeout 60` for longer experiments; split large computations where possible |
| Content search | 30 seconds | `DAVINCI_CONTENT_SEARCH_TIMEOUT`; narrow the path or pattern first |

Search may return partial matches on timeout. Its result limit bounds matching
files, not scanned files. Changed runtime limits require a new evaluation directory.

## Restoring the Environment and Troubleshooting

In a new terminal, activate `.venv` and source your [local configuration](local-configuration.md).

| Problem | Check |
| --- | --- |
| `No module named dvp` | Activate the intended environment and run `python -m pip install -e .` |
| `No module named lean_explore` | Install `'.[lean-explore-local]'` in that environment |
| Missing retrieval models or index | Check the configured asset directories and run the retrieval check script |
| `context_status=unavailable` | Inspect `context_error` and the source snapshot's `.dvp-source.json` |
| Missing `lean`, `lake`, or `.olean` | Check elan's `PATH`, the workspace toolchain, and its compiled cache |
| Retrieved declaration does not compile | Compare the result's source revision with the proof workspace |
