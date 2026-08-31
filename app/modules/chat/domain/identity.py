"""Chat 公开身份与内部 Conversation 身份的领域边界。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID

from app.modules.chat.domain.limits import MAX_CHAT_ARCHITECTURE_ID


IDENTITY_KIND_FILE = "file"
IDENTITY_KIND_WEAPONRY = "weaponry"
IDENTITY_KINDS = frozenset({IDENTITY_KIND_FILE, IDENTITY_KIND_WEAPONRY})

MAX_JAVASCRIPT_SAFE_INTEGER = MAX_CHAT_ARCHITECTURE_ID
_POSITIVE_DECIMAL = re.compile(r"^[1-9][0-9]*$")


def _positive_int(value: int, *, name: str, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} exceeds the JavaScript safe integer limit")
    return value


def require_conversation_id(value: str) -> str:
    """要求内部 Conversation ID 为规范小写 UUID 文本。"""

    normalized = str(value or "").strip()
    try:
        parsed = UUID(normalized)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("conversation_id must be a canonical UUID") from exc
    canonical = str(parsed)
    if normalized != canonical:
        raise ValueError("conversation_id must be a canonical UUID")
    return canonical


@runtime_checkable
class ConversationIdentity(Protocol):
    """两类公开身份共享的最小只读协议。"""

    @property
    def identity_kind(self) -> str:
        ...

    @property
    def identity_key(self) -> str:
        """返回仅供持久化准入竞争使用的规范化键。"""
        ...


@dataclass(frozen=True)
class FileChatIdentity:
    """既有文件对话的公开单 ID 身份。"""

    chat_id: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "chat_id",
            _positive_int(self.chat_id, name="chat_id"),
        )

    @property
    def identity_kind(self) -> str:
        return IDENTITY_KIND_FILE

    @property
    def identity_key(self) -> str:
        return f"{IDENTITY_KIND_FILE}:{self.chat_id}"


@dataclass(frozen=True)
class WeaponryChatIdentity:
    """知识谱系对话的可信 DocSense 用户与类别复合身份。"""

    user_id: int
    architecture_id: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "user_id",
            _positive_int(
                self.user_id,
                name="user_id",
                maximum=MAX_JAVASCRIPT_SAFE_INTEGER,
            ),
        )
        object.__setattr__(
            self,
            "architecture_id",
            _positive_int(
                self.architecture_id,
                name="architecture_id",
                maximum=MAX_JAVASCRIPT_SAFE_INTEGER,
            ),
        )

    @property
    def identity_kind(self) -> str:
        return IDENTITY_KIND_WEAPONRY

    @property
    def identity_key(self) -> str:
        return f"{IDENTITY_KIND_WEAPONRY}:{self.user_id}:{self.architecture_id}"


@dataclass(frozen=True)
class ConversationIdentityBinding:
    """内部 Conversation 与某一代公开身份的不可变绑定。"""

    conversation_id: str
    identity_kind: str
    chat_id: int | None
    user_id: int | None
    architecture_id: int | None
    active: bool
    created_at: str
    released_at: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "conversation_id",
            require_conversation_id(self.conversation_id),
        )
        if self.identity_kind not in IDENTITY_KINDS:
            raise ValueError("identity_kind is unsupported")
        if not isinstance(self.active, bool):
            raise TypeError("active must be bool")
        if self.identity_kind == IDENTITY_KIND_FILE:
            _positive_int(self.chat_id, name="chat_id")
            if self.user_id is not None or self.architecture_id is not None:
                raise ValueError("file identity cannot contain weaponry fields")
            if not self.active:
                # File identity 是全世代墓碑；删除后仍保持占用，不能被释放复用。
                raise ValueError("file identity must remain active for all generations")
        else:
            if self.chat_id is not None:
                raise ValueError("weaponry identity cannot contain chat_id")
            _positive_int(
                self.user_id,
                name="user_id",
                maximum=MAX_JAVASCRIPT_SAFE_INTEGER,
            )
            _positive_int(
                self.architecture_id,
                name="architecture_id",
                maximum=MAX_JAVASCRIPT_SAFE_INTEGER,
            )
        if not str(self.created_at or "").strip():
            raise ValueError("created_at cannot be empty")
        if self.active and str(self.released_at or "").strip():
            raise ValueError("active identity cannot have released_at")
        if not self.active and not str(self.released_at or "").strip():
            raise ValueError("released weaponry identity requires released_at")


def parse_identity_key(value: str) -> FileChatIdentity | WeaponryChatIdentity:
    """严格还原数据库 Guard 中的规范化身份键。"""

    normalized = str(value or "")
    parts = normalized.split(":")
    if len(parts) == 2 and parts[0] == IDENTITY_KIND_FILE:
        if not _POSITIVE_DECIMAL.fullmatch(parts[1]):
            raise ValueError("invalid file identity key")
        return FileChatIdentity(chat_id=int(parts[1]))
    if len(parts) == 3 and parts[0] == IDENTITY_KIND_WEAPONRY:
        if not _POSITIVE_DECIMAL.fullmatch(parts[1]) or not _POSITIVE_DECIMAL.fullmatch(
            parts[2]
        ):
            raise ValueError("invalid weaponry identity key")
        return WeaponryChatIdentity(
            user_id=int(parts[1]),
            architecture_id=int(parts[2]),
        )
    raise ValueError("unsupported conversation identity key")


__all__ = [
    "ConversationIdentity",
    "ConversationIdentityBinding",
    "FileChatIdentity",
    "IDENTITY_KIND_FILE",
    "IDENTITY_KIND_WEAPONRY",
    "IDENTITY_KINDS",
    "MAX_JAVASCRIPT_SAFE_INTEGER",
    "WeaponryChatIdentity",
    "parse_identity_key",
    "require_conversation_id",
]
