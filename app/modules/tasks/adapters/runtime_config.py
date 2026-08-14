"""阶段 2 Task Runtime Config 的显式 mapping/environment 装载 Adapter。"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import math
import os
from pathlib import Path
from typing import Mapping

from app.modules.tasks.domain import TaskLeaseRuntimeSettings


logger = logging.getLogger(__name__)


def _positive_int(mapping: Mapping[str, str], key: str, default: int) -> int:
    raw = mapping.get(key, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{key} 必须是正整数") from exc
    if value <= 0:
        raise ValueError(f"{key} 必须是正整数")
    return value


def _number(
    mapping: Mapping[str, str],
    key: str,
    default: float,
    *,
    allow_zero: bool = False,
) -> float:
    raw = mapping.get(key, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{key} 必须是有限数字") from exc
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{key} 必须是{'非负' if allow_zero else '正'}有限数字")
    return value


@dataclass(frozen=True, slots=True)
class TaskRuntimeConfig:
    """任何后台线程创建前必须完整构造成功的内部配置。"""

    database_path: Path
    report_worker_count: int
    weaponry_worker_count: int
    file_worker_count: int
    heavy_concurrency: int
    executor_scan_interval_seconds: float
    reaper_scan_interval_seconds: float
    recovery_lease_duration_seconds: float
    lease: TaskLeaseRuntimeSettings
    fingerprint: str

    @classmethod
    def from_mapping(
        cls,
        mapping: Mapping[str, str],
        *,
        runtime_directory: str | Path,
        legacy_task_database_path: str | Path,
    ) -> "TaskRuntimeConfig":
        if not isinstance(mapping, Mapping):
            raise TypeError("mapping 必须是 Mapping")
        runtime_root = Path(runtime_directory).resolve()
        legacy_path = Path(legacy_task_database_path).resolve()
        raw_database = mapping.get(
            "DOCSENSE_TASK_CONTROL_DB_PATH",
            str(runtime_root / "db" / "task-control-v2.sqlite3"),
        ).replace("${DOCSENSE_RUNTIME_DIR}", str(runtime_root))
        database_path = Path(raw_database).resolve()
        if database_path == legacy_path:
            raise ValueError("Task Control v2 路径不得与旧 Task DB 相同")

        heartbeat = _number(mapping, "DOCSENSE_TASK_HEARTBEAT_INTERVAL_SECONDS", 5)
        lease_duration = _number(mapping, "DOCSENSE_TASK_LEASE_DURATION_SECONDS", 30)
        busy = _number(mapping, "DOCSENSE_TASK_SQLITE_BUSY_BUDGET_SECONDS", 2)
        jitter = _number(
            mapping,
            "DOCSENSE_TASK_MAX_CLOCK_JITTER_SECONDS",
            3,
            allow_zero=True,
        )
        stop_grace = _number(mapping, "DOCSENSE_TASK_STOP_GRACE_SECONDS", 10)
        lease = TaskLeaseRuntimeSettings(
            heartbeat_interval_seconds=heartbeat,
            lease_duration_seconds=lease_duration,
            sqlite_busy_budget_seconds=busy,
            max_clock_jitter_seconds=jitter,
            stop_grace_seconds=stop_grace,
        )
        recovery_lease = _number(
            mapping,
            "DOCSENSE_TASK_RECOVERY_LEASE_DURATION_SECONDS",
            30,
        )
        minimum = 3 * heartbeat + 2 * busy + jitter
        if recovery_lease < minimum:
            raise ValueError("Recovery lease 不满足冻结租约不等式")
        reaper_scan = _number(mapping, "DOCSENSE_TASK_REAPER_SCAN_INTERVAL_SECONDS", 5)
        if reaper_scan > min(lease_duration, recovery_lease):
            raise ValueError("Reaper scan 不得大于 Task/Recovery lease")
        heavy = _positive_int(mapping, "DOCSENSE_TASK_HEAVY_CONCURRENCY", 1)
        if heavy != 1:
            raise ValueError("阶段 2 未经真实供应商验收时 heavy concurrency 必须为 1")

        values = {
            "report_workers": _positive_int(mapping, "DOCSENSE_TASK_REPORT_WORKER_COUNT", 1),
            "weaponry_workers": _positive_int(mapping, "DOCSENSE_TASK_WEAPONRY_WORKER_COUNT", 1),
            "file_workers": _positive_int(mapping, "DOCSENSE_TASK_ANALYSIS_WORKER_COUNT", 1),
            "heavy": heavy,
            "executor_scan": _number(
                mapping,
                "DOCSENSE_TASK_EXECUTOR_SCAN_INTERVAL_SECONDS",
                1,
            ),
            "reaper_scan": reaper_scan,
            "recovery_lease": recovery_lease,
            "lease": {
                "heartbeat": heartbeat,
                "duration": lease_duration,
                "busy": busy,
                "jitter": jitter,
                "stop_grace": stop_grace,
            },
        }
        fingerprint = hashlib.sha256(
            json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        logger.info(
            "Task Runtime Config 已校验: fingerprint_prefix=%s heavy_concurrency=%d",
            fingerprint[:12],
            heavy,
        )
        return cls(
            database_path=database_path,
            report_worker_count=values["report_workers"],
            weaponry_worker_count=values["weaponry_workers"],
            file_worker_count=values["file_workers"],
            heavy_concurrency=heavy,
            executor_scan_interval_seconds=values["executor_scan"],
            reaper_scan_interval_seconds=reaper_scan,
            recovery_lease_duration_seconds=recovery_lease,
            lease=lease,
            fingerprint=fingerprint,
        )

    @classmethod
    def from_environment(
        cls,
        *,
        runtime_directory: str | Path,
        legacy_task_database_path: str | Path,
    ) -> "TaskRuntimeConfig":
        return cls.from_mapping(
            os.environ,
            runtime_directory=runtime_directory,
            legacy_task_database_path=legacy_task_database_path,
        )


__all__ = ["TaskRuntimeConfig"]
