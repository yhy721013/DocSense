#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
EXECUTABLE_PATH="${DOCSENSE_LIBREOFFICE_EXECUTABLE:-/Applications/LibreOffice.app/Contents/MacOS/soffice}"
SAMPLES_DIR="$SCRIPT_DIR/samples"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "错误：此 preflight 只支持 macOS Apple Silicon (arm64)。" >&2
  exit 1
fi

if [[ ! -s "$SCRIPT_DIR/SHA256SUMS" ]]; then
  echo "错误：缺少或为空的 SHA256SUMS，拒绝执行未校验的离线包。" >&2
  exit 1
fi
(
  cd "$SCRIPT_DIR"
  shasum -a 256 -c SHA256SUMS
)

if [[ ! -x "$EXECUTABLE_PATH" ]]; then
  echo "错误：找不到可执行 LibreOffice：$EXECUTABLE_PATH" >&2
  exit 1
fi

python3 "$SCRIPT_DIR/smoke_test.py" \
  --executable "$EXECUTABLE_PATH" \
  --samples-dir "$SAMPLES_DIR" \
  --expected-version-prefix "26.2.5" \
  --timeout-seconds 120

echo "macOS Apple Silicon 离线依赖 preflight 通过。"
echo "DocSense 配置未被修改；可由运维人员设置 DOCSENSE_LEGACY_OFFICE_ENABLED=true。"
