"""DocSense 应用装配根、依赖容器与任务并发边界。

本模块位于 ``app`` 包根目录，因为它负责组装接口层、应用服务和外部适配器，不属于
任何单一业务 Service。容器只保存可跨请求安全共享的服务、不可变配置和无状态工厂；
任何持有网络 Session 的 AnythingLLM 对象都必须由任务级 Factory 在后台线程内部创建。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any

from flask import current_app

from app.integrations.anythingllm.factory import (
    AnythingLLMGatewayFactory,
    AnythingLLMKnowledgeIndexFactory,
)
from app.integrations.anythingllm.chat_factory import AnythingLLMChatFactory
from app.integrations.anythingllm.policies import document_rag_workspace_settings
from app.modules.report.adapters import (
    AnythingLLMReportClientFactory,
    AnythingLLMReportRagAdapter,
    FileProcessSingletonGuard,
    LegacyReportFileAdapter,
    LocalReportArtifactAdapter,
    LocalReportTaskDispatcher,
    ReportTaskCommandCodec,
    SQLiteReportCallbackAdapter,
    SQLiteReportCallbackRecoverySource,
    SQLiteReportInteractionAuditAdapter,
    SQLiteReportResourceStoreAdapter,
)
from app.modules.report.application import (
    ReportResourceRecoveryService,
    RecoverReportCallbackSynchronously,
    RunReportTask,
    SubmitReportTask,
)
from app.modules.report.ports import (
    ReportTaskDispatcherLifecyclePort,
    ReportTaskDispatcherPort,
)
from app.modules.tasks.adapters import (
    InMemoryProgressAdapter,
    LegacyTaskCommandAdapter,
    LegacyTaskReadAdapter,
    LatestTaskProgressPublisherAdapter,
    UploadTaskLimiter,
)
from app.modules.tasks.application import ProgressSubscriptionService
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
    ReportInfrastructureConfig,
    load_anythingllm_config,
    load_chat_infrastructure_config,
    load_llm_integration_config,
    load_report_infrastructure_config,
)
from app.services.core.database import DatabaseService
from app.services.core.progress_hub import LLMProgressHub
from app.services.core.settings import (
    CHAT_DB_PATH,
    KNOWLEDGE_BASE_DB_PATH,
    RUNTIME_DIR,
)
from app.services.llm_service.task_service import LLMTaskService


logger = logging.getLogger(__name__)

APPLICATION_SERVICES_EXTENSION = "docsense_services"

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
    progress_subscription_service: ProgressSubscriptionService
    upload_task_limiter: UploadTaskLimiter
    report_submit: SubmitReportTask
    report_callback_recovery: RecoverReportCallbackSynchronously
    report_dispatcher: ReportTaskDispatcherLifecyclePort
    llm_config: LLMIntegrationConfig
    anythingllm_config: AnythingLLMConfig
    report_infrastructure_config: ReportInfrastructureConfig
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
            "progress_subscription_service": self.progress_subscription_service,
            "upload_task_limiter": self.upload_task_limiter,
            "report_submit": self.report_submit,
            "report_callback_recovery": self.report_callback_recovery,
            "report_dispatcher": self.report_dispatcher,
            "llm_config": self.llm_config,
            "anythingllm_config": self.anythingllm_config,
            "report_infrastructure_config": self.report_infrastructure_config,
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
        if not isinstance(
            self.progress_subscription_service,
            ProgressSubscriptionService,
        ):
            raise TypeError(
                "progress_subscription_service 必须是 ProgressSubscriptionService"
            )
        if not isinstance(self.report_submit, SubmitReportTask):
            raise TypeError("report_submit 必须是 SubmitReportTask")
        if not isinstance(
            self.report_callback_recovery,
            RecoverReportCallbackSynchronously,
        ):
            raise TypeError(
                "report_callback_recovery 必须是 RecoverReportCallbackSynchronously"
            )
        if not isinstance(self.report_dispatcher, ReportTaskDispatcherPort):
            raise TypeError("report_dispatcher 必须实现 ReportTaskDispatcherPort")
        if not isinstance(
            self.report_dispatcher,
            ReportTaskDispatcherLifecyclePort,
        ):
            raise TypeError(
                "report_dispatcher 必须实现显式 start/stop/close 生命周期"
            )
        if self.report_submit.dispatcher is not self.report_dispatcher:
            raise ValueError(
                "report_submit 与 ApplicationServices 必须共享同一 Dispatcher 实例"
            )
        if not isinstance(
            self.report_infrastructure_config,
            ReportInfrastructureConfig,
        ):
            raise TypeError(
                "report_infrastructure_config 必须是 ReportInfrastructureConfig"
            )
        if not isinstance(self.chat_infrastructure_config, ChatInfrastructureConfig):
            raise TypeError(
                "chat_infrastructure_config must be ChatInfrastructureConfig"
            )
        self._validate_chat_infrastructure_capabilities()
        self._validate_report_infrastructure_capabilities()

    def _validate_chat_infrastructure_capabilities(self) -> None:
        """按部署模式验证已装配适配器的真实能力，禁止错误模式静默启动。

        该校验位于组合根，业务服务不需要知道 SQLite、同步 dispatcher 或轮询
        notifier 的具体类型。阶段 13 以后只需替换容器装配和能力声明即可开放
        新模式，HTTP/SSE 契约与 Chat Application Service 均不受影响。
        """
        logger.info(
            "开始校验文件对话基础设施能力: runtime_mode=%s",
            self.chat_infrastructure_config.runtime_mode,
        )
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
            logger.error(
                "文件对话基础设施能力校验失败: runtime_mode=%s incompatible_components=%s",
                self.chat_infrastructure_config.runtime_mode,
                ",".join(incompatible),
            )
            raise RuntimeError(
                "single_instance 文件对话模式装配了不兼容适配器："
                + ", ".join(incompatible)
            )
        logger.info(
            "文件对话基础设施能力校验通过: runtime_mode=%s component_count=%d",
            self.chat_infrastructure_config.runtime_mode,
            len(capabilities),
        )

    def _validate_report_infrastructure_capabilities(self) -> None:
        """在公开路由可用前校验报告受理与 Worker 的单实例能力。"""

        if self.report_infrastructure_config.runtime_mode != "single_instance":
            raise RuntimeError("unsupported report infrastructure runtime mode")
        if (
            isinstance(self.report_dispatcher, LocalReportTaskDispatcher)
            and not self.report_dispatcher.has_process_guard
        ):
            raise RuntimeError(
                "生产 LocalReportTaskDispatcher 必须装配跨进程单实例锁"
            )
        logger.info(
            "报告基础设施能力校验通过: runtime_mode=%s dispatcher_type=%s "
            "cleanup_http_timeout_seconds=%.3f cleanup_lease_seconds=%.3f",
            self.report_infrastructure_config.runtime_mode,
            type(self.report_dispatcher).__name__,
            self.report_infrastructure_config.cleanup_http_timeout_seconds,
            self.report_infrastructure_config.cleanup_lease_seconds,
        )

    def start_background_services(self) -> None:
        """显式启动容器拥有的报告后台能力。

        当前报告 Dispatcher 包含一条重型任务执行线程，以及彼此隔离的资源恢复、队列
        诊断维护线程。这里所说的“单 Worker”只约束报告业务执行并发，不代表维护工作
        继续与模型调用串在同一线程中。
        """

        logger.info("开始启动 DocSense 后台服务")
        self.report_dispatcher.start()
        logger.info("DocSense 后台服务启动完成")

    def stop_background_services(self, *, timeout_seconds: float | None = None) -> bool:
        """停止领取新报告并有限等待当前任务；不重置 running execution。"""

        return self.report_dispatcher.stop(timeout_seconds=timeout_seconds)

    def close(self) -> None:
        """幂等关闭容器拥有的后台生命周期。"""

        self.report_dispatcher.close()


def create_application_services() -> ApplicationServices:
    """根据环境配置创建生产应用容器，不创建 AnythingLLM 网络 Session。"""
    logger.info("开始创建 DocSense 应用依赖容器")
    # 先校验部署模式，再读取任何外部集成配置或创建数据库文件。这样错误地把
    # SQLite 单实例模式配置成集群时，会在应用启动的最早阶段 fail fast。
    chat_infrastructure_config = load_chat_infrastructure_config()
    report_infrastructure_config = load_report_infrastructure_config()
    logger.info(
        "已读取单实例基础设施配置: chat_runtime_mode=%s report_runtime_mode=%s",
        chat_infrastructure_config.runtime_mode,
        report_infrastructure_config.runtime_mode,
    )
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

    # 内联调度器只接收持久化 run_id。执行器会重新加载已受理快照，并在执行时领取运行权，
    # 因而当前同步路径与未来工作进程入口保持一致。
    chat_dispatcher = InlineChatRunDispatcher(
        execute=chat_run_executor.execute_chat_run,
    )
    # 旧业务发布方与新应用服务必须共享同一个 Hub。类型化 Adapter 只做边界转换，
    # 不另建 latest 或订阅者副本，避免切换期出现两个权威进度源。
    progress_hub = LLMProgressHub()
    progress_adapter = InMemoryProgressAdapter(progress_hub)
    progress_subscription_service = ProgressSubscriptionService(
        progress_snapshots=progress_adapter,
        progress_subscriptions=progress_adapter,
        task_reader=LegacyTaskReadAdapter(task_service),
    )
    upload_task_limiter = UploadTaskLimiter(max_concurrency=1)

    # Report 组合根只共享无网络 Session 的工厂、线程安全 Port 和 SQLite Service。
    # 生成与清理使用两个独立 Client Factory：前者保留 ANYTHINGLLM_TIMEOUT 的既有
    # 语义，后者强制有限 60 秒 HTTP 超时，确保 90 秒清理租约具备可判定边界。
    report_task_commands = LegacyTaskCommandAdapter(
        task_service,
        ReportTaskCommandCodec(),
    )
    report_progress_publisher = LatestTaskProgressPublisherAdapter(
        task_commands=report_task_commands,
        delegate=progress_adapter,
    )
    report_artifacts = LocalReportArtifactAdapter(RUNTIME_DIR / "tasks")
    report_files = LegacyReportFileAdapter(
        report_artifacts,
        download_timeout=llm_config.download_timeout,
        max_download_bytes=report_infrastructure_config.max_download_bytes,
    )
    report_rag = AnythingLLMReportRagAdapter(
        AnythingLLMReportClientFactory(anythingllm_config),
        artifact_path_resolver=report_artifacts.resolve_path,
    )
    report_cleanup_rag = AnythingLLMReportRagAdapter(
        AnythingLLMReportClientFactory(
            replace(
                anythingllm_config,
                timeout=(
                    report_infrastructure_config.cleanup_http_timeout_seconds
                ),
            )
        ),
        artifact_path_resolver=report_artifacts.resolve_path,
    )
    report_audit = SQLiteReportInteractionAuditAdapter(task_service)
    report_resources = ReportResourceRecoveryService(
        store=SQLiteReportResourceStoreAdapter(task_service),
        artifacts=report_artifacts,
        rag=report_cleanup_rag,
        audit=report_audit,
        external_attempt_timeout_seconds=(
            report_infrastructure_config.cleanup_lease_seconds
        ),
        sweep_retry_delay_seconds=(
            report_infrastructure_config.resource_sweep_interval_seconds
        ),
    )
    report_callbacks = SQLiteReportCallbackAdapter(
        task_service,
        callback_url=llm_config.callback_url or "",
        callback_timeout=llm_config.callback_timeout,
        lease_seconds=max(30.0, llm_config.callback_timeout + 5.0),
    )
    report_runner = RunReportTask(
        task_commands=report_task_commands,
        progress_publisher=report_progress_publisher,
        files=report_files,
        artifacts=report_artifacts,
        rag=report_rag,
        audit=report_audit,
        callbacks=report_callbacks,
        resources=report_resources,
    )
    report_callback_recovery = RecoverReportCallbackSynchronously(
        source=SQLiteReportCallbackRecoverySource(task_service),
        callbacks=report_callbacks,
    )
    report_dispatcher = LocalReportTaskDispatcher(
        task_commands=report_task_commands,
        queue_inspector=report_task_commands,
        resources=report_resources,
        callbacks=report_callbacks,
        execute=report_runner.execute,
        config=report_infrastructure_config,
        execution_limiter=upload_task_limiter,
        process_guard=FileProcessSingletonGuard(
            RUNTIME_DIR / "locks" / "report-dispatcher.lock"
        ),
    )
    report_submit = SubmitReportTask(
        task_commands=report_task_commands,
        progress_publisher=report_progress_publisher,
        dispatcher=report_dispatcher,
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
        progress_hub=progress_hub,
        progress_subscription_service=progress_subscription_service,
        upload_task_limiter=upload_task_limiter,
        report_submit=report_submit,
        report_callback_recovery=report_callback_recovery,
        report_dispatcher=report_dispatcher,
        llm_config=llm_config,
        anythingllm_config=anythingllm_config,
        report_infrastructure_config=report_infrastructure_config,
        chat_infrastructure_config=chat_infrastructure_config,
    )
    logger.info(
        "应用依赖容器创建完成: knowledge_index_enabled=%s "
        "upload_max_concurrency=%d chat_runtime_mode=%s report_runtime_mode=%s",
        services.knowledge_index_factory is not None,
        services.upload_task_limiter.max_concurrency,
        services.chat_infrastructure_config.runtime_mode,
        services.report_infrastructure_config.runtime_mode,
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
