"""报告专用多文档 RAG、完整交互轨迹与外部资源清理端口。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable

from app.modules.tasks.domain import TaskId, TaskStepCheckpoint

from app.modules.report.domain.errors import ReportRagError

from .artifacts import ReportArtifactCategory, ReportArtifactRef


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


def _optional_text(value: object, *, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str 或 None")
    return value.strip() or None


def _non_negative_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} 必须是非负整数")
    return value


@dataclass(frozen=True)
class ReportRagSource:
    """一次模型回答引用的供应商无关来源证据。"""

    document_ref: str
    text: str
    source_id: str | None = None
    title: str | None = None
    url: str | None = None
    score: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "document_ref",
            _required_text(self.document_ref, name="document_ref"),
        )
        if not isinstance(self.text, str):
            raise TypeError("text 必须是 str")
        for name in ("source_id", "title", "url"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{name} 必须是 str 或 None")
        if self.score is not None:
            if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
                raise TypeError("score 必须是有限数字或 None")
            normalized_score = float(self.score)
            if normalized_score != normalized_score or normalized_score in (
                float("inf"),
                float("-inf"),
            ):
                raise ValueError("score 必须是有限数字或 None")
            object.__setattr__(self, "score", normalized_score)


@dataclass(frozen=True)
class ReportRagAttempt:
    """一次模型调用的完整、供应商无关审计快照。

    ``sequence_no`` 表示整个报告调用链中的顺序，``attempt_no`` 表示同一操作内的重试
    序号。成功调用允许返回空字符串，以保持已确认的“空 RAG 结果仍成功”契约；但必须
    明确提供 ``raw_response``，从而与“模型尚未返回”区分。
    """

    sequence_no: int
    operation: str
    attempt_no: int
    prompt_kind: str
    prompt_digest: str
    raw_response: str | None
    sources: tuple[ReportRagSource, ...]
    failure_stage: str | None = None
    error_message: str | None = None
    query_mode: str = "query"
    source_count: int | None = None
    verified_source_count: int | None = None
    missing_marker_count: int = 0
    mismatched_marker_count: int = 0
    source_marker_status: str = ""
    call_id: str = ""

    def __post_init__(self) -> None:
        if (
            isinstance(self.sequence_no, bool)
            or not isinstance(self.sequence_no, int)
            or self.sequence_no <= 0
        ):
            raise ValueError("sequence_no 必须是正整数")
        if (
            isinstance(self.attempt_no, bool)
            or not isinstance(self.attempt_no, int)
            or self.attempt_no <= 0
        ):
            raise ValueError("attempt_no 必须是正整数")
        for name in ("operation", "prompt_kind"):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name=name),
            )
        digest = _required_text(self.prompt_digest, name="prompt_digest").lower()
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError("prompt_digest 必须是 SHA-256 小写十六进制摘要")
        object.__setattr__(self, "prompt_digest", digest)
        if self.query_mode != "query":
            raise ValueError("报告 RAG query_mode 必须是 query")
        if self.raw_response is not None and not isinstance(self.raw_response, str):
            raise TypeError("raw_response 必须是 str 或 None")

        failure_stage = _optional_text(self.failure_stage, name="failure_stage")
        error_message = _optional_text(self.error_message, name="error_message")
        if (failure_stage is None) != (error_message is None):
            raise ValueError("failure_stage 与 error_message 必须同时存在或同时为空")
        if failure_stage is None and self.raw_response is None:
            raise ValueError("成功的 RAG attempt 必须明确提供 raw_response")
        object.__setattr__(self, "failure_stage", failure_stage)
        object.__setattr__(self, "error_message", error_message)

        sources = tuple(self.sources)
        if any(not isinstance(item, ReportRagSource) for item in sources):
            raise TypeError("sources 只能包含 ReportRagSource")
        object.__setattr__(self, "sources", sources)
        source_count = (
            len(sources)
            if self.source_count is None
            else _non_negative_int(self.source_count, name="source_count")
        )
        verified_count = (
            len(sources)
            if self.verified_source_count is None
            else _non_negative_int(
                self.verified_source_count,
                name="verified_source_count",
            )
        )
        missing_count = _non_negative_int(
            self.missing_marker_count,
            name="missing_marker_count",
        )
        mismatched_count = _non_negative_int(
            self.mismatched_marker_count,
            name="mismatched_marker_count",
        )
        if verified_count != len(sources):
            raise ValueError("verified_source_count 必须等于已保存来源数量")
        if verified_count + missing_count + mismatched_count != source_count:
            raise ValueError("来源验证分类数量之和必须等于 source_count")
        expected_marker_status = (
            "not_returned"
            if source_count == 0
            else "conflict"
            if mismatched_count
            else "missing"
            if missing_count
            else "matched"
        )
        marker_status = self.source_marker_status.strip() or expected_marker_status
        if marker_status != expected_marker_status:
            raise ValueError("source_marker_status 与来源验证统计不一致")
        object.__setattr__(self, "source_count", source_count)
        object.__setattr__(self, "verified_source_count", verified_count)
        object.__setattr__(self, "source_marker_status", marker_status)
        if not isinstance(self.call_id, str):
            raise TypeError("call_id 必须是 str")
        object.__setattr__(self, "call_id", self.call_id.strip())

    @property
    def succeeded(self) -> bool:
        return self.failure_stage is None


@dataclass(frozen=True)
class ReportRagLifecycleEvent:
    """Context、Conversation、Document 等资源操作的完整生命周期证据。"""

    sequence_no: int
    operation: str
    attempt_no: int
    success: bool
    external_ref: str | None = None
    failure_stage: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.sequence_no, bool)
            or not isinstance(self.sequence_no, int)
            or self.sequence_no <= 0
        ):
            raise ValueError("sequence_no 必须是正整数")
        if (
            isinstance(self.attempt_no, bool)
            or not isinstance(self.attempt_no, int)
            or self.attempt_no <= 0
        ):
            raise ValueError("attempt_no 必须是正整数")
        object.__setattr__(
            self,
            "operation",
            _required_text(self.operation, name="operation"),
        )
        if not isinstance(self.success, bool):
            raise TypeError("success 必须是 bool")
        external_ref = _optional_text(self.external_ref, name="external_ref")
        failure_stage = _optional_text(self.failure_stage, name="failure_stage")
        error_message = _optional_text(self.error_message, name="error_message")
        if self.success and (failure_stage is not None or error_message is not None):
            raise ValueError("成功生命周期事件不得包含失败信息")
        if not self.success and (failure_stage is None or error_message is None):
            raise ValueError("失败生命周期事件必须包含失败阶段和错误信息")
        object.__setattr__(self, "external_ref", external_ref)
        object.__setattr__(self, "failure_stage", failure_stage)
        object.__setattr__(self, "error_message", error_message)


@dataclass(frozen=True)
class ReportRagTrace:
    """一次报告 RAG 会话从资源创建到模型调用的完整审计快照。

    模型调用前失败时 ``attempts`` 可以为空；失败阶段和生命周期事件仍可证明现场。
    成功轨迹则必须至少包含一次 attempt，禁止把仅有摘要的记录伪装成完整模型审计。
    """

    trace_id: str
    context_name: str
    context_ref: str | None
    conversation_ref: str | None
    attempts: tuple[ReportRagAttempt, ...]
    lifecycle_events: tuple[ReportRagLifecycleEvent, ...]
    failure_stage: str | None = None
    error_message: str | None = None
    final_call_id: str = ""
    summary: str = ""

    def __post_init__(self) -> None:
        for name in ("trace_id", "context_name"):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name=name),
            )
        object.__setattr__(
            self,
            "context_ref",
            _optional_text(self.context_ref, name="context_ref"),
        )
        object.__setattr__(
            self,
            "conversation_ref",
            _optional_text(self.conversation_ref, name="conversation_ref"),
        )
        attempts = tuple(self.attempts)
        lifecycle_events = tuple(self.lifecycle_events)
        if any(not isinstance(item, ReportRagAttempt) for item in attempts):
            raise TypeError("attempts 只能包含 ReportRagAttempt")
        if any(
            not isinstance(item, ReportRagLifecycleEvent)
            for item in lifecycle_events
        ):
            raise TypeError("lifecycle_events 只能包含 ReportRagLifecycleEvent")
        if tuple(item.sequence_no for item in attempts) != tuple(
            range(1, len(attempts) + 1)
        ):
            raise ValueError("attempts sequence_no 必须从 1 连续严格递增")
        if tuple(item.sequence_no for item in lifecycle_events) != tuple(
            range(1, len(lifecycle_events) + 1)
        ):
            raise ValueError("lifecycle_events sequence_no 必须从 1 连续严格递增")

        failure_stage = _optional_text(self.failure_stage, name="failure_stage")
        error_message = _optional_text(self.error_message, name="error_message")
        if (failure_stage is None) != (error_message is None):
            raise ValueError("failure_stage 与 error_message 必须同时存在或同时为空")
        if failure_stage is None and not attempts:
            raise ValueError("成功 RAG trace 必须包含至少一次模型调用")
        if failure_stage is None and attempts and not attempts[-1].succeeded:
            raise ValueError("成功 RAG trace 的最后一次 attempt 必须成功")
        object.__setattr__(self, "failure_stage", failure_stage)
        object.__setattr__(self, "error_message", error_message)
        object.__setattr__(self, "attempts", attempts)
        object.__setattr__(self, "lifecycle_events", lifecycle_events)
        if not isinstance(self.final_call_id, str):
            raise TypeError("final_call_id 必须是 str")
        final_call_id = self.final_call_id.strip()
        if final_call_id and attempts and attempts[-1].call_id:
            if final_call_id != attempts[-1].call_id:
                raise ValueError("final_call_id 必须与最后一次 attempt 的 call_id 一致")
        object.__setattr__(self, "final_call_id", final_call_id)
        if not isinstance(self.summary, str):
            raise TypeError("summary 必须是 str")

    @property
    def succeeded(self) -> bool:
        return self.failure_stage is None


@dataclass(frozen=True)
class ReportRagCleanupRef:
    """仍待处置的外部资源集合引用；供应商结构只能由 Adapter 解释。"""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", _required_text(self.value, name="value"))


@dataclass(frozen=True)
class CleanupReportRag:
    """清理外部 RAG 资源的内部命令。

    ``sequence_start`` 仅在持久化清理恢复时使用：首次清理由不透明 cleanup ref 自带起始
    序号，重试则必须从已审计最大序号的下一位继续，禁止覆盖旧生命周期证据。
    """

    cleanup_ref: ReportRagCleanupRef
    sequence_start: int | None = None
    attempt_baselines: tuple[tuple[str, int], ...] = ()
    event_checkpoint: Callable[[ReportRagLifecycleEvent], None] | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    heartbeat: Callable[[], None] | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.cleanup_ref, ReportRagCleanupRef):
            raise TypeError("cleanup_ref 必须是 ReportRagCleanupRef")
        if self.sequence_start is not None and (
            isinstance(self.sequence_start, bool)
            or not isinstance(self.sequence_start, int)
            or self.sequence_start <= 0
        ):
            raise ValueError("sequence_start 必须是正整数或 None")
        baselines = tuple(self.attempt_baselines)
        normalized: list[tuple[str, int]] = []
        for item in baselines:
            if not isinstance(item, tuple) or len(item) != 2:
                raise TypeError("attempt_baselines 元素必须是二元 tuple")
            operation, attempt_no = item
            if not isinstance(operation, str) or not operation.strip():
                raise ValueError("attempt_baselines operation 不能为空")
            if (
                isinstance(attempt_no, bool)
                or not isinstance(attempt_no, int)
                or attempt_no < 0
            ):
                raise ValueError("attempt_baselines attempt_no 必须是非负整数")
            normalized.append((operation.strip(), attempt_no))
        if len({operation for operation, _ in normalized}) != len(normalized):
            raise ValueError("attempt_baselines operation 不得重复")
        object.__setattr__(self, "attempt_baselines", tuple(sorted(normalized)))
        if self.event_checkpoint is not None and not callable(self.event_checkpoint):
            raise TypeError("event_checkpoint 必须可调用或为 None")
        if self.heartbeat is not None and not callable(self.heartbeat):
            raise TypeError("heartbeat 必须可调用或为 None")


@runtime_checkable
class ReportRagStepObserverPort(Protocol):
    """AnythingLLM 细粒度操作与持久 Step 之间的内部桥接。

    ``begin`` 必须在供应商 I/O 前返回；``succeed`` 必须在完整结果取得后写入检查点。
    失败由 Adapter 携带当前 Step 和完整 Trace 交还 Runner 统一审计、分类。
    """

    def begin(self, step_key: str, idempotency_key: str) -> None: ...

    def succeed(self, step_key: str, checkpoint: TaskStepCheckpoint) -> None: ...


@dataclass(frozen=True)
class ReportRagRequest:
    """一次按顺序上传多文档并生成报告的请求。"""

    task_id: TaskId
    trace_id: str
    ordered_source_files: tuple[ReportArtifactRef, ...]
    prompt: str
    context_name: str
    conversation_name: str
    step_observer: ReportRagStepObserverPort | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        object.__setattr__(
            self,
            "trace_id",
            _required_text(self.trace_id, name="trace_id"),
        )
        files = tuple(self.ordered_source_files)
        if not files or any(not isinstance(item, ReportArtifactRef) for item in files):
            raise ValueError("ordered_source_files 必须包含 ReportArtifactRef")
        if any(item.task_id != self.task_id for item in files):
            raise ValueError("RAG 输入 Artifact 必须属于当前 task_id")
        if any(item.category is not ReportArtifactCategory.RAG_INPUT for item in files):
            raise ValueError("ordered_source_files 只能包含 rag_input Artifact")
        object.__setattr__(self, "ordered_source_files", files)
        for name in ("prompt", "context_name", "conversation_name"):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name=name),
            )
        if self.step_observer is not None and not isinstance(
            self.step_observer,
            ReportRagStepObserverPort,
        ):
            raise TypeError("step_observer 必须实现 ReportRagStepObserverPort 或为 None")


@dataclass(frozen=True)
class ReportRagResponse:
    """成功 RAG 结果；``None`` 内容按既有契约仍可形成成功报告。"""

    raw_content: str | None
    trace: ReportRagTrace
    cleanup_ref: ReportRagCleanupRef | None = None

    def __post_init__(self) -> None:
        if self.raw_content is not None and not isinstance(self.raw_content, str):
            raise TypeError("raw_content 必须是 str 或 None")
        if not isinstance(self.trace, ReportRagTrace):
            raise TypeError("trace 必须是 ReportRagTrace")
        if not self.trace.succeeded:
            raise ValueError("成功 RAG 响应不得携带失败 trace")
        final_raw_response = self.trace.attempts[-1].raw_response
        if (self.raw_content or "") != (final_raw_response or ""):
            raise ValueError("raw_content 必须与最后一次 RAG attempt 响应一致")
        if self.cleanup_ref is not None and not isinstance(
            self.cleanup_ref,
            ReportRagCleanupRef,
        ):
            raise TypeError("cleanup_ref 必须是 ReportRagCleanupRef 或 None")


class ReportRagExecutionError(ReportRagError):
    """携带失败现场的 RAG 错误，保证 Application 仍可先审计再收敛。"""

    def __init__(
        self,
        message: str,
        *,
        trace: ReportRagTrace,
        cleanup_ref: ReportRagCleanupRef | None = None,
        external_outcome_unknown: bool = False,
        active_step_key: str = "",
    ) -> None:
        super().__init__(message)
        if not isinstance(trace, ReportRagTrace):
            raise TypeError("trace 必须是 ReportRagTrace")
        if trace.succeeded:
            raise ValueError("ReportRagExecutionError 必须携带失败 trace")
        if cleanup_ref is not None and not isinstance(cleanup_ref, ReportRagCleanupRef):
            raise TypeError("cleanup_ref 必须是 ReportRagCleanupRef 或 None")
        if not isinstance(external_outcome_unknown, bool):
            raise TypeError("external_outcome_unknown 必须是 bool")
        if not isinstance(active_step_key, str):
            raise TypeError("active_step_key 必须是 str")
        self.trace = trace
        self.cleanup_ref = cleanup_ref
        # 写类请求超时或响应协议损坏时，上游可能已经产生资源，但本地尚未拿到可删除
        # 引用。该标志只在内部驱动隔离门禁，不进入公开 HTTP/回调契约。
        self.external_outcome_unknown = external_outcome_unknown
        self.active_step_key = active_step_key.strip()


@runtime_checkable
class ReportRagPort(Protocol):
    """生成报告，并在审计成功后清理任务级外部资源。"""

    def generate(self, request: ReportRagRequest) -> ReportRagResponse:
        ...

    def cleanup(
        self,
        command: CleanupReportRag,
    ) -> tuple[ReportRagLifecycleEvent, ...]:
        ...


__all__ = [
    "CleanupReportRag",
    "ReportRagAttempt",
    "ReportRagCleanupRef",
    "ReportRagExecutionError",
    "ReportRagLifecycleEvent",
    "ReportRagPort",
    "ReportRagRequest",
    "ReportRagResponse",
    "ReportRagSource",
    "ReportRagStepObserverPort",
    "ReportRagTrace",
]
