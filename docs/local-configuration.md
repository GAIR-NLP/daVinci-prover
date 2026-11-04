# Local Configuration

[Back to documentation](README.md)

Keep machine paths, endpoints, tokens, GPU settings, and custom launchers in ignored `.local/`.

| Location | Contents |
| --- | --- |
| `examples/` | Portable configuration templates |
| `scripts/` | Setup and retrieval checks |
| `.local/` | Private machine settings and launchers |
| `runtime/`, `data/` | Installed runtimes and downloaded assets |
| `runs/`, `analysis/`, `logs/` | Generated results and reports |

The last three rows are excluded from Git.

## Proof settings

After [installation](installation.md) and [Comparator registration](verification.md),
create the profiles without overwriting existing settings:

```bash
mkdir -p .local
if [[ ! -e .local/proof.env.sh ]]; then
  cp examples/proof.env.sh .local/proof.env.sh
fi
if [[ ! -e .local/leanexplore.env.sh ]]; then
  cp examples/leanexplore-local.env.sh .local/leanexplore.env.sh
fi
```

Edit the proof profile's model, API URL, workspace, and Comparator paths, and the
retrieval profile's asset and GPU settings. Export `OPENAI_API_KEY` in your shell,
then load both profiles from the repository root:

```bash
source .venv/bin/activate
source .local/proof.env.sh
source .local/leanexplore.env.sh
```

Use the [proof and evaluation commands](usage.md#quick-start); no custom launcher is required.

## Retrieval settings

The retrieval profile checks snapshot/workspace compatibility when sourced.
Complete the [asset setup](leanexplore.md) and verify both default tools:

```bash
python scripts/check_leanexplore_local.py
python -m dvp.cli.check_python
```

Retrieval and Python experiments are enabled by default. Keep custom retrieval
paths in `.local/leanexplore.env.sh`; do not disable a tool to hide a failed setup
check. See [local retrieval](leanexplore.md) for data, models, and GPU configuration.

## Moving to another machine

Create a fresh Python environment, prepare the matching Lean / Mathlib workspace, and register Comparator again. Transfer retrieval assets separately and recreate `.local/` settings. Runtime manifests contain absolute paths; virtual environments and old symlinks are not portable.
