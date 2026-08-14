"""Task Recovery Case、证据、分类和 generation 纯领域规则。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import Enum

from .execution import (
    TaskRecord,
    TaskStep,
    _optional_text,
    _positive_int,
    _required_text,
    _sha256,
    _utc_timestamp,
)
from .models import TaskId
from .states import TaskState, TaskStepState, TaskStepTransition, transition_step_state


_UUIDISH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class RecoveryClassification(str, Enum):
    """Reaper/业务 Policy 的五类稳定结论。"""

    RETRY_SAFE = "retry_safe"
    FINALIZE_FROM_CHECKPOINT = "finalize_from_checkpoint"
    RECONCILE_REQUIRED = "reconcile_required"
    MARK_STALE = "mark_stale"
    DEFER = "defer"


class RecoveryCaseState(str, Enum):
    """一个独立恢复现场的生命周期。"""

    OPEN = "open"
    OBSERVING = "observing"
    AWAITING_EVIDENCE = "awaiting_evidence"
    RESOLVED = "resolved"
    SUPERSEDED = "superseded"


class RecoveryDecisionKind(str, Enum):
    """Recovery Coordinator 可持久化的决定。"""

    KEEP_QUARANTINED = "keep_quarantined"
    RETRY_AUTHORIZED = "retry_authorized"
    FINALIZE_FROM_CHECKPOINT = "finalize_from_checkpoint"
    MARK_STALE = "mark_stale"


class RecoveryObservationKind(str, Enum):
    """事务外探测或补偿返回的有限事实分类。"""

    DEFINITELY_NOT_SENT = "definitely_not_sent"
    EFFECT_CONFIRMED = "effect_confirmed"
    NO_EFFECT_CONFIRMED = "no_effect_confirmed"
    COMPENSATION_CONFIRMED = "compensation_confirmed"
    OUTCOME_UNKNOWN = "outcome_unknown"
    SOURCE_DRIFTED = "source_drifted"
    OWNER_MISMATCH = "owner_mismatch"


class RecoveryOperationKind(str, Enum):
    """Recovery Coordinator 在事务外执行的两类受控操作。"""

    PROBE = "probe"
    COMPENSATION = "compensation"


class RecoveryOperationState(str, Enum):
    """恢复操作的持久阶段；Intent 未收敛时必须继续视为结果未知。"""

    INTENT_RECORDED = "intent_recorded"
    OBSERVATION_RECORDED = "observation_recorded"


@dataclass(frozen=True, slots=True)
class TaskRecoveryIsolation:
    """执行路径发现未知副作用时，原子建立 Recovery Case 所需的冻结身份。

    该对象不携带数据库连接或网络探测结果。Case ID 必须由事务外调用方预先生成，Store 只能按
    完整 Execution Authority 和当前 Step Attempt 做 CAS，不能临时补 ID 或推断 Policy 版本。
    """

    case_id: str
    reason_code: str
    policy_version: str

    def __post_init__(self) -> None:
        case_id = _required_text(self.case_id, name="case_id", maximum=128)
        if _UUIDISH.fullmatch(case_id) is None:
            raise ValueError("case_id 包含不受支持的字符")
        object.__setattr__(self, "case_id", case_id)
        object.__setattr__(
            self,
            "reason_code",
            _required_text(self.reason_code, name="reason_code", maximum=128),
        )
        object.__setattr__(
            self,
            "policy_version",
            _required_text(self.policy_version, name="policy_version", maximum=128),
        )


@dataclass(frozen=True, slots=True)
class RecoveryAuthority:
    """Recovery Case 的独立租约；不得代替 Task Execution Authority。"""

    case_id: str
    generation: int
    owner_id: str
    lease_token: str = field(repr=False)
    fencing_token: int
    lease_expires_at: str

    def __post_init__(self) -> None:
        for name in ("case_id", "owner_id", "lease_token"):
            normalized = _required_text(getattr(self, name), name=name, maximum=256)
            object.__setattr__(self, name, normalized)
        object.__setattr__(self, "generation", _positive_int(self.generation, name="generation"))
        object.__setattr__(self, "fencing_token", _positive_int(self.fencing_token, name="fencing_token"))
        object.__setattr__(self, "lease_expires_at", _utc_timestamp(self.lease_expires_at, name="lease_expires_at"))


@dataclass(frozen=True, slots=True)
class TaskRecoveryCandidate:
    """Reaper 交给纯业务 Policy 的冻结候选，不含 Repository。"""

    task: TaskRecord
    source_attempt_no: int
    source_fencing_token: int
    reason_code: str
    latest_is_current: bool
    evidence_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.task, TaskRecord):
            raise TypeError("task 必须是 TaskRecord")
        if self.task.state is not TaskState.RUNNING:
            raise ValueError("Recovery Candidate 只能来自 running Task")
        for name in ("source_attempt_no", "source_fencing_token"):
            object.__setattr__(self, name, _positive_int(getattr(self, name), name=name))
        object.__setattr__(self, "reason_code", _required_text(self.reason_code, name="reason_code", maximum=128))
        if not isinstance(self.latest_is_current, bool):
            raise TypeError("latest_is_current 必须是 bool")
        object.__setattr__(self, "evidence_digest", _sha256(self.evidence_digest, name="evidence_digest"))


@dataclass(frozen=True, slots=True)
class TaskRecoveryCase:
    """每个 task_id + generation 唯一的恢复当前投影。"""

    case_id: str
    task_id: TaskId
    generation: int
    state: RecoveryCaseState
    source_attempt_no: int
    source_fencing_token: int
    reason_code: str
    policy_version: str
    created_at: str
    recovery_fencing_token: int = 0
    current_decision_id: str = ""
    next_observation_at: str = ""

    def __post_init__(self) -> None:
        case_id = _required_text(self.case_id, name="case_id", maximum=128)
        if _UUIDISH.fullmatch(case_id) is None:
            raise ValueError("case_id 包含不受支持的字符")
        object.__setattr__(self, "case_id", case_id)
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        for name in ("generation", "source_attempt_no", "source_fencing_token"):
            object.__setattr__(self, name, _positive_int(getattr(self, name), name=name))
        if not isinstance(self.state, RecoveryCaseState):
            raise TypeError("state 必须是 RecoveryCaseState")
        object.__setattr__(self, "reason_code", _required_text(self.reason_code, name="reason_code", maximum=128))
        object.__setattr__(self, "policy_version", _required_text(self.policy_version, name="policy_version", maximum=128))
        object.__setattr__(self, "created_at", _utc_timestamp(self.created_at, name="created_at"))
        if type(self.recovery_fencing_token) is not int or self.recovery_fencing_token < 0:
            raise ValueError("recovery_fencing_token 必须是非负整数")
        object.__setattr__(self, "current_decision_id", _optional_text(self.current_decision_id, name="current_decision_id"))
        if self.next_observation_at:
            object.__setattr__(self, "next_observation_at", _utc_timestamp(self.next_observation_at, name="next_observation_at"))


@dataclass(frozen=True, slots=True)
class TaskRecoveryOperation:
    """一次探测或补偿的持久化 Intent 与收敛投影。

    调用方必须先把 ``INTENT_RECORDED`` 提交到数据库，再在事务外执行 I/O。I/O 返回后，
    Observation 与本对象的 ``OBSERVATION_RECORDED`` 状态必须在同一短事务落库。即使新的
    Recovery owner 接管 Case，也只能沿稳定 ``operation_id/idempotency_key`` 对账，不能抹掉
    原 fencing 下已经提交的 Intent。
    """

    operation_id: str
    case_id: str
    generation: int
    recovery_fencing_token: int
    kind: RecoveryOperationKind
    step_key: str
    idempotency_key: str
    intent_digest: str
    external_ref: str
    state: RecoveryOperationState
    intent_at: str
    result_at: str = ""

    def __post_init__(self) -> None:
        for name in ("operation_id", "case_id"):
            value = _required_text(getattr(self, name), name=name, maximum=128)
            if _UUIDISH.fullmatch(value) is None:
                raise ValueError(f"{name} 包含不受支持的字符")
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "generation",
            _positive_int(self.generation, name="generation"),
        )
        object.__setattr__(
            self,
            "recovery_fencing_token",
            _positive_int(
                self.recovery_fencing_token,
                name="recovery_fencing_token",
            ),
        )
        if not isinstance(self.kind, RecoveryOperationKind):
            raise TypeError("kind 必须是 RecoveryOperationKind")
        if not isinstance(self.state, RecoveryOperationState):
            raise TypeError("state 必须是 RecoveryOperationState")
        object.__setattr__(
            self,
            "step_key",
            _required_text(self.step_key, name="step_key"),
        )
        object.__setattr__(
            self,
            "idempotency_key",
            _required_text(self.idempotency_key, name="idempotency_key"),
        )
        object.__setattr__(
            self,
            "intent_digest",
            _sha256(self.intent_digest, name="intent_digest"),
        )
        object.__setattr__(
            self,
            "external_ref",
            _optional_text(self.external_ref, name="external_ref"),
        )
        object.__setattr__(
            self,
            "intent_at",
            _utc_timestamp(self.intent_at, name="intent_at"),
        )
        result_at = ""
        if self.result_at:
            result_at = _utc_timestamp(self.result_at, name="result_at")
            if result_at < self.intent_at:
                raise ValueError("result_at 不得早于 intent_at")
        object.__setattr__(self, "result_at", result_at)
        if (
            self.state is RecoveryOperationState.INTENT_RECORDED
        ) == bool(self.result_at):
            raise ValueError("Intent 状态不得有 result_at，已收敛状态必须有 result_at")


@dataclass(frozen=True, slots=True)
class TaskRecoveryObservation:
    """Recovery fencing 保护的追加证据；只保存摘要和稳定引用。"""

    observation_id: str
    operation_id: str
    case_id: str
    generation: int
    recovery_fencing_token: int
    kind: RecoveryObservationKind
    evidence_digest: str
    observed_at: str
    step_key: str = ""
    external_ref: str = ""
    reason_code: str = ""

    def __post_init__(self) -> None:
        for name in ("observation_id", "operation_id", "case_id"):
            value = _required_text(getattr(self, name), name=name, maximum=128)
            if _UUIDISH.fullmatch(value) is None:
                raise ValueError(f"{name} 包含不受支持的字符")
            object.__setattr__(self, name, value)
        object.__setattr__(self, "generation", _positive_int(self.generation, name="generation"))
        object.__setattr__(self, "recovery_fencing_token", _positive_int(self.recovery_fencing_token, name="recovery_fencing_token"))
        if not isinstance(self.kind, RecoveryObservationKind):
            raise TypeError("kind 必须是 RecoveryObservationKind")
        object.__setattr__(self, "evidence_digest", _sha256(self.evidence_digest, name="evidence_digest"))
        object.__setattr__(self, "observed_at", _utc_timestamp(self.observed_at, name="observed_at"))
        for name in ("step_key", "external_ref"):
            object.__setattr__(self, name, _optional_text(getattr(self, name), name=name))
        object.__setattr__(self, "reason_code", _optional_text(self.reason_code, name="reason_code", maximum=128))


@dataclass(frozen=True, slots=True)
class TaskRecoveryStepResolution:
    """Recovery Decision 对一个精确 unknown Step 投影的重试授权。

    operation/observation/digest 是已落库证据的稳定身份；Store 还必须以 Step Attempt 序号和
    row version 做 CAS。该对象只授权把当前投影转回 pending，不修改旧 Step Attempt 历史。
    """

    source_step_key: str
    source_step_attempt_no: int
    expected_step_row_version: int
    operation_id: str
    observation_id: str
    evidence_digest: str
    target_transition: TaskStepTransition

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_step_key",
            _required_text(self.source_step_key, name="source_step_key"),
        )
        object.__setattr__(
            self,
            "source_step_attempt_no",
            _positive_int(
                self.source_step_attempt_no,
                name="source_step_attempt_no",
            ),
        )
        if (
            type(self.expected_step_row_version) is not int
            or self.expected_step_row_version < 0
        ):
            raise ValueError("expected_step_row_version 必须是非负整数")
        for name in ("operation_id", "observation_id"):
            value = _required_text(getattr(self, name), name=name, maximum=128)
            if _UUIDISH.fullmatch(value) is None:
                raise ValueError(f"{name} 包含不受支持的字符")
            object.__setattr__(self, name, value)
        object.__setattr__(
            self,
            "evidence_digest",
            _sha256(self.evidence_digest, name="evidence_digest"),
        )
        if self.target_transition is not TaskStepTransition.RETRY_AUTHORIZED:
            raise ValueError("Step Resolution 当前只允许 RETRY_AUTHORIZED")


@dataclass(frozen=True, slots=True)
class TaskRecoveryTerminalProjection:
    """从已验证 Step Checkpoint 收敛业务终态所需的最小内部投影。

    ``source_step_attempt_no + checkpoint_code + checkpoint_digest`` 是 Store 必须重新核对的来源身份；
    ``public_status/message/result_ref`` 只是写回既有 latest 投影所需的内部值，不新增任何公开字段。
    正文、凭据、完整响应和文件内容禁止进入该对象。
    """

    source_step_key: str
    source_step_attempt_no: int
    checkpoint_code: str
    checkpoint_digest: str
    public_status: str
    message: str
    result_ref: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_step_key",
            _required_text(self.source_step_key, name="source_step_key"),
        )
        object.__setattr__(
            self,
            "source_step_attempt_no",
            _positive_int(self.source_step_attempt_no, name="source_step_attempt_no"),
        )
        object.__setattr__(
            self,
            "checkpoint_code",
            _required_text(self.checkpoint_code, name="checkpoint_code", maximum=128),
        )
        object.__setattr__(
            self,
            "checkpoint_digest",
            _sha256(self.checkpoint_digest, name="checkpoint_digest"),
        )
        object.__setattr__(
            self,
            "public_status",
            _required_text(self.public_status, name="public_status", maximum=128),
        )
        object.__setattr__(
            self,
            "message",
            _optional_text(self.message, name="message"),
        )
        object.__setattr__(
            self,
            "result_ref",
            _optional_text(self.result_ref, name="result_ref"),
        )


@dataclass(frozen=True, slots=True)
class TaskRecoveryDecision:
    """纯 Policy 形成、随后由 Store 以 Case/Task CAS 提交的决定。"""

    decision_id: str
    task_id: TaskId
    case_id: str
    generation: int
    recovery_fencing_token: int
    expected_task_row_version: int
    source_attempt_no: int
    source_fencing_token: int
    kind: RecoveryDecisionKind
    evidence_digest: str
    reason_code: str
    policy_version: str
    actor_marker: str
    decided_at: str
    retry_from_step_key: str = ""
    terminal_state: TaskState | None = None
    next_observation_at: str = ""
    terminal_projection: TaskRecoveryTerminalProjection | None = None
    step_resolution: TaskRecoveryStepResolution | None = None

    def __post_init__(self) -> None:
        for name in ("decision_id", "case_id"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name=name, maximum=128))
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        for name in ("generation", "recovery_fencing_token", "source_attempt_no", "source_fencing_token"):
            object.__setattr__(self, name, _positive_int(getattr(self, name), name=name))
        if type(self.expected_task_row_version) is not int or self.expected_task_row_version < 0:
            raise ValueError("expected_task_row_version 必须是非负整数")
        if not isinstance(self.kind, RecoveryDecisionKind):
            raise TypeError("kind 必须是 RecoveryDecisionKind")
        object.__setattr__(self, "evidence_digest", _sha256(self.evidence_digest, name="evidence_digest"))
        for name in ("reason_code", "policy_version", "actor_marker"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name=name, maximum=128))
        object.__setattr__(self, "decided_at", _utc_timestamp(self.decided_at, name="decided_at"))
        object.__setattr__(self, "retry_from_step_key", _optional_text(self.retry_from_step_key, name="retry_from_step_key"))
        if self.next_observation_at:
            next_observation_at = _utc_timestamp(
                self.next_observation_at,
                name="next_observation_at",
            )
            if next_observation_at <= self.decided_at:
                raise ValueError("next_observation_at 必须晚于 decided_at")
            object.__setattr__(self, "next_observation_at", next_observation_at)
        if self.terminal_projection is not None and not isinstance(
            self.terminal_projection,
            TaskRecoveryTerminalProjection,
        ):
            raise TypeError("terminal_projection 必须是 TaskRecoveryTerminalProjection 或 None")
        if self.step_resolution is not None and not isinstance(
            self.step_resolution,
            TaskRecoveryStepResolution,
        ):
            raise TypeError("step_resolution 必须是 TaskRecoveryStepResolution 或 None")
        if self.terminal_state not in {None, TaskState.SUCCEEDED, TaskState.FAILED}:
            raise ValueError("terminal_state 只能是 succeeded/failed/None")
        if self.kind is RecoveryDecisionKind.KEEP_QUARANTINED:
            if (
                self.retry_from_step_key
                or self.terminal_state is not None
                or self.terminal_projection is not None
                or self.step_resolution is not None
            ):
                raise ValueError("keep_quarantined 不得携带重试或终态字段")
        elif self.kind is RecoveryDecisionKind.RETRY_AUTHORIZED:
            if (
                not self.retry_from_step_key
                or self.terminal_state is not None
                or self.next_observation_at
                or self.terminal_projection is not None
                or not isinstance(self.step_resolution, TaskRecoveryStepResolution)
                or self.retry_from_step_key
                != self.step_resolution.source_step_key
            ):
                raise ValueError("retry_authorized 必须携带匹配的 Step Resolution")
        elif self.kind is RecoveryDecisionKind.FINALIZE_FROM_CHECKPOINT:
            if (
                self.terminal_state not in {TaskState.SUCCEEDED, TaskState.FAILED}
                or not isinstance(self.terminal_projection, TaskRecoveryTerminalProjection)
                or self.retry_from_step_key
                or self.next_observation_at
                or self.step_resolution is not None
            ):
                raise ValueError("finalize_from_checkpoint 必须只携带完整终态投影")
        elif (
            self.retry_from_step_key
            or self.terminal_state is not None
            or self.next_observation_at
            or self.terminal_projection is not None
            or self.step_resolution is not None
        ):
            raise ValueError("当前 Recovery Decision 不得携带重试或终态字段")

    @property
    def closes_case(self) -> bool:
        return self.kind is not RecoveryDecisionKind.KEEP_QUARANTINED


def create_recovery_case(
    task: TaskRecord,
    *,
    case_id: str,
    source_attempt_no: int,
    source_fencing_token: int,
    reason_code: str,
    policy_version: str,
    created_at: str,
) -> tuple[TaskRecord, TaskRecoveryCase]:
    """创建全新独立恢复现场；这是 recovery_generation 的唯一递增入口。"""

    if not isinstance(task, TaskRecord):
        raise TypeError("task 必须是 TaskRecord")
    if task.state is not TaskState.RUNNING:
        raise ValueError("只有 running Task 可以创建新的 Recovery Case")
    generation = task.recovery_generation + 1
    case = TaskRecoveryCase(
        case_id=case_id,
        task_id=task.task_id,
        generation=generation,
        state=RecoveryCaseState.OPEN,
        source_attempt_no=source_attempt_no,
        source_fencing_token=source_fencing_token,
        reason_code=reason_code,
        policy_version=policy_version,
        created_at=created_at,
    )
    updated = replace(
        task,
        state=TaskState.RECOVERY_REQUIRED,
        recovery_generation=generation,
        current_recovery_case_id=case.case_id,
        recovery_reason_code=case.reason_code,
        retry_from_step_key="",
    )
    return updated, case


def claim_recovery_case(
    case: TaskRecoveryCase,
    *,
    owner_id: str,
    lease_token: str,
    lease_expires_at: str,
) -> tuple[TaskRecoveryCase, RecoveryAuthority]:
    """领取同一个 Case；只递增 recovery fencing，不递增 generation。"""

    if not isinstance(case, TaskRecoveryCase):
        raise TypeError("case 必须是 TaskRecoveryCase")
    if case.state not in {RecoveryCaseState.OPEN, RecoveryCaseState.AWAITING_EVIDENCE}:
        raise ValueError("只有 open/awaiting_evidence Case 可以领取")
    fencing = case.recovery_fencing_token + 1
    authority = RecoveryAuthority(
        case_id=case.case_id,
        generation=case.generation,
        owner_id=owner_id,
        lease_token=lease_token,
        fencing_token=fencing,
        lease_expires_at=lease_expires_at,
    )
    return replace(case, state=RecoveryCaseState.OBSERVING, recovery_fencing_token=fencing), authority


def take_over_expired_recovery_case(
    case: TaskRecoveryCase,
    current_authority: RecoveryAuthority,
    *,
    claimed_at: str,
    owner_id: str,
    lease_token: str,
    lease_expires_at: str,
) -> tuple[TaskRecoveryCase, RecoveryAuthority]:
    """仅在数据库租约已到期时接管 observing Case，并单调递增 fencing。

    Store 必须从同一数据库行构造 ``current_authority``，再于一条条件 UPDATE 中重复核对
    generation/fencing/token/expiry；本纯函数只冻结状态和时间边界，不能替代数据库 CAS。
    """

    if not isinstance(case, TaskRecoveryCase):
        raise TypeError("case 必须是 TaskRecoveryCase")
    if not isinstance(current_authority, RecoveryAuthority):
        raise TypeError("current_authority 必须是 RecoveryAuthority")
    claimed_at = _utc_timestamp(claimed_at, name="claimed_at")
    if case.state is not RecoveryCaseState.OBSERVING:
        raise ValueError("只有 observing Case 需要执行租约接管")
    if (
        current_authority.case_id != case.case_id
        or current_authority.generation != case.generation
        or current_authority.fencing_token != case.recovery_fencing_token
    ):
        raise ValueError("当前 Recovery Authority 与 Case 不一致")
    if current_authority.lease_expires_at > claimed_at:
        raise ValueError("未过期 Recovery lease 不得被抢占")
    lease_expires_at = _utc_timestamp(
        lease_expires_at,
        name="lease_expires_at",
    )
    if lease_expires_at <= claimed_at:
        raise ValueError("新 Recovery lease 必须晚于 claimed_at")
    return claim_recovery_case(
        replace(case, state=RecoveryCaseState.OPEN),
        owner_id=owner_id,
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
    )


def converge_recovery_operation(
    operation: TaskRecoveryOperation,
    observation: TaskRecoveryObservation,
) -> TaskRecoveryOperation:
    """把一个已提交 Intent 与唯一 Observation 收敛，供 Store 事务内复用。"""

    if not isinstance(operation, TaskRecoveryOperation):
        raise TypeError("operation 必须是 TaskRecoveryOperation")
    if not isinstance(observation, TaskRecoveryObservation):
        raise TypeError("observation 必须是 TaskRecoveryObservation")
    if operation.state is not RecoveryOperationState.INTENT_RECORDED:
        raise ValueError("Recovery Operation 已经收敛")
    if (
        observation.operation_id != operation.operation_id
        or observation.case_id != operation.case_id
        or observation.generation != operation.generation
        or observation.step_key != operation.step_key
    ):
        raise ValueError("Observation 与 Recovery Operation 身份不一致")
    if observation.observed_at < operation.intent_at:
        raise ValueError("Observation 不得早于 Operation Intent")
    return replace(
        operation,
        state=RecoveryOperationState.OBSERVATION_RECORDED,
        result_at=observation.observed_at,
    )


def apply_recovery_step_resolution(
    step: TaskStep,
    resolution: TaskRecoveryStepResolution,
) -> TaskStep:
    """按精确 Step CAS 身份把 unknown 当前投影转回 pending。

    旧 ``TaskStepAttempt`` 是追加历史，不作为参数也不被改写；下一个普通 Task Attempt 只能在
    本投影提交成功后，通过标准 ``BEGIN`` 创建新的 Step Attempt。
    """

    if not isinstance(step, TaskStep):
        raise TypeError("step 必须是 TaskStep")
    if not isinstance(resolution, TaskRecoveryStepResolution):
        raise TypeError("resolution 必须是 TaskRecoveryStepResolution")
    if (
        step.step_key != resolution.source_step_key
        or step.current_step_attempt_no != resolution.source_step_attempt_no
        or step.row_version != resolution.expected_step_row_version
    ):
        raise ValueError("Step Resolution 与当前 Step 投影不一致")
    if step.state is not TaskStepState.OUTCOME_UNKNOWN:
        raise ValueError("只有 outcome_unknown Step 可以应用重试授权")
    return replace(
        step,
        state=transition_step_state(step.state, resolution.target_transition),
        checkpoint=None,
        row_version=step.row_version + 1,
    )


def apply_recovery_decision(
    task: TaskRecord,
    case: TaskRecoveryCase,
    decision: TaskRecoveryDecision,
) -> tuple[TaskRecord, TaskRecoveryCase]:
    """纯计算恢复收敛；generation 在所有决定中保持不变。"""

    if not all((isinstance(task, TaskRecord), isinstance(case, TaskRecoveryCase), isinstance(decision, TaskRecoveryDecision))):
        raise TypeError("task/case/decision 类型不正确")
    if task.state is not TaskState.RECOVERY_REQUIRED:
        raise ValueError("Task 不处于 recovery_required")
    if (
        task.task_id != case.task_id
        or decision.task_id != task.task_id
        or task.current_recovery_case_id != case.case_id
        or decision.case_id != case.case_id
        or task.recovery_generation != case.generation
        or decision.generation != case.generation
        or decision.recovery_fencing_token != case.recovery_fencing_token
        or decision.expected_task_row_version != task.row_version
        or decision.source_attempt_no != case.source_attempt_no
        or decision.source_fencing_token != case.source_fencing_token
    ):
        raise ValueError("Recovery Decision 与 Task/Case Authority 不一致")
    if case.state is not RecoveryCaseState.OBSERVING:
        raise ValueError("只有 observing Case 可以提交 Decision")

    if decision.kind is RecoveryDecisionKind.KEEP_QUARANTINED:
        return task, replace(
            case,
            state=RecoveryCaseState.AWAITING_EVIDENCE,
            current_decision_id=decision.decision_id,
            next_observation_at=decision.next_observation_at,
        )
    if decision.kind is RecoveryDecisionKind.RETRY_AUTHORIZED:
        target = TaskState.ACCEPTED
        retry_from = decision.retry_from_step_key
        case_state = RecoveryCaseState.RESOLVED
    elif decision.kind is RecoveryDecisionKind.FINALIZE_FROM_CHECKPOINT:
        assert decision.terminal_state is not None
        target = decision.terminal_state
        retry_from = ""
        case_state = RecoveryCaseState.RESOLVED
    else:
        target = TaskState.STALE
        retry_from = ""
        case_state = RecoveryCaseState.SUPERSEDED

    updated_task = replace(
        task,
        state=target,
        row_version=task.row_version + 1,
        current_recovery_case_id="",
        recovery_reason_code="",
        retry_from_step_key=retry_from,
    )
    updated_case = replace(
        case,
        state=case_state,
        current_decision_id=decision.decision_id,
        next_observation_at="",
    )
    if updated_task.recovery_generation != task.recovery_generation:
        raise AssertionError("Recovery Decision 禁止递增 recovery_generation")
    return updated_task, updated_case


__all__ = [
    "RecoveryAuthority",
    "RecoveryCaseState",
    "RecoveryClassification",
    "RecoveryDecisionKind",
    "RecoveryObservationKind",
    "RecoveryOperationKind",
    "RecoveryOperationState",
    "TaskRecoveryCandidate",
    "TaskRecoveryCase",
    "TaskRecoveryDecision",
    "TaskRecoveryIsolation",
    "TaskRecoveryObservation",
    "TaskRecoveryOperation",
    "TaskRecoveryStepResolution",
    "TaskRecoveryTerminalProjection",
    "apply_recovery_decision",
    "apply_recovery_step_resolution",
    "claim_recovery_case",
    "converge_recovery_operation",
    "create_recovery_case",
    "take_over_expired_recovery_case",
]
