"""任务执行许可与单实例所有权的内部运行时端口。"""

from __future__ import annotations

from typing import Callable, Protocol, runtime_checkable


@runtime_checkable
class TaskExecutionPermitPort(Protocol):
    """可中断地获取共享重型资源许可。

    该端口只表达“等待许可、取消等待、归还许可”三件事，不知道线程、信号量或
    AnythingLLM。Dispatcher 因此可以在收到停机信号后终止尚未开始的许可等待，避免
    一个仍为 ``accepted`` 的任务在应用已经停止后才突然进入业务执行。
    """

    def acquire_interruptibly(
        self,
        cancel_requested: Callable[[], bool],
        *,
        poll_interval_seconds: float,
    ) -> bool:
        ...

    def release(self) -> None:
        ...


@runtime_checkable
class ProcessSingletonGuardPort(Protocol):
    """单实例进程所有权端口；实现不得依赖仅存在于当前 Python 进程的锁。"""

    def acquire(self) -> bool:
        ...

    def release(self) -> None:
        ...


__all__ = [
    "ProcessSingletonGuardPort",
    "TaskExecutionPermitPort",
]
