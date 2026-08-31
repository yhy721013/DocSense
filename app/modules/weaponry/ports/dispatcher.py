"""武器谱持久任务提交后的有界唤醒与生命周期端口。"""

from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable

from app.modules.tasks.domain import TaskId


@runtime_checkable
class WeaponryTaskDispatcherPort(Protocol):
    """发送常量空间唤醒信号；任务事实不得只存在于内存队列。"""

    def dispatch(self, task_id: TaskId) -> None:
        ...


@runtime_checkable
class WeaponryTaskDispatcherLifecyclePort(Protocol):
    """由组合根显式拥有的 Dispatcher 生命周期。"""

    def start(self) -> None:
        ...

    def stop(self, *, timeout_seconds: float | None = None) -> bool:
        """有限等待当前执行函数；超时不得把 running 盲目重置为 accepted。"""
        ...

    def close(self) -> None:
        ...


@runtime_checkable
class WeaponryTaskRunnerPort(Protocol):
    """本地 Worker 只按 TaskId 调用的应用入口。"""

    def execute(self, task_id: TaskId) -> object:
        ...


@runtime_checkable
class WeaponryBoundedMaintenancePort(Protocol):
    """Dispatcher 调用真实 Guard/资源恢复或严格 Fake 的有界维护边界。

    ``limit`` 是单次工作量，不是积压上限。实现必须自行持久化冷却水位；本地
    Dispatcher 只负责固定延迟调用，不能在内存中保存恢复任务列表。
    """

    def run_once(self, *, limit: int) -> object:
        ...


@runtime_checkable
class WeaponryResourceMaintenancePort(Protocol):
    """可在逐资源检查点之间响应停机的资源维护边界。

    ``stop_requested`` 只用于缩短单实例进程停止时间，不承载任务取消语义。资源事实必须
    已经位于持久 Store，提示丢失或进程停止后仍能由下一实例继续扫描。
    """

    def run_once(
        self,
        *,
        limit: int,
        stop_requested: Callable[[], bool] | None = None,
    ) -> object:
        ...


__all__ = [
    "WeaponryBoundedMaintenancePort",
    "WeaponryResourceMaintenancePort",
    "WeaponryTaskDispatcherLifecyclePort",
    "WeaponryTaskDispatcherPort",
    "WeaponryTaskRunnerPort",
]
