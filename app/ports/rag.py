"""供应商无关的文档 RAG 业务契约与执行轨迹。

本模块位于应用层边界，是业务代码与外部 RAG 实现之间的稳定契约。业务代码只允许依赖
这里定义的 DTO 和 Protocol，不应了解外部系统的资源名称、HTTP 路径、认证方式或响应
字段。具体适配器负责实现 Protocol，测试替身则位于测试目录，避免生产抽象包混入实现。
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol, runtime_checkable


MAX_RAG_QUERY_ATTEMPTS = 3
"""单次 analyse 或 ask 允许的模型查询次数硬上限。

该上限属于应用层成本与资源保护契约，生产适配器和测试替身必须共同遵守。调用方可以在
1 到该值之间选择更小次数，但不得通过配置错误或参数透传制造无界模型调用。
"""


def normalize_rag_prompt(value: str) -> str:
    """返回模型调用、摘要计算和审计持久化共同使用的规范 Prompt。

    规范只处理跨平台表示差异：把 ``CRLF`` 和单独 ``CR`` 统一为 ``LF``，并移除首尾
    空白；Prompt 正文内部的换行、缩进和空格保持不变，避免改变结构化指令语义。调用方
    必须把本函数返回的同一个字符串同时用于外部请求与审计，禁止各层自行采用不同的
    ``strip`` 或换行转换规则。
    """
    if not isinstance(value, str):
        raise TypeError("prompt 必须是 str")
    normalized = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValueError("prompt 不能为空")
    return normalized


class RagPromptKind(str, Enum):
    """文档 RAG 模型调用的稳定用途分类。

    使用受控枚举而不是自由文本，可以让审计系统可靠区分首次分析、JSON 修复和领域分类
    修复。``FOLLOW_UP`` 仅用于尚未迁移的通用追问；阶段 9 的文件分析修复必须使用更具体
    的枚举值，不能继续把所有调用归类为 follow_up。
    """

    ANALYSIS = "analysis"
    JSON_REPAIR = "json_repair"
    ARCHITECTURE_REPAIR = "architecture_repair"
    FOLLOW_UP = "follow_up"


def validate_rag_prompt_kind(value: RagPromptKind) -> RagPromptKind:
    """校验业务调用显式传入的提示词用途枚举。

    ``ask`` 会产生真实模型费用和外部副作用，因此必须在发出请求之前拒绝自由文本、空值
    和未知分类。审计 DTO 可以保存枚举序列化后的字符串，但业务调用边界只接受正式枚举，
    以便类型检查和运行时校验共同约束调用方。
    """
    if not isinstance(value, RagPromptKind):
        raise TypeError("prompt_kind 必须是 RagPromptKind")
    return value


def validate_rag_query_max_attempts(value: int) -> int:
    """校验并返回单次 analyse/ask 允许的模型查询总次数。

    该校验属于供应商无关的应用契约，因此与硬上限共同保留在 Port。生产 Gateway 和测试
    Fake 必须调用同一函数，避免两种实现对布尔值、浮点数或边界值产生不同解释。
    """
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= MAX_RAG_QUERY_ATTEMPTS
    ):
        raise ValueError(
            "max_attempts 必须是 1 到 "
            f"{MAX_RAG_QUERY_ATTEMPTS} 之间的整数"
        )
    return value


@dataclass(frozen=True)
class RagSource:
    """一次模型回答引用的供应商无关来源证据。

    ``document_ref`` 是适配器生成的稳定文档身份；业务层只做精确相等比较。其余字段均
    允许缺失，以兼容不同检索实现可提供信息不一致的情况。
    """

    document_ref: str
    text: str
    id: Optional[str] = None
    title: Optional[str] = None
    url: Optional[str] = None
    score: Optional[float] = None

    def __post_init__(self) -> None:
        """保证每条来源都携带可用于目标文档匹配的稳定身份。

        ``document_ref`` 为空会使来源校验退化为“只要存在任意来源就算成功”，这正是端口
        契约需要阻断的错误。证据文本允许为空，因为部分检索实现只能返回文档身份或链接，
        是否强制要求片段内容应由具体业务用例决定。
        """
        if not str(self.document_ref or "").strip():
            raise ValueError("RagSource.document_ref 不能为空")


@dataclass(frozen=True)
class RagAttempt:
    """一次模型调用的可审计记录。

    属性:
        operation: 稳定模型操作名，例如 ``analyse``、``ask`` 或结构修复。
        attempt: 当前操作内从 1 开始的尝试序号。
        prompt_kind: 提示词用途分类。
        prompt_digest: ``normalize_rag_prompt`` 返回的规范 Prompt 的 SHA-256 摘要，用于
            证明主审计记录与实际模型调用对应；普通日志不得输出完整 Prompt。
        query_mode: 文档 RAG 固定为 ``query``，防止适配器回退为无来源约束的对话模式。
        raw_response: 可选原始回答，用于后续交互审计。
        sources: 本次回答携带的来源快照。
        failure_stage: 失败阶段；成功尝试为 ``None``。
        error_message: 已清洗的错误描述；不得包含密钥或完整 Prompt。
    """

    operation: str
    attempt: int
    prompt_kind: str
    raw_response: Optional[str]
    sources: tuple[RagSource, ...]
    failure_stage: Optional[str]
    error_message: Optional[str]
    prompt_digest: str = ""
    query_mode: str = "query"
    source_count: int = -1
    verified_source_count: int = -1
    missing_marker_count: int = 0
    mismatched_marker_count: int = 0
    source_marker_status: str = ""

    def __post_init__(self) -> None:
        """冻结来源集合并校验审计记录的最小结构。"""
        normalized_operation = str(self.operation or "").strip()
        if not normalized_operation:
            raise ValueError("RagAttempt.operation 不能为空")
        object.__setattr__(self, "operation", normalized_operation)
        if (
            isinstance(self.attempt, bool)
            or not isinstance(self.attempt, int)
            or self.attempt < 1
        ):
            raise ValueError("RagAttempt.attempt 必须从 1 开始")
        normalized_prompt_kind = (
            self.prompt_kind.value
            if isinstance(self.prompt_kind, RagPromptKind)
            else str(self.prompt_kind or "").strip()
        )
        allowed_prompt_kinds = {kind.value for kind in RagPromptKind}
        if normalized_prompt_kind not in allowed_prompt_kinds:
            raise ValueError(
                "RagAttempt.prompt_kind 必须是受支持的 RagPromptKind"
            )
        object.__setattr__(self, "prompt_kind", normalized_prompt_kind)
        normalized_prompt_digest = str(self.prompt_digest or "").strip()
        if normalized_prompt_digest and (
            len(normalized_prompt_digest) != 64
            or any(
                character not in "0123456789abcdef"
                for character in normalized_prompt_digest
            )
        ):
            raise ValueError("RagAttempt.prompt_digest 必须是 SHA-256 小写十六进制摘要")
        object.__setattr__(self, "prompt_digest", normalized_prompt_digest)
        normalized_query_mode = str(self.query_mode or "").strip()
        if normalized_query_mode != "query":
            raise ValueError("文档 RagAttempt.query_mode 必须显式为 query")
        object.__setattr__(self, "query_mode", normalized_query_mode)
        normalized_failure_stage = str(self.failure_stage or "").strip() or None
        normalized_error = str(self.error_message or "").strip() or None
        if normalized_failure_stage and not normalized_error:
            raise ValueError("失败的 RagAttempt 必须包含 error_message")
        if not normalized_failure_stage and normalized_error:
            raise ValueError("成功的 RagAttempt 不得包含 error_message")
        if not normalized_failure_stage and not str(self.raw_response or "").strip():
            raise ValueError("成功的 RagAttempt 必须包含 raw_response")
        object.__setattr__(self, "failure_stage", normalized_failure_stage)
        object.__setattr__(self, "error_message", normalized_error)
        object.__setattr__(self, "sources", tuple(self.sources))
        source_count = len(self.sources) if self.source_count < 0 else self.source_count
        verified_count = (
            len(self.sources)
            if self.verified_source_count < 0
            else self.verified_source_count
        )
        counts = {
            "source_count": source_count,
            "verified_source_count": verified_count,
            "missing_marker_count": self.missing_marker_count,
            "mismatched_marker_count": self.mismatched_marker_count,
        }
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counts.values()
        ):
            raise ValueError("RagAttempt 来源统计必须是非负整数")
        if verified_count > source_count:
            raise ValueError("verified_source_count 不能大于 source_count")
        if len(self.sources) != verified_count:
            raise ValueError("RagAttempt.sources 数量必须等于 verified_source_count")
        if (
            verified_count
            + self.missing_marker_count
            + self.mismatched_marker_count
            != source_count
        ):
            raise ValueError("来源验证分类数量之和必须等于 source_count")
        if source_count == 0:
            expected_marker_status = "not_returned"
        elif self.mismatched_marker_count:
            expected_marker_status = "conflict"
        elif self.missing_marker_count:
            expected_marker_status = "missing"
        else:
            expected_marker_status = "matched"
        marker_status = (
            str(self.source_marker_status or "").strip()
            or expected_marker_status
        )
        if marker_status != expected_marker_status:
            raise ValueError(
                "RagAttempt.source_marker_status 与来源验证统计不一致"
            )
        object.__setattr__(self, "source_count", source_count)
        object.__setattr__(self, "verified_source_count", verified_count)
        object.__setattr__(self, "source_marker_status", marker_status)


@dataclass(frozen=True)
class RagLifecycleEvent:
    """一次外部资源生命周期操作的供应商无关审计事件。

    模型交互和资源操作具有不同的数据含义：前者需要保存回答与来源，后者需要保存资源
    引用、执行顺序和补偿结果。本 DTO 专门描述 Context 创建、文档上传、绑定、Pin、回滚
    及失败补偿等操作，避免使用空 ``raw_response`` 伪装成模型调用。

    ``sequence_no`` 是整个 Session 内从 1 开始的全局发生顺序；``attempt`` 是同名操作内
    从 1 开始的尝试序号。成功事件不得携带失败信息，失败事件必须明确 ``failure_stage``。
    """

    sequence_no: int
    operation: str
    attempt: int
    success: bool
    external_ref: Optional[str]
    failure_stage: Optional[str]
    error_message: Optional[str]

    def __post_init__(self) -> None:
        """校验事件顺序、操作名称以及成功状态与失败字段的一致性。"""
        if (
            isinstance(self.sequence_no, bool)
            or not isinstance(self.sequence_no, int)
            or self.sequence_no < 1
        ):
            raise ValueError("RagLifecycleEvent.sequence_no 必须从 1 开始")
        if (
            isinstance(self.attempt, bool)
            or not isinstance(self.attempt, int)
            or self.attempt < 1
        ):
            raise ValueError("RagLifecycleEvent.attempt 必须从 1 开始")
        normalized_operation = str(self.operation or "").strip()
        if not normalized_operation:
            raise ValueError("RagLifecycleEvent.operation 不能为空")
        object.__setattr__(self, "operation", normalized_operation)
        if not isinstance(self.success, bool):
            raise TypeError("RagLifecycleEvent.success 必须是 bool")
        normalized_failure_stage = str(self.failure_stage or "").strip() or None
        normalized_error = str(self.error_message or "").strip() or None
        if self.success and (normalized_failure_stage or normalized_error):
            raise ValueError("成功的 RagLifecycleEvent 不得包含失败信息")
        if not self.success and not normalized_failure_stage:
            raise ValueError("失败的 RagLifecycleEvent 必须包含 failure_stage")
        if not self.success and not normalized_error:
            raise ValueError("失败的 RagLifecycleEvent 必须包含 error_message")
        object.__setattr__(self, "failure_stage", normalized_failure_stage)
        object.__setattr__(self, "error_message", normalized_error)


@dataclass(frozen=True)
class RagExecutionTrace:
    """一个隔离 RAG 会话从创建到当前时刻的完整审计快照。

    ``context_ref`` 和 ``conversation_ref`` 是不透明外部引用。字段允许为 ``None``，用于
    精确表达资源创建过程中的部分成功。``attempts`` 只保存模型调用，
    ``lifecycle_events`` 保存资源状态机；两个序列均按真实发生顺序保留。
    """

    context_name: str
    context_ref: Optional[str]
    conversation_ref: Optional[str]
    attempts: tuple[RagAttempt, ...]
    failure_stage: Optional[str]
    error_message: Optional[str]
    lifecycle_events: tuple[RagLifecycleEvent, ...] = ()

    def __post_init__(self) -> None:
        """冻结模型调用和生命周期序列，并保证上下文名称可用于审计关联。"""
        normalized_context_name = str(self.context_name or "").strip()
        if not normalized_context_name:
            raise ValueError("RagExecutionTrace.context_name 不能为空")
        object.__setattr__(self, "context_name", normalized_context_name)
        object.__setattr__(self, "attempts", tuple(self.attempts))
        object.__setattr__(self, "lifecycle_events", tuple(self.lifecycle_events))
        if any(not isinstance(attempt, RagAttempt) for attempt in self.attempts):
            raise TypeError("RagExecutionTrace.attempts 只能包含 RagAttempt")
        if any(
            not isinstance(event, RagLifecycleEvent)
            for event in self.lifecycle_events
        ):
            raise TypeError(
                "RagExecutionTrace.lifecycle_events 只能包含 RagLifecycleEvent"
            )
        normalized_failure_stage = str(self.failure_stage or "").strip() or None
        normalized_error = str(self.error_message or "").strip() or None
        if normalized_failure_stage and not normalized_error:
            raise ValueError("失败的 RagExecutionTrace 必须包含 error_message")
        if not normalized_failure_stage and normalized_error:
            raise ValueError("成功的 RagExecutionTrace 不得包含 error_message")
        object.__setattr__(self, "failure_stage", normalized_failure_stage)
        object.__setattr__(self, "error_message", normalized_error)
        sequence_numbers = tuple(
            event.sequence_no for event in self.lifecycle_events
        )
        expected_sequence = tuple(range(1, len(self.lifecycle_events) + 1))
        if sequence_numbers != expected_sequence:
            raise ValueError(
                "RagExecutionTrace.lifecycle_events 必须按从 1 开始的连续顺序排列"
            )


@dataclass(frozen=True)
class PreparedDocumentRef:
    """RAG 准备完成后可转交长期知识库的不透明文档句柄。

    ``document_ref`` 用于来源精确匹配，``external_location`` 用于后续索引登记和补偿，
    ``content_sha256`` 则来自实际上传的不可变副本，供永久知识库构造可信幂等身份。两个
    外部引用只能作为不可拆解的值传递、比较和持久化；业务层不得根据其文本格式推导
    供应商目录、HTTP 路径或资源类型。
    """

    document_ref: str
    external_location: str
    content_sha256: str

    def __post_init__(self) -> None:
        """拒绝无法被可靠审计或转交长期知识库的空引用。"""
        if not str(self.document_ref or "").strip():
            raise ValueError("PreparedDocumentRef.document_ref 不能为空")
        if not str(self.external_location or "").strip():
            raise ValueError("PreparedDocumentRef.external_location 不能为空")
        normalized_digest = str(self.content_sha256 or "").strip().casefold()
        if (
            len(normalized_digest) != 64
            or any(character not in "0123456789abcdef" for character in normalized_digest)
        ):
            raise ValueError("PreparedDocumentRef.content_sha256 必须是 SHA-256 摘要")
        object.__setattr__(self, "content_sha256", normalized_digest)


@dataclass(frozen=True)
class RagResult:
    """一次成功 RAG 调用的文本、来源、目标文档句柄和会话轨迹快照。"""

    text: str
    sources: tuple[RagSource, ...]
    prepared_document: PreparedDocumentRef
    trace: RagExecutionTrace

    def __post_init__(self) -> None:
        """拒绝空成功结果，并冻结来源集合。"""
        if not str(self.text or "").strip():
            raise ValueError("RagResult.text 不能为空")
        if not isinstance(self.prepared_document, PreparedDocumentRef):
            raise TypeError("RagResult.prepared_document 类型无效")
        object.__setattr__(self, "sources", tuple(self.sources))
        if any(not isinstance(source, RagSource) for source in self.sources):
            raise TypeError("RagResult.sources 只能包含 RagSource")
        if any(
            source.document_ref != self.prepared_document.document_ref
            for source in self.sources
        ):
            raise ValueError("RagResult.sources 必须全部属于 prepared_document")


@dataclass(frozen=True)
class CleanupResult:
    """隔离资源清理结果。

    ``already_closed=True`` 表示本次调用没有再次执行外部删除。它既可以对应此前清理成功，
    也可以对应此前清理失败但为保证幂等而不再重放删除的情况。
    """

    success: bool
    already_closed: bool
    error_message: str = ""

    def __post_init__(self) -> None:
        """保证清理结果的布尔状态和错误信息可以被审计系统无歧义解释。"""
        if not isinstance(self.success, bool):
            raise TypeError("CleanupResult.success 必须是 bool")
        if not isinstance(self.already_closed, bool):
            raise TypeError("CleanupResult.already_closed 必须是 bool")
        normalized_error = str(self.error_message or "").strip()
        if self.success and normalized_error:
            raise ValueError("成功的 CleanupResult 不得包含 error_message")
        if not self.success and not normalized_error:
            raise ValueError("失败的 CleanupResult 必须包含 error_message")
        object.__setattr__(self, "error_message", normalized_error)


class RagOperationError(RuntimeError):
    """携带完整执行轨迹的 RAG 稳定业务异常。"""

    def __init__(self, message: str, trace: RagExecutionTrace) -> None:
        """保存可审计轨迹，供业务失败路径在没有 Session 时仍能持久化。"""
        super().__init__(message)
        self.trace = trace


@runtime_checkable
class DocumentRagSession(Protocol):
    """一个文件任务独占的隔离 RAG 会话。"""

    def analyse(
        self,
        file_path: str,
        prompt: str,
        *,
        require_sources: bool = True,
        max_attempts: int = 2,
    ) -> RagResult:
        """准备目标文档并完成首次查询；Prompt 按公共契约规范化且只允许调用一次。"""
        ...

    def ask(
        self,
        prompt: str,
        *,
        prompt_kind: RagPromptKind = RagPromptKind.FOLLOW_UP,
        require_sources: bool = True,
        max_attempts: int = 1,
    ) -> RagResult:
        """在已准备会话中按显式用途和规范 Prompt 查询，不重复准备文档。"""
        ...

    @property
    def trace(self) -> RagExecutionTrace:
        """返回截至当前时刻的不可变执行轨迹快照。"""
        ...

    def close(self, *, retain_document: bool) -> CleanupResult:
        """幂等关闭隔离资源，并显式决定是否保留本次上传的全局文档。

        ``retain_document=True`` 只能用于文件分析的全部业务步骤已经成功、该文档需要转交
        永久知识库继续使用的路径。RAG 准备、模型调用或后续业务契约任一失败时，调用方
        必须传入 ``False``，由适配器在删除临时上下文之前补偿删除全局文档。

        审计失败时不得调用本方法。该限制使上层能够保留完整外部现场，并避免在审计记录
        尚未可靠落库时提前执行不可逆删除。
        """
        ...


@runtime_checkable
class DocumentRagPort(Protocol):
    """为单个业务任务创建隔离文档 RAG 会话的抽象入口。"""

    def open_isolated_session(
        self,
        *,
        context_name: str,
        conversation_name: str,
    ) -> DocumentRagSession:
        """创建隔离会话；部分成功必须由实现内部回滚后再抛出异常。"""
        ...


@runtime_checkable
class DocumentRagFactory(Protocol):
    """为单个后台任务创建并托管文档 RAG 端口的应用层工厂契约。

    返回上下文管理器而不是裸 ``DocumentRagPort``，是为了把端口背后的网络连接、认证
    会话和原子 Client 生命周期绑定到任务作用域。调用方必须使用 ``with``；退出时无论
    业务成功还是异常，具体实现都必须关闭自己创建的传输资源。
    """

    def create(self) -> AbstractContextManager[DocumentRagPort]:
        """创建一次不可跨任务、不可跨线程复用的 RAG 端口租约。"""
        ...
