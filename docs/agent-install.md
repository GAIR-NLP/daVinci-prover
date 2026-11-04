# Installation for Coding Agents

[Back to documentation](README.md)

Prepare this checkout so the user can run the [README Quick start](../README.md#quick-start). Complete the checks below; report what actually ran. Do not start Putnam-200 or push changes unless requested.

## 1. Inspect and reuse

Read the [installation guide](installation.md) and [verification setup](verification.md#installing-the-lean-435-verifier). Inspect existing `.local/proof.env.sh` settings without exposing credentials. Check `git status --short`, available Python environments, Lean / Mathlib paths, and Comparator registration. Reuse compatible installations and the user's endpoint, model, credentials, and authorized settings. Ask only for information unavailable from the environment or prior instructions.

Preserve checkout changes and existing configuration. Do not reset the repository, overwrite an environment, or replace supplied credentials with README placeholders. Never print keys. If no checkout exists, use the README clone command in a suitable location.

## 2. Install Python dependencies

Use Linux and an isolated Python **3.11+** environment; **3.12** is recommended. Reuse an existing DVP-only environment or create one:

```bash
cd /path/to/daVinci-prover
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
dvp --help
```

Use the [system package list](installation.md#python-environment) to install missing prerequisites. The base installation is sufficient for both README examples: retrieval is explicitly disabled, and Python experiments are disabled by default. Do not download retrieval databases, model weights, or optional numerical packages for this setup. Add them only when requested, following their respective guides.

## 3. Prepare Lean and Mathlib

Follow [Lean and Mathlib](installation.md#lean-and-mathlib) using **Lean 4.35.0** and Mathlib commit **`56a005855ea17e0b0958f73ec801ae140b0a1679`**. Reuse a matching workspace; otherwise create a separate one. Fetch compiled artifacts with `lake exe cache get`; build missing artifacts if needed. Do not run `lake update` or reuse another Lean version's cache.

When running as root, verification subprocesses use UID/GID **65534**. Choose paths readable and traversable by that identity for the checkout, toolchain, Mathlib, dependencies, and all parent directories. An elan installation under `/root` usually needs an accessible alternative, such as `/opt/dvp/elan`. Avoid broad permission changes.

Confirm `git rev-parse HEAD`, `lake env lean --version`, and the guide's actual `import Mathlib` proof compile successfully. A source-only retrieval snapshot is not a verification workspace.

## 4. Register and validate Comparator

Run the [registration commands](verification.md#installing-the-lean-435-verifier) against those resolved paths. Registration must run real positive and negative checks: accept a valid proof and reject a changed statement, `sorry`, and an added axiom. Inspect the setup report and resulting `4.35.0/runtime.json`; file existence alone is insufficient.

Use the selected verification mode consistently. If sandbox setup fails, diagnose it and report the blocker; never silently switch to `--no-sandbox`. Honor an already authorized mode choice. Re-register after moving paths or changing registered executables; do not edit recorded hashes to bypass validation.

## 5. Save and restore machine settings

Create the template only if it is absent:

```bash
mkdir -p .local
if [[ ! -e .local/proof.env.sh ]]; then
  cp examples/proof.env.sh .local/proof.env.sh
fi
```

Save concrete, non-sensitive paths and model settings there: `DVP_ROOT`, `DAVINCI_TARGET_WORKSPACE`, `DVP_COMPARATOR_ROOT`, any custom `ELAN_HOME` / `PATH`, and the user's model and endpoint. Preserve existing settings and keep credentials in their existing secure source.

Verify restoration in a fresh shell with inherited DVP workspace/runtime variables unset. Activate the chosen environment, source `.local/proof.env.sh`, check `dvp --help`, and verify the workspace version and runtime manifest again. Ensure success does not depend on exports from the installation shell.

## 6. Run one proof and hand off

If usable credentials are available, run the README **single-theorem** command with the configured service and a fresh run directory. Keep retrieval disabled. DeepSeek settings are examples; use only thinking options supported by the chosen service.

Require `result.json` to report success and score **1**, with Comparator acceptance in `comparator_artifacts/`. Compilation alone is insufficient. If credentials are unavailable, finish local setup and state **“Model API and end-to-end proof not tested.”**

Report checked versions, verification mode, proof result and artifact path, or the precise remaining blocker. Provide this machine's actual activation and configuration commands. If using an environment other than `.venv`, give the replacement for the README activation line. Leave the full Putnam-200 command ready for the user to start.
