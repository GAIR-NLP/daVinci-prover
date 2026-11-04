#!/usr/bin/env bash
# Keep this process in the foreground; use nohup/systemd externally if desired.
set -euo pipefail

dvp_server_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
dvp_server_threads="${DVP_LEANEXPLORE_THREADS:-16}"
if ! command -v python >/dev/null 2>&1; then
  printf 'python not found. Activate the environment where you installed DVP first.\n' >&2
  exit 1
fi

export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
export HF_HUB_DISABLE_TELEMETRY=1
export HF_HUB_DISABLE_PROGRESS_BARS=1
export DISABLE_TQDM=1
export OMP_NUM_THREADS="$dvp_server_threads"
export MKL_NUM_THREADS="$dvp_server_threads"
export OPENBLAS_NUM_THREADS="$dvp_server_threads"
export NUMEXPR_NUM_THREADS="$dvp_server_threads"
export LEAN_EXPLORE_RERANKER_BATCH_SIZE="${LEAN_EXPLORE_RERANKER_BATCH_SIZE:-4}"

# DAVINCI_LEANEXPLORE_TOKEN is consumed only from the process environment.
# CUDA_VISIBLE_DEVICES is inherited, so the caller can select a GPU or CPU.
exec python -m dvp.retrieval_server \
  --snapshot "${DVP_LEANEXPLORE_SNAPSHOT:-$dvp_server_root/data/leanexplore-local-4.35/20261020_060431}" \
  --models-dir "${DVP_LEANEXPLORE_MODELS_DIR:-$dvp_server_root/data/leanexplore-models}" \
  --source-dir "${DVP_LEANEXPLORE_SOURCE_DIR:-$dvp_server_root/data/leanexplore-sources}" \
  --host "${DVP_LEANEXPLORE_HOST:-0.0.0.0}" \
  --port "${DVP_LEANEXPLORE_PORT:-8765}" \
  --threads "$dvp_server_threads" \
  --local-concurrency "${DAVINCI_LEANEXPLORE_LOCAL_CONCURRENCY:-4}" \
  --max-pending "${DVP_LEANEXPLORE_MAX_PENDING:-32}" \
  --request-timeout "${DVP_LEANEXPLORE_REQUEST_TIMEOUT:-300}" \
  "$@"
