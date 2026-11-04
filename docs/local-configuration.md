# Local Configuration

[Back to documentation](README.md)

The Git repository contains reusable code and portable templates. Keep machine paths, service URLs, tokens, GPU choices, and custom launchers in ignored `.local/`.

| Location | Purpose |
| --- | --- |
| `examples/` | Public configuration templates with portable defaults and placeholders |
| `scripts/` | Comparator setup, retrieval configuration checks, and server startup |
| [`tools/`](../tools/README.md) | Optional retrieval-data builds and model diagnostics |
| `.local/` | Your machine-specific settings and launchers; excluded from Git |
| `runtime/`, `data/` | Installed Lean / Comparator runtimes and retrieval assets; excluded from Git |
| `runs/`, `analysis/`, `logs/` | Generated results, reports, and logs; excluded from Git |

## Proof settings

Complete [installation](installation.md) and [Comparator registration](verification.md), then copy the template:

```bash
mkdir -p .local
cp examples/proof.env.sh .local/proof.env.sh
```

Edit `.local/proof.env.sh` with your model name, API URL, workspace, and registered Comparator directory. Export `OPENAI_API_KEY` in your shell, then load the file from the repository root:

```bash
source .venv/bin/activate
source .local/proof.env.sh
```

Run `dvp prove` or `dvp eval` using the [usage examples](usage.md#quick-start). No custom launcher is required. Save any launcher you create under `.local/`.

## Retrieval settings

Choose one template after loading the proof settings:

```bash
# Local models and data:
source examples/leanexplore-local.env.sh
```

```bash
# A separate retrieval server:
export DAVINCI_LEANEXPLORE_URL=http://retrieval-server:8765
source examples/leanexplore-remote.env.sh
```

For custom asset paths or server settings, copy the selected template into `.local/`, edit it, and source that copy. Templates can also be sourced directly after setting the variables they document. Both preserve the selected proof workspace and Comparator. The local template checks the pinned Lean 4.35 / Mathlib environment and snapshot metadata before exporting retrieval settings; run `python scripts/check_leanexplore_local.py` to test actual retrieval. For remote mode, use `python scripts/check_leanexplore_remote.py --url "$DAVINCI_LEANEXPLORE_URL"`.

Set `--lean-retrieve true` in the proof or evaluation command. The templates do not select a model, GPU, or unsandboxed verification mode. See the [local retrieval](leanexplore.md) and [server](leanexplore-server.md) guides for asset preparation and deployment.

## Moving to another machine

Install a fresh Python environment, prepare the matching Lean / Mathlib workspace, and register Comparator again: runtime manifests contain absolute paths. Transfer retrieval assets separately when using local retrieval, then create new `.local/` settings for the destination. Do not copy virtual environments or assume symlinks from the old machine remain valid.

Ignored files stay on disk and are not part of an ordinary Git add. Ignore rules do not remove already tracked files or erase Git history; review staged files before publishing.
