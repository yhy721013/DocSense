"""Resolve immutable chat document snapshots from the local knowledge record."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

from app.ports import ChatDocumentRef
from app.services.core.database import DatabaseService


class ChatDocumentNotFoundError(ValueError):
    """Raised when a requested business file is unavailable for file chat."""

    def __init__(self, file_name: str) -> None:
        self.file_name = str(file_name or "").strip()
        super().__init__(f"文件 {self.file_name} 尚未解析，无法用于对话")


@dataclass(frozen=True)
class ResolvedChatDocument:
    """A request-time snapshot of one document available to the chat adapter."""

    file_name: str
    original_name: str
    document: ChatDocumentRef


@runtime_checkable
class ChatDocumentResolver(Protocol):
    """Resolve business file names without exposing a knowledge DB to routes."""

    def resolve_many(
        self,
        file_names: Sequence[str],
    ) -> tuple[ResolvedChatDocument, ...]:
        ...


class DatabaseChatDocumentResolver(ChatDocumentResolver):
    """Adapter from the local knowledge record to supplier-neutral document DTOs."""

    def __init__(self, knowledge_base: DatabaseService) -> None:
        self._knowledge_base = knowledge_base

    def resolve_many(
        self,
        file_names: Sequence[str],
    ) -> tuple[ResolvedChatDocument, ...]:
        resolved: list[ResolvedChatDocument] = []
        for raw_file_name in file_names:
            file_name = str(raw_file_name or "").strip()
            if not file_name:
                raise ValueError("fileNames中包含无效文件名")
            record = self._knowledge_base.get_document_record(file_name)
            if not record:
                raise ChatDocumentNotFoundError(file_name)
            anything_doc_id = str(record.get("anything_doc_id") or "").strip()
            document_ref = str(record.get("document_ref") or "").strip()
            if not document_ref and anything_doc_id:
                document_ref = f"document:{anything_doc_id}"
            if not document_ref:
                raise ValueError(f"文件 {file_name} 缺少可用于对话的文档引用")
            external_location = str(record.get("doc_path") or "").strip()
            if not external_location and anything_doc_id:
                external_location = f"custom-documents/{anything_doc_id}.json"
            if not external_location:
                raise ValueError(f"文件 {file_name} 缺少可用于对话的文档位置")
            resolved.append(
                ResolvedChatDocument(
                    file_name=file_name,
                    original_name=str(
                        record.get("original_name") or file_name
                    ).strip()
                    or file_name,
                    document=ChatDocumentRef(
                        document_ref=document_ref,
                        external_location=external_location,
                    ),
                )
            )
        return tuple(resolved)


__all__ = [
    "ChatDocumentNotFoundError",
    "ChatDocumentResolver",
    "DatabaseChatDocumentResolver",
    "ResolvedChatDocument",
]
