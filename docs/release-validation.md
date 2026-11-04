# Source Release Validation

[Back to documentation](README.md)

Recorded on **2026-11-04** for DVP **0.7.0**.

## Installation and regression checks

- A clean source export installed with `pip install -e .` using existing test-environment dependencies.
- CLI help, package metadata, Lean resources, and the executable sandbox adapter passed checks outside the repository, without a machine profile or `PYTHONPATH` override.
- The package build included the MIT license and README.
- **36** runtime identity/verdict tests and **139** client/acceptance tests passed. These were focused checks, not a full-suite rerun.

With Lean and Comparator configured, repeat the checks with:

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

Tests reject changed executables, missing hashes, and mismatched toolchain paths
before verification. Replacement is detected even if file size and modification
time stay the same. Checks cover registered executables, not every toolchain
library. See [registration](verification.md#installing-the-lean-435-verifier).

## Real model and Comparator check

`deepseek-v4.1-flash` solved `putnam_2025_a3` through LiteLLM in **18 turns** and
**22 tool calls**. Comparator returned exit code `0` and `Your solution is okay!`.
The recorded proof was rebuilt and accepted again after executable-hash validation.

| Environment | Versions |
| --- | --- |
| Evaluation worker | Python 3.12.3, LiteLLM 1.109.0.dev2, OpenAI SDK 2.44.0 |
| Regression tests | LiteLLM 1.108.0, OpenAI SDK 2.54.0 |
| Proof verification | Lean 4.35.0; Mathlib `56a005855ea17e0b0958f73ec801ae140b0a1679` |

Both acceptance checks used explicitly registered **unsandboxed** mode.
Candidate SHA256: `7570945f44a9c738bf17b639e422d25e67c49b93ec1a283c38f95c61ecf786f2`.
This validates one complete execution path; the README's benchmark rates remain
historical measurements.
