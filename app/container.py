"""DocSense 应用装配根、依赖容器与任务并发边界。

本模块位于 ``app`` 包根目录，因为它负责组装接口层、应用服务和外部适配器，不属于
任何单一业务 Service。容器只保存可跨请求安全共享的服务、不可变配置和无状态工厂；
任何持有网络 Session 的 AnythingLLM 对象都必须由任务级 Factory 在后台线程内部创建。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import uuid4

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
from app.modules.report.adapters.local_dispatcher import LocalReportDispatcherSnapshot
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
from app.modules.reassign.adapters import (
    AnythingLLMReassignmentClientFactory,
    AnythingLLMReassignmentKnowledgeAdapterFactory,
    SQLiteReassignmentRepository,
    load_reassignment_infrastructure_config,
)
from app.modules.reassign.application import ReassignmentExecutionSettings
from app.modules.reassign.composition import (
    ReassignApplicationServices,
    compose_reassign_application_services,
)
from app.modules.tasks.adapters import (
    FileProcessSingletonGuard as GenericFileProcessSingletonGuard,
    InMemoryProgressAdapter,
    LegacyTaskCommandAdapter,
    LegacyTaskReadAdapter,
    LatestTaskProgressPublisherAdapter,
    UploadTaskLimiter,
    required_http_lease_seconds,
)
from app.modules.tasks.application import ProgressSubscriptionService
from app.modules.weaponry.adapters import (
    AnythingLLMProvidedEvidenceExtractionAdapter,
    AnythingLLMWeaponryCreationIntentRecoveryAdapter,
    AnythingLLMReadOnlyTermsRuleProvider,
    AnythingLLMTargetEvidenceRetrievalAdapter,
    AnythingLLMWeaponryClientFactory,
    AnythingLLMWeaponryResourceCleanupAdapter,
    DatabaseServiceWeaponryDocumentScopeAdapter,
    LLMTranslationServiceWeaponryAdapter,
    NoAuxiliaryGuidanceAdapter,
    SQLiteWeaponryCallbackAdapter,
    SQLiteWeaponryCallbackRecoverySource,
    SQLiteWeaponryCreationIntentStoreAdapter,
    SQLiteWeaponryInteractionAuditAdapter,
    SQLiteWeaponryResourceStoreAdapter,
    StoreBackedWeaponryResourceRegistrar,
    TermsRuleGuidanceAdapter,
    WeaponryRuntimeCapabilities,
    WeaponryProductionGateSnapshot,
    WeaponryTaskCommandCodec,
    load_weaponry_infrastructure_config,
)
from app.modules.weaponry.adapters.local_dispatcher import (
    LocalWeaponryDispatcherSnapshot,
)
from app.modules.weaponry.composition import (
    WeaponryApplicationServices,
    compose_weaponry_application_services,
)
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
from app.services.llm_service.translation_service import get_translation_service


logger = logging.getLogger(__name__)

APPLICATION_SERVICES_EXTENSION = "docsense_services"


@dataclass(frozen=True)
class ApplicationReadinessSnapshot:
    """内部编排读取的机器就绪快照；不新增公开 HTTP 接口。"""

    ready: bool
    lifecycle_ready: bool
    production_gate_ready: bool
    reasons: tuple[str, ...]
    report: LocalReportDispatcherSnapshot | None
    weaponry: LocalWeaponryDispatcherSnapshot | None
    weaponry_production_gate: WeaponryProductionGateSnapshot | None

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
    # 生产工厂在 1D-6 必须装配完整新链。``None`` 仅保留给不覆盖 weaponry 路由的旧式
    # 单元测试夹具；公开路由遇到 None 会明确失败，绝不会回退到遗留线程。
    weaponry_services: WeaponryApplicationServices | None = None
    # 1E-6 的同步 Saga 生产链。None 仅用于不覆盖 reassign 路由的旧测试夹具；公开路由
    # 必须 fail fast，绝不能回退到已删除的蓝图数据库/AnythingLLM 编排。
    reassign_services: ReassignApplicationServices | None = None

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
        if self.weaponry_services is not None:
            if not isinstance(
                self.weaponry_services,
                WeaponryApplicationServices,
            ):
                raise TypeError(
                    "weaponry_services 必须是 WeaponryApplicationServices 或 None"
                )
            if (
                self.weaponry_services.execution_limiter
                is not self.upload_task_limiter
            ):
                raise ValueError(
                    "Weaponry 必须与 Report/Analysis 共享同一重型任务 limiter"
                )
        if self.reassign_services is not None and not isinstance(
            self.reassign_services,
            ReassignApplicationServices,
        ):
            raise TypeError(
                "reassign_services 必须是 ReassignApplicationServices 或 None"
            )
        self._validate_chat_infrastructure_capabilities()
        self._validate_report_infrastructure_capabilities()
        self._validate_weaponry_infrastructure_capabilities()

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

    def _validate_weaponry_infrastructure_capabilities(self) -> None:
        """只在显式装配时验证 Weaponry；缺省不能用隐式 no-op 伪装就绪。"""

        if self.weaponry_services is None:
            logger.info(
                "测试依赖容器未装配武器谱运行链: production_factory=false"
            )
            return
        dispatcher = self.weaponry_services.dispatcher
        if not dispatcher.has_process_guard:
            raise RuntimeError(
                "生产 LocalWeaponryTaskDispatcher 必须装配跨进程单实例锁"
            )
        production_gate = self.weaponry_services.production_gate_snapshot()
        if not production_gate.ready:
            # 开发阶段仍使用当前环境，因此默认只告警并保持 readiness=false；生产/Docker
            # 必须显式打开 fail-fast，避免编排遗漏探针时仍然启动公开路由。
            logger.warning(
                "武器谱真实供应商能力门禁尚未关闭: reason=%s profile_id=%s",
                production_gate.reason,
                production_gate.profile_id,
            )
            if self.weaponry_services.config.production_gate_required:
                raise RuntimeError(
                    "生产配置要求 Weaponry readiness 门禁通过："
                    f"{production_gate.reason}"
                )
        logger.info(
            "武器谱运行链能力校验通过: runtime_mode=%s "
            "profile_id=%s dispatcher_type=%s",
            self.weaponry_services.config.runtime_mode,
            self.weaponry_services.policies.evidence_selection.profile_id,
            type(dispatcher).__name__,
        )

    def readiness_snapshot(self) -> ApplicationReadinessSnapshot:
        """汇总 Dispatcher 生命周期、致命错误与真实供应商门禁。

        该方法只读内存快照和本地证明文件，不访问 AnythingLLM，也不改变前后端接口。
        后续 Docker Compose/进程管理器可通过内部启动探针调用它，避免“线程已创建”被
        误判为业务可接流量。
        """

        report_snapshot_reader = getattr(self.report_dispatcher, "snapshot", None)
        report = (
            report_snapshot_reader()
            if callable(report_snapshot_reader)
            else None
        )
        weaponry = (
            self.weaponry_services.snapshot()
            if self.weaponry_services is not None
            else None
        )
        gate = (
            self.weaponry_services.production_gate_snapshot()
            if self.weaponry_services is not None
            else None
        )
        reasons: list[str] = []
        if report is None:
            reasons.append("report_dispatcher_snapshot_unavailable")
        elif not report.ready:
            reasons.append(
                f"report_dispatcher_not_ready:{report.fatal_error or report.lifecycle_state}"
            )
        if weaponry is None:
            reasons.append("weaponry_services_not_bound")
        elif not weaponry.ready:
            reasons.append(
                "weaponry_dispatcher_not_ready:"
                f"{weaponry.fatal_error or weaponry.lifecycle_state}"
            )
        if gate is None:
            reasons.append("weaponry_production_gate_unavailable")
        elif not gate.ready:
            reasons.append(f"weaponry_production_gate:{gate.reason}")
        lifecycle_ready = (
            report is not None
            and report.ready
            and weaponry is not None
            and weaponry.ready
        )
        production_gate_ready = gate is not None and gate.ready
        return ApplicationReadinessSnapshot(
            ready=lifecycle_ready and production_gate_ready,
            lifecycle_ready=lifecycle_ready,
            production_gate_ready=production_gate_ready,
            reasons=tuple(reasons),
            report=report,
            weaponry=weaponry,
            weaponry_production_gate=gate,
        )

    def start_background_services(self) -> None:
        """显式启动容器拥有的本地后台能力。

        当前报告 Dispatcher 包含一条重型任务执行线程，以及彼此隔离的资源恢复、队列
        诊断维护线程。这里所说的“单 Worker”只约束报告业务执行并发，不代表维护工作
        继续与模型调用串在同一线程中。
        """

        logger.info("开始启动 DocSense 后台服务")
        self.report_dispatcher.start()
        if self.weaponry_services is not None:
            try:
                self.weaponry_services.start()
            except Exception:
                # 第二个组件启动失败时不能留下“应用创建失败但报告 Worker 仍运行”的
                # 半启动进程。仅停止线程，不回退任何 running execution。
                rolled_back = self.report_dispatcher.stop(
                    timeout_seconds=(
                        self.report_infrastructure_config.stop_timeout_seconds
                    )
                )
                if not rolled_back:
                    logger.critical(
                        "Weaponry 启动失败且 Report 回滚停机超时，必须终止当前进程"
                    )
                raise
        logger.info("DocSense 后台服务启动完成")

    def stop_background_services(self, *, timeout_seconds: float | None = None) -> bool:
        """停止领取新任务并有限等待当前函数；不重置 running execution。"""

        weaponry_stopped = True
        if self.weaponry_services is not None:
            weaponry_stopped = self.weaponry_services.stop(
                timeout_seconds=timeout_seconds
            )
        report_stopped = self.report_dispatcher.stop(
            timeout_seconds=timeout_seconds
        )
        return weaponry_stopped and report_stopped

    def close(self) -> None:
        """幂等关闭容器拥有的后台生命周期。"""

        try:
            if self.weaponry_services is not None:
                self.weaponry_services.close()
        finally:
            self.report_dispatcher.close()


def create_application_services() -> ApplicationServices:
    """根据环境配置创建生产应用容器，不创建 AnythingLLM 网络 Session。"""
    logger.info("开始创建 DocSense 应用依赖容器")
    # 先校验部署模式，再读取任何外部集成配置或创建数据库文件。这样错误地把
    # SQLite 单实例模式配置成集群时，会在应用启动的最早阶段 fail fast。
    chat_infrastructure_config = load_chat_infrastructure_config()
    report_infrastructure_config = load_report_infrastructure_config()
    weaponry_infrastructure_config = load_weaponry_infrastructure_config()
    reassign_infrastructure_config = load_reassignment_infrastructure_config()
    logger.info(
        "已读取单实例基础设施配置: chat_runtime_mode=%s report_runtime_mode=%s "
        "weaponry_runtime_mode=%s reassign_runtime_mode=%s "
        "reassign_total_timeout_seconds=%.3f",
        chat_infrastructure_config.runtime_mode,
        report_infrastructure_config.runtime_mode,
        weaponry_infrastructure_config.runtime_mode,
        reassign_infrastructure_config.runtime_mode,
        reassign_infrastructure_config.total_timeout_seconds,
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

    # 分类节点变更仍是同步接口，但其跨系统写入已经由 Application 的持久化 Saga 管理。
    # Container 只构造无状态 Factory 和单一应用外观：没有共享 HTTP Session，没有后台线程，也
    # 不直接调用任何 Repository 的终态收口接口。每个请求由 Knowledge Factory 创建独立 deadline
    # 与 Transport，实例 owner 仅用于 SQLite lease/fencing，绝不进入公开响应。
    reassign_instance_id = f"reassign-{uuid4().hex}"
    reassign_settings = ReassignmentExecutionSettings(
        lease_owner=reassign_instance_id,
        lease_duration_seconds=(
            reassign_infrastructure_config.total_timeout_seconds
            + reassign_infrastructure_config.compensation_reserve_seconds
        ),
        remote_total_timeout_seconds=(
            reassign_infrastructure_config.total_timeout_seconds
        ),
        # 同步总预算后仍保留补偿窗口，作为显式非零 lease 安全余量；真实环境校准前不把
        # 默认数值描述为容量结论。
        lease_safety_margin_seconds=(
            reassign_infrastructure_config.compensation_reserve_seconds
        ),
    )
    reassign_services = compose_reassign_application_services(
        repository=SQLiteReassignmentRepository(str(KNOWLEDGE_BASE_DB_PATH)),
        knowledge_factory=AnythingLLMReassignmentKnowledgeAdapterFactory(
            AnythingLLMReassignmentClientFactory(anythingllm_config),
            reassign_infrastructure_config,
        ),
        settings=reassign_settings,
        infrastructure_config=reassign_infrastructure_config,
    )

    # Report 组合根只共享无网络 Session 的工厂、线程安全 Port 和 SQLite Service。
    # 生成与清理使用两个独立 Client Factory：前者保留 ANYTHINGLLM_TIMEOUT 的既有
    # 语义，后者强制有限 60 秒分阶段 HTTP 超时；130 秒租约覆盖连接、读取和提交余量。
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
        lease_seconds=max(
            30.0,
            required_http_lease_seconds(llm_config.callback_timeout),
        ),
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

    # Weaponry 与 Report 共享任务数据库、进度 Hub 和重型任务 limiter，但拥有独立业务
    # TaskCommand、Callback Guard、资源事实、进程锁和 Worker。所有 AnythingLLM 工厂只
    # 保存不可变配置；真正的 HTTP Session 仍在单次任务/清理调用内创建并关闭。
    weaponry_task_commands = LegacyTaskCommandAdapter(
        task_service,
        WeaponryTaskCommandCodec(),
    )
    weaponry_progress_publisher = LatestTaskProgressPublisherAdapter(
        task_commands=weaponry_task_commands,
        delegate=progress_adapter,
    )
    weaponry_resources = SQLiteWeaponryResourceStoreAdapter(
        llm_config.task_db_path,
        cleanup_lease_seconds=(
            weaponry_infrastructure_config.cleanup_lease_seconds
        ),
        retry_delay_seconds=(
            weaponry_infrastructure_config.maintenance_interval_seconds
        ),
    )
    weaponry_creation_intents = SQLiteWeaponryCreationIntentStoreAdapter(
        llm_config.task_db_path
    )
    # 同一容器内的 Worker 与维护器共享运行实例标识。新 pending 意图带上该归属后，
    # 维护器会跳过本实例仍在执行的创建窗口；进程重启则生成新标识，遗留意图可被接管。
    weaponry_instance_id = uuid4().hex
    weaponry_resource_registrar = StoreBackedWeaponryResourceRegistrar(
        weaponry_resources,
        weaponry_creation_intents,
        instance_id=weaponry_instance_id,
    )
    weaponry_client_factory = AnythingLLMWeaponryClientFactory(anythingllm_config)
    weaponry_cleanup_client_factory = AnythingLLMWeaponryClientFactory(
        replace(
            anythingllm_config,
            timeout=weaponry_infrastructure_config.cleanup_http_timeout_seconds,
        )
    )
    weaponry_retrieval = AnythingLLMTargetEvidenceRetrievalAdapter(
        weaponry_client_factory,
        weaponry_resource_registrar,
        provider_fingerprint=(
            weaponry_infrastructure_config.provider_fingerprint
        ),
        embedding_fingerprint=(
            weaponry_infrastructure_config.embedding_fingerprint
        ),
    )
    weaponry_extraction = AnythingLLMProvidedEvidenceExtractionAdapter(
        weaponry_client_factory,
        weaponry_resource_registrar,
        model_fingerprint=(
            weaponry_infrastructure_config.extraction_model_fingerprint
        ),
    )
    if weaponry_infrastructure_config.terms_rule_context_enabled:
        # 启用分支只读预先配置的共享术语 workspace；它不拥有上传、绑定或删除权限。
        weaponry_guidance = TermsRuleGuidanceAdapter(
            AnythingLLMReadOnlyTermsRuleProvider(
                weaponry_client_factory,
                workspace_slug=(
                    weaponry_infrastructure_config.terms_workspace_name or ""
                ),
            ),
            catalog_fingerprint=(
                weaponry_infrastructure_config.terms_catalog_fingerprint or ""
            ),
        )
    else:
        weaponry_guidance = NoAuxiliaryGuidanceAdapter()
    weaponry_audit = SQLiteWeaponryInteractionAuditAdapter(
        llm_config.task_db_path
    )
    weaponry_callbacks = SQLiteWeaponryCallbackAdapter(
        task_service,
        callback_url=llm_config.callback_url or "",
        callback_timeout=llm_config.callback_timeout,
        lease_seconds=max(
            30.0,
            required_http_lease_seconds(llm_config.callback_timeout),
        ),
    )
    weaponry_services = compose_weaponry_application_services(
        task_commands=weaponry_task_commands,
        progress_publisher=weaponry_progress_publisher,
        retrieval=weaponry_retrieval,
        extraction=weaponry_extraction,
        guidance=weaponry_guidance,
        translation=LLMTranslationServiceWeaponryAdapter(
            get_translation_service()
        ),
        audit=weaponry_audit,
        callbacks=weaponry_callbacks,
        callback_recovery_source=SQLiteWeaponryCallbackRecoverySource(
            task_service
        ),
        resources=weaponry_resources,
        resource_cleaner=AnythingLLMWeaponryResourceCleanupAdapter(
            weaponry_cleanup_client_factory
        ),
        document_scope=DatabaseServiceWeaponryDocumentScopeAdapter(kb_service),
        execution_limiter=upload_task_limiter,
        process_guard=GenericFileProcessSingletonGuard(
            RUNTIME_DIR / "locks" / "weaponry-dispatcher.lock",
            component_name="武器谱本地 Dispatcher",
        ),
        config=weaponry_infrastructure_config,
        capabilities=WeaponryRuntimeCapabilities(
            provider_fingerprint=(
                weaponry_infrastructure_config.provider_fingerprint
            ),
            embedding_fingerprint=(
                weaponry_infrastructure_config.embedding_fingerprint
            ),
            document_processing_fingerprint=(
                weaponry_infrastructure_config.document_processing_fingerprint
            ),
            extraction_model_fingerprint=(
                weaponry_infrastructure_config.extraction_model_fingerprint
            ),
            query_version=weaponry_infrastructure_config.query_version,
            score_semantics=weaponry_infrastructure_config.score_semantics,
            score_protocol=weaponry_infrastructure_config.score_protocol,
            ranking_strategy=weaponry_infrastructure_config.ranking_strategy,
            extraction_context_strategy=(
                weaponry_infrastructure_config.extraction_context_strategy
            ),
        ),
        creation_intent_recovery=(
            AnythingLLMWeaponryCreationIntentRecoveryAdapter(
                weaponry_cleanup_client_factory,
                weaponry_creation_intents,
                weaponry_resources,
                instance_id=weaponry_instance_id,
                lease_seconds=(
                    weaponry_infrastructure_config.cleanup_lease_seconds
                ),
            )
        ),
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
        weaponry_services=weaponry_services,
        reassign_services=reassign_services,
    )
    logger.info(
        "应用依赖容器创建完成: knowledge_index_enabled=%s "
        "upload_max_concurrency=%d chat_runtime_mode=%s report_runtime_mode=%s "
        "weaponry_runtime_bound=%s reassign_runtime_bound=%s",
        services.knowledge_index_factory is not None,
        services.upload_task_limiter.max_concurrency,
        services.chat_infrastructure_config.runtime_mode,
        services.report_infrastructure_config.runtime_mode,
        services.weaponry_services is not None,
        services.reassign_services is not None,
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
