"""文件对话受理事务使用的不可变文档候选快照。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.services.chat.domain.limits import MAX_CHAT_ARCHITECTURE_ID

_CANDIDATE_SCHEMA_VERSION = 2
_LEGACY_CANDIDATE_SCHEMA_VERSION = 1

CHAT_ARCHITECTURE_CANDIDATE_RESOLVED = "resolved"
CHAT_ARCHITECTURE_CANDIDATE_NOT_FOUND = "not_found"
CHAT_ARCHITECTURE_CANDIDATE_INVALID = "invalid"
CHAT_ARCHITECTURE_CANDIDATE_OUTCOMES = frozenset(
    {
        CHAT_ARCHITECTURE_CANDIDATE_RESOLVED,
        CHAT_ARCHITECTURE_CANDIDATE_NOT_FOUND,
        CHAT_ARCHITECTURE_CANDIDATE_INVALID,
    }
)

CHAT_ARCHITECTURE_ERROR_NOT_FOUND = "architecture_catalog_not_found"
CHAT_ARCHITECTURE_ERROR_INVALID = "architecture_catalog_invalid"
_DOCUMENT_FIELDS = frozenset(
    {
        "file_name",
        "original_name",
        "document_ref",
        "external_location",
    }
)
_PAYLOAD_V1_FIELDS = frozenset(
    {
        "schema_version",
        "explicit_documents",
        "new_session_default_documents",
    }
)
_PAYLOAD_V2_FIELDS = frozenset(
    {
        "schema_version",
        "explicit_documents",
        "new_session_default_documents",
        "architecture_candidates",
    }
)
_ARCHITECTURE_CANDIDATE_FIELDS = frozenset(
    {
        "architecture_id",
        "resolution_outcome",
        "documents",
        "error_code",
    }
)


def _architecture_id(value: Any, *, name: str) -> int:
    """校验已经由 Web 层规范化的 architecture ID。"""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int")
    if value < 1 or value > MAX_CHAT_ARCHITECTURE_ID:
        raise ValueError(f"{name} is out of range")
    return value


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
class ChatArchitectureCandidates:
    """一次精确类别目录读取形成的不可变、有界候选结果。

    ``not_found`` 与 ``invalid`` 不在 Resolver 中直接映射 HTTP。Coordinator 必须先在
    写事务内判断会话是否已经由并发请求创建；已有同 ID 会话应忽略当前候选结果并复用
    已冻结 Scope，避免事务外目录读取产生错误的 404/400。
    """

    architecture_id: int
    resolution_outcome: str
    documents: tuple[ChatDocumentCandidate, ...] = ()
    error_code: str = ""

    def __post_init__(self) -> None:
        architecture_id = _architecture_id(
            self.architecture_id,
            name="architecture_id",
        )
        outcome = _canonical_text(
            self.resolution_outcome,
            name="resolution_outcome",
        )
        if outcome not in CHAT_ARCHITECTURE_CANDIDATE_OUTCOMES:
            raise ValueError("resolution_outcome is not supported")
        documents = _freeze_documents(self.documents, name="documents")
        if not isinstance(self.error_code, str):
            raise TypeError("error_code must be str")
        error_code = self.error_code.strip()
        if error_code != self.error_code:
            raise ValueError("error_code must be normalized")

        if outcome == CHAT_ARCHITECTURE_CANDIDATE_RESOLVED:
            if not documents:
                raise ValueError("resolved architecture candidates cannot be empty")
            if error_code:
                raise ValueError("resolved architecture candidates cannot have error_code")
        else:
            if documents:
                raise ValueError("failed architecture candidates cannot contain documents")
            expected_error = (
                CHAT_ARCHITECTURE_ERROR_NOT_FOUND
                if outcome == CHAT_ARCHITECTURE_CANDIDATE_NOT_FOUND
                else CHAT_ARCHITECTURE_ERROR_INVALID
            )
            if error_code != expected_error:
                raise ValueError("architecture candidate error_code is invalid")

        object.__setattr__(self, "architecture_id", architecture_id)
        object.__setattr__(self, "resolution_outcome", outcome)
        object.__setattr__(self, "documents", documents)
        object.__setattr__(self, "error_code", error_code)

    def to_payload(self) -> dict[str, Any]:
        """生成可进入命令或未来可靠队列的基础类型快照。"""
        return {
            "architecture_id": self.architecture_id,
            "resolution_outcome": self.resolution_outcome,
            "documents": [document.to_payload() for document in self.documents],
            "error_code": self.error_code,
        }

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
    ) -> "ChatArchitectureCandidates":
        if not isinstance(payload, Mapping):
            raise TypeError("architecture_candidates must be a mapping")
        if set(payload) != _ARCHITECTURE_CANDIDATE_FIELDS:
            raise ValueError("architecture candidate fields are invalid")
        return cls(
            architecture_id=payload["architecture_id"],
            resolution_outcome=payload["resolution_outcome"],
            documents=_documents_from_payload(
                payload["documents"],
                name="architecture_documents",
            ),
            error_code=payload["error_code"],
        )


@dataclass(frozen=True)
class ChatDocumentSelectionCandidates:
    """进入受理事务前冻结的显式文档与首次 session 默认文档。

    两组非空候选互斥，避免显式文件和默认全量范围被意外合并。DTO 不依赖 Resolver、
    DatabaseService 或供应商 Port，SQLite 协调器只能看到受理所需的不可变基础字段。
    """

    explicit_documents: tuple[ChatDocumentCandidate, ...] = ()
    new_session_default_documents: tuple[ChatDocumentCandidate, ...] = ()
    architecture_candidates: ChatArchitectureCandidates | None = None

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
        architecture = self.architecture_candidates
        if architecture is not None and not isinstance(
            architecture,
            ChatArchitectureCandidates,
        ):
            raise TypeError(
                "architecture_candidates must be ChatArchitectureCandidates or None"
            )
        if architecture is not None and (explicit or default):
            raise ValueError(
                "file candidates and architecture_candidates are mutually exclusive"
            )
        object.__setattr__(self, "explicit_documents", explicit)
        object.__setattr__(
            self,
            "new_session_default_documents",
            default,
        )
        object.__setattr__(self, "architecture_candidates", architecture)

    def effective_documents(
        self,
        *,
        session_created: bool,
    ) -> tuple[ChatDocumentCandidate, ...]:
        """按事务内 session 创建事实选择唯一有效集合。"""
        if not isinstance(session_created, bool):
            raise TypeError("session_created must be bool")
        if self.architecture_candidates is not None:
            if (
                session_created
                and self.architecture_candidates.resolution_outcome
                == CHAT_ARCHITECTURE_CANDIDATE_RESOLVED
            ):
                return self.architecture_candidates.documents
            return ()
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
            "architecture_candidates": (
                None
                if self.architecture_candidates is None
                else self.architecture_candidates.to_payload()
            ),
        }

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
    ) -> "ChatDocumentSelectionCandidates":
        """从严格 Schema v1 恢复候选，未知版本或字段一律失败关闭。"""
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        if "schema_version" not in payload:
            raise ValueError("document candidate payload fields are invalid")
        schema_version = payload["schema_version"]
        if isinstance(schema_version, bool) or not isinstance(schema_version, int):
            raise ValueError("unsupported document candidate schema_version")
        if schema_version == _LEGACY_CANDIDATE_SCHEMA_VERSION:
            if set(payload) != _PAYLOAD_V1_FIELDS:
                raise ValueError("document candidate payload fields are invalid")
            architecture_candidates = None
        elif schema_version == _CANDIDATE_SCHEMA_VERSION:
            if set(payload) != _PAYLOAD_V2_FIELDS:
                raise ValueError("document candidate payload fields are invalid")
            raw_architecture = payload["architecture_candidates"]
            architecture_candidates = (
                None
                if raw_architecture is None
                else ChatArchitectureCandidates.from_payload(raw_architecture)
            )
        else:
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
            architecture_candidates=architecture_candidates,
        )


__all__ = [
    "CHAT_ARCHITECTURE_CANDIDATE_INVALID",
    "CHAT_ARCHITECTURE_CANDIDATE_NOT_FOUND",
    "CHAT_ARCHITECTURE_CANDIDATE_OUTCOMES",
    "CHAT_ARCHITECTURE_CANDIDATE_RESOLVED",
    "CHAT_ARCHITECTURE_ERROR_INVALID",
    "CHAT_ARCHITECTURE_ERROR_NOT_FOUND",
    "ChatArchitectureCandidates",
    "ChatDocumentCandidate",
    "ChatDocumentSelectionCandidates",
]
