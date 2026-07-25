"""任务快照只读端口。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.modules.tasks.domain.models import TaskBusinessRef, TaskId, TaskSnapshot


@runtime_checkable
class TaskReadPort(Protocol):
    """读取任务事实的抽象边界。

    实现不得发送 Callback、发布 Progress 或修改任务状态。批量方法必须严格保持
    ``business_refs`` 的顺序和长度，以 ``None`` 表示对应位置不存在。
    """

    def get_by_id(self, task_id: TaskId) -> TaskSnapshot | None:
        """按不可变执行 ID 读取同一次任务。"""
        ...

    def get_latest(self, business_ref: TaskBusinessRef) -> TaskSnapshot | None:
        """读取业务键当前最新可见的任务投影。"""
        ...

    def get_latest_many(
        self,
        business_refs: tuple[TaskBusinessRef, ...],
    ) -> tuple[TaskSnapshot | None, ...]:
        """按输入顺序批量读取最新任务，并保留缺失位置。"""
        ...


__all__ = ["TaskReadPort"]
