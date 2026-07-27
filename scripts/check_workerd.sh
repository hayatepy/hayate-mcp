#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
test_dir="$(mktemp -d)"
log_file="${test_dir}.workerd.log"
port=8790
server_pid=""
real_node="$(command -v node)"
node_shim_dir="${test_dir}/node-shim"
hayate_wheel="${HAYATE_ECOSYSTEM_WHEEL:-}"

if [[ -n "${hayate_wheel}" ]]; then
  if [[ ! -f "${hayate_wheel}" || "${hayate_wheel}" != *.whl ]]; then
    echo "HAYATE_ECOSYSTEM_WHEEL must name an existing wheel: ${hayate_wheel}" >&2
    exit 2
  fi
  hayate_wheel="$(cd "$(dirname "${hayate_wheel}")" && pwd)/$(basename "${hayate_wheel}")"
fi

cleanup() {
  if [[ -n "${server_pid}" ]] && kill -0 "${server_pid}" 2>/dev/null; then
    kill "${server_pid}" 2>/dev/null || true
    wait "${server_pid}" 2>/dev/null || true
  fi
  rm -rf "${test_dir}"
  rm -f "${log_file}"
}
trap cleanup EXIT

uv build --wheel --out-dir "${test_dir}/dist"
wheel_path="$(find "${test_dir}/dist" -name '*.whl' -print -quit)"
test -n "${wheel_path}"

cp "${repo_dir}/examples/workers/entry.py" "${test_dir}/entry.py"
cp "${repo_dir}/examples/workers/wrangler.toml" "${test_dir}/wrangler.toml"
sed \
  's|, "hayate-mcp>=0.10.1"||' \
  "${repo_dir}/examples/workers/pyproject.toml" >"${test_dir}/pyproject.toml"
mkdir -p "${node_shim_dir}"
ln -s "${repo_dir}/scripts/node_pyodide_compat.sh" "${node_shim_dir}/node"

(
  cd "${test_dir}"
  PATH="${node_shim_dir}:${PATH}" \
    HAYATE_MCP_REAL_NODE="${real_node}" \
    uvx --from workers-py==1.15.0 pywrangler sync
  PATH="${node_shim_dir}:${PATH}" \
    HAYATE_MCP_REAL_NODE="${real_node}" \
    VIRTUAL_ENV="${test_dir}/.venv-workers/pyodide-venv" \
    uv pip install \
      --no-build \
      --extra-index-url https://index.pyodide.org/0.29.4 \
      --index-strategy unsafe-best-match \
      "${wheel_path}" \
      workers-runtime-sdk
  cp -R .venv-workers/pyodide-venv/lib/python3.13/site-packages/. python_modules/
  if [[ -n "${hayate_wheel}" ]]; then
    # pywrangler resolves pylock.toml against PyPI and rejects uv override
    # files. The core ecosystem gate supplies an unpublished pure-Python
    # Hayate wheel after that sync so workerd exercises the commit under test.
    uv pip install \
      --target python_modules \
      --reinstall \
      --no-deps \
      "${hayate_wheel}"
  fi
  test -e python_modules/hayate
  test -e python_modules/jsonschema
  test -e python_modules/hayate_mcp
  VIRTUAL_ENV="${test_dir}/.venv-workers/pyodide-venv" \
    uv pip show rpds-py | grep -Fx "Version: 0.30.0"
)

# The Workers wheel must stay independent of the Pydantic-based official SDK.
test ! -e "${test_dir}/python_modules/mcp"
test ! -e "${test_dir}/python_modules/pydantic"

(
  cd "${test_dir}"
  uvx --from workers-py==1.15.0 pywrangler dev --port "${port}"
) >"${log_file}" 2>&1 &
server_pid=$!

ready=false
for _ in {1..60}; do
  if curl --fail --silent --max-time 2 "http://127.0.0.1:${port}/" >/dev/null; then
    ready=true
    break
  fi
  if ! kill -0 "${server_pid}" 2>/dev/null; then
    cat "${log_file}"
    exit 1
  fi
  sleep 1
done
if [[ "${ready}" != true ]]; then
  cat "${log_file}"
  exit 1
fi

cd "${repo_dir}"
uv run python scripts/workerd_interop.py "http://127.0.0.1:${port}/mcp"
