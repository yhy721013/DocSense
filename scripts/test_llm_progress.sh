#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="${0:A:h}"
source "${SCRIPT_DIR}/_script_common.sh"

WS_URL="${1:-}"
PAYLOAD_PATH="${2:-${ROOT_DIR}/tests/fixtures/llm/check_task_file_request.json}"
READ_COUNT="${3:-5}"
SEND_QUERY="${4:-false}"

load_env_file
if [[ -z "${WS_URL}" ]]; then
  WS_URL="$(default_ws_url)"
fi

require_file "${PAYLOAD_PATH}"
PYTHON_BIN="$(choose_python)"

exec "${PYTHON_BIN}" - "${WS_URL}" "${PAYLOAD_PATH}" "${READ_COUNT}" "${SEND_QUERY}" <<'PY'
import json
import sys
from pathlib import Path

from simple_websocket import Client


def parse_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


ws_url = sys.argv[1]
payload_path = Path(sys.argv[2])
read_count = int(sys.argv[3])
send_query = parse_bool(sys.argv[4])

ws = Client.connect(ws_url)
try:
    payload_text = payload_path.read_text(encoding="utf-8")
    ws.send(payload_text)

    if send_query:
        query_payload = json.loads(payload_text)
        query_payload["action"] = "query"
        ws.send(json.dumps(query_payload, ensure_ascii=False))

    for _ in range(read_count):
        message = ws.receive(timeout=10)
        if message is None:
            break
        print(message)
finally:
    ws.close()
PY
