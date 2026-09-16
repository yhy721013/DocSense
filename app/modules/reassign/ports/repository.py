"""分类节点变更的本地事实、短事务与所有权端口。

本模块只描述 Application 需要持久化和读取的业务事实。它不暴露 SQLite 连接、SQL、事务
对象或具体数据库异常；实现方必须在每次 Unit of Work 中只完成有限的本地读写，绝不能把
AnythingLLM 等网络 I/O 包在事务内。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from app.modules.reassign.domain import (
    REASSIGNMENT_ERROR_CODE_MAX_LENGTH,
    REASSIGNMENT_ERROR_SUMMARY_MAX_LENGTH,
    REASSIGNMENT_EXTERNAL_REFERENCE_MAX_LENGTH,
    ReassignmentBindingState,
    ReassignDocumentCommand,
    ReassignmentDocumentSnapshot,
    ReassignmentMutationOutcome,
    ReassignmentOperation,
    ReassignmentOperationStatus,
    ReassignmentRawValue,
    ReassignmentStep,
    ReassignmentStepName,
    ReassignmentStepState,
    ReassignmentTerminalEvidence,
)
from app.modules.reassign.ports.knowledge import ReassignmentWorkspaceOwnership


def _required_text(value: object, *, name: str, max_length: int | None = None) -> str:
    """校验端口内部必填文本，不进行隐式字符串转换。"""

    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    if max_length is not None and len(normalized) > max_length:
        raise ValueError(f"{name} 长度不能超过 {max_length}")
    return normalized


def _optional_text(value: object, *, name: str, max_length: int) -> str | None:
    """校验可空诊断文本，防止未脱敏供应商正文进入持久化事实。"""

    if value is None:
        return None
    return _required_text(value, name=name, max_length=max_length)


def _positive_int(value: object, *, name: str) -> int:
    """校验行号、序号和 fencing token 等内部正整数。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} 必须是正整数")
    return value


