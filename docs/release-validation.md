# Source Release Validation

[Back to documentation](README.md)

Validation recorded on **2026-11-04** for DVP **0.7.0**. The supported release
workflow is installation from a source checkout with `pip install -e .`.
Lean runtimes, retrieval assets, credentials, and evaluation traces are configured
separately and are excluded from Git.

## Installation and regression checks

A clean export of the staged source was installed in the private validation
environment using its existing dependencies. Outside the repository, `dvp
--help`, `python -m dvp.cli.prove --help`, and `python -m dvp.cli.evaluate --help`
all passed with no machine profile or `PYTHONPATH` override. The checks also
confirmed the package metadata, Lean resources, and executable sandbox adapter.
A package build included the MIT license and README metadata.

In addition to the 36 runtime tests below, **139 tests passed** across the model
client, original-source preservation, final acceptance, development/acceptance
boundary, and finalization lifecycle. These are focused release checks, not a
claim that the full suite was rerun for this release. With the local Lean
workspace and Comparator configured, they can be repeated with:

```bash
python -m pip install -e '.[test]'
python -m pytest -q \
  tests/verification/test_comparator_runtime_hashes.py \
  tests/verification/test_native_comparator.py \
  tests/verification/test_comparator_verdict.py \
  tests/orchestration/test_comparator_runtime_provenance.py
python -m pytest -q \
  tests/verification/test_comparator_source_integrity.py \
  tests/verification/test_comparator_acceptance.py \
  tests/verification/test_development_acceptance_boundary.py \
  tests/llm_clients \
  tests/orchestration/test_final_acceptance_lifecycle.py
```

## Runtime identity

Runtime loading and startup preflight verify the registered SHA256 values of
`lake`, `leanexport`, `leanchecker`, and the sandbox adapter when selected.
Toolchain executable paths must match the selected toolchain. Missing or changed
hashes stop verification and require explicit re-registration; they are never
silently refreshed.

The hash, runtime, verdict, and provenance regression group passed **36 tests**.
It includes executable replacement with unchanged file size and modification
time, a mismatched executable path, and rejection before a verification process
starts. A runtime mismatch is reported as an infrastructure error, not a failed
mathematical proof. These checks detect changes to the registered executables;
they are not a signature over all toolchain libraries or a sandbox substitute.

## Real model and Comparator check

The LiteLLM evaluation started on 2026-11-04 at 15:18:34 (Asia/Shanghai) produced
an accepted proof of `putnam_2025_a3` with `deepseek-v4.1-flash`:

- 18 model turns, all recorded through the LiteLLM backend;
- 22 tool calls, including file operations, LeanExplore retrieval, Python, and
  Lean compilation;
- Comparator exit code `0` and the acceptance marker `Your solution is okay!`.

The worker used Python **3.12.3**, LiteLLM **1.109.0.dev2**, and OpenAI SDK
**2.44.0**. The local regression environment used LiteLLM **1.108.0** and OpenAI
SDK **2.54.0**; these are separate tested environments.

The saved original and candidate source hashes were checked against the recorded
evidence. The proof was then rebuilt and accepted again with the new executable
hash validation, Lean **4.35.0**, and Mathlib commit
`56a005855ea17e0b0958f73ec801ae140b0a1679`. Both runs used the explicitly registered
**unsandboxed** Comparator mode. This confirms the model-to-tools-to-acceptance
path, not sandbox availability on another machine or a new full-benchmark score.

Candidate SHA256:
`7570945f44a9c738bf17b639e422d25e67c49b93ec1a283c38f95c61ecf786f2`.

The README's performance table contains historical benchmark results. Full
evaluation after the LiteLLM migration is a separate measurement.
