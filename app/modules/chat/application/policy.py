"""按公开身份种类选择共享 Chat 用例的稳定行为策略。"""

from __future__ import annotations

from dataclasses import dataclass

from app.modules.chat.domain.identity import IDENTITY_KIND_FILE, IDENTITY_KIND_WEAPONRY


@dataclass(frozen=True)
class ChatUseCasePolicy:
    """不包含 Web 字段名的应用层策略，避免在共享用例散落业务类型判断。"""

    identity_kind: str
    expose_source_chunks: bool
    expose_user_file_selection: bool


FILE_CHAT_POLICY = ChatUseCasePolicy(IDENTITY_KIND_FILE, False, True)
WEAPONRY_CHAT_POLICY = ChatUseCasePolicy(IDENTITY_KIND_WEAPONRY, True, False)


def chat_policy_for(identity_kind: str) -> ChatUseCasePolicy:
    """返回已冻结策略；未知身份必须失败，不能静默采用文件对话行为。"""
    if identity_kind == IDENTITY_KIND_FILE:
        return FILE_CHAT_POLICY
    if identity_kind == IDENTITY_KIND_WEAPONRY:
        return WEAPONRY_CHAT_POLICY
    raise ValueError("unsupported chat identity kind")


__all__ = [
    "ChatUseCasePolicy",
    "FILE_CHAT_POLICY",
    "WEAPONRY_CHAT_POLICY",
    "chat_policy_for",
]
