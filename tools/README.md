# Maintenance Tools

These optional utilities rebuild LeanExplore data or diagnose model services. They are not required to run proofs with an existing retrieval snapshot. For installation, retrieval checks, and server startup, use the [documentation](../docs/README.md) and `scripts/`.

Activate your DVP environment and run commands from the repository root. Each utility documents its inputs with `--help`:

```bash
python tools/leanexplore/build_leanexplore_database.py --help
python tools/diagnostics/probe_llm_cache.py --help
```

## LeanExplore data builds

The helpers in `tools/leanexplore/` use extracted Lean data and the upstream LeanExplore build source. See the [retrieval guide](../docs/leanexplore.md#mathlib-435-data-and-build-records) for pinned versions, prerequisites, and build checks.

| Utility | Purpose |
| --- | --- |
| [`build_leanexplore_database.py`](leanexplore/build_leanexplore_database.py) | Build a database from extracted declarations and package build records |
| [`audit_leanexplore_reuse.py`](leanexplore/audit_leanexplore_reuse.py) | Compare snapshots and optionally copy reusable descriptions and vectors with `--apply-cache` |
| [`generate_leanexplore_descriptions.py`](leanexplore/generate_leanexplore_descriptions.py) | Generate missing descriptions through model endpoints using the upstream pipeline |
| [`build_leanexplore_embeddings.py`](leanexplore/build_leanexplore_embeddings.py) | Generate missing vectors with a local embedding model |
| [`build_leanexplore_indices.py`](leanexplore/build_leanexplore_indices.py) | Build BM25 and FAISS indexes from the completed database |
| [`complete_leanexplore_snapshot.py`](leanexplore/complete_leanexplore_snapshot.py) | Wait for description generation, then run embeddings, indexing, and a supplied integrity checker |

Configure build inputs, model endpoints, and output directories explicitly. Description generation makes real model requests; local embedding and index stages require the relevant models and dependencies.

`leanexplore_upstream.py` contains shared upstream integration helpers used by the build and diagnostic commands; it is not a standalone command.

## Diagnostics

| Utility | Purpose |
| --- | --- |
| [`benchmark_leanexplore_generation.py`](diagnostics/benchmark_leanexplore_generation.py) | Measure description-generation throughput across concurrency levels |
| [`test_leanexplore_official_generation.py`](diagnostics/test_leanexplore_official_generation.py) | Run selected samples through the official description-generation pipeline |
| [`probe_llm_cache.py`](diagnostics/probe_llm_cache.py) | Inspect model-service prefix-cache reporting and time to first token |

These diagnostics make real model requests. Keep credentials in environment variables and machine-specific settings in ignored `.local/`; save reports under ignored `analysis/`. For a cache-probe example, see the [usage guide](../docs/usage.md#distributed-inference-and-prefix-caching).
