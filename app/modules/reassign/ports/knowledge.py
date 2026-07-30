"""分类节点变更所需的供应商无关知识库能力端口。

这里的引用全部是不透明业务值：没有 AnythingLLM DTO、URL、Header、HTTP Response 或网络
Client。真实 Adapter 在后续阶段负责把端口请求映射为供应商调用，并把 ``false``、超时和
协议异常归类为明确结果，不能以 ``None`` 或空字典伪装成功。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from app.modules.reassign.domain import (
    REASSIGNMENT_ERROR_CODE_MAX_LENGTH,
    REASSIGNMENT_ERROR_SUMMARY_MAX_LENGTH,
    REASSIGNMENT_EXTERNAL_REFERENCE_MAX_LENGTH,
    ReassignmentDocumentSnapshot,
    ReassignmentRawValue,
    ReassignmentStepName,
)


def _required_text(value: object, *, name: str, max_length: int | None = None) -> str:
    """校验端口内部必填文本，不执行隐式 ``str(...)`` 转换。"""

    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    if max_length is not None and len(normalized) > max_length:
        raise ValueError(f"{name} 长度不能超过 {max_length}")
    return normalized


def _optional_text(value: object, *, name: str, max_length: int) -> str | None:
    """校验可空的脱敏诊断或不透明引用。"""

    if value is None:
        return None
    return _required_text(value, name=name, max_length=max_length)


class ReassignmentKnowledgeOutcome(str, Enum):
    """外部动作的四分类结果，禁止用未抛异常代替确定成功。"""

    APPLIED = "applied"
    ALREADY_IN_DESIRED_STATE = "already_in_desired_state"
    KNOWN_FAILURE = "known_failure"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ReassignmentMembershipState(str, Enum):
    """精确成员关系探测的确定状态。"""

    PRESENT = "present"
    ABSENT = "absent"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ReassignmentWorkspaceProbeState(str, Enum):
    """目标 workspace 查回结果，和可能产生写入的 prepare 调用严格分离。"""

    PRESENT = "present"
    ABSENT = "absent"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ReassignmentWorkspaceOwnership(str, Enum):
    """目标 workspace 的创建归属事实。

    ``UNKNOWN`` 表示已经确认唯一 workspace 引用，但供应商协议无法证明它由当前
    Operation 创建还是由并发请求/历史流程创建。该状态允许继续使用 workspace，
    但绝不能作为自动删除整个 workspace 的所有权证据。
    """

    CREATED_BY_OPERATION = "created_by_operation"
    PREEXISTING = "preexisting"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ReassignmentWorkspaceReference:
    """供应商无关、只包含不透明 slug 的 workspace 引用。"""

    slug: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "slug",
            _required_text(
                self.slug,
                name="slug",
                max_length=REASSIGNMENT_EXTERNAL_REFERENCE_MAX_LENGTH,
            ),
        )


@dataclass(frozen=True)
class ReassignmentDocumentReference:
    """远端成员关系操作所需的固定文档身份，不使用展示名模糊匹配。"""

    document_row_id: int
    file_name: str
    doc_path: str
    anything_doc_id: str | None = None
    original_file_name: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.document_row_id, bool)
            or not isinstance(self.document_row_id, int)
            or self.document_row_id < 1
        ):
            raise ValueError("document_row_id 必须是正整数")
        object.__setattr__(
            self,
            "file_name",
            _required_text(self.file_name, name="file_name"),
        )
        # doc_path 是供应商定位主键；这里拒绝空值，空 doc_path 的兼容分支必须在
        # Application 进入 Knowledge Port 之前停止。
        object.__setattr__(
            self,
            "doc_path",
            _required_text(
                self.doc_path,
                name="doc_path",
                max_length=REASSIGNMENT_EXTERNAL_REFERENCE_MAX_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "anything_doc_id",
            _optional_text(
                self.anything_doc_id,
                name="anything_doc_id",
                max_length=REASSIGNMENT_EXTERNAL_REFERENCE_MAX_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "original_file_name",
            _optional_text(
                self.original_file_name,
                name="original_file_name",
                max_length=REASSIGNMENT_EXTERNAL_REFERENCE_MAX_LENGTH,
            ),
        )

    @classmethod
    def from_snapshot(
        cls,
        snapshot: ReassignmentDocumentSnapshot,
    ) -> "ReassignmentDocumentReference":
        """从不可变本地快照构造远端引用，拒绝空路径兼容分支误入网络层。"""

        if not isinstance(snapshot, ReassignmentDocumentSnapshot):
            raise TypeError("snapshot 必须是 ReassignmentDocumentSnapshot")
        return cls(
            document_row_id=snapshot.document_row_id,
            file_name=snapshot.file_name,
            doc_path=snapshot.doc_path or "",
            anything_doc_id=snapshot.anything_doc_id,
            original_file_name=snapshot.original_file_name,
        )


@dataclass(frozen=True)
class ReassignmentWorkspacePreparationRequest:
    """准备目标 workspace 的幂等请求；名称由 Application 保留既有兼容语义后提供。"""

    operation_id: str
    target_architecture_raw: object
    desired_workspace_name: str
    idempotency_key: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            _required_text(self.operation_id, name="operation_id"),
        )
        raw = ReassignmentRawValue.from_external_value(self.target_architecture_raw)
        if raw.value is None:
            raise ValueError("target_architecture_raw 不能为空")
        object.__setattr__(self, "target_architecture_raw", raw)
        object.__setattr__(
            self,
            "desired_workspace_name",
            _required_text(
                self.desired_workspace_name,
                name="desired_workspace_name",
                max_length=REASSIGNMENT_EXTERNAL_REFERENCE_MAX_LENGTH,
            ),
        )
        object.__setattr__(
            self,
            "idempotency_key",
            _required_text(
                self.idempotency_key,
                name="idempotency_key",
                max_length=REASSIGNMENT_EXTERNAL_REFERENCE_MAX_LENGTH,
            ),
        )


@dataclass(frozen=True)
class ReassignmentWorkspaceReferenceProbeRequest:
    """按已经持久化的不透明 slug 精确查回 workspace。

    该请求与按确定性名称执行创建/查回的 ``ReassignmentWorkspacePreparationRequest``
    明确分离。既有本地 mapping 是权威资源引用，即使远端展示名称后来被修改，也必须按
    slug 查回，不能重新套用当前版本的命名规则。
    """

    operation_id: str
    workspace: ReassignmentWorkspaceReference

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            _required_text(self.operation_id, name="operation_id"),
        )
        if not isinstance(self.workspace, ReassignmentWorkspaceReference):
            raise TypeError("workspace 必须是 ReassignmentWorkspaceReference")


@dataclass(frozen=True)
class ReassignmentMembershipProbeRequest:
    """读取某 workspace 内一份精确文档成员关系的只读请求。"""

    operation_id: str
    workspace: ReassignmentWorkspaceReference
    document: ReassignmentDocumentReference

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            _required_text(self.operation_id, name="operation_id"),
        )
        if not isinstance(self.workspace, ReassignmentWorkspaceReference):
            raise TypeError("workspace 必须是 ReassignmentWorkspaceReference")
        if not isinstance(self.document, ReassignmentDocumentReference):
            raise TypeError("document 必须是 ReassignmentDocumentReference")


@dataclass(frozen=True)
class ReassignmentDocumentMutationRequest:
    """加入、删除或 Pin 的外部写请求，必须带有固定步骤和本地幂等键。"""

    operation_id: str
    step_name: ReassignmentStepName
    workspace: ReassignmentWorkspaceReference
    document: ReassignmentDocumentReference
    architecture_raw: object
    idempotency_key: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            _required_text(self.operation_id, name="operation_id"),
        )
        if not isinstance(self.step_name, ReassignmentStepName):
            raise TypeError("step_name 必须是 ReassignmentStepName")
        if not isinstance(self.workspace, ReassignmentWorkspaceReference):
            raise TypeError("workspace 必须是 ReassignmentWorkspaceReference")
        if not isinstance(self.document, ReassignmentDocumentReference):
            raise TypeError("document 必须是 ReassignmentDocumentReference")
        raw = ReassignmentRawValue.from_external_value(self.architecture_raw)
        if raw.value is None:
            raise ValueError("architecture_raw 不能为空")
        object.__setattr__(self, "architecture_raw", raw)
        object.__setattr__(
            self,
            "idempotency_key",
            _required_text(
                self.idempotency_key,
                name="idempotency_key",
                max_length=REASSIGNMENT_EXTERNAL_REFERENCE_MAX_LENGTH,
            ),
        )


@dataclass(frozen=True)
class ReassignmentWorkspacePreparationResult:
    """workspace 查询/创建后的明确分类结果。"""

    outcome: ReassignmentKnowledgeOutcome
    workspace: ReassignmentWorkspaceReference | None = None
    ownership: ReassignmentWorkspaceOwnership | None = None
    error_code: str | None = None
    error_summary: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ReassignmentKnowledgeOutcome):
            raise TypeError("outcome 必须是 ReassignmentKnowledgeOutcome")
        succeeded = self.outcome in {
            ReassignmentKnowledgeOutcome.APPLIED,
            ReassignmentKnowledgeOutcome.ALREADY_IN_DESIRED_STATE,
        }
        if succeeded:
            if not isinstance(self.workspace, ReassignmentWorkspaceReference):
                raise ValueError("成功准备 workspace 时必须返回有效 workspace 引用")
            if not isinstance(self.ownership, ReassignmentWorkspaceOwnership):
                raise ValueError("成功准备 workspace 时必须返回明确的三态创建归属")
        elif self.workspace is not None or self.ownership is not None:
            raise ValueError("失败或未知 workspace 结果不能携带可提交的 workspace 引用")
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
class ReassignmentWorkspaceProbeResult:
    """对目标 workspace 的纯只读查回结果。

    查回只能证明确定性名称对应的唯一引用存在，不能证明创建者，因此生产 Adapter
    通常返回 ``ownership=UNKNOWN``。该事实可以持久化并继续成员关系确认，但不能
    把 workspace 当作当前 Operation 可自动删除的临时资源。
    """

    state: ReassignmentWorkspaceProbeState
    workspace: ReassignmentWorkspaceReference | None = None
    ownership: ReassignmentWorkspaceOwnership | None = None
    error_code: str | None = None
    error_summary: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, ReassignmentWorkspaceProbeState):
            raise TypeError("state 必须是 ReassignmentWorkspaceProbeState")
        if self.state is ReassignmentWorkspaceProbeState.PRESENT:
            if not isinstance(self.workspace, ReassignmentWorkspaceReference):
                raise ValueError("查回到 workspace 时必须返回有效引用")
            if not isinstance(self.ownership, ReassignmentWorkspaceOwnership):
                raise ValueError("查回到 workspace 时必须返回三态创建归属")
        elif self.workspace is not None or self.ownership is not None:
            raise ValueError("未查回 workspace 时不能携带引用或创建归属")
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
class ReassignmentMembershipProbeResult:
    """成员探测结果；未知状态必须由 Application 进入恢复而非盲重发。"""

    state: ReassignmentMembershipState
    error_code: str | None = None
    error_summary: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.state, ReassignmentMembershipState):
            raise TypeError("state 必须是 ReassignmentMembershipState")
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
class ReassignmentDocumentMutationResult:
    """删除、加入或 Pin 的四分类结果与可审计摘要。"""

    outcome: ReassignmentKnowledgeOutcome
    external_reference: str | None = None
    error_code: str | None = None
    error_summary: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, ReassignmentKnowledgeOutcome):
            raise TypeError("outcome 必须是 ReassignmentKnowledgeOutcome")
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


@runtime_checkable
class ReassignmentKnowledgePort(Protocol):
    """未来 AnythingLLM Adapter 必须实现的最小、供应商无关知识库能力。"""

    def prepare_target_workspace(
        self,
        request: ReassignmentWorkspacePreparationRequest,
    ) -> ReassignmentWorkspacePreparationResult:
        ...

    def probe_target_workspace(
        self,
        request: ReassignmentWorkspacePreparationRequest,
    ) -> ReassignmentWorkspaceProbeResult:
        ...

    def probe_workspace_reference(
        self,
        request: ReassignmentWorkspaceReferenceProbeRequest,
    ) -> ReassignmentWorkspaceProbeResult:
        """按既有 mapping 的 slug 只读查回唯一 workspace，不应用名称生成规则。"""

        ...

    def probe_document_membership(
        self,
        request: ReassignmentMembershipProbeRequest,
    ) -> ReassignmentMembershipProbeResult:
        ...

    def detach_document(
        self,
        request: ReassignmentDocumentMutationRequest,
    ) -> ReassignmentDocumentMutationResult:
        ...

    def attach_document(
        self,
        request: ReassignmentDocumentMutationRequest,
    ) -> ReassignmentDocumentMutationResult:
        ...

    def pin_document_best_effort(
        self,
        request: ReassignmentDocumentMutationRequest,
    ) -> ReassignmentDocumentMutationResult:
        ...


@runtime_checkable
class ReassignmentKnowledgePortFactory(Protocol):
    """为每次同步 Operation 创建独立 Knowledge Port 的供应商无关工厂。"""

    def create(self, *, elapsed_seconds: float = 0.0) -> ReassignmentKnowledgePort:
        """创建请求级端口，并扣除 Application 已经消耗的同步编排时间。

        ``elapsed_seconds`` 是内部预算上下文，不属于公开接口参数。Factory 必须为每次调用创建
        独立 deadline、Transport 和其他可变状态，不能把剩余预算跨 Operation 共享。
        """


__all__ = [
    "ReassignmentDocumentMutationRequest",
    "ReassignmentDocumentMutationResult",
    "ReassignmentDocumentReference",
    "ReassignmentKnowledgeOutcome",
    "ReassignmentKnowledgePort",
    "ReassignmentKnowledgePortFactory",
    "ReassignmentMembershipProbeRequest",
    "ReassignmentMembershipProbeResult",
    "ReassignmentMembershipState",
    "ReassignmentWorkspacePreparationRequest",
    "ReassignmentWorkspacePreparationResult",
    "ReassignmentWorkspaceOwnership",
    "ReassignmentWorkspaceProbeResult",
    "ReassignmentWorkspaceProbeState",
    "ReassignmentWorkspaceReference",
    "ReassignmentWorkspaceReferenceProbeRequest",
]
