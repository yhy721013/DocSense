#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="${0:A:h}"
source "${SCRIPT_DIR}/_script_common.sh"

typeset -A CALLER_DATABASE_ENV=()
for env_name in \
  DOCSENSE_RUNTIME_DIR \
  DOCSENSE_LLM_TASK_DB \
  DOCSENSE_KNOWLEDGE_BASE_DB \
  KNOWLEDGE_BASE_DB_PATH
do
  if (( ${+parameters[$env_name]} )); then
    CALLER_DATABASE_ENV[$env_name]="${(P)env_name}"
  fi
done

load_env_file

for env_name env_value in "${(@kv)CALLER_DATABASE_ENV}"; do
  export "${env_name}=${env_value}"
done

PYTHON_BIN="$(choose_python)"

exec "${PYTHON_BIN}" "${ROOT_DIR}/scripts/run_llm_weaponry_directory.py" "$@"
