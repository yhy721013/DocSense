"""Analysis 任务级 RAG 会话、查询与关闭的供应商无关 Port。

会话引用必须由 Application 显式持有并传回 Port，禁止 Adapter 按 execution 在进程内
维护隐藏字典。这样分类、修复和抽取能够复用同一隔离会话，进程重启后的资源事实也可以
从持久化记录恢复，而不是依赖某个 Python 对象仍然存活。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import ContextManager, Protocol, runtime_checkable

from .common import AnalysisExecutionRef


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空 str")
    return value.strip()


class AnalysisRagOperation(str, Enum):
    """RAG 查询用途由 Application 明确指定，Adapter 不猜测 Prompt 语义。"""

    CLASSIFICATION = "classification"
    CLASSIFICATION_REPAIR = "classification_repair"
    IDENTITY_RESELECT = "identity_reselect"
    EXTRACTION = "extraction"
    EXTRACTION_REPAIR = "extraction_repair"
    COMBINED = "combined"


class AnalysisRagSessionOpenStage(str, Enum):
    """打开隔离会话时可审计的外部副作用阶段。"""

    CONTEXT_CREATE = "context_create"
    CONVERSATION_CREATE = "conversation_create"
    DOCUMENT_UPLOAD = "document_upload"
    DOCUMENT_BIND = "document_bind"


class AnalysisRagCloseOutcome(str, Enum):
    """外部关闭结果必须区分已确认、未执行和结果未知。"""

    CONFIRMED = "confirmed"
    KNOWN_NOT_APPLIED = "known_not_applied"
    OUTCOME_UNKNOWN = "outcome_unknown"


class AnalysisRagLifecycleOutcome(str, Enum):
    """单次外部资源操作的确定结果；未知结果必须冻结现场等待恢复判定。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


