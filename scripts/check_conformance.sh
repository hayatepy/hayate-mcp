#!/usr/bin/env bash
set -euo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
port_number="${MCP_CONFORMANCE_PORT:-8930}"
server_url="http://127.0.0.1:${port_number}/mcp"
result_dir="$(mktemp -d "${TMPDIR:-/tmp}/hayate-mcp-conformance.XXXXXX")"
server_log="${result_dir}/server.log"
conformance="${root_dir}/conformance/node_modules/.bin/conformance"

uv run uvicorn server:app \
  --app-dir "${root_dir}/examples/conformance" \
  --host 127.0.0.1 \
  --port "${port_number}" \
  --lifespan off \
  --log-level warning \
  >"${server_log}" 2>&1 &
server_pid=$!

cleanup() {
  kill "${server_pid}" 2>/dev/null || true
  for _ in {1..50}; do
    if ! kill -0 "${server_pid}" 2>/dev/null; then
      wait "${server_pid}" 2>/dev/null || true
      return
    fi
    sleep 0.1
  done
  kill -KILL "${server_pid}" 2>/dev/null || true
  wait "${server_pid}" 2>/dev/null || true
}
trap cleanup EXIT

for attempt in {1..100}; do
  if curl --silent --output /dev/null "http://127.0.0.1:${port_number}/"; then
    break
  fi
  if ! kill -0 "${server_pid}" 2>/dev/null; then
    cat "${server_log}"
    exit 1
  fi
  if [[ "${attempt}" == 100 ]]; then
    cat "${server_log}"
    exit 1
  fi
  sleep 0.1
done

legacy_scenarios=(
  server-initialize
  logging-set-level
  ping
  completion-complete
  tools-list
  tools-call-simple-text
  tools-call-image
  tools-call-audio
  tools-call-embedded-resource
  tools-call-mixed-content
  tools-call-with-logging
  tools-call-error
  tools-call-with-progress
  tools-call-sampling
  tools-call-elicitation
  json-schema-2020-12
  elicitation-sep1034-defaults
  elicitation-sep1330-enums
  resources-list
  resources-read-text
  resources-read-binary
  resources-templates-read
  resources-subscribe
  resources-unsubscribe
  prompts-list
  prompts-get-simple
  prompts-get-with-args
  prompts-get-embedded-resource
  prompts-get-with-image
  dns-rebinding-protection
)

# conformance 0.2.0-alpha.10's legacy server-sse-multiple-streams scenario negotiates
# 2025-11-25, then hard-codes 2025-03-26 on its raw follow-up POSTs. A
# session-correct server must reject those requests. Keep the equivalent
# three-request concurrency coverage in
# tests/test_protocol_version.py::test_concurrent_posts_use_the_sessions_negotiated_version
# until https://github.com/modelcontextprotocol/conformance/issues/412 ships.
for scenario in "${legacy_scenarios[@]}"; do
  "${conformance}" server \
    --url "${server_url}" \
    --scenario "${scenario}" \
    --spec-version 2025-11-25 \
    --output-dir "${result_dir}"
done

modern_scenarios=(
  server-stateless
  completion-complete
  tools-list
  tools-call-simple-text
  tools-call-image
  tools-call-audio
  tools-call-embedded-resource
  tools-call-mixed-content
  tools-call-error
  tools-call-with-progress
  json-schema-2020-12
  server-sse-multiple-streams
  resources-list
  resources-read-text
  resources-read-binary
  resources-templates-read
  sep-2164-resource-not-found
  prompts-list
  prompts-get-simple
  prompts-get-with-args
  prompts-get-embedded-resource
  prompts-get-with-image
  dns-rebinding-protection
  caching
  http-header-validation
  http-custom-header-server-validation
  input-required-result-basic-elicitation
  input-required-result-basic-sampling
  input-required-result-basic-list-roots
  input-required-result-request-state
  input-required-result-multiple-input-requests
  input-required-result-multi-round
  input-required-result-missing-input-response
  input-required-result-non-tool-request
  input-required-result-result-type
  input-required-result-unsupported-methods
  input-required-result-tampered-state
  input-required-result-capability-check
  input-required-result-ignore-extra-params
  input-required-result-validate-input
)

# Pin every 2026 core scenario explicitly. The alpha conformance CLI's
# "active" suite still selects only the pre-2026 subset, while "all" also
# includes optional Tasks extension scenarios that this fixture does not
# advertise.
for scenario in "${modern_scenarios[@]}"; do
  "${conformance}" server \
    --url "${server_url}" \
    --scenario "${scenario}" \
    --spec-version 2026-07-28 \
    --output-dir "${result_dir}"
done

total=$((${#legacy_scenarios[@]} + ${#modern_scenarios[@]}))
echo "Official MCP conformance scenarios passed: ${total}/${total}"
