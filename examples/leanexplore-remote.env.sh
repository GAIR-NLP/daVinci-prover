# Source from Bash after setting DAVINCI_LEANEXPLORE_URL=http://SERVER:8765.
# Set DAVINCI_LEANEXPLORE_TOKEN separately if the server requires authentication.
_dvp_configure_remote_retrieval() {
  if [[ -z ${DAVINCI_LEANEXPLORE_URL:-} ]]; then
    printf 'Set DAVINCI_LEANEXPLORE_URL before sourcing this configuration.\n' >&2
    return 1
  fi
  export DAVINCI_RETRIEVE_BACKEND=lean_explore
  export DAVINCI_LEANEXPLORE_BACKEND=remote
  export DAVINCI_LEANEXPLORE_URL
  export DAVINCI_LEANEXPLORE_TIMEOUT="${DAVINCI_LEANEXPLORE_TIMEOUT:-300}"
  export DAVINCI_LEANEXPLORE_EXPECTED_LEAN_VERSION=4.35.0
  export DAVINCI_LEANEXPLORE_EXPECTED_MATHLIB_COMMIT=56a005855ea17e0b0958f73ec801ae140b0a1679
  export DAVINCI_LEANEXPLORE_EXPECTED_SNAPSHOT=20261020_060431
  unset DAVINCI_RETRIEVE_INDEX
}
if _dvp_configure_remote_retrieval; then
  unset -f _dvp_configure_remote_retrieval
else
  unset -f _dvp_configure_remote_retrieval
  return 1
fi
