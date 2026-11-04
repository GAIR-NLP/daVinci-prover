# Running and Troubleshooting

[Back to the documentation index](README.md)

Run these commands from the repository root with your DVP Python environment activated. First complete [installation](installation.md) and [Comparator registration](verification.md). Retrieval is optional.

## Quick Start

Configure a model service supporting Chat Completions and tool calls. Replace the model name, URL, and key with your service's values:

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

Use `EMPTY` only for a service that does not validate keys. Keep the workspace and Comparator paths selected during installation. To reuse your settings across terminals, copy [the proof template](../examples/proof.env.sh) into ignored `.local/` as described in [local configuration](local-configuration.md).

`DAVINCI_LLM_TRUST_ENV=0` disables system proxy settings for OpenAI-compatible model requests; set it to `1` if your service requires a proxy. It does not control LeanExplore requests. Add thinking options such as `--chat-template-thinking true` only when supported by your model service.

DVP uses the LiteLLM SDK directly; no LiteLLM Proxy server is required. A bare
`--model your-served-model-name` with `--base-url` keeps using an OpenAI-compatible
endpoint. For other providers, use LiteLLM's `provider/model` identifier, such as
`--model deepseek/deepseek-reasoner`, and omit `--base-url` (also unset
`OPENAI_BASE_URL`) to use that provider's default endpoint. Supply `--api-key` or
the provider's documented credential environment variables. Native providers use
LiteLLM's own transport/proxy configuration.

Only explicitly configured generation parameters are sent. Unsupported parameters
raise an error; DVP does not silently drop them, switch models, compress history,
or lower `max_tokens` after a context-window error. Self-hosted `thinking`,
`chat_template_kwargs`, and `stop_token_ids` are passed through as request-body
extensions. Use options supported by the selected provider.

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
  --dataset inputs/fate/fate_h_lean435.jsonl --split all \
  --lean-retrieve false --concurrency 20 \
  --max-turns 100 --max-tool-calls 200 \
  --lean-timeout 600 --llm-timeout 1200 --task-timeout 7200 \
  --attempts-per-task 1 --retries 2 --retry-delay 5 --trace-level full \
  --run-root "$DVP_ROOT/runs/fate_h_$(date +%Y%m%d_%H%M%S)"
```

For a smoke run, change concurrency to `1` and add `--limit 1`. Select `inputs/fate/fate_x_lean435.jsonl` for FATE-X; both datasets contain 100 tasks. The `\` must be the last character on each continued line; copy the entire command and use plain-text URLs.

To enable retrieval, configure [local LeanExplore](leanexplore.md#enabling-lean-435-retrieval) or a [remote server](leanexplore-server.md#4-connect-from-the-dvp-machine), then use `--lean-retrieve true`. Both retrieval templates preserve your selected workspace, Comparator, and model settings. The local template validates the pinned workspace and snapshot before exporting retrieval settings.

For local numerical experiments, run `python -m dvp.cli.check_python`, then add `--python-execute true`. See [Python experiments](python-experiments.md) for supported libraries, limits, and isolation.

Resume batch evaluation with the original command and output directory plus `--resume`. Use a **new run directory** after changing code, policy, or datasets; see the [resume rules](architecture.md#resuming-runs-and-upgrading-fixed-contexts). For outputs, trace levels, and score interpretation, see [tools and run artifacts](architecture.md#tools-and-run-artifacts). Run `dvp prove --help` or `dvp eval --help` for all options.

### DeepSeek comparison run

For the `deepseek-v4.1-flash` service used in the Putnam comparison, the following command explicitly matches the supplied Claude Code sampling settings. Configure the model URL, key, Lean workspace, Comparator, and retrieval backend first. These are experiment settings, not defaults for other models.

```bash
dvp eval \
  --dataset inputs/putnam/putnam200.jsonl --split all \
  --model deepseek-v4.1-flash --lean-retrieve true --concurrency 30 \
  --temperature 1.0 --top-p 0.95 --reasoning-effort high \
  --thinking enabled --chat-template-thinking true --max-tokens 32000 \
  --history-policy current --guidance off --retain-truncated-thinking false \
  --max-consecutive-format-errors 3 --max-consecutive-output-truncations 5 \
  --max-turns 600 --max-tool-calls 2000 \
  --lean-timeout 1800 --llm-timeout 1800 --task-timeout 14400 \
  --attempts-per-task 1 --retries 2 --retry-delay 5 --trace-level full \
  --run-root "$DVP_ROOT/runs/putnam200_ccparams_$(date +%Y%m%d_%H%M%S)"