@dataclass(frozen=True)
class AnalysisRagLifecycleEvent:
    """Context、Conversation、Document 等外部资源操作的可追加审计证据。"""

    sequence_no: int
    operation: str
    attempt_number: int
    outcome: AnalysisRagLifecycleOutcome
    external_ref: str = ""
    error_code: str = ""

    def __post_init__(self) -> None:
        for name in ("sequence_no", "attempt_number"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} 必须是正整数")
        object.__setattr__(
            self,
            "operation",
            _required_text(self.operation, name="operation"),
        )
        if not isinstance(self.outcome, AnalysisRagLifecycleOutcome):
            raise TypeError("outcome 必须是 AnalysisRagLifecycleOutcome")
        for name in ("external_ref", "error_code"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(f"{name} 必须是 str")
            object.__setattr__(self, name, value.strip())
        if self.outcome is AnalysisRagLifecycleOutcome.SUCCEEDED and self.error_code:
            raise ValueError("成功生命周期事件不得携带 error_code")
        if self.outcome is not AnalysisRagLifecycleOutcome.SUCCEEDED and not self.error_code:
            raise ValueError("失败或结果未知的生命周期事件必须携带 error_code")


def _lifecycle_events(
    values: tuple[AnalysisRagLifecycleEvent, ...],
    *,
    name: str,
    required: bool,
) -> tuple[AnalysisRagLifecycleEvent, ...]:
    events = tuple(values)
    if required and not events:
        raise ValueError(f"{name} 不能为空")
    if any(not isinstance(item, AnalysisRagLifecycleEvent) for item in events):
        raise TypeError(f"{name} 只能包含 AnalysisRagLifecycleEvent")
    sequences = tuple(item.sequence_no for item in events)
    if sequences != tuple(sorted(set(sequences))):
        raise ValueError(f"{name}.sequence_no 必须严格递增且不重复")
    return events


@dataclass(frozen=True)
class AnalysisRagSessionOpenRequest:
    """为一个 execution 打开并绑定唯一文档的隔离 RAG 会话。"""

    execution: AnalysisExecutionRef
    upload_path: str

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        object.__setattr__(
            self,
            "upload_path",
            _required_text(self.upload_path, name="upload_path"),
        )


@dataclass(frozen=True)
class AnalysisRagSessionRef:
    """可持久化的任务级 RAG 资源引用，不暴露供应商客户端对象。

    旧 Document RAG Gateway 会在首个模型调用中完成文档上传与绑定。因此刚打开
    Context/Conversation 时，文档四元组允许整体为空；首个成功 ``execute`` 必须返回
    同一 ``session_ref`` 且已绑定文档的新引用。禁止只填其中一项，避免把未知文档误交给
    永久知识库或清理逻辑。
    """

    execution: AnalysisExecutionRef
    session_ref: str
    context_ref: str
    conversation_ref: str
    document_ref: str = ""
    document_location: str = ""
    content_sha256: str = ""
    ingested_file_name: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        object.__setattr__(
            self,
            "session_ref",
            _required_text(self.session_ref, name="session_ref"),
        )
        for field_name in ("context_ref", "conversation_ref"):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), name=field_name),
            )
        document_values = {
            "document_ref": self.document_ref,
            "document_location": self.document_location,
            "content_sha256": self.content_sha256,
            "ingested_file_name": self.ingested_file_name,
        }
        normalized = {
            name: str(value or "").strip()
            for name, value in document_values.items()
        }
        if any(normalized.values()) and not all(normalized.values()):
            raise ValueError("文档引用必须整体为空或整体完整")
        if any(normalized.values()):
            digest = normalized["content_sha256"].lower()
            if (
                len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValueError("content_sha256 必须是 64 位小写十六进制摘要")
            file_name = (
                normalized["ingested_file_name"]
                .replace("\\", "/")
                .rsplit("/", 1)[-1]
            )
            if file_name in {"", ".", ".."}:
                raise ValueError("ingested_file_name 必须是有效文件名")
            normalized["content_sha256"] = digest
            normalized["ingested_file_name"] = file_name
        for field_name, value in normalized.items():
            object.__setattr__(self, field_name, value)

    @property
    def document_bound(self) -> bool:
        """只有完整文档四元组存在时才允许永久知识库接管。"""

        return bool(self.document_ref)

    def with_bound_document(
        self,
        *,
        document_ref: str,
        document_location: str,
        content_sha256: str,
        ingested_file_name: str,
    ) -> "AnalysisRagSessionRef":
        """返回同一会话绑定真实上传文档后的不可变引用。"""

        return AnalysisRagSessionRef(
            execution=self.execution,
            session_ref=self.session_ref,
            context_ref=self.context_ref,
            conversation_ref=self.conversation_ref,
            document_ref=document_ref,
            document_location=document_location,
            content_sha256=content_sha256,
            ingested_file_name=ingested_file_name,
        )

    def with_conversation_ref(self, conversation_ref: str) -> "AnalysisRagSessionRef":
        """返回切换阶段对话后的同一任务会话引用。

        旧 Document RAG Gateway 在身份重选和字段抽取前会创建新的 Conversation。该行为不
        会改变 Context 或已上传文档的归属，但如果继续沿用旧的 conversation_ref，后续
        审计会把实际模型调用错误关联到初始对话。因此只允许在同一 execution/context 内
        更新这一引用，并同步生成新的不透明 session_ref；不暴露任何供应商客户端对象。
        """

        normalized_conversation_ref = _required_text(
            conversation_ref,
            name="conversation_ref",
        )
        return replace(
            self,
            session_ref=f"{self.context_ref}::{normalized_conversation_ref}",
            conversation_ref=normalized_conversation_ref,
        )


@dataclass(frozen=True)
class AnalysisRagSource:
    """模型回答的供应商无关来源快照，供交互审计精确复建。"""

    document_ref: str
    text: str
    source_id: str = ""
    title: str = ""
    url: str = ""
    score: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "document_ref",
            _required_text(self.document_ref, name="document_ref"),
        )
        for field_name in ("text", "source_id", "title", "url"):
            value = getattr(self, field_name)
            if not isinstance(value, str):
                raise TypeError(f"{field_name} 必须是 str")
        if self.score is not None and (
            isinstance(self.score, bool)
            or not isinstance(self.score, (int, float))
        ):
            raise TypeError("score 必须是数字或 None")
        normalized_score = float(self.score) if self.score is not None else None
        if normalized_score is not None and (
            normalized_score != normalized_score
            or normalized_score in {float("inf"), float("-inf")}
        ):
            # 审计存储统一使用 ``allow_nan=False``。如果在 Port 边界放过 NaN/Infinity，
            # 后续会把供应商数据错误误判为 SQLite 审计故障，并不必要地保留远端现场。
            raise ValueError("score 必须是有限数字或 None")


