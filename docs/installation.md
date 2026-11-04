# Installation and Migration

[Back to documentation](README.md)

These instructions target Linux / Bash. A new machine needs a Python environment, a built Lean / Mathlib workspace, and Comparator registration. Retrieval assets are optional. Run the commands below from the repository root, then follow the [usage guide](usage.md#quick-start).

## Environment

| Component | Version / requirement |
| --- | --- |
| Platform | Linux / Bash; Python **3.11+** (**3.12** recommended) |
| Lean / Lake | **4.35.0**, toolchain `leanprover/lean4:v4.35.0` |
| Mathlib | Commit `56a005855ea17e0b0958f73ec801ae140b0a1679`; default workspace `runtime/mathlib4-4.35` |
| Mathlib cache | Compiled artifacts for this commit and its pinned dependencies, fetched with `lake exe cache get` |
| Final verification | Lean 4.35's bundled `lake comparator`, `leanexport`, and `leanchecker`; registered locally before use |
| Optional retrieval | LeanExplore **1.3.x**, custom snapshot `20261020_060431`, matching Mathlib sources, and Qwen3 Embedding/Reranker **0.6B** weights |
| Model service | Chat Completions endpoint supporting tool calling |

## Python environment

Python **3.11+** is supported; **3.12** is recommended. Create a dedicated virtual environment to avoid conflicts with training, model-serving, or system packages. On Ubuntu / Debian, install any missing system dependencies below. Omit `sudo` when running as root.

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git curl ca-certificates build-essential ripgrep bubblewrap libseccomp2
```

Copy the source checkout to the new machine and ensure Python 3.12 and its `venv` module are available. Replace the first path below. The system's default `python3` may be another version; you can substitute a different supported interpreter. Keep the following steps in the same Bash session so `DVP_ROOT` remains set.

```bash
cd /path/to/daVinci-prover
export DVP_ROOT="$PWD"
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m dvp.cli.prove --help
python -m dvp.cli.evaluate --help
```

In a new terminal, reactivate the existing DVP environment:

```bash
cd /path/to/daVinci-prover
export DVP_ROOT="$PWD"
source .venv/bin/activate
```

An existing DVP-only Conda environment or venv is also suitable. Keep it separate from training and model-serving environments. Create environments locally on each machine; for a shared checkout, replace `.venv` with a writable directory on local disk and adjust the activation path. Repository scripts use `python` from the current `PATH` and do not switch environments.

The base install includes LiteLLM for model calls and supports proving and the remote retrieval client. No LiteLLM Proxy service is needed. Install extras as needed:

| Purpose | Command |
| --- | --- |
| Sandboxed Python math experiments | `python -m pip install -e '.[python-experiments]'` |
| Local LeanExplore retrieval | `python -m pip install -e '.[lean-explore-local]'` |
| Standalone retrieval server | `python -m pip install -e '.[lean-explore-server]'` |
| Tests | `python -m pip install -e '.[test]'` |
| Local retrieval and tests | `python -m pip install -e '.[lean-explore-local,test]'` |

`pyproject.toml` declares version ranges so compatible packages already installed in this environment can be reused. The bounds reflect previous tests and upstream requirements; not every allowed combination has been tested. Editable installation (`-e`) makes source changes available without setting `PYTHONPATH`. Run `python` and `dvp` in this environment.

For optional Python experiments, install the `python-experiments` extra, then
run `python -m dvp.cli.check_python` before enabling `--python-execute true`.
All three packages are required: NumPy `>=1.26,<3`, SymPy `>=1.12,<2`, and mpmath
`>=1.3,<2`. They are not part of the base installation. The local worker permits
the standard library and these packages, with network, subprocess, and thread
creation blocked. See [Python experiments](python-experiments.md) for sandbox
limits and examples. Runtime policy v2 changes the executor identity; start a
new run after upgrading from the standard-library-only worker.

When migrating, copy the source and required assets, then reinstall in a new Python environment. Do not copy the old virtual environment.

Local retrieval uses LeanExplore `>=1.3.0,<1.4`, Transformers `>=4.57.1,<5`, Sentence Transformers `>=5.7.0,<6`, and PyTorch `>=2.9.1,<3`. LeanExplore stays within the 1.3 series because DVP uses internal SDK interfaces; BM25 `>=0.3.13,<0.4` retains the current index-loading series. See `pyproject.toml` for all bounds. The Lean / Mathlib commit, index snapshot, and model weights remain matched and do not change when Python dependencies are upgraded.

PyTorch **2.9.1** is the tested baseline; `2.11.0+cu129` also satisfies the installation bounds. A fresh venv does not inherit Torch from the system or other environments. To select a CPU / CUDA build, activate the new environment and follow the [PyTorch installation guide](https://pytorch.org/get-started/locally/) before installing DVP's local/server extra. Pip reuses packages that satisfy the constraints in the active environment. The Torch build determines the CUDA version; GPU use requires a compatible NVIDIA driver. Once retrieval is configured, run `python scripts/check_leanexplore_local.py` to test the actual dependency combination. Prepare model weights and retrieval data separately as described below.

## Lean and Mathlib

Install [elan](https://github.com/leanprover/elan#installation), or ensure its `bin` directory is on `PATH` if it is already installed.

When DVP runs as root, verification subprocesses drop to UID/GID **65534**. The repository, toolchain, Mathlib dependencies, and all parent directories must be readable and traversable by that identity. Use accessible paths outside `/root`; before installing elan, set `export ELAN_HOME=/opt/dvp/elan` or another accessible directory. The default `/root/.elan` is usually inaccessible. These permissions are required in both sandboxed and unsandboxed modes.

```bash
curl https://elan.lean-lang.org/elan-init.sh -sSf | sh -s -- -y --default-toolchain none
export PATH="${ELAN_HOME:-$HOME/.elan}/bin:$PATH"
elan --version
```

The datasets and optional retrieval snapshot use the versions listed [above](#environment). The 4.35 retrieval snapshot was built locally; see [LeanExplore](leanexplore.md#mathlib-435-data-and-build-records) for its provenance and acceptance records.

Install the pinned 4.35 environment into a new workspace directory:

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

Check that the Lean version and Mathlib commit match the table and that the example compiles. `lake exe cache get` prepares dependencies and compiled artifacts according to the manifest; if the cache is incomplete, run `lake build Mathlib` inside the workspace. Avoid `lake update` for this pinned setup because it changes the dependency manifest.

Compiled artifacts are extracted into each package's `.lake/build/`; dependencies live in `.lake/packages/`. Downloaded cache archives normally reside in `~/.cache/mathlib`. Override that directory with `MATHLIB_CACHE_DIR`, or change its base path with `XDG_CACHE_HOME`.

The toolchain and Mathlib commit must match. Changing `lean-toolchain` alone does not upgrade Mathlib, and `.olean` files from other Lean versions cannot be reused.

Retrieval data, source snapshots for context lookup, and the Lean verification workspace are separate resources. `data/leanexplore-sources/` contains source only and cannot serve as `--target-workspace`. Prepare a built verification workspace on each machine; an old machine's symlink is not an installed environment. Complete the compile check above or point `DAVINCI_TARGET_WORKSPACE` to an existing workspace of the same version.

## Retrieval assets and Comparator

1. Register the matching verifier using the [Comparator guide](verification.md). The default registration uses a sandbox; unsandboxed mode requires an explicit choice.
2. Optionally configure [local retrieval](leanexplore.md) or a [remote retrieval server](leanexplore-server.md#4-connect-from-the-dvp-machine). Local retrieval needs the database, indexes, model weights, and matching sources; a remote client needs only the server URL and any required token. Use `--lean-retrieve false` to run without retrieval.
3. Configure the model endpoint and credentials using the [usage guide](usage.md#quick-start), then prove one theorem or evaluate a dataset.

Runtime manifests contain absolute paths and must be regenerated on a new machine. Toolchains, dependencies, and parent directories must be accessible to verification subprocesses, which use UID/GID 65534 when launched as root. Save your own workspace, toolchain, and endpoint settings using the [local configuration templates](local-configuration.md). Runtime files and downloaded assets are excluded from Git and must be prepared separately on each machine.
