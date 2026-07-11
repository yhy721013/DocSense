"""从本地知识记录解析不可变的文件对话文档快照。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable

from app.ports import ChatDocumentRef
from app.services.core.database import DatabaseService


logger = logging.getLogger(__name__)


class ChatDocumentNotFoundError(ValueError):
    """请求的业务文件不可用于文件对话时抛出。"""

    def __init__(self, file_name: str) -> None:
        self.file_name = str(file_name or "").strip()
        super().__init__(f"文件 {self.file_name} 尚未解析，无法用于对话")


@dataclass(frozen=True)
class ResolvedChatDocument:
    """请求时刻可供对话适配器使用的一份文档快照。"""

    file_name: str
    original_name: str
    document: ChatDocumentRef


@runtime_checkable
class ChatDocumentResolver(Protocol):
    """解析业务文件名，但不向路由层暴露知识库。"""

    def resolve_many(
        self,
        file_names: Sequence[str],
    ) -> tuple[ResolvedChatDocument, ...]:
        ...


class DatabaseChatDocumentResolver(ChatDocumentResolver):
    """将本地知识记录转换为供应商无关文档 DTO 的适配器。"""

    def __init__(self, knowledge_base: DatabaseService) -> None:
        self._knowledge_base = knowledge_base

    def resolve_many(
        self,
        file_names: Sequence[str],
    ) -> tuple[ResolvedChatDocument, ...]:
        logger.info(
            "开始解析文件对话文档快照: requested_file_count=%d",
            len(file_names),
        )
        resolved: list[ResolvedChatDocument] = []
        for index, raw_file_name in enumerate(file_names):
            file_name = str(raw_file_name or "").strip()
            if not file_name:
                logger.warning(
                    "文件对话文档解析被拒绝：文件名为空: index=%d",
                    index,
                )
                raise ValueError("fileNames中包含无效文件名")
            record = self._knowledge_base.get_document_record(file_name)
            if not record:
                logger.warning(
                    "文件对话文档解析失败：未找到已解析文件: file_name=%s",
                    file_name,
                )
                raise ChatDocumentNotFoundError(file_name)
            anything_doc_id = str(record.get("anything_doc_id") or "").strip()
            document_ref = str(record.get("document_ref") or "").strip()
            if not document_ref and anything_doc_id:
                document_ref = f"document:{anything_doc_id}"
            if not document_ref:
                logger.warning(
                    "文件对话文档解析失败：文件缺少文档引用: file_name=%s",
                    file_name,
                )
                raise ValueError(f"文件 {file_name} 缺少可用于对话的文档引用")
            external_location = str(record.get("doc_path") or "").strip()
            if not external_location and anything_doc_id:
                external_location = f"custom-documents/{anything_doc_id}.json"
            if not external_location:
                logger.warning(
                    "文件对话文档解析失败：文件缺少文档位置: file_name=%s",
                    file_name,
                )
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
        logger.info(
            "文件对话文档快照解析完成: resolved_file_count=%d",
            len(resolved),
        )
        return tuple(resolved)


__all__ = [
    "ChatDocumentNotFoundError",
    "ChatDocumentResolver",
    "DatabaseChatDocumentResolver",
    "ResolvedChatDocument",
]
