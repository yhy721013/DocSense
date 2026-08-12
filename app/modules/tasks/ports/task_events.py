"""Task 内部事件的只读诊断 Port。

事件写入属于 Admission/Execution/Recovery 原子事务的一部分，不通过本只读 Port
单独追加，避免出现“状态已提交但审计事件丢失”或反向的不一致。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.modules.tasks.domain import TaskEvent, TaskId


@runtime_checkable
class TaskEventQueryPort(Protocol):
    """提供有界、稳定排序的内部事件查询。"""

    def list_for_task(
        self,
        task_id: TaskId,
        *,
        after_sequence_no: int = 0,
        limit: int = 100,
    ) -> tuple[TaskEvent, ...]:
        ...

    def list_by_type(
        self,
        event_type: str,
        *,
        created_at_or_after: str,
        limit: int = 100,
    ) -> tuple[TaskEvent, ...]:
        ...


__all__ = ["TaskEventQueryPort"]
