# Installation for Coding Agents

[Back to documentation](README.md)

Prepare this checkout so the user can run the [README Quick start](../README.md#quick-start) with **local retrieval and Python experiments enabled**. Complete the checks below; report what actually ran. Do not start Putnam-200 or push changes unless requested.

## 1. Inspect and reuse

Read the [installation guide](installation.md) and [verification setup](verification.md#installing-the-lean-435-verifier). Inspect existing `.local/proof.env.sh` and `.local/leanexplore.env.sh` settings without exposing credentials. Check `git status --short`, available Python environments, Lean / Mathlib paths, Comparator registration, and retrieval assets. Reuse compatible installations and the user's endpoint, model, credentials, and authorized settings. Ask only for information unavailable from the environment or prior instructions.

Preserve checkout changes and existing configuration. Do not reset the repository, overwrite an environment, or replace supplied credentials with README placeholders. Never print keys. If no checkout exists, use the README clone command in a suitable location.

## 2. Install Python dependencies

Use Linux and an isolated Python **3.11+** environment; **3.12** is recommended. Reuse an existing DVP-only environment or create one:

```bash
cd /path/to/daVinci-prover
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[lean-explore-local,python-experiments]'
dvp --help
```

Use the [system package list](installation.md#python-environment) to install missing prerequisites. The full installation includes LeanExplore and its local inference dependencies, plus **NumPy, SymPy, and mpmath**. For a specific CPU / CUDA PyTorch build, install it in this environment before the combined install command above.

## 3. Prepare Lean and Mathlib

Follow [Lean and Mathlib](installation.md#lean-and-mathlib) using **Lean 4.35.0** and Mathlib commit **`56a005855ea17e0b0958f73ec801ae140b0a1679`**. Reuse a matching workspace; otherwise create a separate one. Fetch compiled artifacts with `lake exe cache get`; build missing artifacts if needed. Do not run `lake update` or reuse another Lean version's cache.

When running as root, verification subprocesses use UID/GID **65534**. Choose paths readable and traversable by that identity for the checkout, toolchain, Mathlib, dependencies, and all parent directories. An elan installation under `/root` usually needs an accessible alternative, such as `/opt/dvp/elan`. Avoid broad permission changes.

Confirm `git rev-parse HEAD`, `lake env lean --version`, and the guide's actual `import Mathlib` proof compile successfully. A source-only retrieval snapshot is not a verification workspace.

## 4. Register and validate Comparator

Run the [registration commands](verification.md#installing-the-lean-435-verifier) against those resolved paths. Registration must run real positive and negative checks: accept a valid proof and reject a changed statement, `sorry`, and an added axiom. Inspect the setup report and resulting `4.35.0/runtime.json`; file existence alone is insufficient.

Use the selected verification mode consistently. If sandbox setup fails, diagnose it and report the blocker; never silently switch to `--no-sandbox`. Honor an already authorized mode choice. Re-register after moving paths or changing registered executables; do not edit recorded hashes to bypass validation.

## 5. Prepare retrieval and Python

Follow the [local retrieval guide](leanexplore.md#preparing-models-and-source-on-a-new-machine) to download the published Lean 4.35 database, BM25/FAISS indexes, versioned source archives, and pinned embedding/reranker weights. Verify checksums and snapshot/workspace compatibility. Installing the Python package alone does not install these assets; use the published snapshot rather than rebuilding descriptions or indexes.

Prepare the [Python sandbox](python-experiments.md#enable), including its native libraries and Landlock/seccomp support. Both retrieval and Python are part of the requested setup. If dependencies, assets, or isolation checks fail, report the incomplete setup and its cause; do not disable either tool to make the installation appear successful. A reduced configuration is only for an explicit user request.

## 6. Save and restore machine settings

Create the template only if it is absent:

```bash
mkdir -p .local
if [[ ! -e .local/proof.env.sh ]]; then
  cp examples/proof.env.sh .local/proof.env.sh
fi
if [[ ! -e .local/leanexplore.env.sh ]]; then
  cp examples/leanexplore-local.env.sh .local/leanexplore.env.sh
fi
```

Save proof paths and model settings in `.local/proof.env.sh`: `DVP_ROOT`, `DAVINCI_TARGET_WORKSPACE`, `DVP_COMPARATOR_ROOT`, custom `ELAN_HOME` / `PATH`, and the user's model and endpoint. Save retrieval cache, snapshot, source, model paths, and device settings in `.local/leanexplore.env.sh`. Preserve existing settings and keep credentials in their existing secure source.

Verify restoration in a fresh shell with inherited DVP and retrieval configuration unset. Activate the chosen environment, then run:

```bash
source .local/proof.env.sh
source .local/leanexplore.env.sh
dvp --help
python scripts/check_leanexplore_local.py
python -m dvp.cli.check_python
```

Require both checks to pass, and verify the workspace version and runtime manifest again. Success must not depend on exports from the installation shell.

## 7. Run one proof and hand off

If usable credentials are available, run the README **single-theorem** command with the configured service and a fresh run directory. Keep retrieval and Python enabled. DeepSeek settings are examples; use only thinking options supported by the chosen service.

Require `result.json` to report success and score **1**, with Comparator acceptance in `comparator_artifacts/`. Compilation alone is insufficient. If credentials are unavailable, finish local setup and state **“Model API and end-to-end proof not tested.”**

Report checked versions, verification mode, retrieval and Python check results, and the proof result with its artifact path, or the precise remaining blocker. Provide this machine's actual activation and configuration commands. If using an environment other than `.venv`, give the replacement for the README activation line. Leave the full Putnam-200 command ready for the user to start.
