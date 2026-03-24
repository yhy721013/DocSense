#!/bin/zsh

set -euo pipefail

SCRIPT_FILE="${(%):-%N}"
SCRIPT_DIR="${SCRIPT_FILE:A:h}"
ROOT_DIR="${SCRIPT_DIR:h}"

choose_python() {
  if [[ -x "${ROOT_DIR}/.venv/bin/python" ]]; then
    echo "${ROOT_DIR}/.venv/bin/python"
    return 0
  fi
  if [[ -x "${ROOT_DIR}/venv/bin/python" ]]; then
    echo "${ROOT_DIR}/venv/bin/python"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi

  print -u2 -- "未找到可用的 Python 解释器"
  return 1
}

load_env_file() {
  local env_file="${ROOT_DIR}/.env"
  if [[ ! -f "${env_file}" ]]; then
    env_file="${ROOT_DIR}/.env.example"
  fi
  if [[ -f "${env_file}" ]]; then
    set -a
    source "${env_file}"
    set +a
  fi
}

default_base_url() {
  echo "http://${WEB_UI_HOST:-127.0.0.1}:${WEB_UI_PORT:-5001}"
}

default_ws_url() {
  echo "ws://${WEB_UI_HOST:-127.0.0.1}:${WEB_UI_PORT:-5001}/llm/progress"
}

require_file() {
  local file_path="$1"
  if [[ ! -f "${file_path}" ]]; then
    print -u2 -- "文件不存在: ${file_path}"
    return 1
  fi
}

post_json() {
  local url="$1"
  local payload_path="$2"
  local response_file
  local http_status

  require_file "${payload_path}"
  response_file="$(mktemp)"

  http_status="$(
    curl -sS \
      -o "${response_file}" \
      -w "%{http_code}" \
      -X POST \
      -H "Content-Type: application/json; charset=utf-8" \
      --data-binary "@${payload_path}" \
      "${url}"
  )"

  cat "${response_file}"
  echo
  rm -f "${response_file}"

  if (( http_status >= 400 )); then
    print -u2 -- "请求失败，HTTP 状态码: ${http_status}"
    return 1
  fi
}
