"""DocSense 应用装配根、依赖容器与任务并发边界。

本模块位于 ``app`` 包根目录，因为它负责组装接口层、应用服务和外部适配器，不属于
任何单一业务 Service。容器只保存可跨请求安全共享的服务、不可变配置和无状态工厂；
任何持有网络 Session 的 AnythingLLM 对象都必须由任务级 Factory 在后台线程内部创建。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Callable, ParamSpec, TypeVar

from flask import current_app

from app.integrations.anythingllm.factory import (
    AnythingLLMGatewayFactory,
    AnythingLLMKnowledgeIndexFactory,
)
from app.integrations.anythingllm.chat_factory import AnythingLLMChatFactory
from app.integrations.anythingllm.policies import document_rag_workspace_settings
from app.ports import (
    ChatConversationFactory,
    DocumentRagFactory,
    KnowledgeIndexFactory,
)
from app.services.chat import (
    ChatAbortService,
    ChatCommandService,
    ChatHistoryService,
    ChatPersistenceStore,
    ChatRunLockService,
    ChatStore,
)
from app.services.core.config import (
    AnythingLLMConfig,
    LLMIntegrationConfig,
    load_anythingllm_config,
    load_llm_integration_config,
)
from app.services.core.database import ChatDatabaseService, DatabaseService
from app.services.core.progress_hub import LLMProgressHub
from app.services.core.settings import CHAT_DB_PATH, KNOWLEDGE_BASE_DB_PATH
from app.services.llm_service.task_service import LLMTaskService


logger = logging.getLogger(__name__)

APPLICATION_SERVICES_EXTENSION = "docsense_services"

_P = ParamSpec("_P")
_R = TypeVar("_R")


class UploadTaskLimiter:
    """限制上传类后台任务并发数的应用级线程安全组件。

    当前 analysis 与 report 仍共享 AnythingLLM Document Processor，因此阶段 6 保持原有
    单并发行为。待两条链路都迁移到新集成层后，该限制器可以下沉到对应 Factory，而无需
    再修改 Blueprint 的业务校验逻辑。
    """

    def __init__(self, max_concurrency: int = 1) -> None:
        """创建有界并发入口，并拒绝会导致任务永久阻塞的非正配置。"""
        if not isinstance(max_concurrency, int) or max_concurrency < 1:
            raise ValueError("max_concurrency 必须是正整数")
        self._max_concurrency = max_concurrency
        self._semaphore = threading.BoundedSemaphore(max_concurrency)

    @property
    def max_concurrency(self) -> int:
        """返回允许同时执行的上传类任务数量。"""
        return self._max_concurrency

    def run(
        self,
        function: Callable[_P, _R],
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> _R:
        """在并发许可内执行函数，并在所有退出路径上归还许可。"""
        if not callable(function):
            raise TypeError("function 必须可调用")
        logger.debug(
            "等待上传任务并发许可: max_concurrency=%d",
            self._max_concurrency,
        )
        with self._semaphore:
            logger.debug(
                "获得上传任务并发许可: max_concurrency=%d",
                self._max_concurrency,
            )
            return function(*args, **kwargs)


@dataclass(frozen=True)
class ApplicationServices:
    """Flask 应用内可安全共享的依赖集合。

    阶段 8 起两个 AnythingLLM Factory 都是必需能力，但只保存配置和线程安全协调依赖，
    不持有网络 Session。该数据类冻结的是依赖引用，数据库服务和进度 Hub 自身仍按各自
    线程安全契约维护内部状态。
    """

    document_rag_factory: DocumentRagFactory
    knowledge_index_factory: KnowledgeIndexFactory
    chat_conversation_factory: ChatConversationFactory
    task_service: LLMTaskService
    kb_service: DatabaseService
    chat_db: ChatDatabaseService
    chat_store: ChatPersistenceStore
    chat_commands: ChatCommandService
    chat_history: ChatHistoryService
    chat_abort: ChatAbortService
    progress_hub: LLMProgressHub
    upload_task_limiter: UploadTaskLimiter
    llm_config: LLMIntegrationConfig
    anythingllm_config: AnythingLLMConfig

    def __post_init__(self) -> None:
        """在应用启动时拒绝缺失关键依赖，避免请求到达后才出现空引用错误。"""
        required_dependencies: dict[str, Any] = {
            "document_rag_factory": self.document_rag_factory,
            "knowledge_index_factory": self.knowledge_index_factory,
            "chat_conversation_factory": self.chat_conversation_factory,
            "task_service": self.task_service,
            "kb_service": self.kb_service,
            "chat_db": self.chat_db,
            "chat_store": self.chat_store,
            "chat_commands": self.chat_commands,
            "chat_history": self.chat_history,
            "chat_abort": self.chat_abort,
            "progress_hub": self.progress_hub,
            "upload_task_limiter": self.upload_task_limiter,
            "llm_config": self.llm_config,
            "anythingllm_config": self.anythingllm_config,
        }
        missing = [name for name, value in required_dependencies.items() if value is None]
        if missing:
            raise ValueError(f"ApplicationServices 缺少依赖：{', '.join(missing)}")
        if not isinstance(self.document_rag_factory, DocumentRagFactory):
            raise TypeError("document_rag_factory 必须实现 DocumentRagFactory")
        if not isinstance(
            self.knowledge_index_factory,
            KnowledgeIndexFactory,
        ):
            raise TypeError("knowledge_index_factory 必须实现 KnowledgeIndexFactory")
        if not isinstance(
            self.chat_conversation_factory,
            ChatConversationFactory,
        ):
            raise TypeError(
                "chat_conversation_factory must implement ChatConversationFactory"
            )
        if not isinstance(self.chat_store, ChatPersistenceStore):
            raise TypeError("chat_store must implement ChatPersistenceStore")
        if not isinstance(self.chat_commands, ChatCommandService):
            raise TypeError("chat_commands must be ChatCommandService")
        if not isinstance(self.chat_history, ChatHistoryService):
            raise TypeError("chat_history must be ChatHistoryService")
        if not isinstance(self.chat_abort, ChatAbortService):
            raise TypeError("chat_abort must be ChatAbortService")


def create_application_services() -> ApplicationServices:
    """根据环境配置创建生产应用容器，不创建 AnythingLLM 网络 Session。"""
    anythingllm_config = load_anythingllm_config()
    llm_config = load_llm_integration_config()
    task_service = LLMTaskService(llm_config.task_db_path)
    kb_service = DatabaseService(str(KNOWLEDGE_BASE_DB_PATH))
    chat_store = ChatStore(str(CHAT_DB_PATH))
    chat_commands = ChatCommandService(ChatRunLockService(chat_store.db_path))
    services = ApplicationServices(
        document_rag_factory=AnythingLLMGatewayFactory(
            anythingllm_config,
            workspace_settings=document_rag_workspace_settings(),
        ),
        knowledge_index_factory=AnythingLLMKnowledgeIndexFactory(
            anythingllm_config,
            task_service.knowledge_index_operations,
            kb_service,
            workspace_settings=document_rag_workspace_settings(),
        ),
        chat_conversation_factory=AnythingLLMChatFactory(anythingllm_config),
        task_service=task_service,
        kb_service=kb_service,
        chat_db=ChatDatabaseService(str(CHAT_DB_PATH)),
        chat_store=chat_store,
        chat_commands=chat_commands,
        chat_history=ChatHistoryService(chat_store),
        chat_abort=ChatAbortService(
            store=chat_store,
            chat_commands=chat_commands,
        ),
        progress_hub=LLMProgressHub(),
        upload_task_limiter=UploadTaskLimiter(max_concurrency=1),
        llm_config=llm_config,
        anythingllm_config=anythingllm_config,
    )
    logger.info(
        "应用依赖容器创建完成: knowledge_index_enabled=%s "
        "upload_max_concurrency=%d",
        services.knowledge_index_factory is not None,
        services.upload_task_limiter.max_concurrency,
    )
    return services


def get_application_services() -> ApplicationServices:
    """从当前 Flask 应用读取依赖容器，并对缺失或错误类型给出明确异常。"""
    services = current_app.extensions.get(APPLICATION_SERVICES_EXTENSION)
    if services is None:
        raise RuntimeError("Flask 应用尚未安装 DocSense 依赖容器")
    if not isinstance(services, ApplicationServices):
        raise RuntimeError("Flask 应用中的 DocSense 依赖容器类型无效")
    return services
