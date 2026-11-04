# Deploying a standalone LeanExplore retrieval server

[Back to the retrieval guide](leanexplore.md)

The retrieval server keeps the database, BM25/FAISS indexes, embedding model, and reranker loaded, and DVP calls it over HTTP. It uses the existing **394,866** declarations for **Lean 4.35.0 / Mathlib `56a005855ea17e0b0958f73ec801ae140b0a1679`** without regenerating descriptions or vectors. Source context is also added on the server, preserving the client's existing tool-result format.

The server needs only Python, DVP source, and retrieval assets. **It does not require Lean, Comparator, or a compiled Mathlib.** Machines running proofs still need their own Lean / Mathlib / Comparator environments. Hugging Face access, website retrieval, and source downloads are disabled at runtime; initial setup requires access to the release assets, Python packages, and pinned model weights, or prepared local copies.

Actual HTTP verification on the local machine was completed on 2026-10-21: a name query and a natural-language query returned 20 results in total, with raw tool results and model-visible results matching direct local retrieval field by field. The remote client did not load LeanExplore, PyTorch, Sentence Transformers, or FAISS. Requests specifying the wrong version returned HTTP 409. These tests used the local loopback address; a new server deployment still needs a connectivity check from another machine.

## 1. Prepare code and data

Python **3.11 or later** is supported; **3.12** is recommended. Prepare the interpreter using the [installation guide](installation.md#python-environment). Both deployment options below create a dedicated virtual environment on the server's local disk, avoiding reuse of a training, model-service, or system Python environment. Start with 16 CPU threads and 32 GiB of memory; this is a suggested deployment budget, not a measured minimum. Both GPU and CPU are supported; select GPUs with `CUDA_VISIBLE_DEVICES`. Actual throughput depends on hardware and queries. Begin with the default four search pipelines, one shared model executor, and bounded queue; do not start multiple service workers that each load another copy of the models.

The new environment does not inherit PyTorch installed in the system or another environment. If a specific CUDA build is needed, install a build satisfying `>=2.9.1,<3` into the new environment following the [PyTorch installation guide](https://pytorch.org/get-started/locally/), after the activation command below and before installing DVP. Subsequent installation reuses packages that satisfy the dependency ranges.

The three data directories total about **6.7 GiB**. Reserve additional disk space for Python/PyTorch dependencies:

| Directory | Contents | Current size |
| --- | --- | --- |
| `data/leanexplore-local-4.35/20261020_060431/` | Database, BM25/FAISS indexes, and build and acceptance records | 3.6 GiB |
| `data/leanexplore-models/` | Two Qwen3 0.6B models, configuration, and `manifest.json` | 2.3 GiB |
| `data/leanexplore-sources/leanprover-community/mathlib4/56a005855ea17e0b0958f73ec801ae140b0a1679/` | Source at the pinned Mathlib version and `.dvp-source.json` | 803 MiB |

### New machine with shared data

If source and assets are already available on shared storage, enter that checkout. Create the Python environment on the new machine:

```bash
cd /path/to/daVinci-prover
# First deployment: create a dedicated environment on this server's local disk.
python3.12 -m venv /opt/dvp-leanexplore-server-venv
source /opt/dvp-leanexplore-server-venv/bin/activate
# If a specific PyTorch CPU / CUDA build is needed, install it in this environment before the next line.
python -m pip install -e '.[lean-explore-server]'
```

If this machine already has a dedicated DVP retrieval-server environment, activate it and skip creation. The example venv path can be replaced with a new writable directory on the machine's local disk. Activate the same environment in later terminals when starting the server. Share source and data, but do not share or copy virtual environments across machines.

### New machine without shared data

Prepare a DVP source checkout on the server, then create and activate a dedicated Python environment on its local disk:

```bash
cd /path/to/daVinci-prover
python3.12 -m venv /opt/dvp-leanexplore-server-venv
source /opt/dvp-leanexplore-server-venv/bin/activate
# If a specific PyTorch CPU / CUDA build is needed, install it in this environment before the next line.
python -m pip install -e '.[lean-explore-server]'
```

Download the assets from the [LeanExplore 4.35 release](https://github.com/zwhong714/daVinci-prover/releases/tag/leanexplore-4.35.0-20261020_060431). Follow the attached `INSTALL.md` sections **Extract the assets** and **Download the pinned models** from this checkout. The server extra installed above supplies the required retrieval dependencies. Model weights are downloaded separately from Hugging Face; the helper verifies their pinned revisions, sizes, and checksums.

Continue with service startup and HTTP checks below. The local retrieval check in `INSTALL.md` is intended for a machine with the Lean / Mathlib proof workspace; the standalone server does not need that workspace or Comparator.

Python dependencies use the ranges in [`pyproject.toml`](../pyproject.toml), including FastAPI `>=0.128.0,<1`, Pydantic `>=2.7,<3`, and Uvicorn `>=0.40.0,<1`. FastAPI and other upstream dependencies select a compatible Starlette version; DVP no longer pins it separately.

The earlier retrieval test baseline included LeanExplore 1.3.0, PyTorch 2.9.1, Transformers 4.57.1, Sentence Transformers 5.7.0, FAISS CPU 1.15.1, FastAPI 0.128.0, and Uvicorn 0.40.0. These are tested versions, not exact installation requirements. Existing CPU / CUDA builds in the dedicated environment can be reused if they satisfy the ranges. GPU use requires compatible system drivers. After changing dependency versions, complete the actual retrieval acceptance checks below.

## 2. Start the service

Activate the Python environment containing the server dependencies, then run from the repository directory on the new machine:

```bash
source /opt/dvp-leanexplore-server-venv/bin/activate
# Optional: select a GPU, or use an empty value to force CPU.
# export CUDA_VISIBLE_DEVICES=0
# export CUDA_VISIBLE_DEVICES=""

# Optional: set a dedicated Bearer token for the service; use the same value on clients.
# Keep this separate from the model service's OPENAI_API_KEY.
read -r -s -p 'Retrieval token (leave empty for none): ' DAVINCI_LEANEXPLORE_TOKEN
printf '\n'
export DAVINCI_LEANEXPLORE_TOKEN

bash scripts/start_leanexplore_server.sh
```

The default listening address is **`0.0.0.0:8765`**. Default snapshot, model, and source paths are relative to the repository root. Models and indexes load only at service startup; retrieval becomes available after loading completes. GPU mode requires compatible drivers and a PyTorch CUDA wheel. For access over the public Internet, use an HTTPS reverse proxy and a dedicated token.

Adjust paths, the port, or resource limits as needed:

```bash
export DVP_LEANEXPLORE_SNAPSHOT=/data/leanexplore-local-4.35/20261020_060431
export DVP_LEANEXPLORE_MODELS_DIR=/data/leanexplore-models
export DVP_LEANEXPLORE_SOURCE_DIR=/data/leanexplore-sources
export DVP_LEANEXPLORE_HOST=0.0.0.0
export DVP_LEANEXPLORE_PORT=8765
export DVP_LEANEXPLORE_THREADS=16
export DAVINCI_LEANEXPLORE_LOCAL_CONCURRENCY=4
export DVP_LEANEXPLORE_MAX_PENDING=32
export DVP_LEANEXPLORE_REQUEST_TIMEOUT=300
export LEAN_EXPLORE_RERANKER_BATCH_SIZE=4
bash scripts/start_leanexplore_server.sh
```

Set the paths above only when overriding the default directories. The startup script uses `python` from the current `PATH`; it does not activate an environment or install dependencies over the network. Use the same Python environment for installation and startup. Append extra command-line arguments to the startup script, for example `bash scripts/start_leanexplore_server.sh --port 8877`.

The service retains BM25, vector retrieval, and **top-50 reranking**, with `Mathlib` as the default package. The GPU reranker batch size defaults to **4**. By default, four search pipelines share one set of indexes and models; model calls remain serialized while database reads and result processing can overlap. Set `DAVINCI_LEANEXPLORE_LOCAL_CONCURRENCY`, or pass `--local-concurrency`, to change the pipeline limit. This is independent of the model's internal batch size and does not create additional model copies.

At most 32 unfinished HTTP requests are accepted by default, including executing requests. Each has a 300-second timeout, including queue time. A full queue returns HTTP 503; a timeout returns HTTP 504. Requests whose queue deadline expires never start retrieval. A timed-out running request retains its capacity until its underlying work finishes. The client does not silently switch to the website or another version. A longer queue increases waiting capacity, not model throughput. Search responses include a separate `timings` object for server queue time and local execution stages; declaration results are unchanged. `/v1/info` reports the configured pipeline and model concurrency under `execution`.

Use the [local benchmark](leanexplore.md#local-concurrency-and-measurement) on the retrieval machine to measure throughput and check result consistency before increasing the pipeline limit.

To keep the service running after closing the terminal:

```bash
mkdir -p logs
nohup bash scripts/start_leanexplore_server.sh > logs/leanexplore-server.log 2>&1 &
echo $! > logs/leanexplore-server.pid
tail -f logs/leanexplore-server.log
```

## 3. Check the HTTP API on the server

```bash
export DAVINCI_LEANEXPLORE_URL=http://127.0.0.1:8765
DVP_AUTH_HEADERS=()
if [[ -n ${DAVINCI_LEANEXPLORE_TOKEN:-} ]]; then
  DVP_AUTH_HEADERS=(-H "Authorization: Bearer $DAVINCI_LEANEXPLORE_TOKEN")
fi

curl --fail --silent --show-error "$DAVINCI_LEANEXPLORE_URL/healthz"
curl --fail --silent --show-error "${DVP_AUTH_HEADERS[@]}" \
  "$DAVINCI_LEANEXPLORE_URL/v1/info"

curl --fail --silent --show-error --max-time 300 \
  "${DVP_AUTH_HEADERS[@]}" -H 'Content-Type: application/json' \
  "$DAVINCI_LEANEXPLORE_URL/v1/search" \
  --data '{"lean_name":"Nat.Prime.dvd_mul","limit":5,"expected_lean_version":"4.35.0","expected_mathlib_commit":"56a005855ea17e0b0958f73ec801ae140b0a1679","expected_snapshot":"20261020_060431"}'
```

`GET /healthz` is a readiness check that requires no token. `GET /v1/info` returns protocol, data-version, retrieval-configuration, and model information. `POST /v1/search` accepts `lean_name`, `natural_language_query`, `keywords`, `limit`, and three optional expected-version fields; its response contains the complete hit list and version metadata. When a token is configured, both `/v1/info` and `/v1/search` require authentication.

Descriptions, full signatures, dependencies, pinned source, and context in retrieval results are prepared on the server. Remote mode does not depend on local model or source directories on the client. The DVP tool's `include_theorem_proof` / `include_def_proof` parameters still determine whether proof source is shown to the model.

## 4. Connect from the DVP machine

The machine running DVP needs only the base package installed in its own dedicated DVP environment for remote retrieval; it does not need local-retrieval PyTorch/FAISS dependencies or a copy of the data. For a first installation, use the commands below. If the machine already has a dedicated DVP environment, skip creation and activate it directly:

```bash
cd /path/to/daVinci-prover
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
export DAVINCI_LEANEXPLORE_URL=http://SERVER:8765
# If the service has a token, set the same DAVINCI_LEANEXPLORE_TOKEN in this environment.
source examples/leanexplore-remote.env.sh
python scripts/check_leanexplore_remote.py \
  --url "$DAVINCI_LEANEXPLORE_URL" \
  --output analysis/leanexplore-remote-smoke.json
```

Replace `SERVER` with an address reachable from the DVP machine and allow that machine to access server port 8765. The check script uses HTTP and DVP's actual tool path to verify name retrieval, natural-language retrieval, returned versions, and source context, and saves a report. **Passing checks on the server itself does not establish connectivity from another machine; run the commands above on the actual DVP machine too.**

The remote profile pins the expected Lean version to `4.35.0`, Mathlib commit to `56a005855ea17e0b0958f73ec801ae140b0a1679`, and snapshot to `20261020_060431`. A mismatched version returned by the service raises an error. Version requirements also accompany each request, preventing queries from using another dataset after a restart or routing change. The profile configures retrieval only, preserving the current Lean workspace, Comparator, model API, and GPU settings.

For a full proof evaluation, first configure your **4.35 proof runtime** and model using the [usage guide](usage.md#quick-start), then source `examples/leanexplore-remote.env.sh` and run `dvp eval` with `--lean-retrieve true`. Do not also set `--retrieve-index` or subsequently load the local retrieval template. Save machine-specific URLs, tokens, and launch settings under ignored `.local/`; see [local configuration](local-configuration.md).

After deployment, run the connectivity check from the actual DVP machine using an address it can reach. Supply any token through the environment; keep it out of published examples and reports.
