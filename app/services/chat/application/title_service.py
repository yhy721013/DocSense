"""Application service for generating file-chat display titles."""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass

from app.ports import (
    ChatConversationFactory,
    ChatConversationNotFoundError,
    ChatOperationResult,
    ChatPortError,
)
from app.services.chat.application.history_service import ChatHistoryService
from app.services.chat.application.cleanup_dispatcher import (
    ChatCleanupDispatcher,
    InlineChatCleanupDispatcher,
)
from app.services.chat.application.cleanup_service import (
    ChatCleanupJobExecutionError,
    ChatCleanupJobExecutor,
)
from app.services.chat.domain.models import (
    CLEANUP_JOB_SUCCEEDED,
    CLEANUP_REASON_TEMPORARY_THREAD,
    LEASE_CLOSED,
    LEASE_PLANNED,
    RESOURCE_THREAD,
    SESSION_ACTIVE,
    ChatCleanupJob,
)
from app.services.chat.domain.resource_ids import (
    chat_scoped_external_ref,
    chat_temporary_thread_lease_id,
)
from app.services.chat.persistence.store import ChatPersistenceStore
from app.services.chat.persistence.resource_lease_service import (
    ChatResourceLeaseSessionUnavailableError,
)
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
        cleanup_dispatcher: ChatCleanupDispatcher | None = None,
        cleanup_executor: ChatCleanupJobExecutor | None = None,
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
        if cleanup_executor is not None and not isinstance(
            cleanup_executor,
            ChatCleanupJobExecutor,
        ):
            raise TypeError("cleanup_executor must be ChatCleanupJobExecutor")
        self._cleanup_executor = cleanup_executor or ChatCleanupJobExecutor(
            store=store,
            conversation_factory=conversation_factory,
        )
        self._cleanup_dispatcher = cleanup_dispatcher or InlineChatCleanupDispatcher(
            execute=self._cleanup_executor.execute_cleanup_job,
        )
        if not isinstance(self._cleanup_dispatcher, ChatCleanupDispatcher):
            raise TypeError("cleanup_dispatcher must implement ChatCleanupDispatcher")
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
        raw_title = self._generate_with_tracked_temporary_thread(
            chat_id=normalized_chat_id,
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

    def _generate_with_tracked_temporary_thread(
        self,
        *,
        chat_id: str,
        context_ref: str,
        prompt: str,
    ) -> str:
        """Generate a title through a lease-backed temporary conversation.

        The old adapter-owned helper created and deleted this thread internally.
        That made a delete failure invisible to the local recovery model. The
        application service now records a planned lease before the remote side
        effect and retains a cleanup job if the final deletion cannot complete.
        """
        attempt_id = uuid.uuid4().hex
        lease_id = chat_temporary_thread_lease_id(
            chat_id=chat_id,
            attempt_id=attempt_id,
        )
        try:
            # The guarded planned lease is the title/delete admission gate.
            # Delete checks it in the same SQLite critical section used to
            # enter ``deleting``; therefore a title can never start its remote
            # thread after deletion has won the race.
            self._store.resource_leases.begin(
                lease_id=lease_id,
                chat_id=chat_id,
                resource_type=RESOURCE_THREAD,
                require_active_session=True,
            )
        except ChatResourceLeaseSessionUnavailableError as exc:
            raise ChatTitleUnavailableError(
                "chat session is not available for title generation"
            ) from exc
        temporary_session = None
        raw_title = ""
        cleanup_error = ""
        cleanup_attempted = False
        generation_error: Exception | None = None
        try:
            with self._conversation_factory.create() as conversation:
                temporary_session = conversation.open_temporary_conversation(
                    context_ref=context_ref,
                    conversation_name=f"title-{attempt_id}",
                )
                self._store.resource_leases.activate(
                    lease_id=lease_id,
                    external_ref=chat_scoped_external_ref(
                        context_ref=temporary_session.context_ref,
                        resource_ref=temporary_session.conversation_ref,
                    ),
                )
                raw_title = conversation.generate_temporary_reply(
                    session=temporary_session,
                    prompt=prompt,
                )
                cleanup_attempted = True
                cleanup_error = self._delete_temporary_conversation(
                    conversation=conversation,
                    temporary_session=temporary_session,
                    lease_id=lease_id,
                )
        except Exception as exc:
            generation_error = exc
            raise
        finally:
            if temporary_session is None:
                self._close_unresolved_planned_lease(lease_id)
            elif not cleanup_attempted:
                # ``generate_temporary_reply`` failed after the remote thread
                # was created. Still attempt cleanup, but never replace the
                # original generation exception with a cleanup exception.
                cleanup_attempted = True
                try:
                    with self._conversation_factory.create() as cleanup_conversation:
                        cleanup_error = self._delete_temporary_conversation(
                            conversation=cleanup_conversation,
                            temporary_session=temporary_session,
                            lease_id=lease_id,
                        )
                except Exception as cleanup_exc:
                    # Do not replace the model-generation error with a second
                    # failure while opening a cleanup-only request scope.  The
                    # durable job recorded below remains the recovery path.
                    cleanup_error = str(cleanup_exc) or cleanup_exc.__class__.__name__
            if temporary_session is not None and cleanup_error:
                cleanup_job = self._record_temporary_cleanup_failure(
                    chat_id=chat_id,
                    lease_id=lease_id,
                    error_message=cleanup_error,
                )
                cleanup_job = self._dispatch_temporary_cleanup(cleanup_job)
                if (
                    generation_error is None
                    and cleanup_job.status != CLEANUP_JOB_SUCCEEDED
                ):
                    raise ChatTitleGenerationError("标题临时资源清理失败")
        return raw_title

    def _delete_temporary_conversation(
        self,
        *,
        conversation,
        temporary_session,
        lease_id: str,
    ) -> str:
        """Delete a tracked title thread and return a stable failure reason."""
        try:
            result = conversation.delete_conversation(temporary_session)
        except ChatConversationNotFoundError:
            result = ChatOperationResult(success=True, already_applied=True)
        except ChatPortError as exc:
            return str(exc) or exc.__class__.__name__
        except Exception as exc:
            # Cleanup bookkeeping must survive adapter contract defects as
            # well as ordinary remote errors.  Returning a stable failure
            # value lets the caller persist and retry the exact lease.
            return str(exc) or exc.__class__.__name__
        if not isinstance(result, ChatOperationResult):
            return "temporary title delete returned an invalid result"
        if result.success:
            self._store.resource_leases.mark_closed(lease_id)
            return ""
        return result.error_message

    def _close_unresolved_planned_lease(self, lease_id: str) -> None:
        lease = self._store.resource_leases.get(lease_id)
        if lease is not None and lease.status == LEASE_PLANNED:
            self._store.resource_leases.mark_closed(lease_id)

    def _record_temporary_cleanup_failure(
        self,
        *,
        chat_id: str,
        lease_id: str,
        error_message: str,
    ) -> ChatCleanupJob:
        lease = self._store.resource_leases.get(lease_id)
        if lease is not None and lease.status != LEASE_CLOSED:
            if lease.status == LEASE_PLANNED:
                self._store.resource_leases.mark_cleanup_pending(lease_id)
            self._store.resource_leases.record_cleanup_failure(
                lease_id=lease_id,
                error_message=error_message,
            )
        return self._store.cleanup_jobs.enqueue(
            chat_id=chat_id,
            reason=CLEANUP_REASON_TEMPORARY_THREAD,
            lease_id=lease_id,
        )

    def _dispatch_temporary_cleanup(
        self,
        cleanup_job: ChatCleanupJob,
    ) -> ChatCleanupJob:
        """Attempt one immediate cleanup without hiding the durable failure."""
        try:
            return self._cleanup_dispatcher.dispatch(job=cleanup_job)
        except ChatCleanupJobExecutionError as exc:
            return exc.job

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
