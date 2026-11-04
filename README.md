# daVinci-Prover
<div align="left">

[![arXiv](https://img.shields.io/badge/arXiv-xxxx.xxxxx-b31b1b.svg?style=for-the-badge)]()
[![Data](https://img.shields.io/badge/Data-4d5eff?style=for-the-badge&logo=huggingface&logoColor=ffffff&labelColor)]()
[![Model](https://img.shields.io/badge/Model-4d5eff?style=for-the-badge&logo=huggingface&logoColor=ffffff&labelColor)]()
</div>

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

Let your coding agent follow the [installation instructions](docs/agent-install.md).

Or install manually:

```bash
git clone https://github.com/zwhong714/daVinci-prover.git
cd daVinci-prover
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[lean-explore-local,python-experiments]'
mkdir -p .local
cp examples/proof.env.sh .local/proof.env.sh
cp examples/leanexplore-local.env.sh .local/leanexplore.env.sh
dvp --help
```

Prepare the pinned [Lean / Mathlib workspace](docs/installation.md#lean-and-mathlib),
[register Comparator](docs/verification.md#installing-the-lean-435-verifier), and
download the [LeanExplore 4.35 data, models, and source](docs/leanexplore.md).
Save custom paths in the two `.local/` profiles.

## Quick start

After setup, configure the [DeepSeek API](https://api-docs.deepseek.com/) from the repository root:

```bash
source .venv/bin/activate
source .local/proof.env.sh
source .local/leanexplore.env.sh
python scripts/check_leanexplore_local.py
python -m dvp.cli.check_python
export OPENAI_API_KEY="your-deepseek-api-key"
export DAVINCI_MODEL=deepseek-flash
export OPENAI_BASE_URL=https://api.deepseek.com
```

**Prove one theorem:**

```bash
dvp prove \
  --thinking enabled --reasoning-effort high \
  --theorem $'import Mathlib\ntheorem demo (n : Nat) : n + 0 = n := by sorry' \
  --lean-retrieve true --python-execute true \
  --max-turns 20 --max-tokens 16384 --lean-timeout 120 \
  --trace-level full
```

The proof, verdict, and trajectory are saved under `runs/dvp_<timestamp>/`.

**Evaluate all 200 Putnam problems:**

```bash
dvp eval \
  --dataset benchmarks/putnam200.jsonl --split all \
  --thinking enabled --reasoning-effort high \
  --lean-retrieve true --python-execute true --concurrency 20 \
  --max-turns 200 --max-tool-calls 800 --max-tokens 32768 \
  --lean-timeout 1800 --llm-timeout 1800 --task-timeout 14400 \
  --attempts-per-task 1 --retries 2 --trace-level full \
  --run-root "runs/putnam200_$(date +%Y%m%d_%H%M%S)"
```

The run directory contains `summary.json`, `results.jsonl`, and per-task traces under `tasks/`. See [usage](docs/usage.md) for resume and other options.

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

The model calls tools and receives their results. Consecutive edits are saved together, then each changed Lean file is checked once. Local retrieval and [Python experiments](docs/python-experiments.md) are enabled by default.

When development ends, Comparator checks the saved proof and local modules against the unchanged problem. Compilation feedback and the final chat reply do not determine acceptance. See [verification](docs/verification.md).

Batch evaluation supports concurrency, retries, resume, and full traces. See [usage](docs/usage.md) for commands and [architecture](docs/architecture.md) for implementation details.

## Documentation

[Installation](docs/installation.md) · [Usage](docs/usage.md) · [Retrieval](docs/leanexplore.md) · [Benchmarks](benchmarks/) · [All guides](docs/README.md)

## License

[MIT](LICENSE) for DVP code. Third-party software, datasets, retrieval assets, and model weights retain their respective licenses.

Built on Lean, Mathlib, LeanExplore, and LiteLLM, with lessons from Codex, Claude Code, and DSH.
