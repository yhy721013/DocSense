"""公开 Chat 身份到内部 Conversation 聚合的持久化端口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.modules.chat.domain.identity import (
    ConversationIdentity,
    ConversationIdentityBinding,
)
from app.modules.chat.domain.models import ChatSession


class ConversationIdentityConflictError(RuntimeError):
    """公开身份已被另一 Conversation 世代占用。"""


class ConversationAdmissionBusyError(RuntimeError):
    """同一公开身份已有请求处于正式受理前阶段。"""


class ConversationAdmissionLostError(RuntimeError):
    """准入 token 已过期、被消费或不再属于当前调用方。"""


class FileConversationTombstonedError(RuntimeError):
    """文件 chatId 已存在于删除墓碑中，禁止创建新世代。"""


@dataclass(frozen=True)
class ConversationAdmissionLease:
    """创建或受理 Conversation 前持有的短期身份竞争证明。"""

    identity_key: str
    identity_kind: str
    admission_token: str
    owner_instance_id: str
    expires_at: str


@dataclass(frozen=True)
class ConversationResolution:
    """公开身份解析到的内部会话及身份世代。"""

    session: ChatSession
    binding: ConversationIdentityBinding

    @property
    def conversation_id(self) -> str:
        # ChatSession 将在本阶段后续改名；这里集中保留过渡读取，避免应用层把字段名
        # 当作公开 chatId。其值已经是随机 UUID，而不是任何公开身份的拼接值。
        return self.binding.conversation_id


@runtime_checkable
class ConversationIdentityStore(Protocol):
    """身份解析、准入竞争、创建世代及删除释放的产品无关端口。"""

    def resolve_active(
        self,
        identity: ConversationIdentity,
    ) -> ConversationResolution | None:
        ...

    def resolve_any(
        self,
        identity: ConversationIdentity,
    ) -> ConversationResolution | None:
        ...

    def get_by_conversation_id(
        self,
        conversation_id: str,
    ) -> ConversationResolution | None:
        """按内部聚合键读取绑定，供执行恢复和只读投影使用。"""
        ...

    def reserve_admission(
        self,
        identity: ConversationIdentity,
    ) -> ConversationAdmissionLease:
        ...

    def release_admission(self, lease: ConversationAdmissionLease) -> bool:
        ...

    def create_conversation(
        self,
        identity: ConversationIdentity,
        *,
        admission_lease: ConversationAdmissionLease | None = None,
    ) -> ConversationResolution:
        ...

    def finalize_completed_delete(
        self,
        conversation_id: str,
    ) -> None:
        """验证清理成功事实后，原子清除正文并按身份策略完成删除。"""
        ...


__all__ = [
    "ConversationAdmissionBusyError",
    "ConversationAdmissionLease",
    "ConversationAdmissionLostError",
    "ConversationIdentityConflictError",
    "ConversationIdentityStore",
    "ConversationResolution",
    "FileConversationTombstonedError",
]
