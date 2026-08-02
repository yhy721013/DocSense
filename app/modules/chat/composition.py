"""Chat 模块唯一生产组合根。

本模块只在显式调用 ``compose_chat_application_services`` 时构造 SQLite 与 AnythingLLM 对象图；
导入包本身不会打开数据库、创建 HTTP Session 或启动后台线程。全局 Container 只负责提供配置和
跨模块文档解析适配器，不再逐项了解 Chat 用例的构造顺序。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.modules.chat.adapters.anythingllm_factory import AnythingLLMChatFactory
from app.modules.chat.adapters.sqlite.locking.lock_service import ChatRunLockService
from app.modules.chat.adapters.sqlite.store import ChatStore
from app.modules.chat.application.abort_service import ChatAbortService
from app.modules.chat.application.cleanup_dispatcher import InlineChatCleanupDispatcher
from app.modules.chat.application.cleanup_service import ChatCleanupJobExecutor
from app.modules.chat.application.command_service import ChatCommandService
from app.modules.chat.application.delete_service import ChatDeleteService
from app.modules.chat.application.dispatcher import InlineChatRunDispatcher
from app.modules.chat.application.document_resolver import ChatDocumentResolver
from app.modules.chat.application.history_service import ChatHistoryService
from app.modules.chat.application.run_executor import SynchronousChatRunExecutor
from app.modules.chat.application.title_service import ChatTitleService
from app.modules.chat.ports import ChatConversationFactory
from app.modules.chat.ports.coordination import ChatRunCoordinator
from app.modules.chat.ports.persistence import ChatPersistenceStore
from app.services.core.config import AnythingLLMConfig
from app.services.core.settings import (
    CHAT_MAX_CONCURRENT_STREAMS,
    CHAT_MAX_FILES_PER_REQUEST,
    CHAT_MAX_MESSAGE_CHARS,
    CHAT_MAX_OUTPUT_CHARS,
)


@dataclass(frozen=True)
class ChatApplicationServices:
    """一次生产装配形成的完整 Chat 应用服务外观。"""

    conversation_factory: ChatConversationFactory
    store: ChatPersistenceStore
    commands: ChatCommandService
    run_executor: SynchronousChatRunExecutor
    dispatcher: InlineChatRunDispatcher
    history: ChatHistoryService
    title: ChatTitleService
    abort: ChatAbortService
    delete: ChatDeleteService
    cleanup_executor: ChatCleanupJobExecutor


def compose_chat_application_services(
    *,
    db_path: str,
    anythingllm_config: AnythingLLMConfig,
    document_resolver: ChatDocumentResolver,
) -> ChatApplicationServices:
    """构造行为等价且共享同一 Store/Factory 的 Chat 生产对象图。

    参数只包含模块运行真正需要的配置和跨模块只读文档解析边界。当前返回 SQLite 单实例、内联
    Dispatcher 和轮询式中断实现；能力对象仍会如实声明它们不支持共享实例、可靠队列或 fencing。
    """
    store = ChatStore(db_path)
    coordinator: ChatRunCoordinator = ChatRunLockService(db_path)
    commands = ChatCommandService(coordinator)
    history = ChatHistoryService(store)
    conversation_factory = AnythingLLMChatFactory(anythingllm_config)
    cleanup_executor = ChatCleanupJobExecutor(
        store=store,
        conversation_factory=conversation_factory,
    )
    cleanup_dispatcher = InlineChatCleanupDispatcher(
        execute=cleanup_executor.execute_cleanup_job,
    )
    run_executor = SynchronousChatRunExecutor(
        store=store,
        chat_commands=commands,
        conversation_factory=conversation_factory,
        document_resolver=document_resolver,
        max_files_per_request=CHAT_MAX_FILES_PER_REQUEST,
        max_message_chars=CHAT_MAX_MESSAGE_CHARS,
        max_output_chars=CHAT_MAX_OUTPUT_CHARS,
        max_concurrent_streams=CHAT_MAX_CONCURRENT_STREAMS,
    )
    dispatcher = InlineChatRunDispatcher(execute=run_executor.execute_chat_run)
    title = ChatTitleService(
        store=store,
        history_service=history,
        conversation_factory=conversation_factory,
        cleanup_dispatcher=cleanup_dispatcher,
        cleanup_executor=cleanup_executor,
    )
    abort = ChatAbortService(store=store, chat_commands=commands)
    delete = ChatDeleteService(
        store=store,
        chat_commands=commands,
        conversation_factory=conversation_factory,
        cleanup_dispatcher=cleanup_dispatcher,
        cleanup_executor=cleanup_executor,
    )
    return ChatApplicationServices(
        conversation_factory=conversation_factory,
        store=store,
        commands=commands,
        run_executor=run_executor,
        dispatcher=dispatcher,
        history=history,
        title=title,
        abort=abort,
        delete=delete,
        cleanup_executor=cleanup_executor,
    )


__all__ = [
    "ChatApplicationServices",
    "compose_chat_application_services",
]
