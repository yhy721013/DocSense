"""Weaponry 受理时文档快照的持久化 Port。"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.weaponry.domain import WeaponryDocumentSnapshot


@runtime_checkable
class WeaponryTaskDocumentSnapshotStorePort(Protocol):
    """按 TaskId 保存和读取不可变文档身份，不允许按业务键覆盖历史任务。"""

    def replace_for_task(
        self,
        *,
        task_id: TaskId,
        business_ref: TaskBusinessRef,
        documents: Sequence[WeaponryDocumentSnapshot],
    ) -> tuple[WeaponryDocumentSnapshot, ...]: ...

    def list_for_task(
        self,
        task_id: TaskId,
    ) -> tuple[WeaponryDocumentSnapshot, ...]: ...


__all__ = ["WeaponryTaskDocumentSnapshotStorePort"]
