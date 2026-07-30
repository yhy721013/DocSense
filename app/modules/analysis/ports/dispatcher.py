"""Analysis Dispatcher 的有界唤醒与生命周期 Port。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.modules.tasks.domain import TaskId


@runtime_checkable
class AnalysisDispatcherPort(Protocol):
    """Event 只负责唤醒持久扫描器，不能承载无界内存队列。"""

    def wake_up(self) -> None:
        ...

    def start(self) -> None:
        ...

    def stop(self, *, timeout_seconds: float | None = None) -> bool:
        ...

    def close(self) -> None:
        ...


@runtime_checkable
class AnalysisDispatchFailureBackoffPort(Protocol):
    """把调度失败次数与下一次领取时间作为同一持久化事实写入。

    Worker 内存和 ``threading.Event`` 都不能作为退避依据：进程重启后会丢失，未来多实例
    也无法共享。实现必须仅在任务仍为 ``accepted`` 时条件写入，且绝不把 ``running`` 或
    终态重新放回队列。
    """

    def defer_accepted_with_backoff(
        self,
        task_id: TaskId,
        *,
        retry_base_seconds: float,
        retry_max_seconds: float,
        reason: str,
    ) -> bool:
        ...


@runtime_checkable
class AnalysisBoundedMaintenancePort(Protocol):
    """供 Dispatcher 调用的有界、无任务重放维护协作器。"""

    def run_once(self, *, limit: int) -> object:
        ...


__all__ = (
    "AnalysisBoundedMaintenancePort",
    "AnalysisDispatchFailureBackoffPort",
    "AnalysisDispatcherPort",
)
