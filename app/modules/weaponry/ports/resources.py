"""武器谱 execution 外部资源的 ownership、CAS 与恢复端口。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.weaponry.domain import normalize_architecture_id_value

from .common import (
    IdempotentOperationResult,
    non_negative_int,
    optional_text,
    required_text,
)


def _validate_business_ref(value: object) -> TaskBusinessRef:
    if not isinstance(value, TaskBusinessRef):
        raise TypeError("business_ref 必须是 TaskBusinessRef")
    if value.business_type != "weaponry":
        raise ValueError("武器谱资源 business_type 必须是 weaponry")
    architecture_id = normalize_architecture_id_value(value.business_key)
    if str(architecture_id) != value.business_key:
        raise ValueError("武器谱资源 business_key 必须是规范十进制字符串")
    return value


class WeaponryResourceKind(str, Enum):
    """允许登记的供应商无关资源类别。"""

    RETRIEVAL_SCOPE = "retrieval_scope"
    EXTRACTION_CONTEXT = "extraction_context"
    SOURCE_CONVERSATION = "source_conversation"
    TEMPORARY_DOCUMENT = "temporary_document"
    DOCUMENT_BINDING = "document_binding"
    EMBEDDING = "embedding"
    EVIDENCE_CONTEXT = "evidence_context"
    SOURCE_MAPPING = "source_mapping"


class WeaponryResourceOwnership(str, Enum):
    """资源是否允许由当前 execution 清理。"""

    OWNED = "owned"
    SHARED = "shared"


class WeaponryTrackedResourceState(str, Enum):
    ACTIVE = "active"
    CLEANUP_PENDING = "cleanup_pending"
    CLEANUP_UNKNOWN = "cleanup_unknown"
    CLEANED = "cleaned"


class WeaponryResourceRecordState(str, Enum):
    TRACKING = "tracking"
    CLEANUP_PENDING = "cleanup_pending"
    CLEANED = "cleaned"
    QUARANTINED = "quarantined"


class WeaponryResourceCleanupOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class WeaponryCleanupLeaseAcquireOutcome(str, Enum):
    """有界资源恢复取得执行权的结果。"""

    ACQUIRED = "acquired"
    BUSY = "busy"
    NOT_READY = "not_ready"


_CLEANUP_ORDER = {
    WeaponryResourceKind.SOURCE_CONVERSATION: 10,
    WeaponryResourceKind.EVIDENCE_CONTEXT: 20,
    WeaponryResourceKind.EXTRACTION_CONTEXT: 30,
    WeaponryResourceKind.RETRIEVAL_SCOPE: 40,
    WeaponryResourceKind.DOCUMENT_BINDING: 50,
    WeaponryResourceKind.EMBEDDING: 60,
    WeaponryResourceKind.TEMPORARY_DOCUMENT: 70,
    WeaponryResourceKind.SOURCE_MAPPING: 80,
}


@dataclass(frozen=True)
class WeaponryCleanupLease:
    """资源清理执行权的不透明租约与单调 fencing token。"""

    task_id: TaskId
    token: str
    fencing_token: int
    deadline_at: str

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        object.__setattr__(self, "token", required_text(self.token, name="token"))
        if (
            isinstance(self.fencing_token, bool)
            or not isinstance(self.fencing_token, int)
            or self.fencing_token < 1
        ):
            raise ValueError("fencing_token 必须是正整数")
        object.__setattr__(
            self,
            "deadline_at",
            required_text(self.deadline_at, name="deadline_at"),
        )


@dataclass(frozen=True)
class WeaponryCleanupLeaseAcquireResult:
    """只有 acquired 结果可以携带清理租约。"""

    outcome: WeaponryCleanupLeaseAcquireOutcome
    lease: WeaponryCleanupLease | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, WeaponryCleanupLeaseAcquireOutcome):
            raise TypeError("outcome 必须是 WeaponryCleanupLeaseAcquireOutcome")
        if self.outcome is WeaponryCleanupLeaseAcquireOutcome.ACQUIRED:
            if not isinstance(self.lease, WeaponryCleanupLease):
                raise TypeError("acquired 结果必须携带 WeaponryCleanupLease")
        elif self.lease is not None:
            raise ValueError("未取得清理权时不得携带 lease")


@dataclass(frozen=True)
class WeaponryTrackedResource:
    """一项创建后必须立即登记的外部资源或来源映射。"""

    resource_id: str
    kind: WeaponryResourceKind
    external_ref: str
    ownership: WeaponryResourceOwnership
    idempotency_key: str
    document_key: str = ""
    call_id: str = ""
    state: WeaponryTrackedResourceState = WeaponryTrackedResourceState.ACTIVE

    def __post_init__(self) -> None:
        for name in ("resource_id", "external_ref", "idempotency_key"):
            object.__setattr__(
                self,
                name,
                required_text(getattr(self, name), name=name),
            )
        if not isinstance(self.kind, WeaponryResourceKind):
            raise TypeError("kind 必须是 WeaponryResourceKind")
        if not isinstance(self.ownership, WeaponryResourceOwnership):
            raise TypeError("ownership 必须是 WeaponryResourceOwnership")
        for name in ("document_key", "call_id"):
            object.__setattr__(
                self,
                name,
                optional_text(getattr(self, name), name=name),
            )
        if not isinstance(self.state, WeaponryTrackedResourceState):
            raise TypeError("state 必须是 WeaponryTrackedResourceState")
        if (
            self.ownership is WeaponryResourceOwnership.SHARED
            and self.state is not WeaponryTrackedResourceState.ACTIVE
        ):
            raise ValueError("shared 资源不得进入任务清理状态")
        if self.kind is WeaponryResourceKind.SOURCE_MAPPING and not self.document_key:
            raise ValueError("source_mapping 必须绑定 document_key")
        if self.kind is WeaponryResourceKind.SOURCE_CONVERSATION and not self.call_id:
            raise ValueError("source_conversation 必须绑定来源级 call_id")


@dataclass(frozen=True)
class CleanupWeaponryExternalResource:
    """请求 Adapter 幂等清理一个已经持久登记的外部资源。

    Application 只传递不可变资源事实；Adapter 不得自行修改 Resource Store，也不得在
    未取得上层 cleanup lease 时被直接调用。这样网络 I/O 与数据库事务始终分离。
    """

    task_id: TaskId
    resource: WeaponryTrackedResource

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.resource, WeaponryTrackedResource):
            raise TypeError("resource 必须是 WeaponryTrackedResource")
        if self.resource.ownership is not WeaponryResourceOwnership.OWNED:
            raise ValueError("只有 owned 资源允许进入外部清理")


@dataclass(frozen=True)
class WeaponryExternalResourceCleanupResult:
    """一次外部删除的保守结果分类。

    ``FAILED`` 表示能够证明本次没有完成删除，可在持久冷却后重试；
    ``OUTCOME_UNKNOWN`` 表示远端可能已删除但本地没有可靠收到结果，必须隔离对账，
    禁止自动重发。
    """

    outcome: WeaponryResourceCleanupOutcome
    error_code: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, WeaponryResourceCleanupOutcome):
            raise TypeError("outcome 必须是 WeaponryResourceCleanupOutcome")
        error_code = optional_text(self.error_code, name="error_code")
        detail = optional_text(self.detail, name="detail")
        if self.outcome is WeaponryResourceCleanupOutcome.SUCCEEDED and error_code:
            raise ValueError("成功清理不得携带 error_code")
        if self.outcome is not WeaponryResourceCleanupOutcome.SUCCEEDED and not error_code:
            raise ValueError("失败或结果未知清理必须携带 error_code")
        object.__setattr__(self, "error_code", error_code)
        object.__setattr__(self, "detail", detail)


@dataclass(frozen=True)
class WeaponryResourceRecord:
    """一份按 task_id 做 CAS 的任务资源事实快照。"""

    task_id: TaskId
    business_ref: TaskBusinessRef
    resources: tuple[WeaponryTrackedResource, ...] = ()
    state: WeaponryResourceRecordState = WeaponryResourceRecordState.TRACKING
    retry_count: int = 0
    next_retry_at: str = ""
    last_error_code: str = ""
    last_error_message: str = ""
    cleanup_lease: WeaponryCleanupLease | None = None
    cleanup_fencing_token: int = 0
    version: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        _validate_business_ref(self.business_ref)
        if not isinstance(self.resources, (tuple, list)) or any(
            not isinstance(item, WeaponryTrackedResource) for item in self.resources
        ):
            raise TypeError("resources 只能包含 WeaponryTrackedResource")
        resources = tuple(self.resources)
        resource_ids = tuple(item.resource_id for item in resources)
        idempotency_keys = tuple(item.idempotency_key for item in resources)
        if len(set(resource_ids)) != len(resource_ids):
            raise ValueError("resources 不能包含重复 resource_id")
        if len(set(idempotency_keys)) != len(idempotency_keys):
            raise ValueError("resources 不能包含重复 idempotency_key")
        object.__setattr__(self, "resources", resources)
        if not isinstance(self.state, WeaponryResourceRecordState):
            raise TypeError("state 必须是 WeaponryResourceRecordState")
        non_negative_int(self.retry_count, name="retry_count")
        non_negative_int(
            self.cleanup_fencing_token,
            name="cleanup_fencing_token",
        )
        non_negative_int(self.version, name="version")
        for name in ("next_retry_at", "last_error_code", "last_error_message"):
            object.__setattr__(
                self,
                name,
                optional_text(getattr(self, name), name=name),
            )
        if self.cleanup_lease is not None:
            if not isinstance(self.cleanup_lease, WeaponryCleanupLease):
                raise TypeError("cleanup_lease 必须是 WeaponryCleanupLease 或 None")
            if self.cleanup_lease.task_id != self.task_id:
                raise ValueError("cleanup_lease 不属于当前 task_id")
            if self.cleanup_lease.fencing_token != self.cleanup_fencing_token:
                raise ValueError("cleanup_lease fencing_token 与资源记录不一致")
            if self.state is not WeaponryResourceRecordState.CLEANUP_PENDING:
                raise ValueError("只有 cleanup_pending 记录可以持有清理租约")
        if self.state is WeaponryResourceRecordState.CLEANED:
            not_cleaned = tuple(
                item
                for item in resources
                if item.ownership is WeaponryResourceOwnership.OWNED
                and item.state is not WeaponryTrackedResourceState.CLEANED
            )
            if not_cleaned:
                raise ValueError("cleaned 记录仍包含未清理 owned 资源")
            if self.cleanup_lease is not None:
                raise ValueError("cleaned 记录不得持有清理租约")
        if self.state is WeaponryResourceRecordState.TRACKING and any(
            item.ownership is WeaponryResourceOwnership.OWNED
            and item.state is not WeaponryTrackedResourceState.ACTIVE
            for item in resources
        ):
            raise ValueError("tracking 记录的 owned 资源必须处于 active")
        if self.state is WeaponryResourceRecordState.CLEANUP_PENDING and not any(
            item.ownership is WeaponryResourceOwnership.OWNED
            and item.state
            in {
                WeaponryTrackedResourceState.CLEANUP_PENDING,
                WeaponryTrackedResourceState.CLEANUP_UNKNOWN,
            }
            for item in resources
        ):
            raise ValueError("cleanup_pending 记录必须包含待处理 owned 资源")
        if self.state is WeaponryResourceRecordState.QUARANTINED and (
            not self.last_error_code or not self.last_error_message
        ):
            raise ValueError("quarantined 记录必须包含错误码和原因")

    @property
    def owned_cleanup_candidates(self) -> tuple[WeaponryTrackedResource, ...]:
        """按冻结清理顺序返回仍需处理的 owned 资源。"""

        candidates = tuple(
            item
            for item in self.resources
            if item.ownership is WeaponryResourceOwnership.OWNED
            and item.state is not WeaponryTrackedResourceState.CLEANED
        )
        return tuple(
            sorted(
                candidates,
                key=lambda item: (_CLEANUP_ORDER[item.kind], item.resource_id),
            )
        )


@dataclass(frozen=True)
class RegisterWeaponryResource:
    task_id: TaskId
    resource: WeaponryTrackedResource
    expected_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.resource, WeaponryTrackedResource):
            raise TypeError("resource 必须是 WeaponryTrackedResource")
        non_negative_int(self.expected_version, name="expected_version")


@dataclass(frozen=True)
class PrepareWeaponryResourceCleanup:
    task_id: TaskId
    expected_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        non_negative_int(self.expected_version, name="expected_version")


@dataclass(frozen=True)
class AcquireWeaponryCleanupLease:
    task_id: TaskId
    expected_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        non_negative_int(self.expected_version, name="expected_version")


@dataclass(frozen=True)
class ReleaseWeaponryCleanupLease:
    lease: WeaponryCleanupLease
    expected_version: int

    def __post_init__(self) -> None:
        if not isinstance(self.lease, WeaponryCleanupLease):
            raise TypeError("lease 必须是 WeaponryCleanupLease")
        non_negative_int(self.expected_version, name="expected_version")


@dataclass(frozen=True)
class CompleteWeaponryResourceCleanup:
    task_id: TaskId
    lease: WeaponryCleanupLease
    resource_id: str
    outcome: WeaponryResourceCleanupOutcome
    expected_version: int
    error_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.lease, WeaponryCleanupLease):
            raise TypeError("lease 必须是 WeaponryCleanupLease")
        if self.lease.task_id != self.task_id:
            raise ValueError("lease 不属于当前 task_id")
        object.__setattr__(
            self,
            "resource_id",
            required_text(self.resource_id, name="resource_id"),
        )
        if not isinstance(self.outcome, WeaponryResourceCleanupOutcome):
            raise TypeError("outcome 必须是 WeaponryResourceCleanupOutcome")
        non_negative_int(self.expected_version, name="expected_version")
        error_code = optional_text(self.error_code, name="error_code")
        if self.outcome is WeaponryResourceCleanupOutcome.SUCCEEDED and error_code:
            raise ValueError("成功清理不得携带 error_code")
        if self.outcome is not WeaponryResourceCleanupOutcome.SUCCEEDED and not error_code:
            raise ValueError("失败或结果未知清理必须携带 error_code")
        object.__setattr__(self, "error_code", error_code)


@dataclass(frozen=True)
class QuarantineWeaponryResources:
    task_id: TaskId
    expected_version: int
    error_code: str
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        non_negative_int(self.expected_version, name="expected_version")
        object.__setattr__(
            self,
            "error_code",
            required_text(self.error_code, name="error_code"),
        )
        object.__setattr__(self, "reason", required_text(self.reason, name="reason"))


@runtime_checkable
class WeaponryResourceStorePort(Protocol):
    """持久化资源事实；具体实现不得在数据库事务中执行外部清理。"""

    def create(self, record: WeaponryResourceRecord) -> WeaponryResourceRecord:
        ...

    def get(self, task_id: TaskId) -> WeaponryResourceRecord | None:
        ...

    def register(
        self,
        command: RegisterWeaponryResource,
    ) -> WeaponryResourceRecord:
        ...

    def prepare_cleanup(
        self,
        command: PrepareWeaponryResourceCleanup,
    ) -> WeaponryResourceRecord:
        ...

    def acquire_cleanup(
        self,
        command: AcquireWeaponryCleanupLease,
    ) -> WeaponryCleanupLeaseAcquireResult:
        ...

    def complete_cleanup(
        self,
        command: CompleteWeaponryResourceCleanup,
    ) -> WeaponryResourceRecord:
        ...

    def release_cleanup(
        self,
        command: ReleaseWeaponryCleanupLease,
    ) -> IdempotentOperationResult:
        ...

    def quarantine(
        self,
        command: QuarantineWeaponryResources,
    ) -> WeaponryResourceRecord:
        ...

    def list_recoverable(self, *, limit: int) -> tuple[TaskId, ...]:
        ...


@runtime_checkable
class WeaponryExternalResourceCleanupPort(Protocol):
    """事务外、供应商相关的单项幂等删除边界。"""

    def cleanup(
        self,
        command: CleanupWeaponryExternalResource,
    ) -> WeaponryExternalResourceCleanupResult:
        ...


__all__ = [
    "AcquireWeaponryCleanupLease",
    "CleanupWeaponryExternalResource",
    "CompleteWeaponryResourceCleanup",
    "PrepareWeaponryResourceCleanup",
    "QuarantineWeaponryResources",
    "RegisterWeaponryResource",
    "ReleaseWeaponryCleanupLease",
    "WeaponryCleanupLease",
    "WeaponryCleanupLeaseAcquireOutcome",
    "WeaponryCleanupLeaseAcquireResult",
    "WeaponryExternalResourceCleanupPort",
    "WeaponryExternalResourceCleanupResult",
    "WeaponryResourceCleanupOutcome",
    "WeaponryResourceKind",
    "WeaponryResourceOwnership",
    "WeaponryResourceRecord",
    "WeaponryResourceRecordState",
    "WeaponryResourceStorePort",
    "WeaponryTrackedResource",
    "WeaponryTrackedResourceState",
]
