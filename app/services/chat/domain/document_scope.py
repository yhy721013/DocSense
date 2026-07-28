"""文件对话 Requested、Active 与 Effective Scope 的纯领域规则。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.services.chat.domain.document_candidates import ChatDocumentCandidate


CHAT_SCOPE_SOURCE_AUTOMATIC_INITIAL = "automatic_initial"
CHAT_SCOPE_SOURCE_EXPLICIT = "explicit"
CHAT_SCOPE_SOURCE_MODES = frozenset(
    {
        CHAT_SCOPE_SOURCE_AUTOMATIC_INITIAL,
        CHAT_SCOPE_SOURCE_EXPLICIT,
    }
)

CHAT_SCOPE_SELECTION_AUTOMATIC_INITIAL = "automatic_initial"
CHAT_SCOPE_SELECTION_EXPLICIT = "explicit"
CHAT_SCOPE_SELECTION_ACTIVE_REUSE = "active_scope_reuse"
CHAT_SCOPE_SELECTION_MODES = frozenset(
    {
        CHAT_SCOPE_SELECTION_AUTOMATIC_INITIAL,
        CHAT_SCOPE_SELECTION_EXPLICIT,
        CHAT_SCOPE_SELECTION_ACTIVE_REUSE,
    }
)

_DECISION_SCHEMA_VERSION = 1
_DECISION_FIELDS = frozenset(
    {
        "schema_version",
        "selection_mode",
        "creates_scope_revision",
        "scope_source_mode",
        "requested_documents",
        "effective_documents",
    }
)


def _required_text(value: Any, *, name: str) -> str:
    """要求领域身份已经完成规范化，禁止 DTO 静默修复空白。"""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} cannot be empty")
    if normalized != value:
        raise ValueError(f"{name} must be normalized")
    return normalized


def _optional_text(value: Any, *, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str or None")
    normalized = value.strip()
    if normalized != value:
        raise ValueError(f"{name} must be normalized")
    return normalized


def _freeze_documents(
    values: Sequence[ChatDocumentCandidate],
    *,
    name: str,
) -> tuple[ChatDocumentCandidate, ...]:
    """冻结有序文档集合，并在领域边界拒绝重复业务或远端身份。"""
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError(f"{name} must be a sequence")
    documents = tuple(values)
    file_names: set[str] = set()
    document_refs: set[str] = set()
    external_locations: set[str] = set()
    for index, document in enumerate(documents):
        if not isinstance(document, ChatDocumentCandidate):
            raise TypeError(
                f"{name}[{index}] must be ChatDocumentCandidate"
            )
        normalized_location = document.external_location.replace("\\", "/")
        if document.file_name in file_names:
            raise ValueError(f"{name} contains duplicate file_name")
        if document.document_ref in document_refs:
            raise ValueError(f"{name} contains duplicate document_ref")
        if normalized_location in external_locations:
            raise ValueError(f"{name} contains duplicate external_location")
        file_names.add(document.file_name)
        document_refs.add(document.document_ref)
        external_locations.add(normalized_location)
    return documents


def _documents_from_payload(
    value: Any,
    *,
    name: str,
) -> tuple[ChatDocumentCandidate, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{name} must be a sequence")
    return tuple(
        ChatDocumentCandidate.from_payload(
            item,
            name=f"{name}[{index}]",
        )
        for index, item in enumerate(value)
    )


@dataclass(frozen=True)
class ChatRequestedFile:
    """前端本轮明确请求、允许进入历史展示的一份文件。"""

    file_name: str
    original_name: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "file_name",
            _required_text(self.file_name, name="file_name"),
        )
        object.__setattr__(
            self,
            "original_name",
            _required_text(self.original_name, name="original_name"),
        )


@dataclass(frozen=True)
class ChatScopeRevision:
    """一份不可变的会话活动范围版本。"""

    scope_revision_id: str
    chat_id: str
    source_mode: str
    source_run_id: str
    members: tuple[ChatDocumentCandidate, ...]
    created_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "scope_revision_id",
            _required_text(
                self.scope_revision_id,
                name="scope_revision_id",
            ),
        )
        object.__setattr__(
            self,
            "chat_id",
            _required_text(self.chat_id, name="chat_id"),
        )
        object.__setattr__(
            self,
            "source_run_id",
            _required_text(self.source_run_id, name="source_run_id"),
        )
        source_mode = _required_text(
            self.source_mode,
            name="source_mode",
        )
        if source_mode not in CHAT_SCOPE_SOURCE_MODES:
            raise ValueError("source_mode is not supported")
        object.__setattr__(self, "source_mode", source_mode)
        object.__setattr__(
            self,
            "members",
            _freeze_documents(self.members, name="members"),
        )
        object.__setattr__(
            self,
            "created_at",
            _required_text(self.created_at, name="created_at"),
        )


@dataclass(frozen=True)
class ChatScopeHead:
    """一个会话当前指向的活动范围版本。"""

    chat_id: str
    scope_revision_id: str
    updated_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "chat_id",
            _required_text(self.chat_id, name="chat_id"),
        )
        object.__setattr__(
            self,
            "scope_revision_id",
            _required_text(
                self.scope_revision_id,
                name="scope_revision_id",
            ),
        )
        object.__setattr__(
            self,
            "updated_at",
            _required_text(self.updated_at, name="updated_at"),
        )


@dataclass(frozen=True)
class ChatScopeDecision:
    """受理事务根据 session/head 事实得出的唯一范围决策。"""

    selection_mode: str
    requested_documents: tuple[ChatDocumentCandidate, ...]
    effective_documents: tuple[ChatDocumentCandidate, ...]
    creates_scope_revision: bool
    scope_source_mode: str = ""

    def __post_init__(self) -> None:
        selection_mode = _required_text(
            self.selection_mode,
            name="selection_mode",
        )
        if selection_mode not in CHAT_SCOPE_SELECTION_MODES:
            raise ValueError("selection_mode is not supported")
        if not isinstance(self.creates_scope_revision, bool):
            raise TypeError("creates_scope_revision must be bool")
        requested = _freeze_documents(
            self.requested_documents,
            name="requested_documents",
        )
        effective = _freeze_documents(
            self.effective_documents,
            name="effective_documents",
        )
        source_mode = _optional_text(
            self.scope_source_mode,
            name="scope_source_mode",
        )

        if selection_mode == CHAT_SCOPE_SELECTION_EXPLICIT:
            if not requested or effective != requested:
                raise ValueError(
                    "explicit selection must use requested documents"
                )
            if not self.creates_scope_revision:
                raise ValueError(
                    "explicit selection must create scope revision"
                )
            if source_mode != CHAT_SCOPE_SOURCE_EXPLICIT:
                raise ValueError("explicit selection source_mode is invalid")
        elif selection_mode == CHAT_SCOPE_SELECTION_AUTOMATIC_INITIAL:
            if requested:
                raise ValueError(
                    "automatic initial selection cannot have requested files"
                )
            if not self.creates_scope_revision:
                raise ValueError(
                    "automatic initial selection must create scope revision"
                )
            if source_mode != CHAT_SCOPE_SOURCE_AUTOMATIC_INITIAL:
                raise ValueError(
                    "automatic initial selection source_mode is invalid"
                )
        else:
            if requested:
                raise ValueError(
                    "active scope reuse cannot have requested files"
                )
            if self.creates_scope_revision or source_mode:
                raise ValueError(
                    "active scope reuse cannot create scope revision"
                )

        object.__setattr__(self, "selection_mode", selection_mode)
        object.__setattr__(self, "requested_documents", requested)
        object.__setattr__(self, "effective_documents", effective)
        object.__setattr__(self, "scope_source_mode", source_mode)

    @property
    def requested_files(self) -> tuple[ChatRequestedFile, ...]:
        """返回只允许用于历史展示的前端请求文件。"""
        return tuple(
            ChatRequestedFile(
                file_name=document.file_name,
                original_name=document.original_name,
            )
            for document in self.requested_documents
        )

    def to_payload(self) -> dict[str, Any]:
        """转换为只含 JSON 基础类型的严格内部 Schema。"""
        return {
            "schema_version": _DECISION_SCHEMA_VERSION,
            "selection_mode": self.selection_mode,
            "creates_scope_revision": self.creates_scope_revision,
            "scope_source_mode": self.scope_source_mode,
            "requested_documents": [
                document.to_payload()
                for document in self.requested_documents
            ],
            "effective_documents": [
                document.to_payload()
                for document in self.effective_documents
            ],
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ChatScopeDecision":
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        if set(payload) != _DECISION_FIELDS:
            raise ValueError("scope decision payload fields are invalid")
        schema_version = payload["schema_version"]
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != _DECISION_SCHEMA_VERSION
        ):
            raise ValueError("unsupported scope decision schema_version")
        return cls(
            selection_mode=payload["selection_mode"],
            requested_documents=_documents_from_payload(
                payload["requested_documents"],
                name="requested_documents",
            ),
            effective_documents=_documents_from_payload(
                payload["effective_documents"],
                name="effective_documents",
            ),
            creates_scope_revision=payload["creates_scope_revision"],
            scope_source_mode=payload["scope_source_mode"],
        )


def decide_chat_document_scope(
    *,
    session_created: bool,
    requested_documents: Sequence[ChatDocumentCandidate],
    automatic_initial_documents: Sequence[ChatDocumentCandidate],
    current_scope_documents: Sequence[ChatDocumentCandidate] | None,
) -> ChatScopeDecision:
    """根据事务内 session/head 事实选择本轮唯一有效范围。

    事务外的 session 探测只能减少目录读取，不能代替本函数的最终判断。已有 session
    缺少 Scope Head 时必须失败关闭，不能扫描知识库或读取 Workspace bindings 猜测范围。
    """
    if not isinstance(session_created, bool):
        raise TypeError("session_created must be bool")
    requested = _freeze_documents(
        requested_documents,
        name="requested_documents",
    )
    automatic = _freeze_documents(
        automatic_initial_documents,
        name="automatic_initial_documents",
    )
    current = (
        None
        if current_scope_documents is None
        else _freeze_documents(
            current_scope_documents,
            name="current_scope_documents",
        )
    )

    if requested:
        if automatic:
            raise ValueError(
                "explicit request cannot include automatic initial candidates"
            )
        return ChatScopeDecision(
            selection_mode=CHAT_SCOPE_SELECTION_EXPLICIT,
            requested_documents=requested,
            effective_documents=requested,
            creates_scope_revision=True,
            scope_source_mode=CHAT_SCOPE_SOURCE_EXPLICIT,
        )
    if session_created:
        if current is not None:
            raise ValueError(
                "new session cannot already have current scope documents"
            )
        return ChatScopeDecision(
            selection_mode=CHAT_SCOPE_SELECTION_AUTOMATIC_INITIAL,
            requested_documents=(),
            effective_documents=automatic,
            creates_scope_revision=True,
            scope_source_mode=CHAT_SCOPE_SOURCE_AUTOMATIC_INITIAL,
        )
    if current is None:
        raise ValueError("existing chat session is missing active scope")
    return ChatScopeDecision(
        selection_mode=CHAT_SCOPE_SELECTION_ACTIVE_REUSE,
        requested_documents=(),
        effective_documents=current,
        creates_scope_revision=False,
    )


__all__ = [
    "CHAT_SCOPE_SELECTION_ACTIVE_REUSE",
    "CHAT_SCOPE_SELECTION_AUTOMATIC_INITIAL",
    "CHAT_SCOPE_SELECTION_EXPLICIT",
    "CHAT_SCOPE_SELECTION_MODES",
    "CHAT_SCOPE_SOURCE_AUTOMATIC_INITIAL",
    "CHAT_SCOPE_SOURCE_EXPLICIT",
    "CHAT_SCOPE_SOURCE_MODES",
    "ChatRequestedFile",
    "ChatScopeDecision",
    "ChatScopeHead",
    "ChatScopeRevision",
    "decide_chat_document_scope",
]
