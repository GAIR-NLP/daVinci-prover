# Local retrieval with LeanExplore

[Back to the documentation index](README.md)

The local backend performs BM25 and hybrid vector retrieval, reranking, and source-context enrichment. It does not validate proofs. At runtime it does not access the LeanExplore website, Hugging Face, or GitHub; model-service requests are controlled separately by the run configuration.

To share retrieval across DVP machines, see [Deploying a standalone retrieval server](leanexplore-server.md). The server keeps the same data, models, and source context, and clients access your HTTP service through the `remote` backend.

## Enabling Lean 4.35 retrieval

First install local retrieval dependencies with `python -m pip install -e '.[lean-explore-local]'` as described in the [installation guide](installation.md#python-environment), and prepare the retrieval assets and Lean workspace below. With that Python environment activated, run from the repository root:

```bash
source examples/leanexplore-local.env.sh
python scripts/check_leanexplore_local.py
```

Then use the commands in the [usage guide](usage.md#quick-start) with `--lean-retrieve true`. Configure your model endpoint separately; the retrieval template preserves model, workspace, and Comparator settings.

The template enables BM25, vector retrieval, reranking, and source context. The SDK selects an available device and supports CPU or GPU. Set `CUDA_VISIBLE_DEVICES` in your own configuration if needed; adjust reranker batches with `LEAN_EXPLORE_RERANKER_BATCH_SIZE`. No particular machine or GPU is selected by the template.

## Current snapshot and configuration

Download the [LeanExplore 4.35 release](https://github.com/zwhong714/daVinci-prover/releases/tag/leanexplore-4.35.0-20261020_060431) and follow its attached `INSTALL.md`: verify and extract the database, index, and source archives, install the model manifest, then run `fetch_models.py` to download and verify the pinned model weights. Prepare the Python environment and Lean workspace using the [installation guide](installation.md). For a standalone server, follow [retrieval server deployment](leanexplore-server.md#new-machine-without-shared-data).

| Directory | Contents |
| --- | --- |
| `data/leanexplore-local-4.35/20261020_060431/` | Locally built database, FAISS / BM25 indexes, and build and acceptance records |
| `data/leanexplore-models/` | `Qwen3-Embedding-0.6B`, `Qwen3-Reranker-0.6B`, and `manifest.json` recording model commits and file checksums |
| `data/leanexplore-sources/leanprover-community/mathlib4/56a005855ea17e0b0958f73ec801ae140b0a1679/` | Mathlib source for Lean 4.35 and `.dvp-source.json` |

The model weights are downloaded separately from Hugging Face and stored locally with their configuration files. The database already contains generated natural-language explanations; deployment does not require calling a generative model again.

The template uses `python` from the active environment and runs `scripts/check_leanexplore_config.py` before applying retrieval settings. This checks the pinned workspace and completed snapshot without loading retrieval models. It preserves an already selected `DAVINCI_TARGET_WORKSPACE` and `DVP_COMPARATOR_ROOT`. To keep custom paths, copy the template into ignored `.local/`; see [local configuration](local-configuration.md).

The main retrieval settings are shown below for reference. Use the template to apply them with configuration validation:

```bash
export LEAN_EXPLORE_CACHE_DIR="$DVP_ROOT/data/leanexplore-local-4.35"
export LEAN_EXPLORE_VERSION=20261020_060431
export DAVINCI_RETRIEVE_BACKEND=lean_explore
export DAVINCI_LEANEXPLORE_BACKEND=local
export DAVINCI_LEANEXPLORE_USE_LOCAL_DATA=false
export DAVINCI_LEANEXPLORE_PACKAGES=Mathlib
export DAVINCI_LEANEXPLORE_BM25_ONLY=false
export DAVINCI_LEANEXPLORE_RERANK_TOP=50
export DAVINCI_LEANEXPLORE_LOCAL_CONCURRENCY=4
export DAVINCI_LEANEXPLORE_LOCAL_MAX_PENDING=128
export DAVINCI_LEANEXPLORE_EMBEDDING_MODEL="$DVP_ROOT/data/leanexplore-models/Qwen3-Embedding-0.6B"
export DAVINCI_LEANEXPLORE_RERANKER_MODEL="$DVP_ROOT/data/leanexplore-models/Qwen3-Reranker-0.6B"
export DAVINCI_LEANEXPLORE_SOURCE_DIR="$DVP_ROOT/data/leanexplore-sources"
export DAVINCI_LEANEXPLORE_CONTEXT=true
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
unset DAVINCI_RETRIEVE_INDEX
```

`USE_LOCAL_DATA=false` selects the SDK's versioned cache layout; `true` selects its local-build layout. This repository's custom 4.35 snapshot uses the versioned cache layout, so the value is `false`. `BM25_ONLY=false` retains hybrid vector and lexical retrieval, and `RERANK_TOP=50` reranks results with the local reranker. Both models perform local inference only. Retrieval parameters and model versions are pinned, but this does not guarantee the same Top-K ranking as the current website deployment.

By default, the check script blocks Internet socket connections and DNS queries in its process. It performs name and natural-language retrieval through DVP's actual local backend without calling a proof-generation model or Lean verifier. On success it prints `Local retrieval passed; network attempts: 0`. `--output` saves the complete raw results, model-visible results, and network-attempt records. **Do not use `lean-explore search ...` to check the local setup**: that upstream CLI command still calls the website API and is not controlled by DVP's backend variables. The first local query must load the indexes and models; test with low concurrency before increasing it.

Pass `--lean-retrieve true` when running DVP. **Do not also pass `--retrieve-index`**: an explicit JSONL index overrides the backend selection. DVP defaults to local LeanExplore mode; it uses the website only when `DAVINCI_LEANEXPLORE_BACKEND=api` is explicitly set. Without retrieval data, pass `--lean-retrieve false`; source search, file editing, and Lean verification tools remain available.

### Local concurrency and measurement

Local retrieval shares one set of models and indexes. By default, up to **4 search pipelines** can be active, with at most **128 unfinished requests** admitted. Database reads and result processing can overlap model work. Embedding and reranking share a separate execution lock: only one model call runs at a time, including its internal batches. Increasing pipeline concurrency does not load extra model copies or allow simultaneous calls into the shared tokenizer. The database and index work run on a dedicated background event loop so synchronous SDK operations do not block the proof agents' event loop.

`DAVINCI_LEANEXPLORE_LOCAL_CONCURRENCY` sets the pipeline count; `DAVINCI_LEANEXPLORE_LOCAL_MAX_PENDING` bounds active plus queued requests and must be at least that count. A full queue reports a busy error. Cancelling a queued request removes its work; an already running search keeps its capacity until the underlying work completes. Evaluation and the local check script drain and close the shared runtime on exit. Applications using `LeanExploreRetriever` directly should call `await retriever.aclose()` after their callers have stopped.

BM25, embeddings, reranking, package filters, result ordering, and source enrichment retain the same algorithms and settings. Repeated queries still execute; there is no result cache. Raw tool outcomes record `_harness_retrieval_timing`, including pipeline wait, model wait, model execution, context processing, and total time. These measurements are omitted from model-visible feedback. The fields overlap: `search_seconds` includes model wait and execution; `model_seconds` includes lazy loading and the official client call, not just GPU kernels. Do not add every timing field together.

Measure on the machine that will run retrieval after loading its normal local configuration:

```bash
python scripts/benchmark_leanexplore_local.py \
  --concurrency 1 2 4 30 \
  --repetitions 4 \
  --output .local/reports/leanexplore-throughput.json
```

The benchmark loads one retriever, warms it up, and compares sequential and concurrent requests for the same queries. It checks every returned JSON field and the result order, and reports latency, throughput, errors, and differences. It makes no proof-generation model or Lean calls. The client concurrency in this command is distinct from the pipeline limit above. A larger queue only accommodates more waiting callers. Hardware-specific speedups must be measured; pipeline overlap alone does not guarantee higher GPU throughput.

## Mathlib 4.35 data and build records

The current 4.35 data is pinned as follows:

| Item | Pinned value |
| --- | --- |
| Data directory | `data/leanexplore-local-4.35/20261020_060431/` |
| Lean | `4.35.0`, commit `eb6cafb7290e8bd5f2937522d8d804f58f02bafc` |
| Mathlib | `v4.35.0`, commit `56a005855ea17e0b0958f73ec801ae140b0a1679` |
| LeanExplore build source | commit `17b9d6c87a2bf0dc4ea27de05da8a1c9965391b7` |
| Default proof-verification workspace | `runtime/mathlib4-4.35` |
| Build status | **Complete; passed integrity and offline retrieval acceptance checks on 2026-10-20 at 11:10 UTC** |

This data was built locally using a pinned version of the official LeanExplore extraction, description-generation, embedding, and indexing pipeline. This repository distributes the completed snapshot as a GitHub release. It contains **394,866** declarations. Following the official cache rule requiring an **exact `(name, source_text)` match**, the build reused **345,000** descriptions from an earlier official snapshot (Lean 4.32, snapshot `20260714_172516`) and generated the remaining **49,866**. Changes only to a dependency list or docstring do not force regeneration under this rule when the source text is unchanged. This records the build's historical provenance; current retrieval and proofs use only the 4.35 data and environment above and require neither an older runtime nor an older database.

Description generation uses the official dependency layers and prompts: descriptions from earlier layers are supplied to later dependents. It retains the official temperature of `0.3` and sets no additional thinking or output-token limit. This snapshot used `deepSeek-V4-Flash-0731`, with at most **64** concurrent requests to each of two endpoints, for a total cap of **128**. These are historical build parameters, not deployment defaults. The model service and concurrency differ from upstream defaults; the remaining description-generation logic follows the pinned official implementation.

The build completed all **394,866** descriptions and vectors, and both BM25 and FAISS cover every declaration. Of the added vectors, **18** matched the official description-text cache and **49,848** were encoded by the local model. Fingerprint checks confirmed that the retained **345,000** descriptions and vectors were unchanged. Description generation took about **57 minutes**, CPU embedding about **89 minutes**, and index rebuilding about **86 seconds**. Two exact-name and two natural-language queries passed DVP's offline retrieval acceptance checks with **0** network attempts. These checks do not establish full proof success rates or constitute a retrieval-quality benchmark.

The snapshot includes `build-manifest.json` and `provenance/acceptance-report.json`. These are included in the release assets. Historical job logs and deployment-specific acceptance scripts are not shipped.

For a rebuild, check out the LeanExplore build source at the commit above and complete its extraction and description stages. The [build helpers](../tools/README.md#leanexplore-data-builds) in `tools/leanexplore/` require explicit inputs: `build_leanexplore_database.py` takes `--build-status`, description generation takes `--model` and `--extractor-source`, and the embedding and index builders take `--extractor-root`. Run each helper with `--help` for its complete interface. Select paths for your own build; the existing completed snapshot does not need rebuilding.

After all descriptions are present and no process is writing to the database, the embedding and index stages can be run as follows:

```bash
export DVP_BUILD_SNAPSHOT=/path/to/new-snapshot
export DVP_EXTRACTOR_ROOT=/path/to/lean-explore
python tools/leanexplore/build_leanexplore_embeddings.py \
  --database "$DVP_BUILD_SNAPSHOT/lean_explore.db" \
  --extractor-root "$DVP_EXTRACTOR_ROOT" --threads 16
python tools/leanexplore/build_leanexplore_indices.py \
  --output-dir "$DVP_BUILD_SNAPSHOT" \
  --extractor-root "$DVP_EXTRACTOR_ROOT" --threads 16
```

A full build audit must establish complete descriptions and vectors, indexes matching and covering the database, and correct pinned source metadata. Inspect the build and index reports, then perform actual retrieval checks. The optional `tools/leanexplore/complete_leanexplore_snapshot.py` supervisor requires explicit run, snapshot, extractor, and acceptance-script paths; provide your own full integrity checker when using it.

For the completed snapshot documented here, run an offline retrieval smoke check from an activated DVP environment:

```bash
source examples/leanexplore-local.env.sh
python scripts/check_leanexplore_local.py \
  --output analysis/leanexplore-local-smoke.json
```

The smoke check tests name and natural-language retrieval through DVP, including model-visible results and network attempts. It does **not** audit every database row or replace a full build-integrity check. The template checks readiness metadata and version alignment before exporting settings; new Python processes then read the selected snapshot and matching source.

## Preparing models and source on a new machine

The release installation above provides the snapshot, source context, and a pinned model download helper. The official LeanExplore `data fetch` command does not provide this custom snapshot. To rebuild it, follow the pinned extraction, description-generation, embedding, and indexing pipeline, then repeat the integrity and offline retrieval acceptance checks.

The commands below are an alternative for preparing models and Mathlib source manually. Skip them after completing the release installation. Model downloads require Internet access.

```bash
unset HF_HUB_OFFLINE TRANSFORMERS_OFFLINE
python - <<'PYMODELS'
import hashlib
import json
import os
from pathlib import Path
from huggingface_hub import snapshot_download

root = Path(os.environ["DVP_ROOT"]) / "data" / "leanexplore-models"
common = [
    "config.json", "config_sentence_transformers.json", "generation_config.json",
    "model.safetensors", "modules.json", "tokenizer.json", "tokenizer_config.json",
    "merges.txt", "vocab.json",
]
models = [
    ("Qwen/Qwen3-Embedding-0.6B", "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3",
     ["1_Pooling/config.json"]),
    ("Qwen/Qwen3-Reranker-0.6B", "e61197ed45024b0ed8a2d74b80b4d909f1255473",
     ["1_LogitScore/config.json", "sentence_bert_config.json", "chat_template.jinja"]),
]
records = []
for repo_id, revision, extra in models:
    target = root / repo_id.split("/")[-1]
    files = common + extra
    snapshot_download(repo_id, revision=revision, local_dir=target, allow_patterns=files)
    checksums = []
    for filename in files:
        path = target / filename
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        checksums.append({"path": filename, "size": path.stat().st_size,
                          "sha256": digest.hexdigest()})
    records.append({"repo_id": repo_id, "revision": revision,
                    "path": target.name, "files": checksums})
(root / "manifest.json").write_text(json.dumps({"models": records}, indent=2) + "\n")
PYMODELS
```

Finally, export the pinned source from the Mathlib Git workspace prepared using the [installation guide](installation.md). This reads Git objects at the specified commit, without copying local changes or compiling:

```bash
(
  set -e
  export DVP_SOURCE_REVISION=56a005855ea17e0b0958f73ec801ae140b0a1679
  export DVP_SOURCE_ROOT="$DVP_ROOT/data/leanexplore-sources/leanprover-community/mathlib4/$DVP_SOURCE_REVISION"
  DVP_SOURCE_ARCHIVE="$(mktemp)"
  trap 'rm -f "$DVP_SOURCE_ARCHIVE"' EXIT
  git -C "$DAVINCI_TARGET_WORKSPACE" archive --format=tar "$DVP_SOURCE_REVISION" > "$DVP_SOURCE_ARCHIVE"
  export DVP_SOURCE_SHA256="$(sha256sum "$DVP_SOURCE_ARCHIVE" | cut -d ' ' -f 1)"
  mkdir -p "$DVP_SOURCE_ROOT"
  tar -xf "$DVP_SOURCE_ARCHIVE" -C "$DVP_SOURCE_ROOT"
  python - <<'PYSOURCE'
import json
import os
from pathlib import Path
metadata = {
    "repository": "https://github.com/leanprover-community/mathlib4",
    "revision": os.environ["DVP_SOURCE_REVISION"],
    "origin": "git archive of pinned upstream commit",
    "sha256": os.environ["DVP_SOURCE_SHA256"],
}
(Path(os.environ["DVP_SOURCE_ROOT"]) / ".dvp-source.json").write_text(
    json.dumps(metadata, indent=2) + "\n"
)
PYSOURCE
)
source "$DVP_ROOT/examples/leanexplore-local.env.sh"
python "$DVP_ROOT/scripts/check_leanexplore_local.py"
```

Reloading the environment script restores the model offline settings. The local source directory must match the repository and commit in retrieval results, use the layout `<SOURCE_DIR>/<owner>/<repo>/<revision>/`, and contain accurate `.dvp-source.json` metadata. A same-named file from another version cannot replace it.

## Search results and source versions

Raw `lean_retrieve` results retain natural-language explanations and the upstream `id`, `module`, `docstring`, and `dependencies` fields. `source_link`, `source_repository`, `source_revision`, and `source_origin` identify the source; repository and revision fields are parsed from supported GitHub links pinned to a version. Complete raw results are written to the trace; the model receives the compact view described below.

When a tool call sets `include_theorem_proof: true` or `include_def_proof: true`, `source_code` uses the source returned by LeanExplore directly and retains that result's `source_link`. DVP no longer uses remote line numbers to read same-named local files from a different version. If upstream provides no source, it returns `source_error`. `score` is reciprocal rank, not similarity or accuracy.

Source fields preserve the upstream text. `signature_status=source_header` means the declaration name matches the source header and the signature retains all parameters, but it is still not the complete type obtained from `#check`. For declarations generated by mechanisms such as `to_additive`, if `sum_attach` returns source for `prod_attach`, DVP marks it `name_mismatch`, sets `signature=null`, and suggests running `#check Finset.sum_attach` with `lean_verify`. It does not rewrite the multiplicative signature into an unverified additive one. Requested source is then marked `source_code_role=generator`, and the generator's context is not added as the target declaration's context. Anonymous instances, LibraryNote entries, notation, and unrecognized source headers are marked `unavailable`; raw source is not guaranteed to contain a complete proof either.

With `CONTEXT=true` in the local configuration, DVP first verifies the repository and revision in `.dvp-source.json`, then reads the matching source version to add namespaces, outer `variable` declarations, `open` commands, and other information to `context`. It also checks source line numbers against the declaration text. Missing source, version mismatches, or failed checks return `context_status=unavailable` with a specific `context_error` while preserving existing results. **There is no fallback to GitHub requests or a local workspace at another version.** Currently only Mathlib source is prepared. Expanding the package list to `Mathlib,Std,Lean,Init,Batteries` also requires matching source versions for those additional packages to provide their context.

Context is extracted lexically; it is not a complete type after Lean elaboration and may omit implicit context such as macros. If an extracted namespace conflicts with the declaration name, DVP discards only that context, retains the source signature whose name matches, and suggests `#check`. It does not misclassify incomplete lexical parsing as a generated declaration. `context_status` is `available`, `unavailable`, or `disabled`. Set `DAVINCI_LEANEXPLORE_CONTEXT=false` to disable context enrichment. Without a profile, the default is `false` in local mode and `true` in API mode; the local environment script explicitly enables it and provides the source directory.

The tool defaults to five results; an explicit `limit` overrides this. The model view keeps complete source signatures, lexical context, source locations and versions, and one description of at most 200 characters per hit. It omits internal dependency lists, duplicate descriptions, and ordinary success/status fields. A known source-name mismatch keeps its diagnostic and omits potentially misleading prose. Identical context is stored once: `context_ref` points to an earlier result's `rank`. Source/version errors and missing signature or context information remain visible.

The default tool-output budget is 12,000 characters, used as a ceiling rather than a target. Unused space is not filled with longer explanations or dependency lists. Explicitly requested proof source can use the remaining space. Complete signatures and scope commands are never cut in the middle; oversized hits are skipped and counted so later hits can still be shown. Description/source truncation and omitted results are reported. To allow more results or requested source, set `--max-tool-result-chars 24000`; this does not remove the 12,000-character limit on each source item. Full original metadata remains available in the trajectory.

After changing integration code, start a new process with a new `--run-root` instead of resuming an old experiment directory. Pin the model, dataset, and Mathlib commit when comparing retrieval behavior.

## Optional website API

The main workflow does not access the website. Switch explicitly only when remote website retrieval is needed:

```bash
export DAVINCI_RETRIEVE_BACKEND=lean_explore
export DAVINCI_LEANEXPLORE_BACKEND=api
export DAVINCI_LEANEXPLORE_PACKAGES=Mathlib
export DAVINCI_LEANEXPLORE_TIMEOUT=30
export DAVINCI_LEANEXPLORE_RETRIES=2
export DAVINCI_LEANEXPLORE_CONTEXT=true
unset DAVINCI_RETRIEVE_INDEX
```

The website's public API currently requires no account or API key; see the [official configuration guide](https://github.com/lean-explore/lean-explore/blob/main/docs/configuration.md#authentication). `OPENAI_API_KEY` is a model-service credential and is unrelated to retrieval. API mode does not use local snapshots or model paths; `HF_HUB_OFFLINE` / `TRANSFORMERS_OFFLINE` do not block HTTP API requests.

With context enabled in API mode, DVP requests GitHub files at the commit or tag specified by `source_link`. Set `DAVINCI_LEANEXPLORE_CONTEXT=false` to disable these requests. Connection failures, request timeouts, and HTTP 408 / 429 / 500 / 502 / 503 / 504 responses receive up to two retries with backoff by default, respecting the server's `Retry-After`. `DAVINCI_LEANEXPLORE_RETRIES` accepts `0`, `1`, or `2`. API subqueries and retries within a tool call share the `DAVINCI_LEANEXPLORE_TIMEOUT` budget; source-context fetching has a separate total budget of up to 10 seconds. Permanent errors and cancellations are not retried.

Website data can change, and the API version is not pinned by the local `LEAN_EXPLORE_VERSION`. Check the source version in each result's `source_link`; filtering to the `Mathlib` package does not pin a commit. To restore local mode, source `examples/leanexplore-local.env.sh` again. For evaluation, follow the [usage guide](usage.md#quick-start) with a new run directory.
