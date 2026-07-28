"""从本地知识记录解析不可变的文件对话文档快照。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, Sequence, runtime_checkable

from app.ports import ChatDocumentRef
from app.services.chat.domain.document_candidates import (
    CHAT_ARCHITECTURE_CANDIDATE_INVALID,
    CHAT_ARCHITECTURE_CANDIDATE_NOT_FOUND,
    CHAT_ARCHITECTURE_CANDIDATE_RESOLVED,
    CHAT_ARCHITECTURE_ERROR_INVALID,
    CHAT_ARCHITECTURE_ERROR_NOT_FOUND,
    ChatArchitectureCandidates,
    ChatDocumentCandidate,
)
from app.services.core.database import DatabaseService


logger = logging.getLogger(__name__)


class ChatDocumentNotFoundError(ValueError):
    """请求的业务文件不可用于文件对话时抛出。"""

    def __init__(self, file_name: str) -> None:
        self.file_name = str(file_name or "").strip()
        super().__init__(f"文件 {self.file_name} 尚未解析，无法用于对话")


class ChatDocumentCatalogConflictError(ValueError):
    """全量文档目录无法形成唯一、确定快照时抛出。

    该异常只表达 Chat Application 内部的目录一致性问题，不改变公开 HTTP 错误结构。
    后续接入首次空数组选择时，Blueprint 仍会通过既有 ``ValueError -> HTTP 400`` 边界
    返回单一 ``error`` 字段。
    """


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

    def resolve_all_available(self) -> tuple[ResolvedChatDocument, ...]:
        """返回同一知识库读取时点内全部可用于文件对话的文档。"""
        ...


@runtime_checkable
class ChatArchitectureDocumentResolver(Protocol):
    """按 architecture 解析候选的独立能力，避免扩大旧 file Resolver 契约。"""

    def resolve_by_architecture_id(
        self,
        architecture_id: int,
    ) -> ChatArchitectureCandidates:
        """返回一次精确类别目录读取形成的有界候选结果。"""
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
                    "文件对话文档解析失败：未找到已解析文件: index=%d",
                    index,
                )
                raise ChatDocumentNotFoundError(file_name)
            resolved.append(self._resolve_record(file_name=file_name, record=record))
        logger.info(
            "文件对话文档快照解析完成: selection_mode=explicit "
            "resolved_file_count=%d",
            len(resolved),
        )
        return tuple(resolved)

    def resolve_all_available(self) -> tuple[ResolvedChatDocument, ...]:
        """通过单次目录读取构造确定性的全量文档快照。

        ``DatabaseService.list_document_records()`` 已在 SQL 层按
        ``file_name ASC, architecture_id ASC`` 排序。本方法不在循环中按文件名二次查询，
        从而避免一次全量选择混入多个数据库读取时点。任何坏记录或重复身份都会令整个
        快照失败，调用方不会看到部分成功结果。
        """
        logger.info("开始解析文件对话文档快照: selection_mode=all_available")
        records = self._knowledge_base.list_document_records()
        resolved: list[ResolvedChatDocument] = []
        seen_file_names: dict[str, int] = {}
        seen_remote_identities: dict[tuple[str, str], int] = {}

        for index, record in enumerate(records):
            if not isinstance(record, Mapping):
                logger.warning(
                    "全量文件对话目录解析被拒绝：记录类型无效: index=%d "
                    "record_type=%s",
                    index,
                    type(record).__name__,
                )
                raise ValueError("全量文件范围包含无效文档记录")

            file_name = str(record.get("file_name") or "").strip()
            if not file_name:
                logger.warning(
                    "全量文件对话目录解析被拒绝：业务文件名为空: index=%d",
                    index,
                )
                raise ValueError("全量文件范围包含无效fileName")
            if file_name in seen_file_names:
                logger.warning(
                    "全量文件对话目录解析被拒绝：业务文件名重复: "
                    "first_index=%d duplicate_index=%d",
                    seen_file_names[file_name],
                    index,
                )
                raise ChatDocumentCatalogConflictError(
                    "全量文件范围存在重复fileName，无法用于对话"
                )

            document = self._resolve_record(file_name=file_name, record=record)
            remote_identity_keys = (
                (
                    "document_ref",
                    self._normalize_document_ref(
                        document.document.document_ref
                    ),
                ),
                (
                    "external_location",
                    self._normalize_external_location(
                        document.document.external_location
                    ),
                ),
            )
            for identity_key in remote_identity_keys:
                first_index = seen_remote_identities.get(identity_key)
                if first_index is not None:
                    logger.warning(
                        "全量文件对话目录解析被拒绝：远端文档身份重复: "
                        "identity_type=%s first_index=%d duplicate_index=%d",
                        identity_key[0],
                        first_index,
                        index,
                    )
                    raise ChatDocumentCatalogConflictError(
                        "全量文件范围存在重复文档引用，无法用于对话"
                    )

            seen_file_names[file_name] = index
            for identity_key in remote_identity_keys:
                seen_remote_identities[identity_key] = index
            resolved.append(document)

        logger.info(
            "文件对话文档快照解析完成: selection_mode=all_available "
            "catalog_record_count=%d resolved_file_count=%d",
            len(records),
            len(resolved),
        )
        return tuple(resolved)

    def resolve_by_architecture_id(
        self,
        architecture_id: int,
    ) -> ChatArchitectureCandidates:
        """解析精确类别的直接文件，不把目录结果提前映射为 HTTP。

        目录为空或目录记录损坏都返回不可变 outcome，交给 Chat 受理事务结合
        ``session_created`` 裁决。数据库连接/执行类异常继续向上抛出，不能伪装成业务
        空类别；严格 metadata 解码产生的 ``ValueError`` 则属于目录损坏。
        """
        if isinstance(architecture_id, bool) or not isinstance(
            architecture_id,
            int,
        ):
            raise TypeError("architecture_id must be int")
        if architecture_id < 1 or architecture_id > 9223372036854775807:
            raise ValueError("architecture_id is out of range")

        logger.info(
            "开始解析 architecture 文件对话候选: architecture_id=%s",
            architecture_id,
        )
        try:
            records = (
                self._knowledge_base.list_document_records_by_architecture_id(
                    architecture_id
                )
            )
        except (ValueError, TypeError) as exc:
            logger.warning(
                "architecture 文件对话候选解析失败: architecture_id=%s "
                "architecture_candidate_outcome=%s error_code=%s "
                "error_type=%s",
                architecture_id,
                CHAT_ARCHITECTURE_CANDIDATE_INVALID,
                CHAT_ARCHITECTURE_ERROR_INVALID,
                type(exc).__name__,
            )
            return ChatArchitectureCandidates(
                architecture_id=architecture_id,
                resolution_outcome=CHAT_ARCHITECTURE_CANDIDATE_INVALID,
                error_code=CHAT_ARCHITECTURE_ERROR_INVALID,
            )

        if not records:
            logger.info(
                "architecture 文件对话候选解析完成: architecture_id=%s "
                "catalog_record_count=0 resolved_file_count=0 "
                "architecture_candidate_outcome=%s error_code=%s",
                architecture_id,
                CHAT_ARCHITECTURE_CANDIDATE_NOT_FOUND,
                CHAT_ARCHITECTURE_ERROR_NOT_FOUND,
            )
            return ChatArchitectureCandidates(
                architecture_id=architecture_id,
                resolution_outcome=CHAT_ARCHITECTURE_CANDIDATE_NOT_FOUND,
                error_code=CHAT_ARCHITECTURE_ERROR_NOT_FOUND,
            )

        try:
            documents = self._resolve_architecture_records(records)
        except (ChatDocumentCatalogConflictError, TypeError, ValueError) as exc:
            logger.warning(
                "architecture 文件对话候选解析失败: architecture_id=%s "
                "catalog_record_count=%d resolved_file_count=0 "
                "architecture_candidate_outcome=%s error_code=%s "
                "error_type=%s",
                architecture_id,
                len(records),
                CHAT_ARCHITECTURE_CANDIDATE_INVALID,
                CHAT_ARCHITECTURE_ERROR_INVALID,
                type(exc).__name__,
            )
            return ChatArchitectureCandidates(
                architecture_id=architecture_id,
                resolution_outcome=CHAT_ARCHITECTURE_CANDIDATE_INVALID,
                error_code=CHAT_ARCHITECTURE_ERROR_INVALID,
            )

        logger.info(
            "architecture 文件对话候选解析完成: architecture_id=%s "
            "catalog_record_count=%d resolved_file_count=%d "
            "architecture_candidate_outcome=%s error_code=",
            architecture_id,
            len(records),
            len(documents),
            CHAT_ARCHITECTURE_CANDIDATE_RESOLVED,
        )
        return ChatArchitectureCandidates(
            architecture_id=architecture_id,
            resolution_outcome=CHAT_ARCHITECTURE_CANDIDATE_RESOLVED,
            documents=documents,
        )

    def _resolve_architecture_records(
        self,
        records: Sequence[Mapping[str, Any]],
    ) -> tuple[ChatDocumentCandidate, ...]:
        """整体解析并校验类别记录，任何坏成员都令候选整体 invalid。"""
        documents: list[ChatDocumentCandidate] = []
        seen_file_names: set[str] = set()
        seen_document_refs: set[str] = set()
        seen_external_locations: set[str] = set()

        for index, record in enumerate(records):
            if not isinstance(record, Mapping):
                raise TypeError(f"architecture record {index} must be mapping")
            file_name = str(record.get("file_name") or "").strip()
            if not file_name:
                raise ValueError("architecture record file_name is empty")
            # fileNames 模式历史上允许用业务名兜底原名，不能被本需求改变；architecture
            # 首次快照则必须整体校验目录完整性，空原名不能静默变成另一个身份字段。
            original_name = str(record.get("original_name") or "").strip()
            if not original_name:
                raise ValueError("architecture record original_name is empty")
            resolved = self._resolve_record(file_name=file_name, record=record)
            document_ref = self._normalize_document_ref(
                resolved.document.document_ref
            )
            external_location = self._normalize_external_location(
                resolved.document.external_location
            )
            if file_name in seen_file_names:
                raise ChatDocumentCatalogConflictError(
                    "architecture scope contains duplicate file_name"
                )
            if document_ref in seen_document_refs:
                raise ChatDocumentCatalogConflictError(
                    "architecture scope contains duplicate document_ref"
                )
            if external_location in seen_external_locations:
                raise ChatDocumentCatalogConflictError(
                    "architecture scope contains duplicate external_location"
                )
            seen_file_names.add(file_name)
            seen_document_refs.add(document_ref)
            seen_external_locations.add(external_location)
            documents.append(
                ChatDocumentCandidate(
                    file_name=resolved.file_name,
                    original_name=resolved.original_name,
                    document_ref=document_ref,
                    external_location=external_location,
                )
            )
        return tuple(documents)

    @staticmethod
    def _resolve_record(
        *,
        file_name: str,
        record: Mapping[str, Any],
    ) -> ResolvedChatDocument:
        """把一条知识库记录转换为供应商无关快照。

        显式文件选择和全量目录选择必须共用本函数。这样任何资格修正都会同时作用于两条
        路径，不会出现“显式可聊、自动不可聊”或相反的隐性分叉。
        """
        anything_doc_id = str(record.get("anything_doc_id") or "").strip()
        document_ref = str(record.get("document_ref") or "").strip()
        if not document_ref and anything_doc_id:
            document_ref = f"document:{anything_doc_id}"
        if not document_ref:
            logger.warning(
                "文件对话文档解析失败：文件缺少文档引用",
            )
            raise ValueError(f"文件 {file_name} 缺少可用于对话的文档引用")

        external_location = str(record.get("doc_path") or "").strip()
        if not external_location and anything_doc_id:
            external_location = f"custom-documents/{anything_doc_id}.json"
        if not external_location:
            logger.warning(
                "文件对话文档解析失败：文件缺少文档位置",
            )
            raise ValueError(f"文件 {file_name} 缺少可用于对话的文档位置")

        return ResolvedChatDocument(
            file_name=file_name,
            original_name=str(record.get("original_name") or file_name).strip()
            or file_name,
            document=ChatDocumentRef(
                document_ref=document_ref,
                external_location=external_location,
            ),
        )

    @staticmethod
    def _normalize_document_ref(value: str) -> str:
        """规范化 AnythingLLM 文档引用，供目录内唯一性校验使用。"""
        return str(value or "").strip()

    @staticmethod
    def _normalize_external_location(value: str) -> str:
        """统一路径分隔符，避免同一远端位置因平台写法不同绕过判重。"""
        return str(value or "").strip().replace("\\", "/")


__all__ = [
    "ChatArchitectureDocumentResolver",
    "ChatDocumentCatalogConflictError",
    "ChatDocumentNotFoundError",
    "ChatDocumentResolver",
    "DatabaseChatDocumentResolver",
    "ResolvedChatDocument",
]
