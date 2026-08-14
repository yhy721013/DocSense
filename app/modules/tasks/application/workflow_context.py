"""v2 Workflow 的冻结输入、可轮换 Authority 与正常取消上下文。"""

from __future__ import annotations

from threading import Event

from app.modules.tasks.ports import (
    LoadedTaskExecutionInput,
    TaskExecutionAuthoritySessionPort,
)


class TaskWorkflowContext:
    """Workflow 每次写入都从 Session 取得当前 Authority，不缓存过期能力。

    正常取消与 Authority 失权是两个独立事实：前者用于停机或未来内部取消编排，后者由
    heartbeat/CAS 驱动。业务 Adapter 可把 ``stop_requested`` 作为长调用的取消探针。
    """

    def __init__(
        self,
        *,
        session: TaskExecutionAuthoritySessionPort,
        loaded_input: LoadedTaskExecutionInput,
    ) -> None:
        if not isinstance(session, TaskExecutionAuthoritySessionPort):
            raise TypeError("session 必须实现 TaskExecutionAuthoritySessionPort")
        if not isinstance(loaded_input, LoadedTaskExecutionInput):
            raise TypeError("loaded_input 必须是 LoadedTaskExecutionInput")
        self._session = session
        self._loaded_input = loaded_input
        self._cancellation = Event()

    @property
    def session(self) -> TaskExecutionAuthoritySessionPort:
        return self._session

    @property
    def loaded_input(self) -> LoadedTaskExecutionInput:
        return self._loaded_input

    def request_cancellation(self) -> bool:
        first = not self._cancellation.is_set()
        self._cancellation.set()
        return first

    def cancellation_requested(self) -> bool:
        return self._cancellation.is_set()

    def stop_requested(self) -> bool:
        return self.cancellation_requested() or self._session.stop_requested()


__all__ = ["TaskWorkflowContext"]
