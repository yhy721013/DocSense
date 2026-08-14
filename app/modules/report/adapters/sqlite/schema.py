"""Report Control 组件 Manifest 与组件感知 Bootstrap 入口。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.modules.tasks.adapters.sqlite.bootstrap import (
    TaskControlBootstrapResult,
    bootstrap_task_control_database,
)


REPORT_CONTROL_COMPONENT_NAME = "report_control"
REPORT_CONTROL_COMPONENT_VERSION = 1
_MANIFEST_PATH = Path(__file__).with_name("report_control_manifest.json")


def load_report_control_manifest() -> dict[str, Any]:
    """每次返回独立 Manifest 对象，避免调用方意外修改模块级共享状态。"""

    payload = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Report Control 组件 Manifest 必须是 JSON 对象")
    return payload


def bootstrap_report_task_control_database(
    old_database_path: str | Path,
    new_database_path: str | Path,
    *,
    busy_timeout_ms: int = 5_000,
    immutable_offline_snapshot: bool = False,
    writers_stopped_confirmed: bool = False,
) -> TaskControlBootstrapResult:
    """在任何 Report 后台线程启动前原子创建或安装必需组件。

    新库在未发布临时文件内同时完成根 Schema 和 Report 组件；既有空根库则在同一进程级
    Schema 锁与 ``BEGIN EXCLUSIVE`` 下安装。普通短连接仍然只验证，绝不会自动补表。
    """

    manifest = load_report_control_manifest()
    return bootstrap_task_control_database(
        old_database_path,
        new_database_path,
        busy_timeout_ms=busy_timeout_ms,
        immutable_offline_snapshot=immutable_offline_snapshot,
        writers_stopped_confirmed=writers_stopped_confirmed,
        known_components={REPORT_CONTROL_COMPONENT_NAME: manifest},
        required_components={
            REPORT_CONTROL_COMPONENT_NAME: REPORT_CONTROL_COMPONENT_VERSION
        },
    )


__all__ = [
    "REPORT_CONTROL_COMPONENT_NAME",
    "REPORT_CONTROL_COMPONENT_VERSION",
    "bootstrap_report_task_control_database",
    "load_report_control_manifest",
]
