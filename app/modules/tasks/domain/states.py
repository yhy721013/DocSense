"""阶段 2 统一任务内核的三套独立状态机。

本模块只表达纯状态转换，不读取时钟、不访问数据库，也不把内部状态投影为公开状态。
调用方必须提供明确的转换原因；禁止仅传目标状态后让领域层猜测业务语义。
"""

from __future__ import annotations

from enum import Enum


class TaskStateTransitionError(ValueError):
    """Task、Attempt 或 Step 收到未登记的状态转换。"""


class TaskState(str, Enum):
    """持久 Task 当前状态；不包含消息系统的虚构 queued 状态。"""

    ACCEPTED = "accepted"
    RUNNING = "running"
    RECOVERY_REQUIRED = "recovery_required"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STALE = "stale"


class TaskTransition(str, Enum):
    """触发 Task 状态变化的稳定内部原因。"""

    CLAIM = "claim"
    BUSINESS_SUCCEEDED = "business_succeeded"
    BUSINESS_FAILED = "business_failed"
    ISOLATE_FOR_RECOVERY = "isolate_for_recovery"
    SUPERSEDE = "supersede"
    RETRY_SAFE = "retry_safe"
    RETRY_AUTHORIZED = "retry_authorized"
    RECONCILED_SUCCEEDED = "reconciled_succeeded"
    RECONCILED_FAILED = "reconciled_failed"


class TaskAttemptState(str, Enum):
    """一次整任务执行权的生命周期。"""

    LEASED = "leased"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXPIRED = "expired"
    ABANDONED = "abandoned"


class TaskAttemptTransition(str, Enum):
    """Task Attempt 的合法转换原因。"""

    START = "start"
    SUCCEED = "succeed"
    FAIL = "fail"
    LEASE_EXPIRED = "lease_expired"
    ISOLATE_FOR_RECOVERY = "isolate_for_recovery"
    ABANDON_AFTER_CLASSIFICATION = "abandon_after_classification"


