#!/usr/bin/env bash
set -euo pipefail

real_node="${HAYATE_MCP_REAL_NODE:?HAYATE_MCP_REAL_NODE is required}"
args=()
for arg in "$@"; do
  if [[ "${arg}" != "--experimental-wasm-stack-switching" ]]; then
    args+=("${arg}")
  fi
done

exec "${real_node}" "${args[@]}"
