# daVinci-Prover

daVinci-Prover is a Lean theorem-proving harness with local compiler feedback, LeanExplore retrieval, and final acceptance through Lean's **Comparator**. Model calls use the LiteLLM SDK. It supports single proofs, concurrent evaluation, retries, resume, and full execution traces.

## Performance

Historical results on FATE-H, FATE-X, and Putnam-200.

| Putnam-200 | Acceptance rate (%) |
| --- |  ---: |
| `deepseek-v4.1-flash-1012 (1M)` | 87.0 |
| `deepseek-v4-flash-0731 (1M)` |  85.5 |
| `Qwen3.8-27B (256k)` |  10 |


| Fate-X | Acceptance rate (%) |
| --- |  ---: |
| `deepseek-v4.1-flash-1012 (1M)` | 40.0 |
| `deepseek-v4-flash-0731 (1M)` |  38.0 |
| `Qwen3.8-27B (256k)` |  2.0 |

## Installation

Requires Linux, Python **3.11+** (**3.12** recommended), and Lean **4.35.0**. Prepared environment: `GAIR-NLP/daVinci-Prover:4.35`.

Install from source in a dedicated Python environment:

```bash
cd /path/to/daVinci-prover
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
dvp --help
```

Before running proofs, prepare the pinned [Lean / Mathlib workspace and cache](docs/installation.md#lean-and-mathlib) and [register Comparator](docs/verification.md#installing-the-lean-435-verifier). The [installation guide](docs/installation.md) covers system dependencies and optional retrieval packages.

For local retrieval, download the [LeanExplore 4.35 release](https://github.com/zwhong714/daVinci-prover/releases/tag/leanexplore-4.35.0-20261020_060431) and follow its attached `INSTALL.md` to install the snapshot and fetch the pinned model weights.

Then follow the [usage guide](docs/usage.md#quick-start) to configure your model API, prove one theorem, or evaluate FATE-H.

## Harness design

Each attempt starts with the original problem in `Submission.lean`. The agent can build reusable helper modules under `Submission/`, import them into its proof, and inspect the pinned Lean / Mathlib sources through `target/`.

**Tools**

Tools use [JSON schemas](src/dvp/toolkits/schemas.py) and return structured results, including compiler diagnostics and execution errors.

| Tool | Purpose |
| --- | --- |
| `file_read` | Read files with line ranges and pagination. |
| `file_write` | Create or overwrite the submission or a helper file. |
| `file_edit` | Apply one edit or a coordinated batch across writable files. |
| `file_search` | Find files by glob pattern. |
| `content_search` | Search source text or regular expressions with surrounding context. |
| `lean_retrieve` | Search LeanExplore for relevant declarations and source information. |
| `lean_verify` | Compile a Lean file or inline probe with its imported local modules. |
| `python_execute` | Run Python with NumPy, SymPy, and mpmath for numerical and symbolic experiments and enumeration. Optional; enabled with `--python-execute true`. |

Retrieval is enabled by default and can be hidden with `--lean-retrieve false`. To enable Python, install `pip install -e '.[python-experiments]'`, run `python -m dvp.cli.check_python`, and use `--python-execute true`. The local Landlock + seccomp sandbox permits the standard library and only NumPy, SymPy, and mpmath; networking, subprocesses, and threads remain blocked. See [Python experiments](docs/python-experiments.md) for limits and runtime policy v2 migration. Lean and Comparator use their separately configured execution mode.

**Agent loop and history**

The model chooses tools, DVP executes calls in order, and the results become the next request's context. Consecutive writes and edits in one response save in order, then each distinct changed Lean file is checked once before another tool runs or the response ends. Development checks return compiler diagnostics and available proof-hole goals. The model can use `lean_verify` for explicit checks, including inspecting the saved theorem with `import Submission` and `#print axioms` before finishing. Compilation alone does not establish proof acceptance.

Under the default history policy, normal tool exchanges retain their reasoning. Malformed calls and output truncation have separate recovery limits; visible truncated text is retained, while unfinished reasoning is omitted by default. Optional reasoning retention filters extreme repeated-character spans. A normal thinking-only reply is omitted from subsequent history and receives one reminder; another before a valid tool executes ends the attempt. A visible final reply ends development without another repair prompt. See [completion and recovery](docs/usage.md#model-completion-and-tool-format-errors).

**Trusted acceptance**

Development checks never submit a proof. When the model ends or a development limit is reached, DVP checks the saved `Submission.lean` once with Comparator, independently of the remaining model/tool budgets. DVP freezes its imported local sources, rebuilds them, and compares against the unchanged original problem. Comparator checks statements, permitted axioms, and the proof through Lean's kernel. The final chat reply and Python results are not graded. Model/tool infrastructure failures retain their retry handling; final acceptance errors do not restart the model. See [verification](docs/verification.md).

**Evaluation and traces**

Evaluation runs problems concurrently with explicit turn, tool-call, and time limits. It separates proof failures from infrastructure failures and supports compatible checkpoint resume. Full tracing saves model-visible conversations, detailed transitions, and Comparator evidence. See [architecture](docs/architecture.md) for the implementation and artifact layout.

## Documentation

- [Installation](docs/installation.md)
- [Usage](docs/usage.md)
- [Local configuration](docs/local-configuration.md)
- [LeanExplore](docs/leanexplore.md)
- [Retrieval server](docs/leanexplore-server.md)
- [Verification](docs/verification.md)
- [Dataset inputs](inputs/)
- [Architecture](docs/architecture.md)

## Attribution

Citation details coming soon.

Built on Lean, Mathlib, LeanExplore, and lessons from Codex, Claude Code, DSH.

## License

DVP code is released under the [MIT License](LICENSE). Third-party software,
datasets, retrieval snapshots, and model weights retain their respective licenses.
