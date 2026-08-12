"""Task Recovery 扫描、分类与条件收敛 Port。

Recovery Policy 只接收冻结领域事实，不能执行网络、文件或数据库 I/O。探测/补偿在
短事务之外完成，再通过带独立 RecoveryAuthority 的 Observation/Decision 命令写回。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from app.modules.tasks.domain import (
    RecoveryAuthority,
    RecoveryClassification,
    RecoveryOperationState,
    TaskId,
    TaskRecoveryCandidate,
    TaskRecoveryCase,
    TaskRecoveryDecision,
    TaskRecoveryObservation,
    TaskRecoveryOperation,
    TaskStep,
)

from .clock import require_persisted_utc


class TaskRecoveryMutationOutcome(str, Enum):
    """恢复条件写的稳定内部结果。"""

    APPLIED = "applied"
    MISSING = "missing"
    SOURCE_CHANGED = "source_changed"
    ALREADY_CLASSIFIED = "already_classified"
    AUTHORITY_LOST = "authority_lost"
    LEASE_EXPIRED = "lease_expired"
    INVALID_STATE = "invalid_state"
    DUPLICATE_DECISION = "duplicate_decision"
    DUPLICATE_OPERATION = "duplicate_operation"
    DUPLICATE_OBSERVATION = "duplicate_observation"


@dataclass(frozen=True, slots=True)
class TaskRecoveryClaimRequest:
    """领取 Recovery Case 的独立租约，绝不能复用 Task Execution Authority。"""

    case_id: str
    generation: int
    owner_id: str
    lease_token: str
    claimed_at: str
    lease_expires_at: str

    def __post_init__(self) -> None:
        for name in ("case_id", "owner_id", "lease_token"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} 必须是非空 str")
            object.__setattr__(self, name, value.strip())
        if type(self.generation) is not int or self.generation <= 0:
            raise ValueError("generation 必须是正整数")
        for name in ("claimed_at", "lease_expires_at"):
            object.__setattr__(self, name, require_persisted_utc(getattr(self, name), name=name))
        if self.lease_expires_at <= self.claimed_at:
            raise ValueError("Recovery lease 必须晚于 claimed_at")


@dataclass(frozen=True, slots=True)
class TaskRecoveryClaimResult:
    outcome: TaskRecoveryMutationOutcome
    case: TaskRecoveryCase | None = None
    authority: RecoveryAuthority | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, TaskRecoveryMutationOutcome):
            raise TypeError("outcome 必须是 TaskRecoveryMutationOutcome")
        applied = self.outcome is TaskRecoveryMutationOutcome.APPLIED
        if applied:
            if not isinstance(self.case, TaskRecoveryCase):
                raise TypeError("领取成功必须包含 TaskRecoveryCase")
            if not isinstance(self.authority, RecoveryAuthority):
                raise TypeError("领取成功必须包含 RecoveryAuthority")
            if (
                self.case.case_id != self.authority.case_id
                or self.case.generation != self.authority.generation
            ):
                raise ValueError("Recovery Case 与 Authority 身份不一致")
        elif self.case is not None or self.authority is not None:
            raise ValueError("领取未成功不得携带 Case/Authority")


@dataclass(frozen=True, slots=True)
class TaskRecoveryHeartbeatCommand:
    """续租同一 Recovery Case；旧 Authority 在成功后立即失效。"""

    authority: RecoveryAuthority
    heartbeat_at: str
    lease_expires_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.authority, RecoveryAuthority):
            raise TypeError("authority 必须是 RecoveryAuthority")
        for name in ("heartbeat_at", "lease_expires_at"):
            object.__setattr__(
                self,
                name,
                require_persisted_utc(getattr(self, name), name=name),
            )
        if self.lease_expires_at <= self.heartbeat_at:
            raise ValueError("Recovery 续租到期时间必须晚于 heartbeat_at")


@dataclass(frozen=True, slots=True)
class TaskRecoveryHeartbeatResult:
    """Recovery 续租有限结果；成功时返回带新到期时间的 Authority。"""

    outcome: TaskRecoveryMutationOutcome
    authority: RecoveryAuthority | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, TaskRecoveryMutationOutcome):
            raise TypeError("outcome 必须是 TaskRecoveryMutationOutcome")
        if self.outcome is TaskRecoveryMutationOutcome.APPLIED:
            if not isinstance(self.authority, RecoveryAuthority):
                raise TypeError("Recovery heartbeat 成功必须返回更新后的 Authority")
        elif self.authority is not None:
            raise ValueError("Recovery heartbeat 未成功不得携带 Authority")


@dataclass(frozen=True, slots=True)
class TaskRecoveryOperationIntentCommand:
    """先于事务外 I/O 提交恢复操作 Intent 的完整命令。

    ``operation`` 必须由当前 Recovery Authority 创建且仍处于 intent 状态。Store 对
    operation ID 和 Case 内稳定幂等键同时判重，禁止同一业务操作换 ID 后重复执行。
    """

    authority: RecoveryAuthority
    operation: TaskRecoveryOperation

    def __post_init__(self) -> None:
        if not isinstance(self.authority, RecoveryAuthority):
            raise TypeError("authority 必须是 RecoveryAuthority")
        if not isinstance(self.operation, TaskRecoveryOperation):
            raise TypeError("operation 必须是 TaskRecoveryOperation")
        if (
            self.operation.case_id != self.authority.case_id
            or self.operation.generation != self.authority.generation
            or self.operation.recovery_fencing_token
            != self.authority.fencing_token
        ):
            raise ValueError("Recovery Operation 与当前 Authority 身份不一致")
        # 领域对象已经校验 state/result_at 配对；这里再次冻结 Port 只接受 I/O 前 Intent。
        if (
            self.operation.state is not RecoveryOperationState.INTENT_RECORDED
            or self.operation.result_at
        ):
            raise ValueError("begin_operation 只能提交尚未执行的 Intent")


@dataclass(frozen=True, slots=True)
class TaskRecoveryClassificationCommand:
    """把纯 Policy 结论转为单次 CAS 所需的完整持久化输入。"""

    candidate: TaskRecoveryCandidate
    classification: RecoveryClassification
    policy_version: str
    classified_at: str
    case_id: str = ""
    next_action_at: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, TaskRecoveryCandidate):
            raise TypeError("candidate 必须是 TaskRecoveryCandidate")
        if not isinstance(self.classification, RecoveryClassification):
            raise TypeError("classification 必须是 RecoveryClassification")
        if not isinstance(self.policy_version, str) or not self.policy_version.strip():
            raise ValueError("policy_version 必须是非空 str")
        object.__setattr__(self, "policy_version", self.policy_version.strip())
        object.__setattr__(
            self,
            "classified_at",
            require_persisted_utc(self.classified_at, name="classified_at"),
        )
        if not isinstance(self.case_id, str) or not isinstance(self.next_action_at, str):
            raise TypeError("case_id/next_action_at 必须是 str")
        object.__setattr__(self, "case_id", self.case_id.strip())
        requires_case = self.classification in {
            RecoveryClassification.RECONCILE_REQUIRED,
            RecoveryClassification.FINALIZE_FROM_CHECKPOINT,
        }
        requires_next_action = self.classification in {
            RecoveryClassification.RETRY_SAFE,
            RecoveryClassification.DEFER,
        }
        if requires_case != bool(self.case_id):
            raise ValueError("当前 Recovery 分类的 case_id 存在性不正确")
        if requires_next_action:
            next_action_at = require_persisted_utc(
                self.next_action_at,
                name="next_action_at",
            )
            if next_action_at <= self.classified_at:
                raise ValueError("next_action_at 必须晚于 classified_at")
            object.__setattr__(self, "next_action_at", next_action_at)
        elif self.next_action_at:
            raise ValueError("当前 Recovery 分类不得携带 next_action_at")


@dataclass(frozen=True, slots=True)
class TaskRecoveryClassificationResult:
    """分类 CAS 的有限结果；只有隔离类成功时返回新 Case。"""

    outcome: TaskRecoveryMutationOutcome
    classification: RecoveryClassification
    case: TaskRecoveryCase | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, TaskRecoveryMutationOutcome):
            raise TypeError("outcome 必须是 TaskRecoveryMutationOutcome")
        if not isinstance(self.classification, RecoveryClassification):
            raise TypeError("classification 必须是 RecoveryClassification")
        requires_case = (
            self.outcome is TaskRecoveryMutationOutcome.APPLIED
            and self.classification
            in {
                RecoveryClassification.RECONCILE_REQUIRED,
                RecoveryClassification.FINALIZE_FROM_CHECKPOINT,
            }
        )
        if requires_case != isinstance(self.case, TaskRecoveryCase):
            raise ValueError("Recovery 分类结果携带的 Case 不符合分类/结果")


@runtime_checkable
class TaskRecoveryPolicyPort(Protocol):
    """按业务 Registry 对恢复候选和证据进行纯分类。"""

    policy_version: str

    def classify(
        self,
        candidate: TaskRecoveryCandidate,
        *,
        steps: tuple[TaskStep, ...],
        observations: tuple[TaskRecoveryObservation, ...],
    ) -> RecoveryClassification:
        ...


@runtime_checkable
class TaskRecoveryPort(Protocol):
    """在 Recovery UoW 内完成过期复核、Case 和条件写。"""

    def scan_expired_attempts(self, *, expired_before: str, limit: int) -> tuple[TaskId, ...]:
        ...

    def load_candidate(self, task_id: TaskId) -> TaskRecoveryCandidate | None:
        ...

    def classify_candidate_if_current(
        self,
        command: TaskRecoveryClassificationCommand,
    ) -> TaskRecoveryClassificationResult:
        """以 source Attempt/fencing CAS 持久化五类结论；隔离类才创建 generation。"""
        ...

    def claim_case(self, request: TaskRecoveryClaimRequest) -> TaskRecoveryClaimResult:
        ...

    def heartbeat_case(
        self,
        command: TaskRecoveryHeartbeatCommand,
    ) -> TaskRecoveryHeartbeatResult:
        ...

    def begin_operation(
        self,
        command: TaskRecoveryOperationIntentCommand,
    ) -> TaskRecoveryMutationOutcome:
        """在事务外探测/补偿之前提交稳定 Intent；不得持有事务执行 I/O。"""
        ...

    def append_observation(
        self,
        authority: RecoveryAuthority,
        observation: TaskRecoveryObservation,
    ) -> TaskRecoveryMutationOutcome:
        ...

    def decide_if_current(
        self,
        authority: RecoveryAuthority,
        decision: TaskRecoveryDecision,
    ) -> TaskRecoveryMutationOutcome:
        """提交决定时保持现有 recovery generation，不得再次递增。"""
        ...

    def get_case(self, case_id: str) -> TaskRecoveryCase | None:
        ...

    def list_observations(self, case_id: str) -> tuple[TaskRecoveryObservation, ...]:
        ...

    def list_operations(self, case_id: str) -> tuple[TaskRecoveryOperation, ...]:
        ...


__all__ = [
    "TaskRecoveryClaimRequest",
    "TaskRecoveryClaimResult",
    "TaskRecoveryClassificationCommand",
    "TaskRecoveryClassificationResult",
    "TaskRecoveryHeartbeatCommand",
    "TaskRecoveryHeartbeatResult",
    "TaskRecoveryMutationOutcome",
    "TaskRecoveryOperationIntentCommand",
    "TaskRecoveryPolicyPort",
    "TaskRecoveryPort",
]
