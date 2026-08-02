"""用于中断活跃文件对话流的应用服务。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.modules.chat.application.command_service import ChatCommandService
from app.modules.chat.domain.identity import ConversationIdentity
from app.modules.chat.domain.events import ChatStreamEvent
from app.modules.chat.ports.coordination import ChatRunInactiveError
from app.modules.chat.ports.persistence import ChatPersistenceStore


logger = logging.getLogger(__name__)


def _required_text(value: str, *, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} cannot be empty")
    return normalized


@dataclass(frozen=True)
class AbortNotificationCapabilities:
    """取消通知适配器的真实能力声明。

    取消请求的权威事实始终是 ``chat_runs.abort_requested``。通知只用于让正在
    执行的 worker 更快醒来，因此当前单实例轮询实现不会伪造跨实例实时唤醒。
    """

    supports_single_instance: bool
    supports_shared_instances: bool
    supports_cross_instance_wakeup: bool


PERSISTED_ABORT_POLLING_CAPABILITIES = AbortNotificationCapabilities(
    supports_single_instance=True,
    supports_shared_instances=False,
    supports_cross_instance_wakeup=False,
)


@runtime_checkable
class AbortNotifier(Protocol):
    """通知执行器检查已持久化取消请求的产品无关边界。"""

    @property
    def capabilities(self) -> AbortNotificationCapabilities:
        """返回通知器实际可提供的唤醒能力。"""
        ...

    def notify_abort_requested(self, *, conversation_id: str, run_id: str) -> None:
        """尽力通知执行器；失败不得撤销已持久化的 abort 事实。"""
        ...


class PersistedAbortPollingNotifier:
    """当前单实例通知器：不发送信号，由本地执行器轮询持久化标记。

    该空实现刻意存在于容器装配中，避免未来维护者误以为 abort 已具备跨实例
    pub/sub 能力。替换为真实通知系统时只需实现 ``AbortNotifier``。
    """

    capabilities = PERSISTED_ABORT_POLLING_CAPABILITIES

    def notify_abort_requested(self, *, conversation_id: str, run_id: str) -> None:
        _required_text(conversation_id, name="conversation_id")
        _required_text(run_id, name="run_id")
        logger.debug(
            "文件对话中断已持久化，等待本地执行器轮询: conversation_id=%s run_id=%s",
            conversation_id,
            run_id,
        )


@dataclass(frozen=True)
class ChatAbortResult:
    """供应商与公开接口无关的中断结果。"""

    identity: ConversationIdentity
    aborted: bool
    msg: str
    run_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ConversationIdentity):
            raise TypeError("identity must implement ConversationIdentity")


class ChatAbortService:
    """为指定对话当前活跃运行写入持久化中断请求。"""

    def __init__(
        self,
        *,
        store: ChatPersistenceStore,
        chat_commands: ChatCommandService,
        abort_notifier: AbortNotifier | None = None,
    ) -> None:
        self._store = store
        self._chat_commands = chat_commands
        self._abort_notifier = abort_notifier or PersistedAbortPollingNotifier()
        if not isinstance(self._abort_notifier, AbortNotifier):
            raise TypeError("abort_notifier must implement AbortNotifier")

    @property
    def notifier_capabilities(self) -> AbortNotificationCapabilities:
        """向容器暴露通知器能力，以便启动时执行部署模式校验。"""
        return self._abort_notifier.capabilities

    def abort_chat(self, *, identity: ConversationIdentity) -> ChatAbortResult:
        if not isinstance(identity, ConversationIdentity):
            raise TypeError("identity must implement ConversationIdentity")
        resolution = self._store.identities.resolve_active(identity)
        if resolution is None:
            logger.info(
                "对话中断未命中活动 Conversation: identity_kind=%s",
                identity.identity_kind,
            )
            return ChatAbortResult(
                identity=identity,
                aborted=False,
                msg="当前无进行中的流式响应",
            )
        normalized_conversation_id = resolution.conversation_id
        logger.info("收到文件对话中断指令: conversation_id=%s", normalized_conversation_id)
        expired_runs = self._chat_commands.expire_stale_chat_runs(
            conversation_id=normalized_conversation_id,
        )
        if expired_runs:
            logger.warning(
                "文件对话中断前已释放过期运行: conversation_id=%s expired_run_ids=%s",
                normalized_conversation_id,
                ",".join(run.run_id for run in expired_runs),
            )

        active_runs = self._store.runs.list_active(normalized_conversation_id)
        if not active_runs:
            logger.info(
                "文件对话中断指令未命中活跃run: conversation_id=%s",
                normalized_conversation_id,
            )
            return ChatAbortResult(
                identity=identity,
                aborted=False,
                msg="当前无进行中的流式响应",
            )

        active_run = active_runs[0]
        try:
            # 中断请求只写入持久化标记，真正停止流由执行中的 ChatRunEventRecorder
            # 在事件边界读取该标记完成。这样路由层不需要持有进程内 stream 引用，
            # 后续替换为 Redis/队列通知时也不会改变业务语义。
            requested = self._chat_commands.request_abort(
                run_id=active_run.run_id,
            )
        except ChatRunInactiveError as exc:
            logger.info(
                "文件对话中断指令写入失败: conversation_id=%s run_id=%s reason=inactive status=%s",
                normalized_conversation_id,
                exc.run_id,
                exc.status,
            )
            return ChatAbortResult(
                identity=identity,
                aborted=False,
                msg="当前无进行中的流式响应",
            )
        logger.info(
            "文件对话中断标记已写入: conversation_id=%s run_id=%s",
            normalized_conversation_id,
            requested.run_id,
        )
        try:
            # 先持久化再通知：通知丢失时执行器仍会在事件边界读取 abort 标记，
            # 因而不能把通知异常转换成“中断未受理”的错误响应。
            self._abort_notifier.notify_abort_requested(
                conversation_id=normalized_conversation_id,
                run_id=requested.run_id,
            )
        except Exception:
            logger.exception(
                "文件对话中断通知失败，保留持久化标记等待轮询: conversation_id=%s run_id=%s",
                normalized_conversation_id,
                requested.run_id,
            )
        return ChatAbortResult(
            identity=identity,
            aborted=True,
            msg="已发送中断信号",
            run_id=requested.run_id,
        )

    @staticmethod
    def build_abort_signal() -> ChatStreamEvent:
        """构造不携带任何公开或内部身份的共享终态事件。"""
        return ChatStreamEvent("aborted", {})


__all__ = [
    "AbortNotificationCapabilities",
    "AbortNotifier",
    "ChatAbortResult",
    "ChatAbortService",
    "PERSISTED_ABORT_POLLING_CAPABILITIES",
    "PersistedAbortPollingNotifier",
]
