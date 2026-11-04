# daVinci-Prover

A single-agent Lean theorem-proving harness with LiteLLM model calls, local LeanExplore retrieval, compiler feedback, and final acceptance through Lean's **Comparator**.

## Performance

Historical acceptance rates (%):

| Model | Putnam-200 | FATE-X |
| --- | ---: | ---: |
| `deepseek-v4.1-flash-1012 (1M)` | 87.0 | 40.0 |
| `deepseek-v4-flash-0731 (1M)` | 85.5 | 38.0 |
| `Qwen3.8-27B (256k)` | 10.0 | 2.0 |

## Installation

Requires Linux, Python **3.11+** (3.12 recommended), and Lean **4.35.0**.
Prepared environment: `GAIR-NLP/daVinci-Prover:4.35`.

```bash
git clone https://github.com/zwhong714/daVinci-prover.git
cd daVinci-prover
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
dvp --help
```

Prepare the pinned [Lean / Mathlib workspace](docs/installation.md#lean-and-mathlib),
[register Comparator](docs/verification.md#installing-the-lean-435-verifier),
then follow the [quick start](docs/usage.md#quick-start).
Optional [local retrieval](docs/leanexplore.md) uses the published LeanExplore 4.35 snapshot.

## Harness design

Each attempt uses one model conversation and its own `Submission.lean`. The agent can create helper modules under `Submission/` and read the pinned library sources through `target/`.

| Tool | Purpose |
| --- | --- |
| `file_read` | Read files with line ranges and pagination. |
| `file_write` | Create or overwrite files. |
| `file_edit` | Apply one edit or a batch across files. |
| `file_search` | Find files by glob. |
| `content_search` | Search source text with context. |
| `lean_retrieve` | Retrieve Lean declarations locally. |
| `lean_verify` | Compile saved files or inline probes. |
| `python_execute` | Run isolated NumPy, SymPy, and mpmath experiments. |

The model calls tools and receives their results. Consecutive edits are saved together, then each changed Lean file is checked once. Retrieval is enabled by default; [Python](docs/python-experiments.md) is optional.

When development ends, Comparator checks the saved proof and local modules against the unchanged problem. Compilation feedback and the final chat reply do not determine acceptance. See [verification](docs/verification.md).

Batch evaluation supports concurrency, retries, resume, and full traces. See [usage](docs/usage.md) for commands and [architecture](docs/architecture.md) for implementation details.

## Documentation

[Installation](docs/installation.md) · [Usage](docs/usage.md) · [Retrieval](docs/leanexplore.md) · [Benchmarks](benchmarks/) · [All guides](docs/README.md)

## License

[MIT](LICENSE) for DVP code. Third-party software, datasets, retrieval assets, and model weights retain their respective licenses.

Built on Lean, Mathlib, LeanExplore, and LiteLLM, with lessons from Codex, Claude Code, and DSH.
