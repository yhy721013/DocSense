"""从 Task Control v2 与完整结果快照重建 Weaponry 同步 Callback 候选。"""

from __future__ import annotations

import logging

from app.modules.tasks.domain import TaskBusinessRef
from app.modules.tasks.ports import TaskReadPort
from app.modules.weaponry.domain import MAX_ARCHITECTURE_ID
from app.modules.weaponry.ports import (
    WeaponryCallbackRecoveryCandidate,
    WeaponryResultSnapshotStorePort,
)


logger = logging.getLogger(__name__)
_RECOVERABLE_CALLBACK_STATUSES = frozenset(
    {"pending", "failed", "outcome_unknown"}
)
_TERMINAL_PUBLIC_STATUSES = frozenset({"2", "3"})


class SQLiteWeaponryV2CallbackRecoverySource:
    """只读取 latest v2 Task；完整公开 payload 来自摘要保护的业务快照。"""

    def __init__(
        self,
        *,
        task_reader: TaskReadPort,
        results: WeaponryResultSnapshotStorePort,
    ) -> None:
        if not isinstance(task_reader, TaskReadPort):
            raise TypeError("task_reader 必须实现 TaskReadPort")
        if not isinstance(results, WeaponryResultSnapshotStorePort):
            raise TypeError("results 必须实现 WeaponryResultSnapshotStorePort")
        self._tasks = task_reader
        self._results = results

    def load_recoverable(
        self,
        architecture_id: int,
    ) -> WeaponryCallbackRecoveryCandidate | None:
        if (
            isinstance(architecture_id, bool)
            or not isinstance(architecture_id, int)
            or architecture_id < 1
            or architecture_id > MAX_ARCHITECTURE_ID
        ):
            raise ValueError("architecture_id 必须是有效正整数")
        business_ref = TaskBusinessRef("weaponry", str(architecture_id))
        task = self._tasks.get_latest(business_ref)
        if task is None:
            return None
        if task.public_status not in _TERMINAL_PUBLIC_STATUSES:
            return None
        if task.callback_status not in _RECOVERABLE_CALLBACK_STATUSES:
            return None
        result = self._results.get(task.task_id)
        if result is None:
            raise RuntimeError("终态 Weaponry Task 缺少完整结果快照")
        if result.business_ref != business_ref:
            raise RuntimeError("Weaponry 结果快照与 latest 业务身份不一致")
        if result.payload.status != task.public_status:
            raise RuntimeError("Weaponry Task 公开终态与 Callback 快照状态不一致")
        logger.debug(
            "已从 v2 控制面加载 Weaponry Callback 恢复候选: "
            "task_id=%s architecture_id=%s callback_status=%s",
            task.task_id,
            architecture_id,
            task.callback_status,
        )
        return WeaponryCallbackRecoveryCandidate(
            task_id=task.task_id,
            architecture_id=architecture_id,
            payload=result.payload,
            callback_attempts=task.callback_attempts,
        )


__all__ = ["SQLiteWeaponryV2CallbackRecoverySource"]
