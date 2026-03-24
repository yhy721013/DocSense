#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="${0:A:h}"
source "${SCRIPT_DIR}/_script_common.sh"

BASE_URL="${1:-}"
PAYLOAD_PATH="${2:-${ROOT_DIR}/tests/fixtures/llm/analysis_request.json}"

load_env_file
if [[ -z "${BASE_URL}" ]]; then
  BASE_URL="$(default_base_url)"
fi

post_json "${BASE_URL}/llm/analysis" "${PAYLOAD_PATH}"
