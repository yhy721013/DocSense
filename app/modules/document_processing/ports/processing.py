"""共享文档处理 Application 所依赖的窄端口与边界 DTO。"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import (
    BinaryIO,
    Callable,
    ContextManager,
    Protocol,
    runtime_checkable,
)

from app.modules.document_processing.domain import (
    ArtifactKind,
    ArtifactRef,
    DocumentProcessingRequest,
    DocumentRepresentation,
    LineageEvent,
)
from app.modules.tasks.domain import TaskId


@runtime_checkable
class ArtifactContent(Protocol):
    """可重复打开的二进制内容源；Application 无需知道本地路径。"""

    def open_reader(self) -> ContextManager[BinaryIO]:
        """打开只读流，并由上下文管理器负责关闭。"""


@dataclass(frozen=True, slots=True)
class ProcessorOutput:
    """Processor 产生、尚未获得 Artifact Store 所有权的候选内容。"""

    content: ArtifactContent
    kind: ArtifactKind
    representation: DocumentRepresentation
    media_type: str
    warnings: tuple[str, ...] = ()
    ordinal: int = 1
    _lease: "_ProcessorOutputLease" = field(
        default_factory=lambda: _ProcessorOutputLease(),
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.content, ArtifactContent):
            raise TypeError("content 必须实现 ArtifactContent")
        if not isinstance(self.kind, ArtifactKind):
            raise TypeError("kind 必须是 ArtifactKind")
        if not isinstance(self.representation, DocumentRepresentation):
            raise TypeError("representation 必须是 DocumentRepresentation")
        if not isinstance(self.media_type, str) or not self.media_type.strip():
            raise ValueError("media_type 不能为空")
        object.__setattr__(self, "media_type", self.media_type.strip().lower())
        warnings = tuple(self.warnings)
        if any(not isinstance(item, str) for item in warnings):
            raise TypeError("warnings 只能包含 str")
        object.__setattr__(self, "warnings", warnings)
        if (
            isinstance(self.ordinal, bool)
            or not isinstance(self.ordinal, int)
            or self.ordinal <= 0
        ):
            raise ValueError("ordinal 必须是正整数")

    @classmethod
    def with_cleanup(
        cls,
        *,
        content: ArtifactContent,
        kind: ArtifactKind,
        representation: DocumentRepresentation,
        media_type: str,
        cleanup: Callable[[], None],
        warnings: tuple[str, ...] = (),
        ordinal: int = 1,
    ) -> "ProcessorOutput":
        """建立带幂等清理租约的候选输出。"""

        if not callable(cleanup):
            raise TypeError("cleanup 必须可调用")
        return cls(
            content=content,
            kind=kind,
            representation=representation,
            media_type=media_type,
            warnings=warnings,
            ordinal=ordinal,
            _lease=_ProcessorOutputLease(cleanup),
        )

    def close(self) -> None:
        """幂等释放 Processor 私有 scratch；不得删除已发布 Artifact。"""

        self._lease.close()

    def __enter__(self) -> "ProcessorOutput":
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()


class _ProcessorOutputLease:
    """在冻结输出 DTO 内保存最小可变清理状态。"""

    def __init__(self, callback: Callable[[], None] | None = None) -> None:
        self._callback = callback
        self._closed = False
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            callback = self._callback
            self._callback = None
        if callback is not None:
            callback()


@dataclass(frozen=True, slots=True)
class ArtifactPublication:
    """将候选内容发布到确定性 Artifact 身份所需的全部事实。"""

    task_id: TaskId
    step_key: str
    kind: ArtifactKind
    representation: DocumentRepresentation
    media_type: str
    ordinal: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.step_key, str) or not self.step_key.strip():
            raise ValueError("step_key 不能为空")
        object.__setattr__(self, "step_key", self.step_key.strip().lower())
        if not isinstance(self.kind, ArtifactKind):
            raise TypeError("kind 必须是 ArtifactKind")
        if not isinstance(self.representation, DocumentRepresentation):
            raise TypeError("representation 必须是 DocumentRepresentation")
        if not isinstance(self.media_type, str) or not self.media_type.strip():
            raise ValueError("media_type 不能为空")
        object.__setattr__(self, "media_type", self.media_type.strip().lower())
        if (
            isinstance(self.ordinal, bool)
            or not isinstance(self.ordinal, int)
            or self.ordinal <= 0
        ):
            raise ValueError("ordinal 必须是正整数")


class ProcessingRecordState(str, Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ProcessingAcquireDecision(str, Enum):
    ACQUIRED = "acquired"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True, slots=True)
class ProcessingRecordSnapshot:
    """单个幂等步骤的持久化快照。"""

    step_key: str
    state: ProcessingRecordState
    claim_token: str | None = None
    artifact: ArtifactRef | None = None
    lineage: LineageEvent | None = None
    error_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.step_key, str) or not self.step_key.strip():
            raise ValueError("step_key 不能为空")
        object.__setattr__(self, "step_key", self.step_key.strip().lower())
        if not isinstance(self.state, ProcessingRecordState):
            raise TypeError("state 必须是 ProcessingRecordState")
        if self.claim_token is not None and (
            not isinstance(self.claim_token, str) or not self.claim_token.strip()
        ):
            raise ValueError("claim_token 必须是非空 str 或 None")
        if self.artifact is not None and not isinstance(self.artifact, ArtifactRef):
            raise TypeError("artifact 必须是 ArtifactRef 或 None")
        if self.lineage is not None and not isinstance(self.lineage, LineageEvent):
            raise TypeError("lineage 必须是 LineageEvent 或 None")
        object.__setattr__(self, "error_code", str(self.error_code).strip())
        if self.state is ProcessingRecordState.RUNNING and not self.claim_token:
            raise ValueError("running 记录必须包含 claim_token")
        if self.state is ProcessingRecordState.SUCCEEDED and (
            self.artifact is None or self.lineage is None
        ):
            raise ValueError("succeeded 记录必须包含 Artifact 与 Lineage")


@dataclass(frozen=True, slots=True)
class ProcessingAcquireResult:
    decision: ProcessingAcquireDecision
    snapshot: ProcessingRecordSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.decision, ProcessingAcquireDecision):
            raise TypeError("decision 必须是 ProcessingAcquireDecision")
        if not isinstance(self.snapshot, ProcessingRecordSnapshot):
            raise TypeError("snapshot 必须是 ProcessingRecordSnapshot")


@runtime_checkable
class DocumentProcessorPort(Protocol):
    """一个供应商无关的文档转换步骤。"""

    def process(self, request: DocumentProcessingRequest) -> ProcessorOutput:
        ...


@runtime_checkable
class ArtifactStorePort(Protocol):
    """发布、验证、读取和按所有权清理不可变 Artifact。"""

    def publish(
        self,
        publication: ArtifactPublication,
        content: ArtifactContent,
    ) -> ArtifactRef:
        ...

    def verify(self, artifact: ArtifactRef) -> bool:
        ...

    def open_reader(self, artifact: ArtifactRef) -> ContextManager[BinaryIO]:
        ...

    def delete_if_owned(self, artifact: ArtifactRef) -> bool:
        ...


@runtime_checkable
class ProcessingRecordPort(Protocol):
    """文档处理步骤、结果和谱系事实的持久化边界。"""

    def acquire(
        self,
        request: DocumentProcessingRequest,
    ) -> ProcessingAcquireResult:
        ...

    def complete(
        self,
        request: DocumentProcessingRequest,
        *,
        claim_token: str,
        artifact: ArtifactRef,
        lineage: LineageEvent,
    ) -> None:
        ...

    def fail(
        self,
        request: DocumentProcessingRequest,
        *,
        claim_token: str,
        error_code: str,
    ) -> None:
        ...

    def mark_outcome_unknown(
        self,
        request: DocumentProcessingRequest,
        *,
        error_code: str,
        claim_token: str | None = None,
    ) -> None:
        ...

    def get(self, step_key: str) -> ProcessingRecordSnapshot | None:
        ...


@runtime_checkable
class ProcessingRecoveryPort(Protocol):
    """内部恢复边界；只接受已经由运维或供应商对账确认的事实。"""

    def resolve_failed(
        self,
        request: DocumentProcessingRequest,
        *,
        error_code: str,
    ) -> None:
        """把 running/unknown 显式确认为失败，使上层可执行既定降级。"""

    def recover_completed(
        self,
        request: DocumentProcessingRequest,
        *,
        artifact: ArtifactRef,
        lineage: LineageEvent,
    ) -> None:
        """用已验证 Artifact 修复 running/unknown 的成功事实。"""

    def quarantine_stale_running(
        self,
        *,
        stale_before_epoch: float,
        limit: int,
    ) -> tuple[str, ...]:
        """把明确选中的陈旧 running 隔离为 unknown，绝不自动重跑。"""


@runtime_checkable
class ArtifactCatalogPort(Protocol):
    """登记所有已发布 Artifact 的所有权事实，包括没有 Processor Step 的 source。"""

    def register_artifact(self, artifact: ArtifactRef) -> None:
        ...


@runtime_checkable
class ResourcePort(Protocol):
    """未来重型处理许可的窄边界；资源实现不能泄漏到领域对象。"""

    def acquire(self, request: DocumentProcessingRequest) -> ContextManager[None]:
        ...


__all__ = [
    "ArtifactContent",
    "ArtifactCatalogPort",
    "ArtifactPublication",
    "ArtifactStorePort",
    "DocumentProcessorPort",
    "ProcessingAcquireDecision",
    "ProcessingAcquireResult",
    "ProcessingRecordPort",
    "ProcessingRecoveryPort",
    "ProcessingRecordSnapshot",
    "ProcessingRecordState",
    "ProcessorOutput",
    "ResourcePort",
]
