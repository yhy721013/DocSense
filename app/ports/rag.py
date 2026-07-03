"""供应商无关的文档 RAG 业务契约与执行轨迹。

本模块位于应用层边界，是业务代码与外部 RAG 实现之间的稳定契约。业务代码只允许依赖
这里定义的 DTO 和 Protocol，不应了解外部系统的资源名称、HTTP 路径、认证方式或响应
字段。具体适配器负责实现 Protocol，测试替身则位于测试目录，避免生产抽象包混入实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable


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
    """一次外部模型调用或生命周期操作的可审计记录。

    属性:
        operation: 稳定操作名，例如 ``analyse``、``ask`` 或资源回滚操作。
        attempt: 当前操作内从 1 开始的尝试序号。
        prompt_kind: 提示词用途分类，只记录类型，不记录完整 Prompt。
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

    def __post_init__(self) -> None:
        """冻结来源集合并校验审计记录的最小结构。"""
        if not str(self.operation or "").strip():
            raise ValueError("RagAttempt.operation 不能为空")
        if self.attempt < 1:
            raise ValueError("RagAttempt.attempt 必须从 1 开始")
        if not str(self.prompt_kind or "").strip():
            raise ValueError("RagAttempt.prompt_kind 不能为空")
        object.__setattr__(self, "sources", tuple(self.sources))


@dataclass(frozen=True)
class RagExecutionTrace:
    """一个隔离 RAG 会话从创建到当前时刻的完整审计快照。

    ``context_ref`` 和 ``conversation_ref`` 是不透明外部引用。字段允许为 ``None``，用于
    精确表达资源创建过程中的部分成功。``attempts`` 按真实发生顺序保存，调用方不得根据
    operation 重新排序。
    """

    context_name: str
    context_ref: Optional[str]
    conversation_ref: Optional[str]
    attempts: tuple[RagAttempt, ...]
    failure_stage: Optional[str]
    error_message: Optional[str]

    def __post_init__(self) -> None:
        """冻结尝试序列并保证上下文名称可用于审计关联。"""
        if not str(self.context_name or "").strip():
            raise ValueError("RagExecutionTrace.context_name 不能为空")
        object.__setattr__(self, "attempts", tuple(self.attempts))


@dataclass(frozen=True)
class RagResult:
    """一次成功 RAG 调用的文本、来源和会话轨迹快照。"""

    text: str
    sources: tuple[RagSource, ...]
    trace: RagExecutionTrace

    def __post_init__(self) -> None:
        """拒绝空成功结果，并冻结来源集合。"""
        if not str(self.text or "").strip():
            raise ValueError("RagResult.text 不能为空")
        object.__setattr__(self, "sources", tuple(self.sources))


@dataclass(frozen=True)
class CleanupResult:
    """隔离资源清理结果。

    ``already_closed=True`` 表示本次调用没有再次执行外部删除。它既可以对应此前清理成功，
    也可以对应此前清理失败但为保证幂等而不再重放删除的情况。
    """

    success: bool
    already_closed: bool
    error_message: str = ""


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
        """准备目标文档并完成首次查询；同一 Session 只允许调用一次。"""
        ...

    def ask(
        self,
        prompt: str,
        *,
        require_sources: bool = True,
        max_attempts: int = 1,
    ) -> RagResult:
        """在已准备会话中继续查询，不重复上传或建立文档上下文。"""
        ...

    @property
    def trace(self) -> RagExecutionTrace:
        """返回截至当前时刻的不可变执行轨迹快照。"""
        ...

    def close(self) -> CleanupResult:
        """幂等关闭隔离资源并返回稳定清理结果。"""
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
