"""统一 Task 执行条件写 Port。

除 claim 外的写命令都携带完整 ``TaskExecutionAuthority``。Adapter 必须同时核对
task_id、attempt_no、lease_token、fencing_token 与租约，不能把 owner_id 当作写权限。
所有返回均为有限分类，失权、重复终态等正常并发结果不得伪装成成功。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from app.modules.tasks.domain import (
    TaskAttempt,
    TaskExecutionAuthority,
    TaskId,
    TaskOwnerIdentity,
    TaskRecord,
    TaskRecoveryIsolation,
    TaskStep,
    TaskStepAttempt,
    TaskStepCheckpoint,
    TaskStepState,
    TaskStepTransition,
    TaskTransition,
)

from .clock import require_persisted_utc


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


class TaskExecutionMutationOutcome(str, Enum):
    """执行写入的稳定内部结果。"""

    APPLIED = "applied"
    MISSING = "missing"
    NOT_RUNNABLE = "not_runnable"
    AUTHORITY_LOST = "authority_lost"
    LEASE_EXPIRED = "lease_expired"
    STALE_LATEST = "stale_latest"
    INVALID_STATE = "invalid_state"
    DUPLICATE_STEP_INTENT = "duplicate_step_intent"
    DUPLICATE_TERMINAL = "duplicate_terminal"


@dataclass(frozen=True, slots=True)
class TaskClaimRequest:
    """claim 所需的启动世代身份和新租约；lease_token 不得进入日志。"""

    task_id: TaskId
    task_type: str
    owner: TaskOwnerIdentity
    lease_token: str = field(repr=False)
    claimed_at: str
    lease_expires_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.owner, TaskOwnerIdentity):
            raise TypeError("owner 必须是 TaskOwnerIdentity")
        for name in ("task_type", "lease_token"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name=name))
        for name in ("claimed_at", "lease_expires_at"):
            object.__setattr__(self, name, require_persisted_utc(getattr(self, name), name=name))
        if self.lease_expires_at <= self.claimed_at:
            raise ValueError("lease_expires_at 必须晚于 claimed_at")

    @property
    def owner_id(self) -> str:
        """兼容 Authority 构造的只读诊断文本；持久层应使用 ``owner`` 拆分字段。"""

        return self.owner.owner_id


@dataclass(frozen=True, slots=True)
class TaskExecutionClaimResult:
    """claim 成功同时返回 Task、Attempt 和后续写入必须携带的 Authority。"""

    outcome: TaskExecutionMutationOutcome
    task: TaskRecord | None = None
    attempt: TaskAttempt | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, TaskExecutionMutationOutcome):
            raise TypeError("outcome 必须是 TaskExecutionMutationOutcome")
        claimed = self.outcome is TaskExecutionMutationOutcome.APPLIED
        if claimed:
            if not isinstance(self.task, TaskRecord) or not isinstance(self.attempt, TaskAttempt):
                raise TypeError("claim 成功必须包含 TaskRecord 和 TaskAttempt")
            if self.task.task_id != self.attempt.authority.task_id:
                raise ValueError("Task 与 Attempt 身份不一致")
        elif self.task is not None or self.attempt is not None:
            raise ValueError("claim 未成功不得携带执行事实")


@dataclass(frozen=True, slots=True)
class TaskHeartbeatCommand:
    authority: TaskExecutionAuthority
    heartbeat_at: str
    lease_expires_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.authority, TaskExecutionAuthority):
            raise TypeError("authority 必须是 TaskExecutionAuthority")
        for name in ("heartbeat_at", "lease_expires_at"):
            object.__setattr__(self, name, require_persisted_utc(getattr(self, name), name=name))
        if self.lease_expires_at <= self.heartbeat_at:
            raise ValueError("续租到期时间必须晚于 heartbeat_at")


@dataclass(frozen=True, slots=True)
class TaskHeartbeatResult:
    """续租结果；成功时返回包含新到期时间的不可变 Authority。"""

    outcome: TaskExecutionMutationOutcome
    authority: TaskExecutionAuthority | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, TaskExecutionMutationOutcome):
            raise TypeError("outcome 必须是 TaskExecutionMutationOutcome")
        if self.outcome is TaskExecutionMutationOutcome.APPLIED:
            if not isinstance(self.authority, TaskExecutionAuthority):
                raise TypeError("heartbeat 成功必须返回更新后的 Authority")
        elif self.authority is not None:
            raise ValueError("heartbeat 未成功不得携带 Authority")


@dataclass(frozen=True, slots=True)
class TaskProgressCommand:
    authority: TaskExecutionAuthority
    progress: float
    message: str
    public_status: str
    updated_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.authority, TaskExecutionAuthority):
            raise TypeError("authority 必须是 TaskExecutionAuthority")
        if isinstance(self.progress, bool) or not isinstance(self.progress, (int, float)):
            raise TypeError("progress 必须是数字")
        normalized = float(self.progress)
        if normalized != normalized or normalized in (float("inf"), float("-inf")):
            raise ValueError("progress 必须是有限数字")
        if normalized < 0.0 or normalized > 1.0:
            raise ValueError("progress 必须位于 0 到 1")
        object.__setattr__(self, "progress", normalized)
        if not isinstance(self.message, str):
            raise TypeError("message 必须是 str")
        object.__setattr__(self, "public_status", _required_text(self.public_status, name="public_status"))
        object.__setattr__(self, "updated_at", require_persisted_utc(self.updated_at, name="updated_at"))


@dataclass(frozen=True, slots=True)
class TaskStepIntentCommand:
    """外部动作前先持久化的 Step intent。"""

    authority: TaskExecutionAuthority
    step: TaskStep
    intent_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.authority, TaskExecutionAuthority):
            raise TypeError("authority 必须是 TaskExecutionAuthority")
        if not isinstance(self.step, TaskStep):
            raise TypeError("step 必须是 TaskStep")
        if self.step.task_id != self.authority.task_id:
            raise ValueError("Step 与 Authority 的 task_id 不一致")
        object.__setattr__(self, "intent_at", require_persisted_utc(self.intent_at, name="intent_at"))


@dataclass(frozen=True, slots=True)
class TaskStepCompletionCommand:
    """显式提交一次运行中 Step Attempt 的确定结果或 unknown 隔离结果。"""

    authority: TaskExecutionAuthority
    step_key: str
    step_attempt_no: int
    transition: TaskStepTransition
    checkpoint: TaskStepCheckpoint | None
    error_code: str
    completed_at: str
    recovery_isolation: TaskRecoveryIsolation | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.authority, TaskExecutionAuthority):
            raise TypeError("authority 必须是 TaskExecutionAuthority")
        object.__setattr__(self, "step_key", _required_text(self.step_key, name="step_key"))
        if type(self.step_attempt_no) is not int or self.step_attempt_no <= 0:
            raise ValueError("step_attempt_no 必须是正整数")
        if self.transition not in {
            TaskStepTransition.SUCCEED,
            TaskStepTransition.FAIL,
            TaskStepTransition.MARK_OUTCOME_UNKNOWN,
        }:
            raise ValueError("执行端 Step 完成只允许 succeed/fail/mark_outcome_unknown")
        if self.checkpoint is not None and not isinstance(self.checkpoint, TaskStepCheckpoint):
            raise TypeError("checkpoint 必须是 TaskStepCheckpoint 或 None")
        if not isinstance(self.error_code, str):
            raise TypeError("error_code 必须是 str")
        object.__setattr__(self, "error_code", self.error_code.strip())
        if self.recovery_isolation is not None and not isinstance(
            self.recovery_isolation,
            TaskRecoveryIsolation,
        ):
            raise TypeError("recovery_isolation 必须是 TaskRecoveryIsolation 或 None")

        if self.transition is TaskStepTransition.SUCCEED:
            if self.checkpoint is None or self.error_code or self.recovery_isolation is not None:
                raise ValueError("Step succeed 必须只携带 checkpoint")
        elif self.transition is TaskStepTransition.FAIL:
            if self.checkpoint is not None or not self.error_code or self.recovery_isolation is not None:
                raise ValueError("Step fail 必须只携带 error_code")
        elif not self.error_code or not isinstance(
            self.recovery_isolation,
            TaskRecoveryIsolation,
        ):
            raise ValueError("Step outcome_unknown 必须携带 error_code 和 Recovery Isolation")
        object.__setattr__(self, "completed_at", require_persisted_utc(self.completed_at, name="completed_at"))


@dataclass(frozen=True, slots=True)
class TaskStepSkipCommand:
    """在没有执行外部动作前显式跳过 pending Step。

    跳过不是“成功且空结果”。Store 需要以 ``skipped_at`` 同时保存跳过 intent/result 时间，并追加
    独立 Step Attempt；这样恢复逻辑不会把未执行的步骤误判成已产生副作用。
    """

    authority: TaskExecutionAuthority
    step: TaskStep
    reason_code: str
    skipped_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.authority, TaskExecutionAuthority):
            raise TypeError("authority 必须是 TaskExecutionAuthority")
        if not isinstance(self.step, TaskStep):
            raise TypeError("step 必须是 TaskStep")
        if self.step.task_id != self.authority.task_id:
            raise ValueError("Step 与 Authority 的 task_id 不一致")
        if self.step.state is not TaskStepState.PENDING:
            raise ValueError("只有 pending Step 可以显式跳过")
        object.__setattr__(
            self,
            "reason_code",
            _required_text(self.reason_code, name="reason_code"),
        )
        object.__setattr__(
            self,
            "skipped_at",
            require_persisted_utc(self.skipped_at, name="skipped_at"),
        )


@dataclass(frozen=True, slots=True)
class TaskTerminalCommand:
    """业务终态命令；结果正文由业务 Store 保存，本命令只持有内部引用。"""

    authority: TaskExecutionAuthority
    transition: TaskTransition
    public_status: str
    message: str
    result_ref: str
    completed_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.authority, TaskExecutionAuthority):
            raise TypeError("authority 必须是 TaskExecutionAuthority")
        if self.transition not in {
            TaskTransition.BUSINESS_SUCCEEDED,
            TaskTransition.BUSINESS_FAILED,
        }:
            raise ValueError("业务终态命令只允许 business_succeeded/business_failed")
        object.__setattr__(self, "public_status", _required_text(self.public_status, name="public_status"))
        if not isinstance(self.message, str) or not isinstance(self.result_ref, str):
            raise TypeError("message/result_ref 必须是 str")
        object.__setattr__(self, "completed_at", require_persisted_utc(self.completed_at, name="completed_at"))


@runtime_checkable
class TaskExecutionPort(Protocol):
    """在窄 Execution UoW 内执行 Authority 条件写。"""

    def get_task(self, task_id: TaskId) -> TaskRecord | None:
        ...

    def get_step(self, task_id: TaskId, step_key: str) -> TaskStep | None:
        """读取稳定 Step 当前投影；读取结果本身不授予任何写权限。"""
        ...

    def get_step_attempt(
        self,
        task_id: TaskId,
        step_key: str,
        step_attempt_no: int,
    ) -> TaskStepAttempt | None:
        """读取不可变 Step Attempt 历史，供 Intent 重放和恢复诊断。"""
        ...

    def claim(self, request: TaskClaimRequest) -> TaskExecutionClaimResult:
        ...

    def start(self, authority: TaskExecutionAuthority, *, started_at: str) -> TaskExecutionMutationOutcome:
        ...

    def heartbeat(self, command: TaskHeartbeatCommand) -> TaskHeartbeatResult:
        ...

    def begin_step(self, command: TaskStepIntentCommand) -> TaskExecutionMutationOutcome:
        ...

    def complete_step(self, command: TaskStepCompletionCommand) -> TaskExecutionMutationOutcome:
        ...

    def skip_step(self, command: TaskStepSkipCommand) -> TaskExecutionMutationOutcome:
        ...

    def update_progress(self, command: TaskProgressCommand) -> TaskExecutionMutationOutcome:
        ...

    def finish(self, command: TaskTerminalCommand) -> TaskExecutionMutationOutcome:
        ...


@runtime_checkable
class TaskRunnableQueryPort(Protocol):
    """只读扫描某一业务类型的可领取 Task；不创建租约。"""

    def scan_runnable(self, task_type: str, *, not_after: str, limit: int) -> tuple[TaskId, ...]:
        ...


__all__ = [
    "TaskClaimRequest",
    "TaskExecutionClaimResult",
    "TaskExecutionMutationOutcome",
    "TaskExecutionPort",
    "TaskHeartbeatCommand",
    "TaskHeartbeatResult",
    "TaskProgressCommand",
    "TaskRunnableQueryPort",
    "TaskStepCompletionCommand",
    "TaskStepIntentCommand",
    "TaskStepSkipCommand",
    "TaskTerminalCommand",
]
