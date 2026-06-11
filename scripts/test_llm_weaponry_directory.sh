#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="${0:A:h}"
source "${SCRIPT_DIR}/_script_common.sh"

load_env_file
PYTHON_BIN="$(choose_python)"

exec "${PYTHON_BIN}" "${ROOT_DIR}/scripts/run_llm_weaponry_directory.py" "$@"
