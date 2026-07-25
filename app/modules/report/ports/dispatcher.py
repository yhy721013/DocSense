"""报告任务持久化事实提交后的有界唤醒端口。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.modules.tasks.domain import TaskId


@runtime_checkable
class ReportTaskDispatcherPort(Protocol):
    """唤醒扫描执行器；任务事实不得只存在于具体实现的内存队列。"""

    def dispatch(self, task_id: TaskId) -> None:
        ...


@runtime_checkable
class ReportTaskDispatcherLifecyclePort(Protocol):
    """应用组合根拥有的报告 Worker 生命周期边界。

    ``stop`` 必须在有限时间内返回；返回 ``False`` 表示当前执行函数仍未退出，只能记录并
    由进程级停机继续隔离，不能在阶段 1C 中把其对应 running 盲目改回 accepted。
    """

    def start(self) -> None:
        ...

    def stop(self, *, timeout_seconds: float | None = None) -> bool:
        ...

    def close(self) -> None:
        ...


__all__ = [
    "ReportTaskDispatcherLifecyclePort",
    "ReportTaskDispatcherPort",
]
