#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="${0:A:h}"
source "${SCRIPT_DIR}/_script_common.sh"

PORT="${1:-8000}"
DIRECTORY="${2:-tests/fixtures/files}"
PYTHON_BIN="$(choose_python)"

cd "${ROOT_DIR}"
exec "${PYTHON_BIN}" -m http.server "${PORT}" --directory "${DIRECTORY}"
