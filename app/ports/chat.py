"""供应商无关的文件对话端口、DTO 与稳定异常。

本模块定义文件对话应用服务与外部对话实现之间的边界。业务层只传递不透明资源引用和
领域事件，不解析外部系统资源名称、请求路径、认证方式或响应字段。具体适配器负责把
这些稳定 DTO 映射到外部协议，测试替身位于测试目录。
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import Enum
from typing import Iterator, Optional, Protocol, Sequence, runtime_checkable


class ChatRole(str, Enum):
    """对话历史中业务层可见的消息角色。"""

    USER = "user"
    ASSISTANT = "assistant"


def _required_text(value: str, *, name: str) -> str:
    """规范化并校验端口边界必须携带的非空文本。"""
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


def _required_content(value: str, *, name: str) -> str:
    """校验模型或历史正文，同时保留原始空白字符。"""
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    if value == "":
        raise ValueError(f"{name} 不能为空")
    return value


@dataclass(frozen=True)
class ChatSessionRefs:
    """一次文件对话关联的外部上下文与对话不透明引用。"""

    context_ref: str
    conversation_ref: str

    def __post_init__(self) -> None:
        """拒绝无法持久化和清理的空引用。"""
        object.__setattr__(
            self,
            "context_ref",
            _required_text(self.context_ref, name="context_ref"),
        )
        object.__setattr__(
            self,
            "conversation_ref",
            _required_text(self.conversation_ref, name="conversation_ref"),
        )


@dataclass(frozen=True)
class ChatDocumentRef:
    """文件对话可引用的文档不透明身份。"""

    document_ref: str
    external_location: str = ""

    def __post_init__(self) -> None:
        """校验文档身份，并规范化可选外部位置。"""
        object.__setattr__(
            self,
            "document_ref",
            _required_text(self.document_ref, name="document_ref"),
        )
        object.__setattr__(
            self,
            "external_location",
            str(self.external_location or "").strip(),
        )


@dataclass(frozen=True)
class ChatMessageSnapshot:
    """外部对话历史在端口边界的不可变消息快照。"""

    role: ChatRole | str
    content: str
    timestamp_ms: Optional[int] = None
    linked_documents: tuple[ChatDocumentRef, ...] = ()

    def __post_init__(self) -> None:
        """规范化角色、内容、时间戳和关联文档快照。"""
        role_value = self.role.value if isinstance(self.role, ChatRole) else str(self.role or "").strip()
        allowed_roles = {role.value for role in ChatRole}
        if role_value not in allowed_roles:
            raise ValueError("ChatMessageSnapshot.role 不是受支持的角色")
        object.__setattr__(self, "role", role_value)
        object.__setattr__(
            self,
            "content",
            _required_content(self.content, name="content"),
        )
        if self.timestamp_ms is not None and (
            isinstance(self.timestamp_ms, bool)
            or not isinstance(self.timestamp_ms, int)
            or self.timestamp_ms < 0
        ):
            raise ValueError("ChatMessageSnapshot.timestamp_ms 必须是非负整数或 None")
        documents = tuple(self.linked_documents)
        if any(not isinstance(document, ChatDocumentRef) for document in documents):
            raise TypeError("ChatMessageSnapshot.linked_documents 只能包含 ChatDocumentRef")
        object.__setattr__(self, "linked_documents", documents)


@dataclass(frozen=True)
class ChatChunk:
    """一次流式对话返回的领域文本片段。"""

    content: str
    sequence_no: int

    def __post_init__(self) -> None:
        """拒绝空片段和不稳定序号。"""
        object.__setattr__(
            self,
            "content",
            _required_content(self.content, name="content"),
        )
        if (
            isinstance(self.sequence_no, bool)
            or not isinstance(self.sequence_no, int)
            or self.sequence_no < 1
        ):
            raise ValueError("ChatChunk.sequence_no 必须是从 1 开始的整数")


@dataclass(frozen=True)
class ChatOperationResult:
    """无需返回实体的文件对话资源操作结果。"""

    success: bool
    already_applied: bool = False
    error_message: str = ""

    def __post_init__(self) -> None:
        """保证操作结果没有互相矛盾的状态。"""
        if not isinstance(self.success, bool):
            raise TypeError("ChatOperationResult.success 必须是 bool")
        if not isinstance(self.already_applied, bool):
            raise TypeError("ChatOperationResult.already_applied 必须是 bool")
        normalized_error = str(self.error_message or "").strip()
        if self.already_applied and not self.success:
            raise ValueError("already_applied=True 时 success 必须为 True")
        if self.success and normalized_error:
            raise ValueError("成功的 ChatOperationResult 不得包含 error_message")
        if not self.success and not normalized_error:
            raise ValueError("失败的 ChatOperationResult 必须包含 error_message")
        object.__setattr__(self, "error_message", normalized_error)


class ChatPortError(RuntimeError):
    """文件对话端口稳定异常基类。"""


class ChatConversationNotFoundError(ChatPortError):
    """目标对话或上下文不存在。"""


class ChatConversationConflictError(ChatPortError):
    """目标对话存在并发、状态或幂等冲突。"""


class ChatResourceError(ChatPortError):
    """文件对话外部资源创建、绑定或清理失败。"""

    def __init__(
        self,
        message: str = "",
        *,
        resource_refs: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.resource_refs = tuple(
            _required_text(resource_ref, name="resource_ref")
            for resource_ref in resource_refs
        )


class ChatResponseError(ChatPortError):
    """外部对话实现返回了无法形成稳定业务结果的响应。"""


@runtime_checkable
class ChatConversationPort(Protocol):
    """文件对话应用服务访问外部对话实现的稳定端口。"""

    def open_conversation(
        self,
        *,
        context_name: str,
        conversation_name: str,
    ) -> ChatSessionRefs:
        """按业务名称创建或打开一个可持久化引用的对话。"""
        ...

    def attach_documents(
        self,
        session: ChatSessionRefs,
        documents: Sequence[ChatDocumentRef],
    ) -> tuple[ChatDocumentRef, ...]:
        """把一组文档引用加入目标对话，并返回当前可用文档快照。"""
        ...

    def stream_message(
        self,
        session: ChatSessionRefs,
        message: str,
        *,
        document_refs: Sequence[str] = (),
    ) -> Iterator[ChatChunk]:
        """发送用户消息并返回领域文本片段，不包含 SSE 格式。"""
        ...

    def fetch_messages(
        self,
        session: ChatSessionRefs,
    ) -> tuple[ChatMessageSnapshot, ...]:
        """读取目标对话的外部历史快照。"""
        ...

    def open_temporary_conversation(
        self,
        *,
        context_ref: str,
        conversation_name: str,
    ) -> ChatSessionRefs:
        """Create a separately tracked temporary conversation in one context."""
        ...

    def generate_temporary_reply(
        self,
        *,
        session: ChatSessionRefs,
        prompt: str,
    ) -> str:
        """Generate one non-streaming reply in a tracked temporary thread."""
        ...

    def delete_conversation(
        self,
        session: ChatSessionRefs,
    ) -> ChatOperationResult:
        """幂等删除目标对话资源。"""
        ...

    def delete_context(
        self,
        context_ref: str,
    ) -> ChatOperationResult:
        """幂等删除目标上下文资源。"""
        ...


@runtime_checkable
class ChatConversationFactory(Protocol):
    """为一次请求或后台任务创建并托管文件对话端口的工厂契约。"""

    def create(self) -> AbstractContextManager[ChatConversationPort]:
        """创建一次不可跨请求复用的文件对话端口租约。"""
        ...
