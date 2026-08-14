"""阶段 2 本地线程式 Task lease heartbeat supervisor。

线程只负责周期等待和调用统一 Execution UoW。测试可注入手动 Pulse 并推进 FakeClock，
不需要用真实 sleep 证明租约；生产默认 Pulse 使用 ``Event.wait``，stop 可立即打断等待。
"""

from __future__ import annotations

from threading import Event, Lock, Thread
from typing import Protocol
import logging

from app.modules.tasks.domain import (
    TaskExecutionAuthority,
    TaskLeaseRuntimeSettings,
    add_persisted_utc_seconds,
)
from app.modules.tasks.ports import (
    ClockAnomalyError,
    ClockPort,
    LeaseSupervisorOutcome,
    LeaseSupervisorResult,
    TaskExecutionAuthoritySessionPort,
    TaskExecutionMutationOutcome,
    TaskExecutionStopRequested,
    TaskExecutionUnitOfWorkFactory,
    TaskHeartbeatCommand,
    TaskHeartbeatResult,
)


logger = logging.getLogger(__name__)


class LeaseHeartbeatPulse(Protocol):
    """等待下一心跳时点；返回 False 表示 stop 已请求。"""

    def wait_next(self, interval_seconds: float) -> bool:
        ...

    def stop(self) -> None:
        ...


class EventLeaseHeartbeatPulse:
    """基于 Event 的默认可中断周期等待。"""

    def __init__(self) -> None:
        self._stop_event = Event()

    def wait_next(self, interval_seconds: float) -> bool:
        return not self._stop_event.wait(interval_seconds)

    def stop(self) -> None:
        self._stop_event.set()


