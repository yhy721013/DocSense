"""Analysis Control 组件 Manifest 与组件感知 Bootstrap 入口。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.modules.tasks.adapters.sqlite.bootstrap import (
    TaskControlBootstrapResult,
    bootstrap_task_control_database,
)


ANALYSIS_CONTROL_COMPONENT_NAME = "analysis_control"
ANALYSIS_CONTROL_COMPONENT_VERSION = 2
_MANIFEST_PATH = Path(__file__).with_name("analysis_control_manifest.json")


def load_analysis_control_manifest() -> dict[str, Any]:
    """返回新的 Manifest 对象，避免调用方修改模块级 Schema 身份。"""

    payload = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("Analysis Control 组件 Manifest 必须是 JSON 对象")
    return payload


def bootstrap_analysis_task_control_database(
    old_database_path: str | Path,
    new_database_path: str | Path,
    *,
    busy_timeout_ms: int = 5_000,
    immutable_offline_snapshot: bool = False,
    writers_stopped_confirmed: bool = False,
) -> TaskControlBootstrapResult:
    """离线安装 Analysis 组件；生产线程不得早于该严格门禁启动。"""

    manifest = load_analysis_control_manifest()
    return bootstrap_task_control_database(
        old_database_path,
        new_database_path,
        busy_timeout_ms=busy_timeout_ms,
        immutable_offline_snapshot=immutable_offline_snapshot,
        writers_stopped_confirmed=writers_stopped_confirmed,
        known_components={ANALYSIS_CONTROL_COMPONENT_NAME: manifest},
        required_components={
            ANALYSIS_CONTROL_COMPONENT_NAME: ANALYSIS_CONTROL_COMPONENT_VERSION
        },
    )


__all__ = [
    "ANALYSIS_CONTROL_COMPONENT_NAME",
    "ANALYSIS_CONTROL_COMPONENT_VERSION",
    "bootstrap_analysis_task_control_database",
    "load_analysis_control_manifest",
]
