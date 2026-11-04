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

After [installation](installation.md) and [Comparator registration](verification.md), copy the template:

```bash
mkdir -p .local
cp examples/proof.env.sh .local/proof.env.sh
```

Edit its model, API URL, workspace, and Comparator paths. Export `OPENAI_API_KEY` in your shell, then load the settings from the repository root:

```bash
source .venv/bin/activate
source .local/proof.env.sh
```

Use the [proof and evaluation commands](usage.md#quick-start); no custom launcher is required.

## Retrieval settings

Load the local retrieval template after the proof settings:

```bash
source examples/leanexplore-local.env.sh
python scripts/check_leanexplore_local.py
```

For custom paths, copy the template into `.local/` and edit that copy. Enable the tool with `--lean-retrieve true`. See [local retrieval](leanexplore.md) for data, models, and GPU configuration.

## Moving to another machine

Create a fresh Python environment, prepare the matching Lean / Mathlib workspace, and register Comparator again. Transfer retrieval assets separately and recreate `.local/` settings. Runtime manifests contain absolute paths; virtual environments and old symlinks are not portable.
