"""Application service for generating file-chat display titles."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app.ports import ChatConversationFactory
from app.services.chat.application.history_service import ChatHistoryService
from app.services.chat.domain.models import SESSION_ACTIVE
from app.services.chat.persistence.store import ChatPersistenceStore
from app.services.core.prompts import build_chat_title_prompt


logger = logging.getLogger(__name__)

DEFAULT_TITLE_HISTORY_LIMIT = 12
DEFAULT_TITLE_MESSAGE_MAX_CHARS = 1000
DEFAULT_MAX_TITLE_CHARS = 20

_QUOTE_CHARS = "\"'`“”‘’《》「」『』"
_PREFIX_PATTERN = re.compile(
    r"^(?:标题|对话标题|生成标题|建议标题|题目)\s*[:：]\s*",
    flags=re.IGNORECASE,
)
_BULLET_PATTERN = re.compile(r"^\s*(?:[-*•]|\d+[.、．])\s*")
_THINK_PATTERN = re.compile(r"<think>[\s\S]*?(?:</think>|$)", flags=re.IGNORECASE)


class ChatTitleEmptyHistoryError(ValueError):
    """Raised when an existing chat has no committed messages for title input."""


class ChatTitleGenerationError(RuntimeError):
    """Raised when title generation cannot produce a stable display title."""


class ChatTitleUnavailableError(RuntimeError):
    """Raised when session lifecycle forbids external title generation."""


def _required_text(value: str, *, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


def _positive_int(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} 必须是正整数")
    return value


@dataclass(frozen=True)
class ChatTitleResult:
    """API-facing result for `/llm/chat/title`."""

    chat_id: str
    title: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "chat_id",
            _required_text(self.chat_id, name="chat_id"),
        )
        object.__setattr__(self, "title", str(self.title or "").strip())

    def to_response(self) -> dict[str, str]:
        """Return the exact public JSON payload shape."""
        return {"chatId": self.chat_id, "title": self.title}


class ChatTitleService:
    """Generate short titles from local committed chat history.

    The service intentionally depends on the supplier-neutral Chat Port instead
    of `AnythingLLMClient`. It reads only local committed messages, builds a
    bounded prompt, and asks the adapter for a standalone reply in the existing
    workspace so the title prompt is never appended to the main conversation
    thread.
    """

    def __init__(
        self,
        *,
        store: ChatPersistenceStore,
        history_service: ChatHistoryService,
        conversation_factory: ChatConversationFactory,
        history_limit: int = DEFAULT_TITLE_HISTORY_LIMIT,
        message_max_chars: int = DEFAULT_TITLE_MESSAGE_MAX_CHARS,
        max_title_chars: int = DEFAULT_MAX_TITLE_CHARS,
    ) -> None:
        if not isinstance(store, ChatPersistenceStore):
            raise TypeError("store must implement ChatPersistenceStore")
        if not isinstance(history_service, ChatHistoryService):
            raise TypeError("history_service must be ChatHistoryService")
        if not isinstance(conversation_factory, ChatConversationFactory):
            raise TypeError(
                "conversation_factory must implement ChatConversationFactory"
            )
        self._store = store
        self._history_service = history_service
        self._conversation_factory = conversation_factory
        self._history_limit = _positive_int(history_limit, name="history_limit")
        self._message_max_chars = _positive_int(
            message_max_chars,
            name="message_max_chars",
        )
        self._max_title_chars = _positive_int(
            max_title_chars,
            name="max_title_chars",
        )

    def generate_title(self, *, chat_id: str) -> ChatTitleResult:
        """Generate a display title for one chat without mutating history."""
        normalized_chat_id = _required_text(chat_id, name="chat_id")
        session = self._store.sessions.get(normalized_chat_id)
        if session is None:
            logger.info(
                "标题生成跳过: 对话不存在 chat_id=%s",
                normalized_chat_id,
            )
            return ChatTitleResult(chat_id=normalized_chat_id, title="")
        if session.status != SESSION_ACTIVE:
            raise ChatTitleUnavailableError(
                "chat session is not available for title generation"
            )

        title_messages = self._history_service.list_title_messages(
            normalized_chat_id,
            limit=self._history_limit,
            max_content_chars=self._message_max_chars,
        )
        if not title_messages:
            logger.warning(
                "标题生成请求被拒绝: 对话历史为空 chat_id=%s",
                normalized_chat_id,
            )
            raise ChatTitleEmptyHistoryError("对话历史为空，无法生成标题")

        if not session.workspace_ref:
            logger.error(
                "标题生成失败: 对话缺少workspace引用 chat_id=%s message_count=%d",
                normalized_chat_id,
                len(title_messages),
            )
            raise ChatTitleGenerationError("对话缺少可用于标题生成的工作区引用")

        prompt = build_chat_title_prompt(
            title_messages,
            max_title_chars=self._max_title_chars,
        )
        logger.info(
            "开始生成文件对话标题: chat_id=%s message_count=%d prompt_chars=%d",
            normalized_chat_id,
            len(title_messages),
            len(prompt),
        )
        with self._conversation_factory.create() as conversation:
            raw_title = conversation.generate_standalone_reply(
                context_ref=session.workspace_ref,
                prompt=prompt,
            )
        title = self.clean_title(raw_title, max_chars=self._max_title_chars)
        if not title:
            logger.error(
                "标题生成失败: 模型返回空标题 chat_id=%s raw_chars=%d",
                normalized_chat_id,
                len(str(raw_title or "")),
            )
            raise ChatTitleGenerationError("模型未生成标题")
        logger.info(
            "文件对话标题生成完成: chat_id=%s title=%s title_chars=%d",
            normalized_chat_id,
            title,
            len(title),
        )
        return ChatTitleResult(chat_id=normalized_chat_id, title=title)

    @staticmethod
    def clean_title(
        raw_title: str,
        *,
        max_chars: int = DEFAULT_MAX_TITLE_CHARS,
    ) -> str:
        """Normalize model output into a single bounded display title."""
        max_title_chars = _positive_int(max_chars, name="max_chars")
        text = str(raw_title or "")
        text = _THINK_PATTERN.sub("", text)
        text = text.replace("\r", "\n").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:\w+)?", "", text).strip()
            text = re.sub(r"```$", "", text).strip()
        # Models sometimes return a sentence and then explanations. The first
        # non-empty line is the only defensible title candidate.
        for line in text.splitlines():
            candidate = line.strip()
            if candidate:
                text = candidate
                break
        text = text.strip(_QUOTE_CHARS).strip()
        text = _BULLET_PATTERN.sub("", text).strip()
        text = _PREFIX_PATTERN.sub("", text).strip()
        text = text.strip(_QUOTE_CHARS).strip()
        text = re.sub(r"\s+", "", text)
        text = text.strip("，。,.、；;：:！!？? ")
        return text[:max_title_chars]


__all__ = [
    "ChatTitleEmptyHistoryError",
    "ChatTitleGenerationError",
    "ChatTitleUnavailableError",
    "ChatTitleResult",
    "ChatTitleService",
]
