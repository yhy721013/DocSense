"""用于生成文件对话展示标题的应用服务。"""

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
from app.services.chat.domain.chat_id import chat_id_public_value
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
    """已有对话没有可用于标题输入的已提交消息时抛出。"""


class ChatTitleGenerationError(RuntimeError):
    """标题生成无法得到稳定展示标题时抛出。"""


class ChatTitleUnavailableError(RuntimeError):
    """会话生命周期禁止调用外部标题生成时抛出。"""


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
    """面向 `/llm/chat/title` 接口的结果。"""

    chat_id: str
    title: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "chat_id",
            _required_text(self.chat_id, name="chat_id"),
        )
        object.__setattr__(self, "title", str(self.title or "").strip())

    def to_response(self) -> dict[str, object]:
        """返回严格符合公开接口的 JSON 载荷形状。"""
        return {"chatId": chat_id_public_value(self.chat_id), "title": self.title}


class ChatTitleService:
    """根据本地已提交的对话历史生成短标题。

    本服务刻意依赖供应商无关的 Chat Port，而不是 `AnythingLLMClient`。它仅读取
    本地已提交消息、构造长度受控的提示词，并让适配器在既有工作区创建独立回复，
    从而确保标题提示词不会被追加到主对话线程。
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
        """在不修改历史记录的前提下，为一个对话生成展示标题。"""
        normalized_chat_id = _required_text(chat_id, name="chat_id")
        session = self._store.sessions.get(normalized_chat_id)
        if session is None:
            logger.info(
                "标题生成跳过: 对话不存在 chat_id=%s",
                normalized_chat_id,
            )
            return ChatTitleResult(chat_id=normalized_chat_id, title="")
        if session.status != SESSION_ACTIVE:
            logger.warning(
                "标题生成被拒绝：会话当前不可用: chat_id=%s status=%s",
                normalized_chat_id,
                session.status,
            )
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
            "文件对话标题生成完成: chat_id=%s title_chars=%d",
            normalized_chat_id,
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
        """通过由租约保护的临时对话生成标题。

        旧的适配器内部辅助方法会自行创建和删除该线程，导致删除失败无法被本地恢复模型
        感知。现在应用服务会在远端副作用发生前记录 planned 租约，若最终删除未完成，
        则保留清理任务。
        """
        attempt_id = uuid.uuid4().hex
        lease_id = chat_temporary_thread_lease_id(
            chat_id=chat_id,
            attempt_id=attempt_id,
        )
        try:
            # 受保护的 planned 租约是标题和删除操作的准入闸门。删除操作会在进入
            # ``deleting`` 的同一个 SQLite 临界区检查它，因此删除抢占成功后，标题
            # 绝不会再创建远端线程。
            self._store.resource_leases.begin(
                lease_id=lease_id,
                chat_id=chat_id,
                resource_type=RESOURCE_THREAD,
                require_active_session=True,
            )
            logger.info(
                "标题临时线程计划租约已创建: chat_id=%s lease_id=%s",
                chat_id,
                lease_id,
            )
        except ChatResourceLeaseSessionUnavailableError as exc:
            logger.warning(
                "标题生成被拒绝：创建临时线程前会话已不可用: chat_id=%s status=%s",
                chat_id,
                exc.status,
            )
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
                logger.info(
                    "标题临时线程已创建，准备记录租约身份: chat_id=%s lease_id=%s",
                    chat_id,
                    lease_id,
                )
                self._store.resource_leases.activate(
                    lease_id=lease_id,
                    external_ref=chat_scoped_external_ref(
                        context_ref=temporary_session.context_ref,
                        resource_ref=temporary_session.conversation_ref,
                    ),
                )
                logger.info(
                    "标题临时线程租约已激活，开始请求模型回复: chat_id=%s lease_id=%s",
                    chat_id,
                    lease_id,
                )
                raw_title = conversation.generate_temporary_reply(
                    session=temporary_session,
                    prompt=prompt,
                )
                logger.info(
                    "标题临时线程模型回复已生成: chat_id=%s lease_id=%s reply_chars=%d",
                    chat_id,
                    lease_id,
                    len(raw_title),
                )
                cleanup_attempted = True
                cleanup_error = self._delete_temporary_conversation(
                    conversation=conversation,
                    temporary_session=temporary_session,
                    lease_id=lease_id,
                )
        except Exception as exc:
            generation_error = exc
            logger.exception(
                "标题临时线程生成流程发生异常: chat_id=%s lease_id=%s error_type=%s",
                chat_id,
                lease_id,
                exc.__class__.__name__,
            )
            raise
        finally:
            if temporary_session is None:
                self._close_unresolved_planned_lease(lease_id)
            elif not cleanup_attempted:
                # ``generate_temporary_reply`` 在远端线程创建后失败。仍须尝试清理，
                # 但绝不能用清理异常替换原始生成异常。
                cleanup_attempted = True
                logger.info(
                    "标题生成异常后开始补偿删除临时线程: chat_id=%s lease_id=%s",
                    chat_id,
                    lease_id,
                )
                try:
                    with self._conversation_factory.create() as cleanup_conversation:
                        cleanup_error = self._delete_temporary_conversation(
                            conversation=cleanup_conversation,
                            temporary_session=temporary_session,
                            lease_id=lease_id,
                        )
                except Exception as cleanup_exc:
                    # 打开仅用于清理的请求作用域时出现的第二个失败，不能替换模型生成
                    # 错误。下方记录的持久化任务仍是恢复路径。
                    cleanup_error = str(cleanup_exc) or cleanup_exc.__class__.__name__
                    logger.exception(
                        "标题临时线程补偿删除异常，准备记录可重试任务: chat_id=%s lease_id=%s error_type=%s",
                        chat_id,
                        lease_id,
                        cleanup_exc.__class__.__name__,
                    )
            if temporary_session is not None and cleanup_error:
                logger.warning(
                    "标题临时线程未能立即清理，准备记录可重试任务: chat_id=%s lease_id=%s error_chars=%d",
                    chat_id,
                    lease_id,
                    len(cleanup_error),
                )
                cleanup_job = self._record_temporary_cleanup_failure(
                    chat_id=chat_id,
                    lease_id=lease_id,
                    error_message=cleanup_error,
                )
                cleanup_job = self._dispatch_temporary_cleanup(cleanup_job)
                logger.info(
                    "标题临时线程清理任务已调度: chat_id=%s lease_id=%s job_id=%s status=%s",
                    chat_id,
                    lease_id,
                    cleanup_job.job_id,
                    cleanup_job.status,
                )
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
        """删除已跟踪的标题线程，并返回稳定的失败原因。"""
        logger.info("开始删除标题临时线程: lease_id=%s", lease_id)
        try:
            result = conversation.delete_conversation(temporary_session)
        except ChatConversationNotFoundError:
            result = ChatOperationResult(success=True, already_applied=True)
            logger.info("标题临时线程已不存在，无需重复删除: lease_id=%s", lease_id)
        except ChatPortError as exc:
            logger.warning(
                "删除标题临时线程失败: lease_id=%s error_type=%s",
                lease_id,
                exc.__class__.__name__,
            )
            return str(exc) or exc.__class__.__name__
        except Exception as exc:
            # 清理记账既要覆盖普通远端错误，也要覆盖适配器契约缺陷。返回稳定失败值，
            # 才能让调用方持久化并重试准确的租约。
            logger.exception(
                "删除标题临时线程发生未预期异常: lease_id=%s error_type=%s",
                lease_id,
                exc.__class__.__name__,
            )
            return str(exc) or exc.__class__.__name__
        if not isinstance(result, ChatOperationResult):
            logger.error(
                "删除标题临时线程失败：端口返回了错误结果类型: lease_id=%s returned_type=%s",
                lease_id,
                type(result).__name__,
            )
            return "temporary title delete returned an invalid result"
        if result.success:
            self._store.resource_leases.mark_closed(lease_id)
            logger.info(
                "标题临时线程删除完成: lease_id=%s already_applied=%s",
                lease_id,
                result.already_applied,
            )
            return ""
        logger.warning(
            "标题临时线程删除未成功，保留失败原因供后续重试: lease_id=%s error_chars=%d",
            lease_id,
            len(result.error_message),
        )
        return result.error_message

    def _close_unresolved_planned_lease(self, lease_id: str) -> None:
        lease = self._store.resource_leases.get(lease_id)
        if lease is not None and lease.status == LEASE_PLANNED:
            self._store.resource_leases.mark_closed(lease_id)
            logger.info("标题临时线程未创建远端资源，已关闭计划租约: lease_id=%s", lease_id)

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
        cleanup_job = self._store.cleanup_jobs.enqueue(
            chat_id=chat_id,
            reason=CLEANUP_REASON_TEMPORARY_THREAD,
            lease_id=lease_id,
        )
        logger.info(
            "标题临时线程清理失败已持久化: chat_id=%s lease_id=%s job_id=%s",
            chat_id,
            lease_id,
            cleanup_job.job_id,
        )
        return cleanup_job

    def _dispatch_temporary_cleanup(
        self,
        cleanup_job: ChatCleanupJob,
    ) -> ChatCleanupJob:
        """尝试一次立即清理，同时不掩盖持久化失败。"""
        logger.info(
            "开始内联尝试标题临时线程清理: job_id=%s chat_id=%s",
            cleanup_job.job_id,
            cleanup_job.chat_id,
        )
        try:
            result = self._cleanup_dispatcher.dispatch(job=cleanup_job)
            logger.info(
                "标题临时线程内联清理执行结束: job_id=%s status=%s",
                result.job_id,
                result.status,
            )
            return result
        except ChatCleanupJobExecutionError as exc:
            logger.warning(
                "标题临时线程内联清理失败，保留可重试任务: job_id=%s",
                exc.job.job_id,
            )
            return exc.job

    @staticmethod
    def clean_title(
        raw_title: str,
        *,
        max_chars: int = DEFAULT_MAX_TITLE_CHARS,
    ) -> str:
        """将模型输出规范化为单个长度受限的展示标题。"""
        max_title_chars = _positive_int(max_chars, name="max_chars")
        text = str(raw_title or "")
        text = _THINK_PATTERN.sub("", text)
        text = text.replace("\r", "\n").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:\w+)?", "", text).strip()
            text = re.sub(r"```$", "", text).strip()
        # 模型有时会先返回一句标题，再给出解释。首个非空行才是唯一合理的标题候选。
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