def _nonnegative_int(value: object, *, name: str) -> int:
    """校验步骤尝试次数等允许为零的内部整数。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} 必须是非负整数")
    return value


class ReassignmentReservationOutcome(str, Enum):
    """原子保留同文档执行权的确定结果。"""

    ACQUIRED = "acquired"
    DOCUMENT_NOT_FOUND = "document_not_found"
    ACTIVE_OPERATION_EXISTS = "active_operation_exists"


class ReassignmentWorkspacePreparationClaimOutcome(str, Enum):
    """按目标分类串行化 workspace 准备的确定结果。

    该结果只描述内部持久化准备权，不能映射为公开接口的并发语义。不同文档可以同时迁移到
    不同目标分类；只有尚未写入本地 mapping 的同一目标分类需要彼此排队。
    """

    ACQUIRED = "acquired"
    MAPPING_EXISTS = "mapping_exists"
    ACTIVE_CLAIM_EXISTS = "active_claim_exists"


class ReassignmentWriteOutcome(str, Enum):
    """受 lease/fencing 保护的本地写结果。"""

    APPLIED = "applied"
    OPERATION_NOT_FOUND = "operation_not_found"
    STALE_LEASE = "stale_lease"
    CONFLICT = "conflict"
    NOT_EXPIRED = "not_expired"


class ReassignmentEventType(str, Enum):
    """追加审计事件的受控类别，不保存任意供应商正文。"""

    OPERATION_RESERVED = "operation_reserved"
    LEASE_RENEWED = "lease_renewed"
    LEASE_TAKEN_OVER = "lease_taken_over"
    STEP_MUTATION_STARTED = "step_mutation_started"
    STEP_COMPLETED = "step_completed"
    OPERATION_TRANSITIONED = "operation_transitioned"
    WORKSPACE_MAPPING_RECORDED = "workspace_mapping_recorded"
    WORKSPACE_PREPARATION_CLAIM_ACQUIRED = "workspace_preparation_claim_acquired"
    WORKSPACE_PREPARATION_CLAIM_RELEASED = "workspace_preparation_claim_released"
    WORKSPACE_PREPARATION_CLAIM_BLOCKED = "workspace_preparation_claim_blocked"
    WORKSPACE_PREPARATION_CLAIM_TAKEN_OVER = "workspace_preparation_claim_taken_over"
    WORKSPACE_PREPARATION_FACT_RECORDED = "workspace_preparation_fact_recorded"
    BEST_EFFORT_PIN_ATTEMPTED = "best_effort_pin_attempted"
    BEST_EFFORT_PIN_COMPLETED = "best_effort_pin_completed"
    NO_SIDE_EFFECT_FAILURE_FINALIZED = "no_side_effect_failure_finalized"
    LOCAL_ARCHITECTURE_COMMITTED = "local_architecture_committed"
    LOCAL_ARCHITECTURE_CONFLICT = "local_architecture_conflict"
    RECOVERY_OBSERVATION_RECORDED = "recovery_observation_recorded"
    RECOVERY_OPERATION_FINALIZED = "recovery_operation_finalized"


@dataclass(frozen=True)
class ReassignmentLease:
    """一次本地写入权的内部 lease 与 fencing 事实。"""

    operation_id: str
    owner: str
    token: str
    fencing_token: int
    expires_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            _required_text(self.operation_id, name="operation_id"),
        )
        object.__setattr__(self, "owner", _required_text(self.owner, name="owner"))
        object.__setattr__(self, "token", _required_text(self.token, name="token"))
        object.__setattr__(
            self,
            "fencing_token",
            _positive_int(self.fencing_token, name="fencing_token"),
        )
        object.__setattr__(
            self,
            "expires_at",
            _required_text(self.expires_at, name="expires_at"),
        )


@dataclass(frozen=True)
class ReassignmentWorkspacePreparationClaim:
    """按目标分类持有的短期持久化准备权及 fencing 事实。

    文档 Operation lease 保护的是一份源文档；本对象保护的是“尚未落库的目标 workspace
    mapping”。二者不能混用：一个目标分类可能同时被多份不同源文档迁移，而同一时刻只允许
    一个 Operation 发起该目标的 workspace 创建。``fencing_token`` 对目标分类单独递增，
    过期 owner 即使稍后恢复执行，也不能提交陈旧的 mapping。
    """

    target_architecture_raw: object
    operation_id: str
    owner: str
    token: str
    fencing_token: int
    expires_at: str

    def __post_init__(self) -> None:
        raw = ReassignmentRawValue.from_external_value(self.target_architecture_raw)
        if raw.value is None:
            raise ValueError("target_architecture_raw 不能为空")
        object.__setattr__(self, "target_architecture_raw", raw)
        object.__setattr__(
            self,
            "operation_id",
            _required_text(self.operation_id, name="operation_id"),
        )
        object.__setattr__(self, "owner", _required_text(self.owner, name="owner"))
        object.__setattr__(self, "token", _required_text(self.token, name="token"))
        object.__setattr__(
            self,
            "fencing_token",
            _positive_int(self.fencing_token, name="fencing_token"),
        )
        object.__setattr__(
            self,
            "expires_at",
            _required_text(self.expires_at, name="expires_at"),
        )


@dataclass(frozen=True)
class ReassignmentReservationRequest:
    """创建 Operation 前由 Application 提供的命令和 lease 初始事实。"""

    command: ReassignDocumentCommand
    operation_id: str
    lease_owner: str
    lease_token: str
    lease_expires_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.command, ReassignDocumentCommand):
            raise TypeError("command 必须是 ReassignDocumentCommand")
        object.__setattr__(
            self,
            "operation_id",
            _required_text(self.operation_id, name="operation_id"),
        )
        object.__setattr__(
            self,
            "lease_owner",
            _required_text(self.lease_owner, name="lease_owner"),
        )
        object.__setattr__(
            self,
            "lease_token",
            _required_text(self.lease_token, name="lease_token"),
        )
        object.__setattr__(
            self,
            "lease_expires_at",
            _required_text(self.lease_expires_at, name="lease_expires_at"),
        )


@dataclass(frozen=True)
class ReassignmentOperationRecord:
    """Operation 与持久化诊断、workspace 事实的完整读取快照。"""

    operation: ReassignmentOperation
    source_workspace_slug: str | None
    target_workspace_slug: str | None
    target_workspace_ownership: ReassignmentWorkspaceOwnership | None
    error_code: str | None
    error_summary: str | None
    recovery_required_fencing_token: int | None
    created_at: str
    updated_at: str
    finished_at: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.operation, ReassignmentOperation):
            raise TypeError("operation 必须是 ReassignmentOperation")
        object.__setattr__(
            self,
            "source_workspace_slug",
            _optional_text(
                self.source_workspace_slug,
                name="source_workspace_slug",
                max_length=REASSIGNMENT_EXTERNAL_REFERENCE_MAX_LENGTH,
            ),
        )
        target_workspace_slug = _optional_text(
            self.target_workspace_slug,
            name="target_workspace_slug",
            max_length=REASSIGNMENT_EXTERNAL_REFERENCE_MAX_LENGTH,
        )
        object.__setattr__(self, "target_workspace_slug", target_workspace_slug)
        if self.target_workspace_ownership is not None and not isinstance(
            self.target_workspace_ownership,
            ReassignmentWorkspaceOwnership,
        ):
            raise TypeError(
                "target_workspace_ownership 必须是 "
                "ReassignmentWorkspaceOwnership 或 None"
            )
        if (target_workspace_slug is None) != (
            self.target_workspace_ownership is None
        ):
            raise ValueError(
                "目标 workspace 引用与创建归属必须同时存在或同时为空"
            )
        object.__setattr__(
            self,
            "error_code",
            _optional_text(
                self.error_code,
                name="error_code",
                max_length=REASSIGNMENT_ERROR_CODE_MAX_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "error_summary",
            _optional_text(
                self.error_summary,
                name="error_summary",
                max_length=REASSIGNMENT_ERROR_SUMMARY_MAX_LENGTH,
            ),
        )
        if self.recovery_required_fencing_token is not None:
            object.__setattr__(
                self,
                "recovery_required_fencing_token",
                _positive_int(
                    self.recovery_required_fencing_token,
                    name="recovery_required_fencing_token",
                ),
            )
        if (
            self.operation.status is ReassignmentOperationStatus.RECOVERY_REQUIRED
            and self.recovery_required_fencing_token is None
        ):
            raise ValueError(
                "recovery_required Operation 必须保存进入隔离时的 fencing token"
            )
        object.__setattr__(
            self,
            "created_at",
            _required_text(self.created_at, name="created_at"),
        )
        object.__setattr__(
            self,
            "updated_at",
            _required_text(self.updated_at, name="updated_at"),
        )
        object.__setattr__(
            self,
            "finished_at",
            _optional_text(
                self.finished_at,
                name="finished_at",
                max_length=64,
            ),
        )
        terminal_statuses = {
            ReassignmentOperationStatus.SUCCEEDED,
            ReassignmentOperationStatus.FAILED,
            ReassignmentOperationStatus.COMPENSATED,
        }
        if self.operation.status in terminal_statuses and self.finished_at is None:
            raise ValueError("释放文档保护的终态必须保存 finished_at")
        if self.operation.status not in terminal_statuses and self.finished_at is not None:
            raise ValueError("未终态 Operation 不能保存 finished_at")

    @property
    def lease(self) -> ReassignmentLease:
        """从 Operation 成组 lease 事实生成后续条件写所需的不可变凭据。"""

        operation = self.operation
        if (
            operation.lease_owner is None
            or operation.lease_token is None
            or operation.lease_expires_at is None
            or operation.fencing_token is None
        ):
            raise ValueError("Operation 不包含完整 lease 事实")
        return ReassignmentLease(
            operation_id=operation.operation_id,
            owner=operation.lease_owner,
            token=operation.lease_token,
            fencing_token=operation.fencing_token,
            expires_at=operation.lease_expires_at,
        )


@dataclass(frozen=True)
class ReassignmentStepRecord:
    """Step 与写后探测、尝试次数和时间戳的持久化读取快照。"""

    step: ReassignmentStep
    attempt_count: int
    last_attempt_fencing_token: int | None
    mutation_started_at: str | None
    probe_outcome: ReassignmentMutationOutcome | None
    created_at: str
    updated_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.step, ReassignmentStep):
            raise TypeError("step 必须是 ReassignmentStep")
        object.__setattr__(
            self,
            "attempt_count",
            _nonnegative_int(self.attempt_count, name="attempt_count"),
        )
        if self.last_attempt_fencing_token is not None:
            object.__setattr__(
                self,
                "last_attempt_fencing_token",
                _positive_int(
                    self.last_attempt_fencing_token,
                    name="last_attempt_fencing_token",
                ),
            )
        if self.attempt_count == 0 and self.last_attempt_fencing_token is not None:
            raise ValueError("未尝试的 Step 不能保存 last_attempt_fencing_token")
        if self.attempt_count > 0 and self.last_attempt_fencing_token is None:
            raise ValueError("已尝试的 Step 必须保存 last_attempt_fencing_token")
        object.__setattr__(
            self,
            "mutation_started_at",
            _optional_text(
                self.mutation_started_at,
                name="mutation_started_at",
                max_length=64,
            ),
        )
        if self.attempt_count > 0 and self.mutation_started_at is None:
            raise ValueError("已尝试的 Step 必须保存 mutation_started_at")
        if self.probe_outcome is not None and not isinstance(
            self.probe_outcome,
            ReassignmentMutationOutcome,
        ):
            raise TypeError("probe_outcome 必须是 ReassignmentMutationOutcome 或 None")
        object.__setattr__(
            self,
            "created_at",
            _required_text(self.created_at, name="created_at"),
        )
        object.__setattr__(
            self,
            "updated_at",
            _required_text(self.updated_at, name="updated_at"),
        )


@dataclass(frozen=True)
class ReassignmentAuditEvent:
    """只追加的内部审计事件；不会携带公开响应字段或供应商正文。"""

    operation_id: str
    sequence_no: int
    event_type: ReassignmentEventType
    occurred_at: str
    step_name: ReassignmentStepName | None = None
    operation_status: ReassignmentOperationStatus | None = None
    detail_code: str | None = None
    reference_digest: str | None = None
    fencing_token: int | None = None
    attempt_count: int | None = None
    probe_outcome: ReassignmentMutationOutcome | None = None
    actor_digest: str | None = None
    reason_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            _required_text(self.operation_id, name="operation_id"),
        )
        object.__setattr__(
            self,
            "sequence_no",
            _positive_int(self.sequence_no, name="sequence_no"),
        )
        if not isinstance(self.event_type, ReassignmentEventType):
            raise TypeError("event_type 必须是 ReassignmentEventType")
        object.__setattr__(
            self,
            "occurred_at",
            _required_text(self.occurred_at, name="occurred_at"),
        )
        if self.step_name is not None and not isinstance(
            self.step_name,
            ReassignmentStepName,
        ):
            raise TypeError("step_name 必须是 ReassignmentStepName 或 None")
        if self.operation_status is not None and not isinstance(
            self.operation_status,
            ReassignmentOperationStatus,
        ):
            raise TypeError(
                "operation_status 必须是 ReassignmentOperationStatus 或 None"
            )
        object.__setattr__(
            self,
            "detail_code",
            _optional_text(
                self.detail_code,
                name="detail_code",
                max_length=REASSIGNMENT_ERROR_CODE_MAX_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "reference_digest",
            _optional_text(
                self.reference_digest,
                name="reference_digest",
                max_length=REASSIGNMENT_ERROR_CODE_MAX_LENGTH,
            ),
        )
        if self.fencing_token is not None:
            object.__setattr__(
                self,
                "fencing_token",
                _positive_int(self.fencing_token, name="fencing_token"),
            )
        if self.attempt_count is not None:
            object.__setattr__(
                self,
                "attempt_count",
                _nonnegative_int(self.attempt_count, name="attempt_count"),
            )
        if self.probe_outcome is not None and not isinstance(
            self.probe_outcome,
            ReassignmentMutationOutcome,
        ):
            raise TypeError(
                "probe_outcome 必须是 ReassignmentMutationOutcome 或 None"
            )
        object.__setattr__(
            self,
            "actor_digest",
            _optional_text(
                self.actor_digest,
                name="actor_digest",
                max_length=REASSIGNMENT_ERROR_CODE_MAX_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "reason_code",
            _optional_text(
                self.reason_code,
                name="reason_code",
                max_length=REASSIGNMENT_ERROR_CODE_MAX_LENGTH,
            ),
        )


@dataclass(frozen=True)
class ReassignmentReservationResult:
    """原子保留文档后的结果，不把活动 Operation 当作请求成功缓存。"""

    outcome: ReassignmentReservationOutcome
    record: ReassignmentOperationRecord | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ReassignmentReservationOutcome):
            raise TypeError("outcome 必须是 ReassignmentReservationOutcome")
        if self.outcome is ReassignmentReservationOutcome.ACQUIRED:
            if not isinstance(self.record, ReassignmentOperationRecord):
                raise ValueError("acquired 必须携带 ReassignmentOperationRecord")
        elif self.record is not None:
            raise ValueError("未获得执行权时不能携带 Operation 记录")


@dataclass(frozen=True)
class ReassignmentWorkspacePreparationClaimRequest:
    """申请同一目标分类 workspace 准备权的内部 Compare-And-Swap 请求。"""

    operation_lease: ReassignmentLease
    target_architecture_raw: object
    claim_token: str
    claim_expires_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.operation_lease, ReassignmentLease):
            raise TypeError("operation_lease 必须是 ReassignmentLease")
        raw = ReassignmentRawValue.from_external_value(self.target_architecture_raw)
        if raw.value is None:
            raise ValueError("target_architecture_raw 不能为空")
        object.__setattr__(self, "target_architecture_raw", raw)
        object.__setattr__(
            self,
            "claim_token",
            _required_text(self.claim_token, name="claim_token"),
        )
        object.__setattr__(
            self,
            "claim_expires_at",
            _required_text(self.claim_expires_at, name="claim_expires_at"),
        )


@dataclass(frozen=True)
class ReassignmentWorkspacePreparationClaimResult:
    """准备权申请结果；成功持有与已存在 mapping 互斥。"""

    outcome: ReassignmentWorkspacePreparationClaimOutcome
    claim: ReassignmentWorkspacePreparationClaim | None = None
    workspace_slug: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(
            self.outcome,
            ReassignmentWorkspacePreparationClaimOutcome,
        ):
            raise TypeError(
                "outcome 必须是 ReassignmentWorkspacePreparationClaimOutcome"
            )
        if self.outcome is ReassignmentWorkspacePreparationClaimOutcome.ACQUIRED:
            if not isinstance(self.claim, ReassignmentWorkspacePreparationClaim):
                raise ValueError("acquired 必须携带 ReassignmentWorkspacePreparationClaim")
            if self.workspace_slug is not None:
                raise ValueError("acquired 不能同时携带 workspace_slug")
        elif self.outcome is ReassignmentWorkspacePreparationClaimOutcome.MAPPING_EXISTS:
            if self.claim is not None:
                raise ValueError("mapping_exists 不能携带准备权")
            object.__setattr__(
                self,
                "workspace_slug",
                _required_text(self.workspace_slug, name="workspace_slug"),
            )
        elif self.claim is not None or self.workspace_slug is not None:
            raise ValueError("未获得准备权时不能携带 claim 或 workspace_slug")


@dataclass(frozen=True)
class ReassignmentLeaseUpdateResult:
    """续租或过期接管后的确定性结果。"""

    outcome: ReassignmentWriteOutcome
    lease: ReassignmentLease | None = None
    workspace_preparation_claim: ReassignmentWorkspacePreparationClaim | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ReassignmentWriteOutcome):
            raise TypeError("outcome 必须是 ReassignmentWriteOutcome")
        if self.outcome is ReassignmentWriteOutcome.APPLIED:
            if not isinstance(self.lease, ReassignmentLease):
                raise ValueError("applied 必须携带新的 ReassignmentLease")
            claim = self.workspace_preparation_claim
            if claim is not None:
                if not isinstance(claim, ReassignmentWorkspacePreparationClaim):
                    raise TypeError(
                        "workspace_preparation_claim 必须是 "
                        "ReassignmentWorkspacePreparationClaim 或 None"
                    )
                if (
                    claim.operation_id != self.lease.operation_id
                    or claim.owner != self.lease.owner
                    or claim.expires_at != self.lease.expires_at
                ):
                    raise ValueError("续租返回的准备权必须与新 Operation lease 一致")
        elif self.lease is not None or self.workspace_preparation_claim is not None:
            raise ValueError("非 applied 结果不能携带 lease 或准备权")


class ReassignmentLocalCommitState(str, Enum):
    """恢复时对本地 ``documents`` 权威行的三态读取结果。

    ``SOURCE_UNCHANGED`` 与 ``TARGET_COMMITTED`` 只在冻结文档身份仍完全匹配时成立；任何
    行缺失、文档身份变化或第三方分类值都统一为 ``CONFLICT``。这样恢复器不会仅按文件名
    猜测本地 CAS 是否已经提交。
    """

    SOURCE_UNCHANGED = "source_unchanged"
    TARGET_COMMITTED = "target_committed"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class ReassignmentRecoveryObservation:
    """一次恢复探测得到的最小、脱敏跨系统状态快照。

    该 DTO 只保存枚举结论、操作者与原因码，绝不保存 AnythingLLM 响应正文、文档路径或
    Authorization 等敏感内容。实际操作者仅用于 Adapter 计算摘要后写入审计表。
    """

    lease: ReassignmentLease
    local_commit_state: ReassignmentLocalCommitState
    source_binding_state: ReassignmentBindingState
    target_binding_state: ReassignmentBindingState
    remote_membership_required: bool
    actor: str
    reason_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.lease, ReassignmentLease):
            raise TypeError("lease 必须是 ReassignmentLease")
        if not isinstance(self.local_commit_state, ReassignmentLocalCommitState):
            raise TypeError("local_commit_state 必须是 ReassignmentLocalCommitState")
        if not isinstance(self.source_binding_state, ReassignmentBindingState):
            raise TypeError("source_binding_state 必须是 ReassignmentBindingState")
        if not isinstance(self.target_binding_state, ReassignmentBindingState):
            raise TypeError("target_binding_state 必须是 ReassignmentBindingState")
        if not isinstance(self.remote_membership_required, bool):
            raise TypeError("remote_membership_required 必须是 bool")
        object.__setattr__(
            self,
            "actor",
            _required_text(
                self.actor,
                name="actor",
                max_length=REASSIGNMENT_EXTERNAL_REFERENCE_MAX_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "reason_code",
            _required_text(
                self.reason_code,
                name="reason_code",
                max_length=REASSIGNMENT_ERROR_CODE_MAX_LENGTH,
            ),
        )


@dataclass(frozen=True)
class ReassignmentRecoveryObservationRecord:
    """已追加到本地事实表的恢复观测记录。

    ``observation_id`` 是只供内部最终收口使用的不透明数据库键，不能进入 Presenter 或公开
    HTTP 响应。恢复终态必须引用同一 fencing 下最新的一条记录，防止旧探测覆盖新现场。
    """

    observation_id: int
    observation: ReassignmentRecoveryObservation
    observed_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observation_id",
            _positive_int(self.observation_id, name="observation_id"),
        )
        if not isinstance(self.observation, ReassignmentRecoveryObservation):
            raise TypeError("observation 必须是 ReassignmentRecoveryObservation")
        object.__setattr__(
            self,
            "observed_at",
            _required_text(self.observed_at, name="observed_at"),
        )


@dataclass(frozen=True)
class ReassignmentRecoveryFinalizationRequest:
    """以已持久化恢复观测为门禁的专用终态收口请求。

    与通用状态转换不同，本请求只能由恢复服务发起，并要求 Adapter 复核当前 lease、最新观测、
    本地分类行以及（如有）被接管的目标 workspace 准备权。这样旧 fencing 或没有人工审计
    的调用不能释放同文档保护。
    """

    lease: ReassignmentLease
    observation: ReassignmentRecoveryObservationRecord
    next_status: ReassignmentOperationStatus
    current_step: ReassignmentStepName
    terminal_evidence: ReassignmentTerminalEvidence
    preparation_claim: ReassignmentWorkspacePreparationClaim | None = None
    error_code: str | None = None
    error_summary: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.lease, ReassignmentLease):
            raise TypeError("lease 必须是 ReassignmentLease")
        if not isinstance(self.observation, ReassignmentRecoveryObservationRecord):
            raise TypeError("observation 必须是 ReassignmentRecoveryObservationRecord")
        if self.observation.observation.lease != self.lease:
            raise ValueError("恢复终态使用的 observation 必须属于同一 lease")
        if self.next_status not in {
            ReassignmentOperationStatus.SUCCEEDED,
            ReassignmentOperationStatus.FAILED,
            ReassignmentOperationStatus.COMPENSATED,
        }:
            raise ValueError("恢复终态只能进入 succeeded、failed 或 compensated")
        if not isinstance(self.current_step, ReassignmentStepName):
            raise TypeError("current_step 必须是 ReassignmentStepName")
        if not isinstance(self.terminal_evidence, ReassignmentTerminalEvidence):
            raise TypeError("terminal_evidence 必须是 ReassignmentTerminalEvidence")
        if self.preparation_claim is not None and not isinstance(
            self.preparation_claim,
            ReassignmentWorkspacePreparationClaim,
        ):
            raise TypeError(
                "preparation_claim 必须是 ReassignmentWorkspacePreparationClaim 或 None"
            )
        object.__setattr__(
            self,
            "error_code",
            _optional_text(
                self.error_code,
                name="error_code",
                max_length=REASSIGNMENT_ERROR_CODE_MAX_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "error_summary",
            _optional_text(
                self.error_summary,
                name="error_summary",
                max_length=REASSIGNMENT_ERROR_SUMMARY_MAX_LENGTH,
            ),
        )


@dataclass(frozen=True)
class ReassignmentStepCompletion:
    """结束一个已经发起的 Step 所需的受控结果事实。"""

    lease: ReassignmentLease
    step_name: ReassignmentStepName
    next_state: ReassignmentStepState
    external_reference: str | None = None
    error_code: str | None = None
    error_summary: str | None = None
    probe_outcome: ReassignmentMutationOutcome | None = None
    recovery_authorized: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.lease, ReassignmentLease):
            raise TypeError("lease 必须是 ReassignmentLease")
        if not isinstance(self.step_name, ReassignmentStepName):
            raise TypeError("step_name 必须是 ReassignmentStepName")
        if not isinstance(self.next_state, ReassignmentStepState):
            raise TypeError("next_state 必须是 ReassignmentStepState")
        if self.next_state not in {
            ReassignmentStepState.SUCCEEDED,
            ReassignmentStepState.KNOWN_FAILED,
            ReassignmentStepState.OUTCOME_UNKNOWN,
        }:
            raise ValueError("next_state 必须是 Step 的已确认终态")
        object.__setattr__(
            self,
            "external_reference",
            _optional_text(
                self.external_reference,
                name="external_reference",
                max_length=REASSIGNMENT_EXTERNAL_REFERENCE_MAX_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "error_code",
            _optional_text(
                self.error_code,
                name="error_code",
                max_length=REASSIGNMENT_ERROR_CODE_MAX_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "error_summary",
            _optional_text(
                self.error_summary,
                name="error_summary",
                max_length=REASSIGNMENT_ERROR_SUMMARY_MAX_LENGTH,
            ),
        )
        if self.probe_outcome is not None and not isinstance(
            self.probe_outcome,
            ReassignmentMutationOutcome,
        ):
            raise TypeError("probe_outcome 必须是 ReassignmentMutationOutcome 或 None")
        allowed_probe_outcomes = {
            ReassignmentStepState.SUCCEEDED: {
                ReassignmentMutationOutcome.CONFIRMED_EFFECT,
                ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT,
            },
            ReassignmentStepState.KNOWN_FAILED: {
                ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT,
            },
            ReassignmentStepState.OUTCOME_UNKNOWN: {
                ReassignmentMutationOutcome.OUTCOME_UNKNOWN,
            },
        }
        if (
            self.probe_outcome is not None
            and self.probe_outcome not in allowed_probe_outcomes[self.next_state]
        ):
            raise ValueError(
                "probe_outcome 与 next_state 表达了互相矛盾的步骤事实"
            )
        if not isinstance(self.recovery_authorized, bool):
            raise TypeError("recovery_authorized 必须是 bool")


@dataclass(frozen=True)
class ReassignmentOperationTransition:
    """描述带 fencing 的领域状态转换。

    DTO 保留领域终态证据用于完整表达和测试状态机；Repository 的通用转换入口只接受非终态。
    释放文档保护必须改用校验对应持久化事实的专用提交/收敛入口。
    """

    lease: ReassignmentLease
    next_status: ReassignmentOperationStatus
    current_step: ReassignmentStepName | None = None
    terminal_evidence: ReassignmentTerminalEvidence | None = None
    error_code: str | None = None
    error_summary: str | None = None
    recovery_authorized: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.lease, ReassignmentLease):
            raise TypeError("lease 必须是 ReassignmentLease")
        if not isinstance(self.next_status, ReassignmentOperationStatus):
            raise TypeError("next_status 必须是 ReassignmentOperationStatus")
        if self.current_step is not None and not isinstance(
            self.current_step,
            ReassignmentStepName,
        ):
            raise TypeError("current_step 必须是 ReassignmentStepName 或 None")
        if self.terminal_evidence is not None and not isinstance(
            self.terminal_evidence,
            ReassignmentTerminalEvidence,
        ):
            raise TypeError("terminal_evidence 必须是 ReassignmentTerminalEvidence 或 None")
        object.__setattr__(
            self,
            "error_code",
            _optional_text(
                self.error_code,
                name="error_code",
                max_length=REASSIGNMENT_ERROR_CODE_MAX_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "error_summary",
            _optional_text(
                self.error_summary,
                name="error_summary",
                max_length=REASSIGNMENT_ERROR_SUMMARY_MAX_LENGTH,
            ),
        )
        if not isinstance(self.recovery_authorized, bool):
            raise TypeError("recovery_authorized 必须是 bool")


@dataclass(frozen=True)
class ReassignmentLocalCommitRequest:
    """将 documents 行与 commit Step/Operation 终态一次提交的内部请求。"""

    lease: ReassignmentLease
    expected_document: ReassignmentDocumentSnapshot
    target_architecture_raw: object
    terminal_evidence: ReassignmentTerminalEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.lease, ReassignmentLease):
            raise TypeError("lease 必须是 ReassignmentLease")
        if not isinstance(self.expected_document, ReassignmentDocumentSnapshot):
            raise TypeError("expected_document 必须是 ReassignmentDocumentSnapshot")
        raw = ReassignmentRawValue.from_external_value(self.target_architecture_raw)
        if raw.value is None:
            raise ValueError("target_architecture_raw 不能为空")
        object.__setattr__(self, "target_architecture_raw", raw)
        if not isinstance(self.terminal_evidence, ReassignmentTerminalEvidence):
            raise TypeError("terminal_evidence 必须是 ReassignmentTerminalEvidence")


@dataclass(frozen=True)
class ReassignmentNoSideEffectFailureRequest:
    """以已验证“没有待恢复副作用”为前提结束失败 Operation 的内部请求。

    通用状态转换不能释放文档保护。本请求要求 Repository 复核所有前向/补偿步骤及目标
    workspace 归属，只有不存在已确认或未知的本次外部副作用时才允许进入 ``failed``。
    """

    lease: ReassignmentLease
    current_step: ReassignmentStepName | None
    error_code: str
    error_summary: str | None
    terminal_evidence: ReassignmentTerminalEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.lease, ReassignmentLease):
            raise TypeError("lease 必须是 ReassignmentLease")
        if self.current_step is not None and not isinstance(
            self.current_step,
            ReassignmentStepName,
        ):
            raise TypeError("current_step 必须是 ReassignmentStepName 或 None")
        object.__setattr__(
            self,
            "error_code",
            _required_text(
                self.error_code,
                name="error_code",
                max_length=REASSIGNMENT_ERROR_CODE_MAX_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "error_summary",
            _optional_text(
                self.error_summary,
                name="error_summary",
                max_length=REASSIGNMENT_ERROR_SUMMARY_MAX_LENGTH,
            ),
        )
        if not isinstance(self.terminal_evidence, ReassignmentTerminalEvidence):
            raise TypeError("terminal_evidence 必须是 ReassignmentTerminalEvidence")


@dataclass(frozen=True)
class ReassignmentWorkspaceMappingRequest:
    """登记目标 workspace 映射并完成 prepare Step 的内部本地写请求。"""

    lease: ReassignmentLease
    target_architecture_raw: object
    workspace_slug: str
    ownership: ReassignmentWorkspaceOwnership
    preparation_claim: ReassignmentWorkspacePreparationClaim | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.lease, ReassignmentLease):
            raise TypeError("lease 必须是 ReassignmentLease")
        raw = ReassignmentRawValue.from_external_value(self.target_architecture_raw)
        if raw.value is None:
            raise ValueError("target_architecture_raw 不能为空")
        object.__setattr__(self, "target_architecture_raw", raw)
        object.__setattr__(
            self,
            "workspace_slug",
            _required_text(
                self.workspace_slug,
                name="workspace_slug",
                max_length=REASSIGNMENT_EXTERNAL_REFERENCE_MAX_LENGTH,
            ),
        )
        if not isinstance(self.ownership, ReassignmentWorkspaceOwnership):
            raise TypeError("ownership 必须是 ReassignmentWorkspaceOwnership")
        if self.preparation_claim is not None and not isinstance(
            self.preparation_claim,
            ReassignmentWorkspacePreparationClaim,
        ):
            raise TypeError(
                "preparation_claim 必须是 ReassignmentWorkspacePreparationClaim 或 None"
            )


@dataclass(frozen=True)
class ReassignmentWorkspacePreparationFactRequest:
    """mapping 未能提交时，保存已经确认的远端 workspace 现场。

    该事实不会创建或覆盖 ``workspaces`` 映射，也不会把 prepare Step 伪装为已经完成；
    它只让 1E-5 能够按准确 slug 和三态归属执行探测、补偿或人工恢复。
    ``recovery_authorized`` 只允许持有更新 fencing 的恢复器为既有未知 Step 补记事实，
    普通前向请求不能借此覆盖 ``recovery_required`` 现场。
    """

    lease: ReassignmentLease
    workspace_slug: str
    ownership: ReassignmentWorkspaceOwnership
    error_code: str
    recovery_authorized: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.lease, ReassignmentLease):
            raise TypeError("lease 必须是 ReassignmentLease")
        object.__setattr__(
            self,
            "workspace_slug",
            _required_text(
                self.workspace_slug,
                name="workspace_slug",
                max_length=REASSIGNMENT_EXTERNAL_REFERENCE_MAX_LENGTH,
            ),
        )
        if not isinstance(self.ownership, ReassignmentWorkspaceOwnership):
            raise TypeError("ownership 必须是 ReassignmentWorkspaceOwnership")
        object.__setattr__(
            self,
            "error_code",
            _required_text(
                self.error_code,
                name="error_code",
                max_length=REASSIGNMENT_ERROR_CODE_MAX_LENGTH,
            ),
        )
        if not isinstance(self.recovery_authorized, bool):
            raise TypeError("recovery_authorized 必须是 bool")


@dataclass(frozen=True)
class ReassignmentBestEffortPinCompletion:
    """Pin 审计完成事实；Pin 仍然不参与核心 Saga 成败判定。"""

    lease: ReassignmentLease
    mutation_outcome: ReassignmentMutationOutcome
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.lease, ReassignmentLease):
            raise TypeError("lease 必须是 ReassignmentLease")
        if not isinstance(self.mutation_outcome, ReassignmentMutationOutcome):
            raise TypeError("mutation_outcome 必须是 ReassignmentMutationOutcome")
        object.__setattr__(
            self,
            "error_code",
            _optional_text(
                self.error_code,
                name="error_code",
                max_length=REASSIGNMENT_ERROR_CODE_MAX_LENGTH,
            ),
        )


@dataclass(frozen=True)
class ReassignmentExpiredLeaseTakeoverRequest:
    """恢复服务接管过期 Operation 时的 Compare-And-Swap 请求。

    ``actor`` 与 ``reason_code`` 形成可追溯的人工/自动恢复审计。``workspace_claim_token``
    仅用于把同一 Operation 已过期的目标 workspace 准备权换成新的持有凭据；它不是公开
    请求参数，也不会在 HTTP 响应中暴露。
    """

    operation_id: str
    expected_fencing_token: int
    lease_owner: str
    lease_token: str
    lease_expires_at: str
    reason_code: str = "lease_expired"
    actor: str | None = None
    workspace_claim_token: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            _required_text(self.operation_id, name="operation_id"),
        )
        object.__setattr__(
            self,
            "expected_fencing_token",
            _positive_int(
                self.expected_fencing_token,
                name="expected_fencing_token",
            ),
        )
        object.__setattr__(
            self,
            "lease_owner",
            _required_text(self.lease_owner, name="lease_owner"),
        )
        object.__setattr__(
            self,
            "lease_token",
            _required_text(self.lease_token, name="lease_token"),
        )
        object.__setattr__(
            self,
            "lease_expires_at",
            _required_text(self.lease_expires_at, name="lease_expires_at"),
        )
        object.__setattr__(
            self,
            "reason_code",
            _required_text(
                self.reason_code,
                name="reason_code",
                max_length=REASSIGNMENT_ERROR_CODE_MAX_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "actor",
            _optional_text(
                self.actor,
                name="actor",
                max_length=REASSIGNMENT_EXTERNAL_REFERENCE_MAX_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "workspace_claim_token",
            _optional_text(
                self.workspace_claim_token,
                name="workspace_claim_token",
                max_length=REASSIGNMENT_EXTERNAL_REFERENCE_MAX_LENGTH,
            ),
        )


@dataclass(frozen=True)
class ReassignmentRecoveryCursor:
    """有界恢复扫描的稳定游标，按 ``lease_expires_at + operation_id`` 排序。"""

    lease_expires_at: str
    operation_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "lease_expires_at",
            _required_text(self.lease_expires_at, name="lease_expires_at"),
        )
        object.__setattr__(
            self,
            "operation_id",
            _required_text(self.operation_id, name="operation_id"),
        )


@runtime_checkable
class ReassignmentUnitOfWork(Protocol):
    """一次短 SQLite 事务的抽象。

    Application 必须在本地事实写入后立即离开该上下文，再调用 Knowledge Port。任何实现都不应
    在 Unit of Work 内部调用网络；严格 Fake 会把这条规则变成可失败的测试门禁。
    """

    @property
    def active(self) -> bool:
        """当前 Unit of Work 是否仍持有本地事务。"""

    @property
    def read_only(self) -> bool:
        """当前 Unit of Work 是否只允许读取事实。"""

    def __enter__(self) -> "ReassignmentUnitOfWork":
        ...

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> bool | None:
        ...

    def commit(self) -> None:
        """提交当前短事务；提交后该 UoW 不可继续使用。"""

    def rollback(self) -> None:
        """回滚当前短事务；回滚后该 UoW 不可继续使用。"""

    def get_document_snapshot(
        self,
        *,
        file_name: str,
        source_architecture_id: int,
    ) -> ReassignmentDocumentSnapshot | None:
        ...

    def reserve(
        self,
        request: ReassignmentReservationRequest,
    ) -> ReassignmentReservationResult:
        ...

    def get_operation(
        self,
        operation_id: str,
    ) -> ReassignmentOperationRecord | None:
        ...

    def get_step(
        self,
        *,
        operation_id: str,
        step_name: ReassignmentStepName,
    ) -> ReassignmentStepRecord | None:
        ...

    def list_steps(self, operation_id: str) -> tuple[ReassignmentStepRecord, ...]:
        ...

    def list_events(self, operation_id: str) -> tuple[ReassignmentAuditEvent, ...]:
        ...

    def list_recoverable_operations(
        self,
        *,
        limit: int,
        cursor: ReassignmentRecoveryCursor | None = None,
    ) -> tuple[ReassignmentOperationRecord, ...]:
        """按过期 lease 有界扫描仍持有文档保护的 Operation。"""

    def probe_local_commit_state(
        self,
        operation_id: str,
    ) -> ReassignmentLocalCommitState:
        """只读比对冻结文档身份与当前权威分类，供恢复服务避免猜测 CAS 结果。"""

    def get_workspace_slug(self, architecture_raw: ReassignmentRawValue) -> str | None:
        ...

    def acquire_workspace_preparation_claim(
        self,
        request: ReassignmentWorkspacePreparationClaimRequest,
    ) -> ReassignmentWorkspacePreparationClaimResult | ReassignmentWriteOutcome:
        """原子申请目标 workspace 的准备权，支持过期 fencing 接管。"""

    def release_workspace_preparation_claim(
        self,
        claim: ReassignmentWorkspacePreparationClaim,
    ) -> ReassignmentWriteOutcome:
        """释放或标记已持久化的目标准备权，陈旧 fencing 不得影响新 owner。"""

    def renew_lease(
        self,
        *,
        lease: ReassignmentLease,
        lease_expires_at: str,
    ) -> ReassignmentLeaseUpdateResult:
        ...

    def take_over_expired_lease(
        self,
        request: ReassignmentExpiredLeaseTakeoverRequest,
    ) -> ReassignmentLeaseUpdateResult:
        ...

    def record_recovery_observation(
        self,
        observation: ReassignmentRecoveryObservation,
    ) -> ReassignmentRecoveryObservationRecord | ReassignmentWriteOutcome:
        """追加恢复探测事实及脱敏人工审计；不在此入口改变终态。"""

    def finalize_recovery_operation(
        self,
        request: ReassignmentRecoveryFinalizationRequest,
    ) -> ReassignmentOperationRecord | ReassignmentWriteOutcome:
        """以最新恢复观测收口终态，并原子释放已接管的准备权。"""

    def begin_step_mutation(
        self,
        *,
        lease: ReassignmentLease,
        step_name: ReassignmentStepName,
        recovery_authorized: bool = False,
    ) -> ReassignmentStepRecord | ReassignmentWriteOutcome:
        ...

    def complete_step(
        self,
        completion: ReassignmentStepCompletion,
    ) -> ReassignmentStepRecord | ReassignmentWriteOutcome:
        ...

    def transition_operation(
        self,
        transition: ReassignmentOperationTransition,
    ) -> ReassignmentOperationRecord | ReassignmentWriteOutcome:
        """执行非终态转换；不得通过此入口释放文档保护。"""

    def record_workspace_mapping(
        self,
        request: ReassignmentWorkspaceMappingRequest,
    ) -> ReassignmentOperationRecord | ReassignmentWriteOutcome:
        ...

    def record_workspace_preparation_fact(
        self,
        request: ReassignmentWorkspacePreparationFactRequest,
    ) -> ReassignmentOperationRecord | ReassignmentWriteOutcome:
        """保存 mapping 冲突前已确认的远端资源身份，不释放准备权。"""

        ...

    def begin_best_effort_pin(
        self,
        *,
        lease: ReassignmentLease,
    ) -> ReassignmentWriteOutcome:
        """在 Pin 外部写之前追加审计意图；失败时 Application 必须跳过 Pin。"""

        ...

    def complete_best_effort_pin(
        self,
        completion: ReassignmentBestEffortPinCompletion,
    ) -> ReassignmentWriteOutcome:
        """追加 Pin 的有界结果事实，不改变关键 Step 或 Operation 终态。"""

        ...

    def finalize_no_side_effect_failure(
        self,
        request: ReassignmentNoSideEffectFailureRequest,
    ) -> ReassignmentOperationRecord | ReassignmentWriteOutcome:
        """核验无待恢复副作用后，以失败终态释放同文档保护。"""

    def commit_local_architecture(
        self,
        request: ReassignmentLocalCommitRequest,
    ) -> ReassignmentOperationRecord | ReassignmentWriteOutcome:
        ...


@runtime_checkable
class ReassignmentRepositoryPort(Protocol):
    """创建短生命周期 UoW 的供应商无关 Repository 工厂。"""

    def unit_of_work(
        self,
        *,
        read_only: bool = False,
    ) -> ReassignmentUnitOfWork:
        ...


__all__ = [
    "ReassignmentAuditEvent",
    "ReassignmentBestEffortPinCompletion",
    "ReassignmentEventType",
    "ReassignmentExpiredLeaseTakeoverRequest",
    "ReassignmentLease",
    "ReassignmentLeaseUpdateResult",
    "ReassignmentLocalCommitState",
    "ReassignmentLocalCommitRequest",
    "ReassignmentNoSideEffectFailureRequest",
    "ReassignmentOperationRecord",
    "ReassignmentOperationTransition",
    "ReassignmentRecoveryCursor",
    "ReassignmentRecoveryFinalizationRequest",
    "ReassignmentRecoveryObservation",
    "ReassignmentRecoveryObservationRecord",
    "ReassignmentRepositoryPort",
    "ReassignmentReservationOutcome",
    "ReassignmentReservationRequest",
    "ReassignmentReservationResult",
    "ReassignmentStepCompletion",
    "ReassignmentStepRecord",
    "ReassignmentUnitOfWork",
    "ReassignmentWorkspacePreparationClaim",
    "ReassignmentWorkspacePreparationClaimOutcome",
    "ReassignmentWorkspacePreparationClaimRequest",
    "ReassignmentWorkspacePreparationClaimResult",
    "ReassignmentWorkspaceMappingRequest",
    "ReassignmentWorkspacePreparationFactRequest",
    "ReassignmentWriteOutcome",
]
