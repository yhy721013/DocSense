"""阶段 2-3 Execution Runtime 的严格、可控离线 Fake。"""

from __future__ import annotations

from collections.abc import Callable
from threading import Condition, Event

from app.modules.tasks.ports import (
    LoadedTaskExecutionInput,
    LeaseSupervisorOutcome,
    LeaseSupervisorResult,
    TaskWorkflowContextPort,
)
from app.modules.tasks.domain import TaskId


class FixedTaskExecutionSnapshotLoader:
    """按 TaskId 返回预制冻结输入；不读取数据库。"""

    def __init__(self, inputs: dict[TaskId, LoadedTaskExecutionInput]) -> None:
        self._inputs = dict(inputs)
        self.loaded: list[TaskId] = []

    def load(self, task_id: TaskId) -> LoadedTaskExecutionInput:
        self.loaded.append(task_id)
        try:
            return self._inputs[task_id]
        except KeyError as exc:
            raise LookupError("Fake 冻结输入不存在") from exc


class FixedTaskLeaseTokenFactory:
    """测试专用确定性 token 工厂；生产代码禁止使用固定 token。"""

    def __init__(self, tokens: tuple[str, ...]) -> None:
        if not isinstance(tokens, tuple) or not tokens:
            raise ValueError("tokens 必须是非空 tuple")
        if any(not isinstance(token, str) or not token.strip() for token in tokens):
            raise ValueError("tokens 只能包含非空 str")
        self._tokens = list(tokens)

    def new_token(self) -> str:
        if not self._tokens:
            raise RuntimeError("固定 lease token 已耗尽")
        return self._tokens.pop(0)


class ManualLeaseHeartbeatPulse:
    """无需 sleep 的手动 heartbeat pulse，可被 supervisor.stop 立即唤醒。"""

    def __init__(self) -> None:
        self._condition = Condition()
        self._pending = 0
        self._stopped = False
        self.waiting = Event()

    def wait_next(self, interval_seconds: float) -> bool:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds 必须大于 0")
        with self._condition:
            self.waiting.set()
            while self._pending == 0 and not self._stopped:
                self._condition.wait()
            if self._stopped:
                return False
            self._pending -= 1
            self.waiting.clear()
            return True

    def pulse(self) -> None:
        with self._condition:
            if self._stopped:
                raise RuntimeError("ManualLeaseHeartbeatPulse 已停止")
            self._pending += 1
            self._condition.notify_all()

    def stop(self) -> None:
        with self._condition:
            self._stopped = True
            self._condition.notify_all()


class StrictTaskWorkflowRunnerFake:
    """记录 Session 并执行测试注入逻辑，不替调用方自动补 Authority。"""

    def __init__(
        self,
        action: Callable[[TaskWorkflowContextPort], None] | None = None,
    ) -> None:
        self._action = action
        self.contexts: list[TaskWorkflowContextPort] = []

    @property
    def sessions(self):
        """兼容既有断言的只读视图；新测试应直接检查 contexts。"""

        return [context.session for context in self.contexts]

    def run(self, context: TaskWorkflowContextPort) -> None:
        self.contexts.append(context)
        if self._action is not None:
            self._action(context)


class FakeLeaseHeartbeatSupervisor:
    """Runtime 编排测试用 supervisor；可在 start 时确定性注入失权。"""

    def __init__(
        self,
        *,
        start_result: LeaseSupervisorResult | None = None,
    ) -> None:
        self._start_result = start_result
        self._result = LeaseSupervisorResult(LeaseSupervisorOutcome.STOPPED)
        self.session = None
        self.started = False
        self.stopped = False

    def start(self, session) -> None:
        if self.started:
            raise RuntimeError("Fake supervisor 不可重复启动")
        self.started = True
        self.session = session
        if self._start_result is not None:
            session.request_stop(self._start_result)
            self._result = self._start_result

    def stop(self) -> LeaseSupervisorResult:
        if not self.started:
            raise RuntimeError("Fake supervisor 尚未启动")
        self.stopped = True
        return self._result


__all__ = [
    "FakeLeaseHeartbeatSupervisor",
    "FixedTaskExecutionSnapshotLoader",
    "FixedTaskLeaseTokenFactory",
    "ManualLeaseHeartbeatPulse",
    "StrictTaskWorkflowRunnerFake",
]
