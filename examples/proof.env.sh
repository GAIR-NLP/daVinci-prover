# Source from Bash, or copy to .local/proof.env.sh and edit for your machine.
# Keep credentials in your shell or private configuration, not in this example.
export DVP_ROOT="${DVP_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)}"
export PATH="${ELAN_HOME:-$HOME/.elan}/bin:$PATH"
export DAVINCI_TARGET_WORKSPACE="${DAVINCI_TARGET_WORKSPACE:-$DVP_ROOT/runtime/mathlib4-4.35}"
export DVP_COMPARATOR_ROOT="${DVP_COMPARATOR_ROOT:-$DVP_ROOT/runtime/comparator}"
export DAVINCI_MODEL="${DAVINCI_MODEL:-your-served-model-name}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-http://127.0.0.1:8000/v1}"
export DAVINCI_LLM_TRUST_ENV="${DAVINCI_LLM_TRUST_ENV:-0}"

# Export OPENAI_API_KEY before running DVP. For an already exported platform key:
# export OPENAI_API_KEY="$INF_API_KEY"
# Select thinking options only when supported by your model service.
