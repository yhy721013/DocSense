#!/bin/bash
set -euo pipefail

EXPECTED_SHA256="c99fb4fe574437fc4cb820a4ca15271bca325920861f7139858b36d7f9df78ad"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
DMG_PATH="$SCRIPT_DIR/installer/LibreOffice_26.2.5_MacOS_aarch64.dmg"
TARGET_APP="/Applications/LibreOffice.app"
REPLACE=false

path_exists() {
  [[ -e "$1" || -L "$1" ]]
}

version_is_expected() {
  local version_output="$1"
  if printf '%s\n' "$version_output" |
      grep -Eiq 'LibreOfficeDev|(^|[^[:alpha:]])(alpha|beta|rc|nightly|development)([0-9._-]*)([^[:alpha:]]|$)'; then
    return 1
  fi
  [[ "$version_output" =~ LibreOffice[[:space:]]+26\.2\.5(\.[0-9]+)*([[:space:]]|$) ]]
}

if [[ "${1:-}" == "--replace" ]]; then
  REPLACE=true
elif [[ $# -ne 0 ]]; then
  echo "用法：$0 [--replace]" >&2
  exit 2
fi

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
  echo "错误：此离线包只支持 macOS Apple Silicon (arm64)。" >&2
  exit 1
fi

if [[ ! -f "$DMG_PATH" ]]; then
  echo "错误：缺少官方 DMG：$DMG_PATH" >&2
  exit 1
fi

ACTUAL_SHA256="$(shasum -a 256 "$DMG_PATH" | awk '{print $1}')"
if [[ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]]; then
  echo "错误：DMG SHA-256 校验失败。" >&2
  exit 1
fi

if path_exists "$TARGET_APP"; then
  EXISTING_VERSION=""
  # 目标本身是符号链接时不执行其内容；必须由运维显式确认 --replace。
  if [[ ! -L "$TARGET_APP" && -x "$TARGET_APP/Contents/MacOS/soffice" ]]; then
    EXISTING_VERSION="$("$TARGET_APP/Contents/MacOS/soffice" --version 2>&1 || true)"
    if version_is_expected "$EXISTING_VERSION"; then
      echo "LibreOffice 26.2.5 已安装，无需覆盖。"
      exit 0
    fi
  fi
  if [[ "$REPLACE" != true ]]; then
    echo "错误：$TARGET_APP 已存在但无法确认是锁定稳定版本；确认替换时显式传入 --replace。" >&2
    exit 1
  fi
fi

MOUNT_DIR="$(mktemp -d /tmp/docsense-lo-mount.XXXXXX)"
BACKUP_APP=""
MOUNTED=false

cleanup() {
  if [[ "$MOUNTED" == true ]]; then
    hdiutil detach "$MOUNT_DIR" -quiet >/dev/null 2>&1 || true
  fi
  rmdir "$MOUNT_DIR" >/dev/null 2>&1 || true
}
trap cleanup EXIT

hdiutil attach -nobrowse -readonly -mountpoint "$MOUNT_DIR" "$DMG_PATH" >/dev/null
MOUNTED=true
SOURCE_APP="$MOUNT_DIR/LibreOffice.app"
if [[ ! -x "$SOURCE_APP/Contents/MacOS/soffice" ]]; then
  echo "错误：DMG 中未找到预期 LibreOffice.app。" >&2
  exit 1
fi

if path_exists "$TARGET_APP"; then
  if [[ "$REPLACE" != true ]]; then
    echo "错误：未获得 --replace 授权，拒绝移动既有 $TARGET_APP。" >&2
    exit 1
  fi
  BACKUP_APP="/Applications/LibreOffice.app.docsense-backup-$(date -u +%Y%m%dT%H%M%SZ)"
  sudo mv "$TARGET_APP" "$BACKUP_APP"
  echo "原应用已移动到可恢复备份：$BACKUP_APP"
fi

if ! sudo ditto "$SOURCE_APP" "$TARGET_APP"; then
  if path_exists "$TARGET_APP"; then
    FAILED_APP="/Applications/LibreOffice.app.docsense-failed-$(date -u +%Y%m%dT%H%M%SZ)"
    sudo mv "$TARGET_APP" "$FAILED_APP" || true
  fi
  if [[ -n "$BACKUP_APP" ]] && path_exists "$BACKUP_APP"; then
    sudo mv "$BACKUP_APP" "$TARGET_APP" || true
  fi
  echo "错误：LibreOffice 安装失败，已尝试恢复原应用。" >&2
  exit 1
fi

INSTALLED_VERSION="$("$TARGET_APP/Contents/MacOS/soffice" --version 2>&1 || true)"
if ! version_is_expected "$INSTALLED_VERSION"; then
  if path_exists "$TARGET_APP"; then
    FAILED_APP="/Applications/LibreOffice.app.docsense-failed-$(date -u +%Y%m%dT%H%M%SZ)"
    sudo mv "$TARGET_APP" "$FAILED_APP" || true
  fi
  if [[ -n "$BACKUP_APP" ]] && path_exists "$BACKUP_APP"; then
    sudo mv "$BACKUP_APP" "$TARGET_APP" || true
  fi
  echo "错误：安装后的版本门禁失败：$INSTALLED_VERSION" >&2
  echo "已尝试恢复原应用；失败副本保留在带 docsense-failed 时间戳的路径。" >&2
  exit 1
fi

echo "安装完成：$INSTALLED_VERSION"
echo "DocSense 配置未被修改；请先运行 ./preflight.sh，再由运维人员启用功能开关。"
