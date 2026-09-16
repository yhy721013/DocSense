"""持久化任务积压与运行中 execution 的只读诊断端口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.modules.tasks.domain.models import TaskId


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


def _optional_timestamp(value: object, *, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name=name)


@dataclass(frozen=True)
class TaskQueueSnapshot:
    """某一任务类型的活动积压数量和有界运行样本。

    该快照只供日志、监控和人工处置使用，不能据此把 ``running`` 重置为
    ``accepted``。阶段 2 在 Attempt/Step/Checkpoint 和 Worker 租约齐备前，进程
    崩溃遗留的 running 必须保持原状，避免重复执行具有外部副作用的步骤。
    """

    task_type: str
    accepted_count: int
    running_count: int
    oldest_accepted_at: str | None = None
    oldest_running_at: str | None = None
    running_task_ids: tuple[TaskId, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "task_type",
            _required_text(self.task_type, name="task_type"),
        )
        for name in (
            "accepted_count",
            "running_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数")
        object.__setattr__(
            self,
            "oldest_accepted_at",
            _optional_timestamp(
                self.oldest_accepted_at,
                name="oldest_accepted_at",
            ),
        )
        object.__setattr__(
            self,
            "oldest_running_at",
            _optional_timestamp(
                self.oldest_running_at,
                name="oldest_running_at",
            ),
        )
        task_ids = tuple(self.running_task_ids)
        if any(not isinstance(item, TaskId) for item in task_ids):
            raise TypeError("running_task_ids 只能包含 TaskId")
        if len(set(task_ids)) != len(task_ids):
            raise ValueError("running_task_ids 不得重复")
        if len(task_ids) > self.running_count:
            raise ValueError("running_task_ids 数量不得超过 running_count")
        object.__setattr__(self, "running_task_ids", task_ids)

@runtime_checkable
class TaskQueueInspectionPort(Protocol):
    """读取持久化队列状态；实现不得修改 execution 或触发外部副作用。"""

    def inspect_queue(
        self,
        task_type: str,
        *,
        running_sample_limit: int,
    ) -> TaskQueueSnapshot:
        ...


__all__ = ["TaskQueueInspectionPort", "TaskQueueSnapshot"]
