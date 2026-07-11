"""DocSense 应用装配根、依赖容器与任务并发边界。

本模块位于 ``app`` 包根目录，因为它负责组装接口层、应用服务和外部适配器，不属于
任何单一业务 Service。容器只保存可跨请求安全共享的服务、不可变配置和无状态工厂；
任何持有网络 Session 的 AnythingLLM 对象都必须由任务级 Factory 在后台线程内部创建。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
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
    ChatCleanupJobExecutor,
    ChatCommandService,
    ChatDeleteService,
    DatabaseChatDocumentResolver,
    ChatHistoryService,
    ChatPersistenceStore,
    ChatRunDispatcher,
    ChatRunLockService,
    ChatStore,
    ChatTitleService,
    SynchronousChatRunExecutor,
    InlineChatRunDispatcher,
    InlineChatCleanupDispatcher,
)
from app.services.core.config import (
    AnythingLLMConfig,
    ChatInfrastructureConfig,
    LLMIntegrationConfig,
    load_anythingllm_config,
    load_chat_infrastructure_config,
    load_llm_integration_config,
)
from app.services.core.database import DatabaseService
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
    chat_store: ChatPersistenceStore
    chat_commands: ChatCommandService
    chat_run_executor: SynchronousChatRunExecutor
    chat_dispatcher: ChatRunDispatcher
    chat_history: ChatHistoryService
    chat_title: ChatTitleService
    chat_abort: ChatAbortService
    chat_delete: ChatDeleteService
    chat_cleanup_executor: ChatCleanupJobExecutor
    progress_hub: LLMProgressHub
    upload_task_limiter: UploadTaskLimiter
    llm_config: LLMIntegrationConfig
    anythingllm_config: AnythingLLMConfig
    chat_infrastructure_config: ChatInfrastructureConfig = field(
        default_factory=ChatInfrastructureConfig.single_instance
    )

    def __post_init__(self) -> None:
        """在应用启动时拒绝缺失关键依赖，避免请求到达后才出现空引用错误。"""
        required_dependencies: dict[str, Any] = {
            "document_rag_factory": self.document_rag_factory,
            "knowledge_index_factory": self.knowledge_index_factory,
            "chat_conversation_factory": self.chat_conversation_factory,
            "task_service": self.task_service,
            "kb_service": self.kb_service,
            "chat_store": self.chat_store,
            "chat_commands": self.chat_commands,
            "chat_run_executor": self.chat_run_executor,
            "chat_dispatcher": self.chat_dispatcher,
            "chat_history": self.chat_history,
            "chat_title": self.chat_title,
            "chat_abort": self.chat_abort,
            "chat_delete": self.chat_delete,
            "chat_cleanup_executor": self.chat_cleanup_executor,
            "progress_hub": self.progress_hub,
            "upload_task_limiter": self.upload_task_limiter,
            "llm_config": self.llm_config,
            "anythingllm_config": self.anythingllm_config,
            "chat_infrastructure_config": self.chat_infrastructure_config,
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
        if not isinstance(self.chat_run_executor, SynchronousChatRunExecutor):
            raise TypeError("chat_run_executor must be SynchronousChatRunExecutor")
        if not isinstance(self.chat_dispatcher, ChatRunDispatcher):
            raise TypeError("chat_dispatcher must implement ChatRunDispatcher")
        if not isinstance(self.chat_history, ChatHistoryService):
            raise TypeError("chat_history must be ChatHistoryService")
        if not isinstance(self.chat_title, ChatTitleService):
            raise TypeError("chat_title must be ChatTitleService")
        if not isinstance(self.chat_abort, ChatAbortService):
            raise TypeError("chat_abort must be ChatAbortService")
        if not isinstance(self.chat_delete, ChatDeleteService):
            raise TypeError("chat_delete must be ChatDeleteService")
        if not isinstance(self.chat_cleanup_executor, ChatCleanupJobExecutor):
            raise TypeError("chat_cleanup_executor must be ChatCleanupJobExecutor")
        if not isinstance(self.chat_infrastructure_config, ChatInfrastructureConfig):
            raise TypeError(
                "chat_infrastructure_config must be ChatInfrastructureConfig"
            )
        self._validate_chat_infrastructure_capabilities()

    def _validate_chat_infrastructure_capabilities(self) -> None:
        """按部署模式验证已装配适配器的真实能力，禁止错误模式静默启动。

        该校验位于组合根，业务服务不需要知道 SQLite、同步 dispatcher 或轮询
        notifier 的具体类型。阶段 13 以后只需替换容器装配和能力声明即可开放
        新模式，HTTP/SSE 契约与 Chat Application Service 均不受影响。
        """
        if (
            self.chat_infrastructure_config.runtime_mode
            != "single_instance"
        ):
            # ``ChatInfrastructureConfig`` 已在构造期拒绝该路径；保留防御性
            # 检查，避免未来扩展配置时绕过适配器能力门禁。
            raise RuntimeError("unsupported chat infrastructure runtime mode")

        capabilities = {
            "persistence": self.chat_store.capabilities.supports_single_instance,
            "run_lease": self.chat_commands.lease_capabilities.supports_single_instance,
            "run_dispatcher": self.chat_dispatcher.capabilities.supports_single_instance,
            "abort_notifier": self.chat_abort.notifier_capabilities.supports_single_instance,
            "cleanup_dispatcher": self.chat_delete.cleanup_dispatcher_capabilities.supports_single_instance,
        }
        incompatible = [name for name, value in capabilities.items() if not value]
        if incompatible:
            raise RuntimeError(
                "single_instance 文件对话模式装配了不兼容适配器："
                + ", ".join(incompatible)
            )


def create_application_services() -> ApplicationServices:
    """根据环境配置创建生产应用容器，不创建 AnythingLLM 网络 Session。"""
    # 先校验部署模式，再读取任何外部集成配置或创建数据库文件。这样错误地把
    # SQLite 单实例模式配置成集群时，会在应用启动的最早阶段 fail fast。
    chat_infrastructure_config = load_chat_infrastructure_config()
    anythingllm_config = load_anythingllm_config()
    llm_config = load_llm_integration_config()
    task_service = LLMTaskService(llm_config.task_db_path)
    kb_service = DatabaseService(str(KNOWLEDGE_BASE_DB_PATH))
    chat_store = ChatStore(str(CHAT_DB_PATH))
    chat_commands = ChatCommandService(ChatRunLockService(str(CHAT_DB_PATH)))
    chat_history = ChatHistoryService(chat_store)
    chat_conversation_factory = AnythingLLMChatFactory(anythingllm_config)
    chat_cleanup_executor = ChatCleanupJobExecutor(
        store=chat_store,
        conversation_factory=chat_conversation_factory,
    )
    chat_cleanup_dispatcher = InlineChatCleanupDispatcher(
        execute=chat_cleanup_executor.execute_cleanup_job,
    )
    chat_run_executor = SynchronousChatRunExecutor(
        store=chat_store,
        chat_commands=chat_commands,
        conversation_factory=chat_conversation_factory,
        document_resolver=DatabaseChatDocumentResolver(kb_service),
    )

    # The inline dispatcher receives only a durable run_id. Its executor reloads
    # the accepted snapshot and claims the run at execution time, which keeps
    # the current synchronous path aligned with a future worker entry point.
    chat_dispatcher = InlineChatRunDispatcher(
        execute=chat_run_executor.execute_chat_run,
    )
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
        chat_conversation_factory=chat_conversation_factory,
        task_service=task_service,
        kb_service=kb_service,
        chat_store=chat_store,
        chat_commands=chat_commands,
        chat_run_executor=chat_run_executor,
        chat_dispatcher=chat_dispatcher,
        chat_history=chat_history,
        chat_title=ChatTitleService(
            store=chat_store,
            history_service=chat_history,
            conversation_factory=chat_conversation_factory,
            cleanup_dispatcher=chat_cleanup_dispatcher,
            cleanup_executor=chat_cleanup_executor,
        ),
        chat_abort=ChatAbortService(
            store=chat_store,
            chat_commands=chat_commands,
        ),
        chat_delete=ChatDeleteService(
            store=chat_store,
            chat_commands=chat_commands,
            conversation_factory=chat_conversation_factory,
            cleanup_dispatcher=chat_cleanup_dispatcher,
            cleanup_executor=chat_cleanup_executor,
        ),
        chat_cleanup_executor=chat_cleanup_executor,
        progress_hub=LLMProgressHub(),
        upload_task_limiter=UploadTaskLimiter(max_concurrency=1),
        llm_config=llm_config,
        anythingllm_config=anythingllm_config,
        chat_infrastructure_config=chat_infrastructure_config,
    )
    logger.info(
        "应用依赖容器创建完成: knowledge_index_enabled=%s "
        "upload_max_concurrency=%d chat_runtime_mode=%s",
        services.knowledge_index_factory is not None,
        services.upload_task_limiter.max_concurrency,
        services.chat_infrastructure_config.runtime_mode,
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
