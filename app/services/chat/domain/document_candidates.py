"""文件对话受理事务使用的不可变文档候选快照。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


_CANDIDATE_SCHEMA_VERSION = 1
_DOCUMENT_FIELDS = frozenset(
    {
        "file_name",
        "original_name",
        "document_ref",
        "external_location",
    }
)
_PAYLOAD_FIELDS = frozenset(
    {
        "schema_version",
        "explicit_documents",
        "new_session_default_documents",
    }
)


def _canonical_text(value: Any, *, name: str) -> str:
    """要求快照字段已经是非空规范文本，禁止恢复时悄悄修复脏数据。"""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} cannot be empty")
    if normalized != value:
        raise ValueError(f"{name} must be normalized")
    return normalized


@dataclass(frozen=True)
class ChatDocumentCandidate:
    """一份可跨线程、持久化命令或未来队列传递的文档候选。"""

    file_name: str
    original_name: str
    document_ref: str
    external_location: str

    def __post_init__(self) -> None:
        for name in (
            "file_name",
            "original_name",
            "document_ref",
            "external_location",
        ):
            object.__setattr__(
                self,
                name,
                _canonical_text(getattr(self, name), name=name),
            )

    def to_input_tuple(self) -> tuple[str, str, str, str]:
        """转换为现有不可变 run input 写入格式。"""
        return (
            self.file_name,
            self.original_name,
            self.document_ref,
            self.external_location,
        )

    def to_user_file_tuple(self) -> tuple[str, str]:
        """转换为 pending user 消息的文件展示快照。"""
        return self.file_name, self.original_name

    def to_payload(self) -> dict[str, str]:
        """转换为只含 JSON 基础类型的独立对象。"""
        return {
            "file_name": self.file_name,
            "original_name": self.original_name,
            "document_ref": self.document_ref,
            "external_location": self.external_location,
        }

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
        *,
        name: str = "document",
    ) -> "ChatDocumentCandidate":
        """从严格字段集合恢复候选文档。"""
        if not isinstance(payload, Mapping):
            raise TypeError(f"{name} must be a mapping")
        if set(payload) != _DOCUMENT_FIELDS:
            raise ValueError(f"{name} fields are invalid")
        return cls(
            file_name=payload["file_name"],
            original_name=payload["original_name"],
            document_ref=payload["document_ref"],
            external_location=payload["external_location"],
        )


def _freeze_documents(
    values: Sequence[ChatDocumentCandidate],
    *,
    name: str,
) -> tuple[ChatDocumentCandidate, ...]:
    """复制并校验候选序列，防止请求侧列表在受理前被并发改写。"""
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{name} must be a sequence")
    documents = tuple(values)
    for index, document in enumerate(documents):
        if not isinstance(document, ChatDocumentCandidate):
            raise TypeError(
                f"{name}[{index}] must be ChatDocumentCandidate"
            )
    return documents


def _documents_from_payload(
    value: Any,
    *,
    name: str,
) -> tuple[ChatDocumentCandidate, ...]:
    """严格恢复内部候选文档，拒绝部分损坏快照。"""
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{name} must be a sequence")
    return tuple(
        ChatDocumentCandidate.from_payload(
            raw_document,
            name=f"{name}[{index}]",
        )
        for index, raw_document in enumerate(value)
    )


@dataclass(frozen=True)
class ChatDocumentSelectionCandidates:
    """进入受理事务前冻结的显式文档与首次 session 默认文档。

    两组非空候选互斥，避免显式文件和默认全量范围被意外合并。DTO 不依赖 Resolver、
    DatabaseService 或供应商 Port，SQLite 协调器只能看到受理所需的不可变基础字段。
    """

    explicit_documents: tuple[ChatDocumentCandidate, ...] = ()
    new_session_default_documents: tuple[ChatDocumentCandidate, ...] = ()

    def __post_init__(self) -> None:
        explicit = _freeze_documents(
            self.explicit_documents,
            name="explicit_documents",
        )
        default = _freeze_documents(
            self.new_session_default_documents,
            name="new_session_default_documents",
        )
        if explicit and default:
            raise ValueError(
                "explicit_documents and new_session_default_documents "
                "cannot both be non-empty"
            )
        object.__setattr__(self, "explicit_documents", explicit)
        object.__setattr__(
            self,
            "new_session_default_documents",
            default,
        )

    def effective_documents(
        self,
        *,
        session_created: bool,
    ) -> tuple[ChatDocumentCandidate, ...]:
        """按事务内 session 创建事实选择唯一有效集合。"""
        if not isinstance(session_created, bool):
            raise TypeError("session_created must be bool")
        if self.explicit_documents:
            return self.explicit_documents
        if session_created:
            return self.new_session_default_documents
        return ()

    def to_payload(self) -> dict[str, Any]:
        """返回只含 JSON 基础类型的独立副本。"""
        return {
            "schema_version": _CANDIDATE_SCHEMA_VERSION,
            "explicit_documents": [
                document.to_payload()
                for document in self.explicit_documents
            ],
            "new_session_default_documents": [
                document.to_payload()
                for document in self.new_session_default_documents
            ],
        }

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
    ) -> "ChatDocumentSelectionCandidates":
        """从严格 Schema v1 恢复候选，未知版本或字段一律失败关闭。"""
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        if set(payload) != _PAYLOAD_FIELDS:
            raise ValueError("document candidate payload fields are invalid")
        schema_version = payload["schema_version"]
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != _CANDIDATE_SCHEMA_VERSION
        ):
            raise ValueError("unsupported document candidate schema_version")
        return cls(
            explicit_documents=_documents_from_payload(
                payload["explicit_documents"],
                name="explicit_documents",
            ),
            new_session_default_documents=_documents_from_payload(
                payload["new_session_default_documents"],
                name="new_session_default_documents",
            ),
        )


__all__ = [
    "ChatDocumentCandidate",
    "ChatDocumentSelectionCandidates",
]
