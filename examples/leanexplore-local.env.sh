# Source from Bash after installing local retrieval and preparing its assets.
# Copy to .local/ to customize paths; keep proof-runtime selection separate.
_dvp_configure_local_retrieval() {
  local dvp_profile_root dvp_profile_workspace dvp_profile_cache dvp_profile_version
  dvp_profile_root="${DVP_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}" || return
  dvp_profile_workspace="${DAVINCI_TARGET_WORKSPACE:-$dvp_profile_root/runtime/mathlib4-4.35}"
  dvp_profile_cache="${LEAN_EXPLORE_CACHE_DIR:-$dvp_profile_root/data/leanexplore-local-4.35}"
  dvp_profile_version="${LEAN_EXPLORE_VERSION:-20261020_060431}"

  # Validate before exporting anything. This reads metadata; it never builds Lean.
  python "$dvp_profile_root/scripts/check_leanexplore_config.py" \
    --snapshot "$dvp_profile_cache/$dvp_profile_version" \
    --workspace "$dvp_profile_workspace" || return

  export DVP_ROOT="$dvp_profile_root"
  export DAVINCI_TARGET_WORKSPACE="$dvp_profile_workspace"
  export LEAN_EXPLORE_CACHE_DIR="$dvp_profile_cache"
  export LEAN_EXPLORE_VERSION="$dvp_profile_version"
  export DAVINCI_RETRIEVE_BACKEND=lean_explore
  export DAVINCI_LEANEXPLORE_USE_LOCAL_DATA=false
  export DAVINCI_LEANEXPLORE_PACKAGES=Mathlib
  export DAVINCI_LEANEXPLORE_BM25_ONLY=false
  export DAVINCI_LEANEXPLORE_RERANK_TOP="${DAVINCI_LEANEXPLORE_RERANK_TOP:-50}"
  export DAVINCI_LEANEXPLORE_LOCAL_CONCURRENCY="${DAVINCI_LEANEXPLORE_LOCAL_CONCURRENCY:-4}"
  export DAVINCI_LEANEXPLORE_LOCAL_MAX_PENDING="${DAVINCI_LEANEXPLORE_LOCAL_MAX_PENDING:-128}"
  export DAVINCI_LEANEXPLORE_EMBEDDING_MODEL="${DAVINCI_LEANEXPLORE_EMBEDDING_MODEL:-$DVP_ROOT/data/leanexplore-models/Qwen3-Embedding-0.6B}"
  export DAVINCI_LEANEXPLORE_RERANKER_MODEL="${DAVINCI_LEANEXPLORE_RERANKER_MODEL:-$DVP_ROOT/data/leanexplore-models/Qwen3-Reranker-0.6B}"
  export DAVINCI_LEANEXPLORE_SOURCE_DIR="${DAVINCI_LEANEXPLORE_SOURCE_DIR:-$DVP_ROOT/data/leanexplore-sources}"
  export DAVINCI_LEANEXPLORE_CONTEXT=true
  export LEAN_EXPLORE_RERANKER_BATCH_SIZE="${LEAN_EXPLORE_RERANKER_BATCH_SIZE:-4}"
  export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1
  export DISABLE_TQDM=1 HF_HUB_DISABLE_PROGRESS_BARS=1
  export OMP_NUM_THREADS="${OMP_NUM_THREADS:-16}"
  export MKL_NUM_THREADS="${MKL_NUM_THREADS:-16}"
  export OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-16}"
  export NUMEXPR_NUM_THREADS="${NUMEXPR_NUM_THREADS:-16}"
  unset DAVINCI_RETRIEVE_INDEX
}
if _dvp_configure_local_retrieval; then
  unset -f _dvp_configure_local_retrieval
else
  unset -f _dvp_configure_local_retrieval
  return 1
fi
