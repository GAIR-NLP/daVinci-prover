# LeanExplore Retrieval

[Back to documentation](README.md)

LeanExplore is enabled by default and runs locally on the proof machine using a pinned snapshot, BM25/vector search, a reranker, and source context. Retrieval provides references; Comparator validates proofs.

## Enabling Lean 4.35 retrieval

From the repository root in your [DVP environment](installation.md#python-environment), install retrieval dependencies first:

```bash
python -m pip install -e '.[lean-explore-local]'
```

The pinned Qwen3 embedding and reranker models have passed the offline retrieval check with Transformers **4.57.1** and **5.16.1**, using Sentence Transformers **5.7.0**. To select the tested 5.x version:

```bash
python -m pip install -e '.[lean-explore-local,python-experiments]' 'transformers==5.16.1'
```

Prepare the [Lean workspace](installation.md#lean-and-mathlib), [download the assets](#preparing-models-and-source-on-a-new-machine), and create the [saved profile](local-configuration.md#proof-settings). Then check retrieval:

```bash
source .local/proof.env.sh
source .local/leanexplore.env.sh
python scripts/check_leanexplore_local.py
```

Proof and evaluation commands enable retrieval by default. Missing assets mean installation is incomplete; finish the downloads and checks before running. Use `--lean-retrieve false` only for an intentional run without retrieval. Do not also pass `--retrieve-index`: an explicit JSONL index overrides the selected backend.

The template checks workspace/snapshot compatibility, enables offline model inference, and preserves your model API and Comparator settings. The check script exercises name and natural-language queries with networking blocked; success prints `Local retrieval passed; network attempts: 0`. Add `--output analysis/leanexplore-local-smoke.json` to save the report. The upstream `lean-explore search` CLI calls the website and does not test DVP's local configuration.

## Current snapshot and configuration

| Asset | Default location |
| --- | --- |
| Database and BM25/FAISS indexes | `data/leanexplore-local-4.35/20261020_060431/` |
| Qwen3 Embedding/Reranker 0.6B and pinned manifest | `data/leanexplore-models/` |
| Versioned source trees and `.dvp-source.json` metadata | `data/leanexplore-sources/<owner>/<repo>/<revision>/` |

The database includes generated descriptions. Deployment only downloads assets and runs the embedding/reranker models; it does not regenerate descriptions.

Keep a copy of `examples/leanexplore-local.env.sh` at ignored `.local/leanexplore.env.sh`. Set its supported overrides before sourcing it, or edit that private copy:

| Variable | Default / purpose |
| --- | --- |
| `LEAN_EXPLORE_CACHE_DIR` | `$DVP_ROOT/data/leanexplore-local-4.35` |
| `LEAN_EXPLORE_VERSION` | `20261020_060431` |
| `DAVINCI_LEANEXPLORE_EMBEDDING_MODEL` | `$DVP_ROOT/data/leanexplore-models/Qwen3-Embedding-0.6B` |
| `DAVINCI_LEANEXPLORE_RERANKER_MODEL` | `$DVP_ROOT/data/leanexplore-models/Qwen3-Reranker-0.6B` |
| `DAVINCI_LEANEXPLORE_SOURCE_DIR` | `$DVP_ROOT/data/leanexplore-sources` |
| `DAVINCI_LEANEXPLORE_RERANK_TOP` | `50` candidates |
| `LEAN_EXPLORE_RERANKER_BATCH_SIZE` | `4` |
| `CUDA_VISIBLE_DEVICES` | Select a GPU; empty selects CPU |

The profile selects hybrid retrieval, the `Mathlib` package, and source context. Data uses the SDK's versioned cache layout. `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` keep model loading offline.

### Local concurrency and measurement

One shared set of models and indexes serves up to **4 search pipelines** by default. Embedding/reranking calls are serialized; database reads and result processing can overlap. Increasing pipeline concurrency does not create more model copies or guarantee higher GPU throughput.

- `DAVINCI_LEANEXPLORE_LOCAL_CONCURRENCY`: active pipeline limit, default `4`.
- `DAVINCI_LEANEXPLORE_LOCAL_MAX_PENDING`: active plus queued requests, default `128`; must be at least the pipeline limit. A full queue reports a busy error.

Measure throughput and result consistency after loading the local profile:

```bash
python scripts/benchmark_leanexplore_local.py \
  --concurrency 1 2 4 30 --repetitions 4 \
  --output .local/reports/leanexplore-throughput.json
```

The benchmark warms one retriever and compares the same queries sequentially and concurrently. Its client concurrency is separate from the pipeline limit. Raw traces include `_harness_retrieval_timing`; overlapping timing fields should not be summed. Repeated queries execute again. Programmatic users should close the retriever with `await retriever.aclose()`.

## Mathlib 4.35 data and build records

| Item | Version |
| --- | --- |
| Snapshot | `20261020_060431`, **394,866 declarations** |
| Lean | `4.35.0`, commit `eb6cafb7290e8bd5f2937522d8d804f58f02bafc` |
| Mathlib | `56a005855ea17e0b0958f73ec801ae140b0a1679` |
| LeanExplore build source | `17b9d6c87a2bf0dc4ea27de05da8a1c9965391b7` |

The snapshot follows the pinned upstream extraction, description, embedding, and indexing pipeline. It reused **345,000** descriptions under the exact `(name, source_text)` cache rule and generated **49,866** with `deepSeek-V4-Flash-0731`. All declarations have descriptions and vectors, and both indexes cover the database. Build details and integrity/offline checks are recorded in the snapshot's `build-manifest.json` and `provenance/acceptance-report.json`.

Use the published assets below for deployment.

## Preparing models and source on a new machine

With the local extra installed above, download the archives, `SHA256SUMS`, `models-manifest.json`, and `fetch_models.py` from the [LeanExplore 4.35 release](https://github.com/GAIR-NLP/daVinci-prover/releases/tag/leanexplore-4.35.0-20261020_060431). Its attached `INSTALL.md` contains the full procedure. From a checkout without conflicting assets at these paths:

```bash
export DVP_ROOT="$PWD"
export DVP_RELEASE_DIR=/path/to/downloaded-release-assets
(
  cd "$DVP_RELEASE_DIR"
  sha256sum --check --ignore-missing SHA256SUMS
)
for part in database indexes sources; do
  tar -xzf "$DVP_RELEASE_DIR/leanexplore-4.35.0-20261020_060431-$part.tar.gz" -C "$DVP_ROOT"
done
mkdir -p "$DVP_ROOT/data/leanexplore-models"
cp "$DVP_RELEASE_DIR/models-manifest.json" "$DVP_ROOT/data/leanexplore-models/manifest.json"
unset HF_HUB_OFFLINE TRANSFORMERS_OFFLINE
python "$DVP_RELEASE_DIR/fetch_models.py" --models-dir "$DVP_ROOT/data/leanexplore-models"
```

The download helper fetches pinned Hugging Face revisions and verifies file sizes and SHA256 values. The source archive supplies versioned context files; it does not replace the compiled Mathlib proof workspace. After downloading, source `.local/leanexplore.env.sh` and run the [local check](#enabling-lean-435-retrieval).

## Search results and source versions

The model receives complete declaration headers, available lexical context, source locations/versions, and one description of at most **200 characters** per hit. The default is **5 hits**. Repeated context is referenced once; omitted or truncated content is marked. Full raw results remain in the trajectory.

- Requested proof source (`include_theorem_proof` / `include_def_proof`) comes from the retrieved version. Source-name mismatches are marked; DVP does not invent a matching signature.
- Context requires matching repository/revision metadata in `.dvp-source.json`. Missing or mismatched sources report an error rather than substituting another version. Additional packages need their own matching source trees.
- Headers and lexical context are not fully elaborated Lean types. Use `lean_verify` with `#check` when needed. Retrieval `score` is reciprocal rank, not an accuracy estimate.
- The default result budget is **12,000 characters**. Oversized complete signatures are omitted rather than cut in half. `--max-tool-result-chars 24000` allows more output; each requested source item still has its own 12,000-character limit.

Set `DAVINCI_LEANEXPLORE_CONTEXT=false` after loading the profile to disable enrichment. Start a new run when changing code or retrieval configuration so comparisons use consistent settings.