class AnalysisRagSessionOpenError(RuntimeError):
    """携带打开阶段和部分资源现场，供 Application 先持久化再收敛。"""

    def __init__(
        self,
        message: str,
        *,
        execution: AnalysisExecutionRef,
        stage: AnalysisRagSessionOpenStage,
        partial_session: AnalysisRagSessionRef | None = None,
        lifecycle_events: tuple[AnalysisRagLifecycleEvent, ...],
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        if not isinstance(execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        if not isinstance(stage, AnalysisRagSessionOpenStage):
            raise TypeError("stage 必须是 AnalysisRagSessionOpenStage")
        if partial_session is not None:
            if not isinstance(partial_session, AnalysisRagSessionRef):
                raise TypeError("partial_session 必须是 AnalysisRagSessionRef 或 None")
            if partial_session.execution != execution:
                raise ValueError("partial_session 必须属于当前 execution")
        if not isinstance(outcome_unknown, bool):
            raise TypeError("outcome_unknown 必须是 bool")
        events = _lifecycle_events(
            lifecycle_events,
            name="lifecycle_events",
            required=True,
        )
        if not any(
            item.outcome is not AnalysisRagLifecycleOutcome.SUCCEEDED
            for item in events
        ):
            raise ValueError("打开失败必须至少携带一个失败或结果未知的生命周期事件")
        has_unknown_event = any(
            item.outcome is AnalysisRagLifecycleOutcome.OUTCOME_UNKNOWN
            for item in events
        )
        if outcome_unknown != has_unknown_event:
            raise ValueError("outcome_unknown 必须与生命周期未知结果一致")
        self.execution = execution
        self.stage = stage
        self.partial_session = partial_session
        self.lifecycle_events = events
        self.outcome_unknown = outcome_unknown


@dataclass(frozen=True)
class AnalysisRagSessionOpenResult:
    """打开会话的引用和初始生命周期；两者必须作为一个结果交给 Application。"""

    session: AnalysisRagSessionRef
    lifecycle_events: tuple[AnalysisRagLifecycleEvent, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.session, AnalysisRagSessionRef):
            raise TypeError("session 必须是 AnalysisRagSessionRef")
        events = _lifecycle_events(
            self.lifecycle_events,
            name="lifecycle_events",
            required=True,
        )
        if any(
            item.outcome is not AnalysisRagLifecycleOutcome.SUCCEEDED
            for item in events
        ):
            raise ValueError("成功打开结果只能携带成功生命周期事件")
        if tuple(item.sequence_no for item in events) != tuple(
            range(1, len(events) + 1)
        ):
            raise ValueError("打开生命周期 sequence_no 必须从 1 连续递增")
        object.__setattr__(self, "lifecycle_events", events)


@dataclass(frozen=True)
class AnalysisRagRequest:
    """在显式隔离会话中执行一次分类、抽取或有限修复。"""

    execution: AnalysisExecutionRef
    session: AnalysisRagSessionRef
    operation: AnalysisRagOperation
    prompt: str
    attempt_number: int

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        if not isinstance(self.session, AnalysisRagSessionRef):
            raise TypeError("session 必须是 AnalysisRagSessionRef")
        if self.session.execution != self.execution:
            raise ValueError("session 必须属于当前 execution")
        if not isinstance(self.operation, AnalysisRagOperation):
            raise TypeError("operation 必须是 AnalysisRagOperation")
        if not isinstance(self.prompt, str) or not self.prompt:
            raise ValueError("prompt 必须是非空 str")
        if (
            isinstance(self.attempt_number, bool)
            or not isinstance(self.attempt_number, int)
            or self.attempt_number < 1
        ):
            raise ValueError("attempt_number 必须是正整数")


@dataclass(frozen=True)
class AnalysisRagResult:
    """一次查询的响应；会话和 operation 必须与请求逐项关联。"""

    execution: AnalysisExecutionRef
    session: AnalysisRagSessionRef
    operation: AnalysisRagOperation
    attempt_number: int
    answer: str
    sources: tuple[AnalysisRagSource, ...] = ()
    lifecycle_events: tuple[AnalysisRagLifecycleEvent, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        if not isinstance(self.session, AnalysisRagSessionRef):
            raise TypeError("session 必须是 AnalysisRagSessionRef")
        if self.session.execution != self.execution:
            raise ValueError("session 必须属于当前 execution")
        if not isinstance(self.operation, AnalysisRagOperation):
            raise TypeError("operation 必须是 AnalysisRagOperation")
        if (
            isinstance(self.attempt_number, bool)
            or not isinstance(self.attempt_number, int)
            or self.attempt_number < 1
        ):
            raise ValueError("attempt_number 必须是正整数")
        if not isinstance(self.answer, str):
            raise TypeError("answer 必须是 str")
        sources = tuple(self.sources)
        if any(not isinstance(item, AnalysisRagSource) for item in sources):
            raise TypeError("sources 只能包含 AnalysisRagSource")
        if self.session.document_bound and any(
            item.document_ref != self.session.document_ref for item in sources
        ):
            raise ValueError("sources 必须全部属于当前 session.document_ref")
        object.__setattr__(self, "sources", sources)
        lifecycle_events = _lifecycle_events(
            self.lifecycle_events,
            name="lifecycle_events",
            required=False,
        )
        if any(
            item.outcome is not AnalysisRagLifecycleOutcome.SUCCEEDED
            for item in lifecycle_events
        ):
            raise ValueError("成功查询结果只能携带成功生命周期事件")
        object.__setattr__(
            self,
            "lifecycle_events",
            lifecycle_events,
        )


class AnalysisRagExecutionError(RuntimeError):
    """携带失败查询的完整关联和现场，供 Application 先审计再决定是否收敛。"""

    def __init__(
        self,
        message: str,
        *,
        request: AnalysisRagRequest,
        error_code: str,
        raw_response: str | None = None,
        sources: tuple[AnalysisRagSource, ...] = (),
        lifecycle_events: tuple[AnalysisRagLifecycleEvent, ...] = (),
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        if not isinstance(request, AnalysisRagRequest):
            raise TypeError("request 必须是 AnalysisRagRequest")
        self.error_code = _required_text(error_code, name="error_code")
        if raw_response is not None and not isinstance(raw_response, str):
            raise TypeError("raw_response 必须是 str 或 None")
        normalized_sources = tuple(sources)
        if any(not isinstance(item, AnalysisRagSource) for item in normalized_sources):
            raise TypeError("sources 只能包含 AnalysisRagSource")
        if request.session.document_bound and any(
            item.document_ref != request.session.document_ref
            for item in normalized_sources
        ):
            # 失败响应同样属于交互审计证据，不能比成功响应放宽文档归属校验，否则损坏的
            # Adapter 可能把上一任务或上一文档的来源串入当前失败 attempt。
            raise ValueError("sources 必须全部属于当前 request.session.document_ref")
        events = _lifecycle_events(
            lifecycle_events,
            name="lifecycle_events",
            required=False,
        )
        if not isinstance(outcome_unknown, bool):
            raise TypeError("outcome_unknown 必须是 bool")
        has_unknown_event = any(
            item.outcome is AnalysisRagLifecycleOutcome.OUTCOME_UNKNOWN
            for item in events
        )
        if outcome_unknown != has_unknown_event:
            raise ValueError("outcome_unknown 必须与生命周期未知结果一致")
        self.request = request
        self.raw_response = raw_response
        self.sources = normalized_sources
        self.lifecycle_events = events
        self.outcome_unknown = outcome_unknown


@dataclass(frozen=True)
class AnalysisRagCloseRequest:
    """关闭任务级会话；永久知识已接管文档时必须显式保留文档。"""

    execution: AnalysisExecutionRef
    session: AnalysisRagSessionRef
    retain_document: bool

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        if not isinstance(self.session, AnalysisRagSessionRef):
            raise TypeError("session 必须是 AnalysisRagSessionRef")
        if self.session.execution != self.execution:
            raise ValueError("session 必须属于当前 execution")
        if not isinstance(self.retain_document, bool):
            raise TypeError("retain_document 必须是 bool")


@dataclass(frozen=True)
class AnalysisRagCloseResult:
    """关闭结果未知时不得由 Application 自动重发删除。"""

    execution: AnalysisExecutionRef
    session: AnalysisRagSessionRef
    outcome: AnalysisRagCloseOutcome
    lifecycle_events: tuple[AnalysisRagLifecycleEvent, ...]
    detail_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        if not isinstance(self.session, AnalysisRagSessionRef):
            raise TypeError("session 必须是 AnalysisRagSessionRef")
        if self.session.execution != self.execution:
            raise ValueError("session 必须属于当前 execution")
        if not isinstance(self.outcome, AnalysisRagCloseOutcome):
            raise TypeError("outcome 必须是 AnalysisRagCloseOutcome")
        if not isinstance(self.detail_code, str):
            raise TypeError("detail_code 必须是 str")
        object.__setattr__(self, "detail_code", self.detail_code.strip())
        if self.outcome is AnalysisRagCloseOutcome.CONFIRMED and self.detail_code:
            raise ValueError("确认关闭结果不得携带 detail_code")
        if self.outcome is not AnalysisRagCloseOutcome.CONFIRMED and not self.detail_code:
            raise ValueError("未确认关闭结果必须携带 detail_code")
        events = _lifecycle_events(
            self.lifecycle_events,
            name="lifecycle_events",
            required=True,
        )
        if self.outcome is AnalysisRagCloseOutcome.CONFIRMED and any(
            item.outcome is not AnalysisRagLifecycleOutcome.SUCCEEDED
            for item in events
        ):
            raise ValueError("确认关闭结果只能携带成功生命周期事件")
        if self.outcome is AnalysisRagCloseOutcome.OUTCOME_UNKNOWN and not any(
            item.outcome is AnalysisRagLifecycleOutcome.OUTCOME_UNKNOWN
            for item in events
        ):
            raise ValueError("关闭结果未知时必须携带结果未知的生命周期事件")
        object.__setattr__(self, "lifecycle_events", events)


@runtime_checkable
class AnalysisRagPort(Protocol):
    """显式打开、复用和关闭任务级 RAG 会话。"""

    def open_session(
        self,
        request: AnalysisRagSessionOpenRequest,
    ) -> AnalysisRagSessionOpenResult:
        ...

    def execute(self, request: AnalysisRagRequest) -> AnalysisRagResult:
        ...

    def close_session(
        self,
        request: AnalysisRagCloseRequest,
    ) -> AnalysisRagCloseResult:
        ...


@runtime_checkable
class AnalysisRagPortFactory(Protocol):
    """为一个 execution 创建独立 Transport/Session 的短生命周期 Port。"""

    def create(
        self,
        execution: AnalysisExecutionRef,
    ) -> ContextManager[AnalysisRagPort]:
        ...


__all__ = (
    "AnalysisRagCloseOutcome",
    "AnalysisRagCloseRequest",
    "AnalysisRagCloseResult",
    "AnalysisRagExecutionError",
    "AnalysisRagLifecycleEvent",
    "AnalysisRagLifecycleOutcome",
    "AnalysisRagOperation",
    "AnalysisRagPort",
    "AnalysisRagPortFactory",
    "AnalysisRagRequest",
    "AnalysisRagResult",
    "AnalysisRagSource",
    "AnalysisRagSessionOpenError",
    "AnalysisRagSessionOpenRequest",
    "AnalysisRagSessionOpenResult",
    "AnalysisRagSessionOpenStage",
    "AnalysisRagSessionRef",
)