class TaskStepState(str, Enum):
    """稳定 Step 投影与单次 Step Attempt 共用的结果状态集合。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    SKIPPED = "skipped"
    COMPENSATED = "compensated"


class TaskStepTransition(str, Enum):
    """Step 由 intent 到单一结果的合法转换原因。"""

    BEGIN = "begin"
    SUCCEED = "succeed"
    FAIL = "fail"
    MARK_OUTCOME_UNKNOWN = "mark_outcome_unknown"
    SKIP = "skip"
    COMPENSATE = "compensate"
    # 只允许 Recovery Decision 在核验证据和旧 Step 投影后使用；普通执行路径不得把
    # outcome_unknown 当作可直接重放的 pending。
    RETRY_AUTHORIZED = "retry_authorized"


class StepEffectKind(str, Enum):
    """副作用发生位置；与能否重放正交。"""

    PURE = "pure"
    LOCAL_WRITE = "local_write"
    EXTERNAL_READ = "external_read"
    EXTERNAL_WRITE = "external_write"


class StepReplayPolicy(str, Enum):
    """过期或响应丢失后的默认恢复约束。"""

    SAFE = "safe"
    IDEMPOTENT_AFTER_PROBE = "idempotent_after_probe"
    RECONCILE_ONLY = "reconcile_only"
    NEVER_AUTO = "never_auto"


_TASK_TRANSITIONS = {
    (TaskState.ACCEPTED, TaskTransition.CLAIM): TaskState.RUNNING,
    (TaskState.ACCEPTED, TaskTransition.SUPERSEDE): TaskState.STALE,
    (TaskState.RUNNING, TaskTransition.BUSINESS_SUCCEEDED): TaskState.SUCCEEDED,
    (TaskState.RUNNING, TaskTransition.BUSINESS_FAILED): TaskState.FAILED,
    (TaskState.RUNNING, TaskTransition.ISOLATE_FOR_RECOVERY): TaskState.RECOVERY_REQUIRED,
    (TaskState.RUNNING, TaskTransition.SUPERSEDE): TaskState.STALE,
    # 只有完成业务 Policy 分类并通过 source Attempt/fencing CAS 的 Reaper 才能使用该原因。
    # 它不是通用 reset；普通 Worker 仍不能把任意 running Task 改回 accepted。
    (TaskState.RUNNING, TaskTransition.RETRY_SAFE): TaskState.ACCEPTED,
    (TaskState.RECOVERY_REQUIRED, TaskTransition.RETRY_AUTHORIZED): TaskState.ACCEPTED,
    (TaskState.RECOVERY_REQUIRED, TaskTransition.RECONCILED_SUCCEEDED): TaskState.SUCCEEDED,
    (TaskState.RECOVERY_REQUIRED, TaskTransition.RECONCILED_FAILED): TaskState.FAILED,
    (TaskState.RECOVERY_REQUIRED, TaskTransition.SUPERSEDE): TaskState.STALE,
}

_ATTEMPT_TRANSITIONS = {
    (TaskAttemptState.LEASED, TaskAttemptTransition.START): TaskAttemptState.RUNNING,
    (TaskAttemptState.LEASED, TaskAttemptTransition.LEASE_EXPIRED): TaskAttemptState.EXPIRED,
    (TaskAttemptState.RUNNING, TaskAttemptTransition.SUCCEED): TaskAttemptState.SUCCEEDED,
    (TaskAttemptState.RUNNING, TaskAttemptTransition.FAIL): TaskAttemptState.FAILED,
    (TaskAttemptState.RUNNING, TaskAttemptTransition.LEASE_EXPIRED): TaskAttemptState.EXPIRED,
    # Step 结果未知时必须立即撤销旧业务执行权，不能等待租约自然到期后再隔离。
    (TaskAttemptState.RUNNING, TaskAttemptTransition.ISOLATE_FOR_RECOVERY): TaskAttemptState.ABANDONED,
    (
        TaskAttemptState.EXPIRED,
        TaskAttemptTransition.ABANDON_AFTER_CLASSIFICATION,
    ): TaskAttemptState.ABANDONED,
}

_STEP_TRANSITIONS = {
    (TaskStepState.PENDING, TaskStepTransition.BEGIN): TaskStepState.RUNNING,
    (TaskStepState.PENDING, TaskStepTransition.SKIP): TaskStepState.SKIPPED,
    (TaskStepState.RUNNING, TaskStepTransition.SUCCEED): TaskStepState.SUCCEEDED,
    (TaskStepState.RUNNING, TaskStepTransition.FAIL): TaskStepState.FAILED,
    (
        TaskStepState.RUNNING,
        TaskStepTransition.MARK_OUTCOME_UNKNOWN,
    ): TaskStepState.OUTCOME_UNKNOWN,
    (
        TaskStepState.OUTCOME_UNKNOWN,
        TaskStepTransition.SUCCEED,
    ): TaskStepState.SUCCEEDED,
    (
        TaskStepState.OUTCOME_UNKNOWN,
        TaskStepTransition.FAIL,
    ): TaskStepState.FAILED,
    (
        TaskStepState.OUTCOME_UNKNOWN,
        TaskStepTransition.COMPENSATE,
    ): TaskStepState.COMPENSATED,
    (
        TaskStepState.OUTCOME_UNKNOWN,
        TaskStepTransition.RETRY_AUTHORIZED,
    ): TaskStepState.PENDING,
}


def transition_task_state(current: TaskState, transition: TaskTransition) -> TaskState:
    """按冻结原因推进 Task；非法或终态回退一律 fail closed。"""

    if not isinstance(current, TaskState) or not isinstance(transition, TaskTransition):
        raise TypeError("current/transition 必须是 TaskState/TaskTransition")
    target = _TASK_TRANSITIONS.get((current, transition))
    if target is None:
        raise TaskStateTransitionError(
            f"非法 Task 转换: {current.value} --{transition.value}--> ?"
        )
    return target


def transition_attempt_state(
    current: TaskAttemptState,
    transition: TaskAttemptTransition,
) -> TaskAttemptState:
    """推进整任务 Attempt；expired 必须先分类再 abandoned。"""

    if not isinstance(current, TaskAttemptState) or not isinstance(
        transition, TaskAttemptTransition
    ):
        raise TypeError("current/transition 必须是 TaskAttemptState/TaskAttemptTransition")
    target = _ATTEMPT_TRANSITIONS.get((current, transition))
    if target is None:
        raise TaskStateTransitionError(
            f"非法 Task Attempt 转换: {current.value} --{transition.value}--> ?"
        )
    return target


def transition_step_state(
    current: TaskStepState,
    transition: TaskStepTransition,
) -> TaskStepState:
    """推进 Step；unknown 只能通过对账收敛、补偿或已授权重试。"""

    if not isinstance(current, TaskStepState) or not isinstance(
        transition, TaskStepTransition
    ):
        raise TypeError("current/transition 必须是 TaskStepState/TaskStepTransition")
    target = _STEP_TRANSITIONS.get((current, transition))
    if target is None:
        raise TaskStateTransitionError(
            f"非法 Task Step 转换: {current.value} --{transition.value}--> ?"
        )
    return target


TASK_TERMINAL_STATES = frozenset(
    {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.STALE}
)
ATTEMPT_TERMINAL_STATES = frozenset(
    {TaskAttemptState.SUCCEEDED, TaskAttemptState.FAILED, TaskAttemptState.ABANDONED}
)
STEP_TERMINAL_STATES = frozenset(
    {
        TaskStepState.SUCCEEDED,
        TaskStepState.FAILED,
        TaskStepState.OUTCOME_UNKNOWN,
        TaskStepState.SKIPPED,
        TaskStepState.COMPENSATED,
    }
)


__all__ = [
    "ATTEMPT_TERMINAL_STATES",
    "STEP_TERMINAL_STATES",
    "TASK_TERMINAL_STATES",
    "StepEffectKind",
    "StepReplayPolicy",
    "TaskAttemptState",
    "TaskAttemptTransition",
    "TaskState",
    "TaskStateTransitionError",
    "TaskStepState",
    "TaskStepTransition",
    "TaskTransition",
    "transition_attempt_state",
    "transition_step_state",
    "transition_task_state",
]
