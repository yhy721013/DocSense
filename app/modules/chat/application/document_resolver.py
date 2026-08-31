"""Chat 用例读取文档目录所依赖的稳定抽象。

本模块只定义应用层输入、输出和业务异常，不知道知识库使用 SQLite、MySQL 还是
其他存储。具体的 DocSense 知识记录读取与 AnythingLLM 文档引用转换由
``adapters.knowledge_documents`` 实现。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

from app.modules.chat.ports import ChatDocumentRef
from app.modules.chat.domain.document_candidates import ChatArchitectureCandidates


class ChatDocumentNotFoundError(ValueError):
    """请求的业务文件不可用于文件对话时抛出。"""

    def __init__(self, file_name: str) -> None:
        self.file_name = str(file_name or "").strip()
        super().__init__(f"文件 {self.file_name} 尚未解析，无法用于对话")


class ChatDocumentCatalogConflictError(ValueError):
    """文档目录无法形成唯一且确定的不可变快照时抛出。"""


@dataclass(frozen=True)
class ResolvedChatDocument:
    """请求时刻可供对话适配器使用的一份文档快照。"""

    file_name: str
    original_name: str
    document: ChatDocumentRef
    structured_source_key: str = ""


@runtime_checkable
class ChatDocumentResolver(Protocol):
    """按业务文件身份读取文档快照的应用端口。"""

    def resolve_many(
        self,
        file_names: Sequence[str],
    ) -> tuple[ResolvedChatDocument, ...]:
        ...

    def resolve_all_available(self) -> tuple[ResolvedChatDocument, ...]:
        """返回同一目录读取时点内全部可用于文件对话的文档。"""
        ...


@runtime_checkable
class ChatArchitectureDocumentResolver(Protocol):
    """按知识谱系类别读取直接文件候选的独立应用端口。"""

    def resolve_by_architecture_id(
        self,
        architecture_id: int,
    ) -> ChatArchitectureCandidates:
        """返回一次精确类别目录读取形成的有界候选结果。"""
        ...


__all__ = [
    "ChatArchitectureDocumentResolver",
    "ChatDocumentCatalogConflictError",
    "ChatDocumentNotFoundError",
    "ChatDocumentResolver",
    "ResolvedChatDocument",
]
