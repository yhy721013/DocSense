"""Weaponry Control 组件 Manifest 与组件感知 Bootstrap 入口。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.modules.tasks.adapters.sqlite.bootstrap import (
    TaskControlBootstrapResult,
    bootstrap_task_control_database,
)


WEAPONRY_CONTROL_COMPONENT_NAME = "weaponry_control"
WEAPONRY_CONTROL_COMPONENT_VERSION = 1
_MANIFEST_PATH = Path(__file__).with_name("weaponry_control_manifest.json")


def load_weaponry_control_manifest() -> dict[str, Any]:
    """返回独立 Manifest 对象，禁止调用方修改模块级 Schema 身份。"""

    payload = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Weaponry Control 组件 Manifest 必须是 JSON 对象")
    return payload


def bootstrap_weaponry_task_control_database(
    old_database_path: str | Path,
    new_database_path: str | Path,
    *,
    busy_timeout_ms: int = 5_000,
    immutable_offline_snapshot: bool = False,
    writers_stopped_confirmed: bool = False,
) -> TaskControlBootstrapResult:
    """离线创建或安装 Weaponry 组件；生产线程不得早于本门禁启动。"""

    manifest = load_weaponry_control_manifest()
    return bootstrap_task_control_database(
        old_database_path,
        new_database_path,
        busy_timeout_ms=busy_timeout_ms,
        immutable_offline_snapshot=immutable_offline_snapshot,
        writers_stopped_confirmed=writers_stopped_confirmed,
        known_components={WEAPONRY_CONTROL_COMPONENT_NAME: manifest},
        required_components={
            WEAPONRY_CONTROL_COMPONENT_NAME: WEAPONRY_CONTROL_COMPONENT_VERSION
        },
    )


__all__ = [
    "WEAPONRY_CONTROL_COMPONENT_NAME",
    "WEAPONRY_CONTROL_COMPONENT_VERSION",
    "bootstrap_weaponry_task_control_database",
    "load_weaponry_control_manifest",
]
