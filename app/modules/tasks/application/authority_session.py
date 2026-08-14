"""阶段 2 v2 Workflow 的线程安全 Authority Session。

Session 只协调本 Runtime 内 heartbeat 与短 Task 条件写，不替代 SQLite 的完整
Authority CAS，也不提供分布式互斥。调用方不得在授权临界区中执行任何外部 I/O。
"""

from __future__ import annotations

from threading import RLock
from typing import Callable, TypeVar

from app.modules.tasks.domain import TaskExecutionAuthority
from app.modules.tasks.ports import (
    ClockAnomalyError,
    LeaseSupervisorOutcome,
    LeaseSupervisorResult,
    TaskExecutionMutationOutcome,
    TaskExecutionStopRequested,
    TaskHeartbeatResult,
)


TAuthorizedResult = TypeVar("TAuthorizedResult")

_NON_STOPPING_MUTATION_OUTCOMES = {
    TaskExecutionMutationOutcome.APPLIED,
    # 这两个结果是幂等收敛事实，不等价于租约失权；业务 Workflow 负责决定是否返回。
    TaskExecutionMutationOutcome.DUPLICATE_STEP_INTENT,
    TaskExecutionMutationOutcome.DUPLICATE_TERMINAL,
}


class TaskExecutionAuthoritySession:
    """原子轮换 Authority，并把失权转换为单向协作停止信号。"""

    def __init__(self, authority: TaskExecutionAuthority) -> None:
        if not isinstance(authority, TaskExecutionAuthority):
            raise TypeError("authority 必须是 TaskExecutionAuthority")
        self._lock = RLock()
        self._authority = authority
        self._stop_result: LeaseSupervisorResult | None = None

    def current_authority(self) -> TaskExecutionAuthority:
        """返回不可变快照，仅供诊断；写入必须改用 ``run_authorized``。"""

        with self._lock:
            return self._authority

    def run_authorized(
        self,
        operation: Callable[[TaskExecutionAuthority], TAuthorizedResult],
    ) -> TAuthorizedResult:
        """在 heartbeat 不会轮换 expiry 的短临界区执行一次条件写。

        这里不能自动识别 operation 是否包含网络或阻塞逻辑，因此边界由 v2 Workflow
        契约和架构测试共同约束。SQLite Store 仍会再次校验全部 Authority 字段。
        """

        if not callable(operation):
            raise TypeError("operation 必须可调用")
        with self._lock:
            self._raise_if_stopped()
            return operation(self._authority)

    def run_mutation(
        self,
        operation: Callable[
            [TaskExecutionAuthority],
            TaskExecutionMutationOutcome,
        ],
    ) -> TaskExecutionMutationOutcome:
        """执行短条件写，并确保调用方不能忽略失权后继续产生副作用。

        ``run_authorized`` 仍用于读取有限内部结果或测试诊断；所有 Task 执行期条件写应
        使用本方法。APPLIED 与两类明确幂等结果交还 Workflow 处理，其余有限结果会先
        原子设置停止事实，再抛出 ``TaskExecutionStopRequested``，因此错误调用方无法
        仅因忘记检查返回值而越过失权边界。
        """

        if not callable(operation):
            raise TypeError("operation 必须可调用")
        with self._lock:
            self._raise_if_stopped()
            try:
                outcome = operation(self._authority)
                if not isinstance(outcome, TaskExecutionMutationOutcome):
                    raise TypeError("mutation operation 必须返回 TaskExecutionMutationOutcome")
                if outcome in _NON_STOPPING_MUTATION_OUTCOMES:
                    return outcome
                stop_result = LeaseSupervisorResult(
                    LeaseSupervisorOutcome.AUTHORITY_LOST,
                    outcome,
                )
                self._set_stop_locked(stop_result)
                raise TaskExecutionStopRequested(stop_result)
            except TaskExecutionStopRequested:
                raise
            except ClockAnomalyError:
                self._set_stop_locked(
                    LeaseSupervisorResult(LeaseSupervisorOutcome.CLOCK_UNSAFE)
                )
                raise
            except Exception:
                self._set_stop_locked(
                    LeaseSupervisorResult(LeaseSupervisorOutcome.INFRASTRUCTURE_ERROR)
                )
                raise

    def renew_authority(
        self,
        operation: Callable[[TaskExecutionAuthority], TaskHeartbeatResult],
    ) -> TaskHeartbeatResult:
        """提交 heartbeat 后、释放能力门前原子换入新 Authority。"""

        if not callable(operation):
            raise TypeError("operation 必须可调用")
        with self._lock:
            self._raise_if_stopped()
            previous = self._authority
            try:
                result = operation(previous)
                if not isinstance(result, TaskHeartbeatResult):
                    raise TypeError("heartbeat operation 必须返回 TaskHeartbeatResult")
                if result.outcome is not TaskExecutionMutationOutcome.APPLIED:
                    self._set_stop_locked(
                        LeaseSupervisorResult(
                            LeaseSupervisorOutcome.AUTHORITY_LOST,
                            result.outcome,
                        )
                    )
                    return result
                assert result.authority is not None
                self._validate_renewed_authority(previous, result.authority)
                self._authority = result.authority
                return result
            except ClockAnomalyError:
                self._set_stop_locked(
                    LeaseSupervisorResult(LeaseSupervisorOutcome.CLOCK_UNSAFE)
                )
                raise
            except Exception:
                self._set_stop_locked(
                    LeaseSupervisorResult(
                        LeaseSupervisorOutcome.INFRASTRUCTURE_ERROR
                    )
                )
                raise

    def request_stop(self, result: LeaseSupervisorResult) -> bool:
        """设置不可逆停止事实；并发重复通知只保留第一个确定原因。"""

        if not isinstance(result, LeaseSupervisorResult):
            raise TypeError("result 必须是 LeaseSupervisorResult")
        if result.outcome is LeaseSupervisorOutcome.STOPPED:
            raise ValueError("正常停止不能标记为 Authority 失权")
        with self._lock:
            return self._set_stop_locked(result)

    def stop_requested(self) -> bool:
        with self._lock:
            return self._stop_result is not None

    def stop_result(self) -> LeaseSupervisorResult | None:
        with self._lock:
            return self._stop_result

    def _raise_if_stopped(self) -> None:
        if self._stop_result is not None:
            raise TaskExecutionStopRequested(self._stop_result)

    def _set_stop_locked(self, result: LeaseSupervisorResult) -> bool:
        if self._stop_result is not None:
            return False
        self._stop_result = result
        return True

    @staticmethod
    def _validate_renewed_authority(
        previous: TaskExecutionAuthority,
        renewed: TaskExecutionAuthority,
    ) -> None:
        """heartbeat 只能推进 expiry，不能静默改变其余能力字段。"""

        stable_fields = (
            "task_id",
            "attempt_no",
            "owner_id",
            "lease_token",
            "fencing_token",
        )
        if any(getattr(previous, name) != getattr(renewed, name) for name in stable_fields):
            raise ValueError("heartbeat 返回的 Authority 身份发生越界变化")
        if renewed.lease_expires_at <= previous.lease_expires_at:
            raise ValueError("heartbeat 必须严格推进 lease_expires_at")


__all__ = ["TaskExecutionAuthoritySession"]