class ThreadedLeaseHeartbeatSupervisor:
    """持续续租一个 Authority Session；实例只允许启动一次。"""

    def __init__(
        self,
        *,
        clock: ClockPort,
        execution_uow_factory: TaskExecutionUnitOfWorkFactory,
        lease_settings: TaskLeaseRuntimeSettings,
        pulse: LeaseHeartbeatPulse | None = None,
        thread_name: str = "task-lease-heartbeat",
    ) -> None:
        if not isinstance(clock, ClockPort):
            raise TypeError("clock 必须实现 ClockPort")
        if not callable(execution_uow_factory):
            raise TypeError("execution_uow_factory 必须可调用")
        if not isinstance(lease_settings, TaskLeaseRuntimeSettings):
            raise TypeError("lease_settings 必须是 TaskLeaseRuntimeSettings")
        if pulse is not None and not (
            callable(getattr(pulse, "wait_next", None))
            and callable(getattr(pulse, "stop", None))
        ):
            raise TypeError("pulse 必须实现 LeaseHeartbeatPulse")
        if not isinstance(thread_name, str) or not thread_name.strip():
            raise ValueError("thread_name 必须是非空 str")

        self._clock = clock
        self._execution_uow_factory = execution_uow_factory
        self._lease_settings = lease_settings
        self._pulse = pulse or EventLeaseHeartbeatPulse()
        self._thread_name = thread_name.strip()
        self._state_lock = Lock()
        self._thread: Thread | None = None
        self._session: TaskExecutionAuthoritySessionPort | None = None
        self._result: LeaseSupervisorResult | None = None
        self._started = False

    def start(self, session: TaskExecutionAuthoritySessionPort) -> None:
        if not isinstance(session, TaskExecutionAuthoritySessionPort):
            raise TypeError("session 必须实现 TaskExecutionAuthoritySessionPort")
        with self._state_lock:
            if self._started:
                raise RuntimeError("LeaseHeartbeatSupervisor 实例不可重复启动")
            self._started = True
            self._session = session
            thread = Thread(
                target=self._run,
                name=self._thread_name,
                daemon=False,
            )
            self._thread = thread
            thread.start()

    def stop(self) -> LeaseSupervisorResult:
        with self._state_lock:
            if not self._started or self._thread is None:
                raise RuntimeError("LeaseHeartbeatSupervisor 尚未启动")
            thread = self._thread
        self._pulse.stop()
        # 不强杀 Python 线程。等待上限来自启动前已校验的 stop grace；超时意味着本
        # Runtime 已无法证明 heartbeat 线程完成回收，必须失败关闭并由 Executor 降级。
        thread.join(timeout=self._lease_settings.stop_grace_seconds)
        if thread.is_alive():
            assert self._session is not None
            timeout_result = LeaseSupervisorResult(
                LeaseSupervisorOutcome.INFRASTRUCTURE_ERROR
            )
            self._request_stop(self._session, timeout_result)
            logger.critical(
                "Task heartbeat 线程未在 stop grace 内退出: thread_name=%s "
                "reason_code=heartbeat_stop_timeout",
                self._thread_name,
            )
            raise RuntimeError("LeaseHeartbeatSupervisor 停止超时")
        with self._state_lock:
            if self._result is None:
                raise RuntimeError("LeaseHeartbeatSupervisor 未产生结束结果")
            return self._result

    def _run(self) -> None:
        assert self._session is not None
        session = self._session
        result = LeaseSupervisorResult(LeaseSupervisorOutcome.STOPPED)
        try:
            while self._pulse.wait_next(
                self._lease_settings.heartbeat_interval_seconds
            ):
                heartbeat = session.renew_authority(self._renew_once)
                if heartbeat.outcome is TaskExecutionMutationOutcome.APPLIED:
                    renewed = heartbeat.authority
                    assert renewed is not None
                    logger.debug(
                        "Task heartbeat 已续租: task_id=%s attempt_no=%d fencing=%d",
                        renewed.task_id,
                        renewed.attempt_no,
                        renewed.fencing_token,
                    )
                    continue

                result = LeaseSupervisorResult(
                    LeaseSupervisorOutcome.AUTHORITY_LOST,
                    heartbeat.outcome,
                )
                authority = session.current_authority()
                logger.warning(
                    "Task heartbeat 失权: task_id=%s attempt_no=%d fencing=%d "
                    "outcome=%s reason_code=heartbeat_authority_lost",
                    authority.task_id,
                    authority.attempt_no,
                    authority.fencing_token,
                    heartbeat.outcome.value,
                )
                self._request_stop(session, result)
                break
        except TaskExecutionStopRequested as exc:
            result = exc.result
        except ClockAnomalyError:
            result = LeaseSupervisorResult(LeaseSupervisorOutcome.CLOCK_UNSAFE)
            self._request_stop(session, result)
        except Exception as exc:
            # wait_next 与 heartbeat/UoW 均属于 Supervisor 基础设施；任何一个抛错都必须
            # 产生稳定结果并请求 Workflow 停止，不能让后台线程静默死亡。
            result = LeaseSupervisorResult(LeaseSupervisorOutcome.INFRASTRUCTURE_ERROR)
            authority = session.current_authority()
            logger.error(
                "Task heartbeat 基础设施异常: task_id=%s attempt_no=%d fencing=%d "
                "reason_code=heartbeat_infrastructure_error error_type=%s",
                authority.task_id,
                authority.attempt_no,
                authority.fencing_token,
                type(exc).__name__,
            )
            self._request_stop(session, result)
        finally:
            with self._state_lock:
                self._result = result

    def _renew_once(
        self,
        authority: TaskExecutionAuthority,
    ) -> TaskHeartbeatResult:
        heartbeat_at = self._clock.now_utc()
        lease_expires_at = add_persisted_utc_seconds(
            heartbeat_at,
            seconds=self._lease_settings.lease_duration_seconds,
        )
        if lease_expires_at <= authority.lease_expires_at:
            raise ClockAnomalyError("heartbeat 无法严格推进 lease_expires_at")
        command = TaskHeartbeatCommand(
            authority=authority,
            heartbeat_at=heartbeat_at,
            lease_expires_at=lease_expires_at,
        )
        with self._execution_uow_factory() as unit_of_work:
            result = unit_of_work.execution.heartbeat(command)
            if result.outcome is TaskExecutionMutationOutcome.APPLIED:
                unit_of_work.commit()
            return result

    @staticmethod
    def _request_stop(
        session: TaskExecutionAuthoritySessionPort,
        result: LeaseSupervisorResult,
    ) -> None:
        session.request_stop(result)


__all__ = [
    "EventLeaseHeartbeatPulse",
    "LeaseHeartbeatPulse",
    "ThreadedLeaseHeartbeatSupervisor",
]