```

The turn and tool caps are explicit experimental limits; native harness rounds are not interchangeable with DVP turns. There is no per-response tool-call cap: calls execute in their returned order, subject to the remaining episode budgets. To compare against the uncapped Codex/DSH requests instead, omit `--max-tokens` and clear `DAVINCI_MAX_TOKENS`; do the same for sampling fields intentionally left to the service. Keep the endpoint and all other settings fixed between comparisons. Neither this command nor the history changes establish a new benchmark score.

### Editing and automatic checks

Consecutive `file_edit` and `file_write` calls in one response save in order,
then each distinct changed `.lean` destination is checked once before another
tool runs or the response ends. `file_edit` accepts single edits or an `edits`
array.

Automatic checks build the imported local modules for each destination. A group
of consecutive writes returns its final compiler diagnostics once, with the last
successful save. Earlier saves report that checking is deferred. Each check may
launch multiple Lean processes. Non-Lean files are saved without compilation.
Use `lean_verify` to recheck an unchanged target after editing only its imported
helpers. These checks do not submit the proof; Comparator grades the saved entry
after development stops.

An invalid edit call saves nothing; earlier successful calls remain saved.
Compiler errors leave the saved changes in place and return diagnostics for
each checked file. Before each save, any configured Lean-check budget must
cover all distinct Lean destinations pending in that group. Each tool call
still consumes its own tool budget. Explicit `lean_verify` calls execute normally.

## Modular Proofs

Local modules are enabled automatically. The submission layout matches the shared Putnam task convention used in the Codex, Claude Code, and DeepSeek Harness comparison runs. Keep the target theorem in `Submission.lean`; save reusable definitions and lemmas under `Submission/`. Paths determine import names, including case:

```lean
-- Submission/Helpers.lean
import Mathlib

theorem add_zero_helper (n : Nat) : n + 0 = n := by simp
```

```lean
-- Submission.lean
import Mathlib
import Submission.Helpers

theorem demo (n : Nat) : n + 0 = n := by
  exact add_zero_helper n
```

Saved Lean destinations are checked automatically as described above. Nested paths work too: `Submission/Counting/Base.lean` becomes `import Submission.Counting.Base`. Each module needs its own imports; it does not inherit the entry file's declarations. Circular imports are errors.

`file_edit` accepts either the single-edit arguments or an `edits` list. Set a shared `file_path` to edit one file multiple times; an item can override it to edit another file:

```json
{
  "file_path": "Submission/Helpers.lean",
  "edits": [
    {"old_str": "lemma old_name", "new_str": "lemma new_name"},
    {"file_path": "Submission.lean", "old_str": "exact old_name n", "new_str": "exact new_name n"}
  ]
}
```

Edits are evaluated in list order, so later edits see earlier changes to the same file. Items without their own path use the shared `file_path`, or `Submission.lean` when no shared path is given. All paths and replacements are checked before files are written; an invalid item leaves the files unchanged. With `edits`, put `old_str`, `new_str`, `old_line_range`, `replace_all`, and `target_path` inside each item, not at the top level. Every save in a consecutive group completes before verification starts, and multiple edits to one Lean destination produce one check of its final contents. When only helpers changed, call `lean_verify` with `{"file_path": "Submission.lean"}` to check the entry and its imported modules together. A successful save is not proof acceptance.

Before finishing, inspect the saved theorem's axiom dependencies with an inline `lean_verify` call, replacing `demo` with its fully qualified name:

```json
{"code": "import Submission\n#print axioms demo\n"}
```

This compiles the current saved `Submission.lean` and its imported helpers before running the probe; it does not edit or submit them. A separate probe file can also import `Submission`, provided the entry does not import that probe and create a cycle. The permitted axioms are `propext`, `Quot.sound`, `Classical.choice`, and axioms explicitly supplied in the original task. Any other dependency needs to be eliminated from the proof.

Lean tool feedback is labelled `check_kind: "compilation"`: `success` means compilation succeeded, not final proof acceptance. Ordinary checks can compile files with proof holes and return warnings; draft checks also report dependency holes and local goals. Empty hole summaries are omitted. `#print axioms` output is diagnostic and does not replace final verification of statements and proofs.

