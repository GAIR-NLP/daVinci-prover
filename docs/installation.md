# Installation

[Back to documentation](README.md)

Run these commands in Bash from the source checkout. Set up Python, the pinned Lean / Mathlib workspace, and Comparator before running proofs.

For installation by a coding agent, use the [agent instructions](agent-install.md).

## Environment

| Component | Requirement |
| --- | --- |
| Platform | Linux; Python **3.11+** (**3.12** recommended) |
| Lean / Lake | **4.35.0**, `leanprover/lean4:v4.35.0` |
| Mathlib | Commit `56a005855ea17e0b0958f73ec801ae140b0a1679` |
| Verification | Lean's bundled `lake comparator`, `leanexport`, and `leanchecker` |
| Model | Tool-calling API supported by LiteLLM |
| Optional retrieval | LeanExplore **1.3.x**, snapshot `20261020_060431`, and matching models and sources |

## Python environment

On Ubuntu / Debian, install missing system packages; omit `sudo` when running as root:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git curl ca-certificates build-essential ripgrep bubblewrap libseccomp2
```

Use a dedicated environment with Python 3.12 and its `venv` module available:

```bash
cd /path/to/daVinci-prover
export DVP_ROOT="$PWD"
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
dvp --help
```

Keep this shell open for the remaining steps. In a new shell, set `DVP_ROOT` and reactivate `.venv`. An existing DVP-only Conda environment also works. Editable installation needs no `PYTHONPATH` override.

The base install includes LiteLLM for model calls. Optional packages:

| Feature | Install |
| --- | --- |
| NumPy, SymPy, and mpmath experiments | `pip install -e '.[python-experiments]'` |
| Local LeanExplore | `pip install -e '.[lean-explore-local]'` |
| Tests | `pip install -e '.[test]'` |

Dependency ranges are in [`pyproject.toml`](../pyproject.toml). For a specific CPU / CUDA PyTorch build, install it in this environment before the local retrieval extra. Run the [retrieval check](leanexplore.md) or [Python preflight](python-experiments.md) after configuring that feature.

## Lean and Mathlib

When DVP runs as root, verification subprocesses use UID/GID **65534**. The repository, toolchain, Mathlib dependencies, and their parent directories must be readable and traversable by that identity. Before installing elan as root, set `export ELAN_HOME=/opt/dvp/elan` or another accessible location outside `/root`. This applies in both verification modes.

Install [elan](https://github.com/leanprover/elan#installation) if needed:

```bash
curl https://elan.lean-lang.org/elan-init.sh -sSf | sh -s -- -y --default-toolchain none
export PATH="${ELAN_HOME:-$HOME/.elan}/bin:$PATH"
```

Create the pinned workspace, fetch compiled artifacts, and check a small proof:

```bash
export DVP_MATHLIB_COMMIT=56a005855ea17e0b0958f73ec801ae140b0a1679
export DAVINCI_TARGET_WORKSPACE="$DVP_ROOT/runtime/mathlib4-4.35"
mkdir -p "$DVP_ROOT/runtime"
git clone https://github.com/leanprover-community/mathlib4.git "$DAVINCI_TARGET_WORKSPACE"
git -C "$DAVINCI_TARGET_WORKSPACE" checkout --detach "$DVP_MATHLIB_COMMIT"
(
  cd "$DAVINCI_TARGET_WORKSPACE"
  elan toolchain install "$(cat lean-toolchain)"
  lake exe cache get
  lake env lean --version
  lake env lean --stdin <<'LEAN'
import Mathlib
example (n : ℕ) : n + 0 = n := by simp
LEAN
)
git -C "$DAVINCI_TARGET_WORKSPACE" rev-parse HEAD
```

Confirm the version and commit match the table. If the cache is incomplete, run `lake build Mathlib` inside the workspace. Avoid `lake update`, which changes the pinned dependencies.

Compiled artifacts live in each package's `.lake/build/`; dependencies live in `.lake/packages/`. Cache downloads default to `~/.cache/mathlib`, configurable with `MATHLIB_CACHE_DIR`. Artifacts from another Lean version cannot be reused. Retrieval's source-only snapshot is not a verification workspace.

## Retrieval assets and Comparator

1. [Register Comparator](verification.md#installing-the-lean-435-verifier).
2. Optionally configure [local retrieval](leanexplore.md). Use `--lean-retrieve false` to disable retrieval.
3. Save [local settings](local-configuration.md) and follow the [usage examples](usage.md#quick-start).

On another machine, create a fresh Python environment, prepare the same Lean / Mathlib workspace, and register Comparator again. Runtime manifests contain absolute paths. Transfer retrieval assets separately; virtual environments and machine-specific symlinks are not portable.