Final acceptance still requires every original proof obligation to be proved without disallowed axiom dependencies. Unimported helper files do not affect acceptance. Each check rebuilds the imported source closure in a temporary directory; helper source is reusable, but previous local `.olean` files and verifier results are not reused. Final Comparator evidence binds the entry and dependency source hashes. See [verification](verification.md).

## Selecting Retrieval

After configuring the proof runtime and model, select one retrieval backend:

```bash
# Local retrieval: requires the local extra, snapshot, model weights, and sources.
source examples/leanexplore-local.env.sh
python scripts/check_leanexplore_local.py
```

Or connect to a separate retrieval server:

```bash
export DAVINCI_LEANEXPLORE_URL=http://retrieval-server:8765
source examples/leanexplore-remote.env.sh
python scripts/check_leanexplore_remote.py --url "$DAVINCI_LEANEXPLORE_URL"
```

Run the evaluation command in [Quick Start](#quick-start) with `--lean-retrieve true`. Neither template selects a model, GPU, or unsandboxed verification. To choose a device or service-specific thinking options, add them to your own ignored configuration or launch command. See [local configuration](local-configuration.md) for reusable templates.

The single-theorem options `--theorem`, `--theorem-file`, and `--quiet` belong to `dvp prove`. Batch evaluation uses `dvp eval --dataset ...` and task-selection options.

An explicit `--retrieve-index /path/to/index.jsonl` selects the JSONL retriever. Its process-wide search concurrency is controlled by `DVP_RETRIEVAL_CONCURRENCY` (a positive integer, default `1`); this setting does not control LeanExplore inference or evaluation concurrency.

## Model Completion and Tool Format Errors

A normal `stop` response with visible text and no valid tools ends development immediately. DVP does not issue a completion-repair prompt. The visible response remains in history, and DVP grades the saved `Submission.lean` afterward. The wording of the final reply does not affect the proof score.

A thinking-only or empty normal response receives one reminder to produce a visible response or use a tool. Another such response before a valid tool executes ends the attempt (`reason=empty_response`). Executing a valid tool resets the empty-response reminder, including a Lean check that returns proof errors. These rules apply to both history policies. Direct episode/replay inputs without a finish reason use the same normal-completion rules.

The prompt describes the task, workspace, tools, and proof-acceptance requirements. It does not prescribe an initial edit, forbid reading the seed, or require a target check between searches or auxiliary lemmas. `--guidance off` is the default: Lean diagnostics, tool results, and protocol-error feedback remain available, but extra workflow hints are not appended. `--guidance soft` explicitly enables the older advisory hints. No per-turn countdown or estimated remaining budget is injected into the conversation.

With `--history-policy current`, a normal response containing only thinking is omitted from subsequent model history and archived in `rejected_responses`. The empty-response reminder rule still applies. Reasoning accompanying a valid tool call or a visible final answer remains in history.

Development checks, including successful compilation of the target, never submit a proof or invoke Comparator. When development ends normally or reaches a turn, tool, Lean, task-time, empty-response, format-error, or output-truncation limit, DVP freezes and grades the saved `Submission.lean` and its imported sources once. It does not substitute a prior inline proof or require a successful preliminary check; saved drafts are also judged by Comparator. Only source-bound acceptance earns success. The final verdict is recorded in the trajectory and is not returned to the model for revision. `result.rollout_reason` records why development stopped; `result.reason` records the final grading outcome.

Final acceptance runs outside `--task-timeout` with its own `--lean-timeout`. It uses no model turn or tool-call budget; `lean_calls` includes the final check and may exceed `--max-lean-calls` by one. Total wall time can exceed the development timeout by the final check and cleanup. A development task timeout does not trigger an infrastructure retry. Model/tool infrastructure aborts retain their retry behavior and skip final acceptance; external cancellation does not start a background check. Final Comparator timeouts or infrastructure failures remain unscored and do not restart model generation.

`dvp prove` and `dvp eval` share `--max-consecutive-format-errors`, which defaults to `3`; set it to `0` to disable this limit. Unknown tools, malformed arguments unrelated to output truncation, and complete arguments that fail schema or tool-specific validation count as format errors. A response claiming tool calls but containing no parseable call also receives a tool-format correction. Multiple format errors in one response count only once. A complete response with valid tool calls resets the count, including a tool that executes normally and returns a Lean compilation or proof-checking error.

By default, the first two errors receive corrective feedback. The third consecutive format error stops development with `reason=repeated_format_error`, then finalization grades the saved submission. The protocol stop does not force a zero score or trigger infrastructure retries through `--retries`; explicitly configured independent attempts still follow `--attempts-per-task`. Tool selection continues to use `tool_choice=auto`, with the tools enabled for the attempt and final Comparator acceptance.

With the default `--history-policy current`, a format-error response is omitted from subsequent model history, including its text, thinking, and malformed calls. The next request contains a user-role correction instead. A valid tool call that reports a Lean error keeps its normal assistant/tool history.

Tool correction uses the same Chat Completions response fields for every backend. Unknown calls receive feedback naming the rejected tool and listing the tools actually enabled for that attempt. Malformed entries in `tool_calls` are reported individually, including when other calls in the response are valid; they are not silently discarded. DVP does not guess tool aliases or execute rejected calls. Valid calls in a mixed response retain their actual results. These errors use the existing consecutive format-error limit.

For an empty or thinking-only response, the reminder states that no visible text or tool call was received and lists the available tools. It lets the model resend a complete call or provide visible text without guessing what the service generated internally. The one-reminder limit is unchanged; after it ends development, finalization checks the saved submission. If a service has discarded output, the client cannot reconstruct it; the correction requests a new response rather than executing a guessed call. No service-specific patch, environment variable, or raw-token request is required.

### Output truncation recovery

`finish_reason=length` with no tool call, or with tool arguments cut off before forming complete JSON, is classified as `output_truncated`, not a format error. Pure output truncation neither increments nor clears an unresolved format-error streak. A complete valid tool call still executes even if the provider reports `length`; that finish reason alone does not make the call fail. Unknown tools and complete but invalid arguments remain genuine format errors, including when the provider reports `length`.

Recovery feedback identifies the output limit and asks for smaller steps using saved files and tool results. It states that incomplete calls were not executed. It does not discuss remaining task time or turn budgets.

Output truncation has a separate counter controlled by `--max-consecutive-output-truncations`, default `5`. Five consecutive truncated responses stop development with `truncated=true` and `reason=repeated_output_truncation`, then finalization grades the saved submission. The truncation stop does not force a zero score or trigger infrastructure retries. A complete response with valid tool calls resets the truncation streak. Set the option to `0` to disable only this limit; ordinary turn, tool-call, and Lean-call budgets still apply. Every truncated response consumes a model turn and generated tokens; recovery is a new model turn, not an HTTP retry.

Under the current history policy, the default `--retain-truncated-thinking false` removes unfinished reasoning from the next request while retaining visible partial text. A pure-thinking truncated response leaves only recovery feedback. Explicitly setting the option to `true` retains useful partial reasoning, but a conservative repetition check still excludes an entire reasoning block if it contains at least 4,096 identical consecutive non-whitespace characters. This narrow check does not classify semantic repetition or ordinary long derivations. Filtered originals remain in audit artifacts. Retention provides context in a new Chat Completions request; it is not native token-level or signed-block continuation. In both modes, malformed calls are removed and actual executed operations are reported. The option changes history retention, not truncation or format-error counting. Normal thinking-only `stop` responses are still omitted, and reasoning accompanying valid tool calls is retained.

With `--trace-level sft` or `full`, `conversation.json.rejected_responses` archives filtered raw responses, including thinking, with their one-based `turn` and `errors`, separately from model-visible `messages`. Retained visible text and thinking remain in `messages`; full traces also retain the original action in `trajectory.jsonl`. DVP executes multi-call responses sequentially. If a batch mixes executed valid calls with rejected calls, corrective feedback reports actual outcomes and the executed arguments, preserving workspace effects without resending malformed calls.

Both thresholds, the retention choice, and the history policy are recorded in episode artifacts and the evaluation manifest, and contribute to `config_id` and resume identity checks. For a controlled retention comparison, hold both consecutive-error thresholds, token/turn/tool budgets, model, dataset, and sampling settings constant. Use a new run directory after upgrading the implementation; these settings cannot change on `--resume`.

### Legacy history comparison

`--history-policy legacy` restores retention of normalized assistant replies and their paired tool results, including truncated thinking and rejected calls. It takes precedence over `--retain-truncated-thinking`. Normal final responses remain in history under either policy. Invalid JSON arguments are normalized in model-visible history as before; their raw values remain in full trajectories. Rejected calls are still rejected by the executor, and final acceptance still requires Comparator.

For the historical retention settings, use `--history-policy legacy --max-consecutive-format-errors 0 --max-consecutive-output-truncations 0 --max-tokens 16384 --max-turns 200 --max-tool-calls 600`. This selects the old history retention behavior and removes the two newer early-stop thresholds; it does not restore the old forced-tool continuation loop or roll back the current tool feedback. The current completion and single-recovery rules still apply. Task timeout and the ordinary turn/tool budgets remain active. Start in a fresh run directory.

## Distributed Inference and Prefix Caching

The model client automatically sends `X-SMG-Routing-Key`. Batch evaluation generates a stable key from the actual run directory and task ID. Multiple turns, HTTP retries, whole-task retries, and compatible resumed runs for the same task share this key; different tasks use different keys. For a single theorem or a directly instantiated client, each client generates its own key by default; you can also set it explicitly with `routing_key=`. The server must still use a routing policy that recognizes this header and enable prefix caching.

Each attempt's `attempt_result.json` saves the actual key in `endpoint.routing_key`; `conversation.json` saves it in `routing.routing_key`. This value does not contain the API key. Do not share one fixed key across all concurrent tasks, as this may concentrate the load on a single worker.

A standalone script can check the server's reported cached-token count and time to first token. The following command makes real model requests: by default, three consecutive requests with at most 16 generated tokens each. The example reads the key from `OPENAI_API_KEY`, or prompts for hidden input in an interactive terminal when unset.

```bash
python tools/diagnostics/probe_llm_cache.py \
  --base-url "$OPENAI_BASE_URL" --model "$DAVINCI_MODEL" \
  --api-key-env OPENAI_API_KEY \
  --routing-key dvp-cache-check --probe-id cache-check-1 \
  --output analysis/cache_probe.json
```

By default, the script also generates a key shared by all repeated requests in that invocation. Fix `--routing-key` and `--probe-id` to probe the same prefix again. The report preserves the source of cache fields and marks missing fields as unknown. Time to first token includes network and queueing time, so it cannot be treated directly as prefill time.

Regular proof runs save per-call model records in `generation_metadata.calls` in
`conversation.json`: the routing key, LiteLLM model identifier, requested generation
parameters, numeric usage, finish reason, duration, and `sdk_request_count`.
For OpenAI-compatible endpoints, usage and finish reasons come from the received
response; missing values remain unknown. Other providers use LiteLLM normalization
(`usage_source=litellm`), whose defaults or estimates are not necessarily
server-measured counts. Provider-specific cache fields retain their own semantics.
These records are separate from model messages and are never sent back to the model.

The conversation schema is `dvp.conversation.v3`. Rejected replies are stored in
`rejected_responses`; per-call records are stored in `generation_metadata.calls`.
The client makes non-streaming requests. Duration includes retries and backoff,
not time to first token. Artifacts are saved when an attempt ends with trace level
`sft` or `full`; they are not live logs.

## Model API Errors

LiteLLM supplies provider adapters and error types. DVP retries connection errors,
timeouts, and transient HTTP 408/409/429/500/502/503/504 failures at most three times,
with delays of 1, 2, and 4 seconds. A retry uses the same conversation, parameters,
and routing key. Retries inside LiteLLM and the OpenAI SDK are disabled, so this
budget is not multiplied. Authentication, permission, invalid-parameter, and
context-window errors are not retried or repaired. There are no platform-specific
HTTP 403 exceptions.

Exhausted transient failures retain their LiteLLM exception type and are classified
as `infrastructure_error`; `--retries` controls independent whole-task retries.
Exhausted infrastructure failures have `valid_for_scoring=false` and are excluded
from the valid-sample denominator for `pass@k`. `success_rate_selected` still uses
all selected tasks, making incomplete coverage visible.

After changing client code or SDK versions, start a new process and run directory.
The manifest records LiteLLM, OpenAI, and Tenacity versions; `--resume` rejects a
changed execution baseline. Historical artifacts are not rewritten.

## Custom Datasets

The Fate-H input is [`inputs/fate/fate_h_lean435.jsonl`](../inputs/fate/fate_h_lean435.jsonl), containing 100 tasks for Lean **4.35.0** and Mathlib commit `56a005855ea17e0b0958f73ec801ae140b0a1679`. Each JSONL line must contain at least `task_id` and `theorem_statement`; custom datasets with fixed context also require a contract file in the same directory. Copy the dataset and its sidecars when moving machines.

Use the [evaluation command above](#quick-start) with `--dataset /path/to/tasks.jsonl`. Start with `--limit 1 --concurrency 1`. For a single theorem, use `dvp prove --theorem-file /path/to/problem.lean` in place of `--theorem`.

The repository also retains original problem statements from MathOlympiadBench, ProofNet, and DeepSeek-ProverBench for future migration, **without claiming they have been adapted to the current environment**. Browse [dataset inputs](../inputs/) for retained files. Changing `--target-workspace` does not automatically port tasks or make scores from different environments directly equivalent.

## Tool Timeouts

Python experiments default to 30 seconds per call. For longer numerical or
symbolic experiments, start with `--python-timeout 60`; the model can request a
shorter timeout within that maximum. Reduce large enumerations, use appropriate
numerical libraries, or split work into saved intermediate results before
raising the limit further. A longer deadline cannot repair a nonterminating
loop.

Content search defaults to 30 seconds, configurable with
`DAVINCI_CONTENT_SEARCH_TIMEOUT`. Keep this limit initially and narrow searches
to a module or file when possible. The result `limit` bounds matching files,
not the number of files scanned. A timed-out search may still return useful
partial matches. Use a new run directory when changing runtime limits or code.

## Restoring the Environment and Troubleshooting

In a new terminal, first activate the Python environment where DVP and local retrieval dependencies are installed, then restore your saved settings and select retrieval:

```bash
cd /path/to/daVinci-prover
export DVP_ROOT="$PWD"
source .venv/bin/activate
source .local/proof.env.sh
source examples/leanexplore-local.env.sh
python scripts/check_leanexplore_local.py
```

Create `.local/proof.env.sh` using the [configuration guide](local-configuration.md), or repeat the exports in [Quick Start](#quick-start). For remote retrieval, source its template and run the remote check instead.

- `No module named dvp`: Confirm that the intended Python environment is active, then run `python -m pip install -e .` from the repository root.
- `No module named lean_explore`: Run `python -m pip install -e '.[lean-explore-local]'` in the same Python environment; use `'.[lean-explore-local,test]'` if you also need test dependencies.
- Local index or models not found: Check that all three `data/leanexplore-*` directories are complete. Model directories must contain weights, tokenizer files, and configuration files; confirm that the current process uses the absolute paths set by the environment script.
- `context_status=unavailable`: Inspect `context_error` and check `.dvp-source.json` in the source directory against the result's `source_link`; local mode does not fetch missing source files from the network.
- `lean` / `lake` not found, or missing `.olean` files: Check elan's `PATH`, verify `lean-toolchain` and `lake env lean --version` in the actual target workspace, and prepare the cache.
- A declaration is retrieved but Lean reports it missing or with a different type: Check the retrieval result's source version against the local workspace; successful retrieval does not mean the declaration is usable in every Mathlib version.
