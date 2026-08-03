"""供路由与契约测试复用的完全离线应用依赖工厂。

该模块只位于测试包中。它使用临时 SQLite 文件和端口 Fake 组装完整的
``ApplicationServices``，不会读取生产配置、创建网络 Session，也不会连接
AnythingLLM、模型服务或回调接收方。

把测试容器集中在这里有两个目的：

1. 路由测试必须显式声明依赖，避免 ``create_app()`` 隐式构建生产容器；
2. 后续替换 Web 框架或基础设施适配器时，契约测试仍可复用同一组业务依赖。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Iterable

from app.container import ApplicationServices, UploadTaskLimiter
from app.modules.analysis.adapters import (
    AnalysisTranslationExecutionCoordinator,
    LegacyAnalysisAuditAdapter,
    LegacyAnalysisFilePreparationAdapter,
    LegacyAnalysisKnowledgeAdapter,
    LegacyAnalysisRagAdapterFactory,
    LocalAnalysisTaskWorkspaceAdapter,
    SQLiteAnalysisBatchCommandAdapter,
    SQLiteAnalysisCallbackAdapter,
    SQLiteAnalysisCallbackRecoverySource,
    SQLiteAnalysisResourceStoreAdapter,
    SerializedAnalysisTranslationAdapter,
)
from app.modules.analysis.composition import compose_analysis_application_services
from app.modules.debug.composition import compose_debug_application_services
from app.modules.analysis.ports import (
    AnalysisCallbackDelivery,
    AnalysisCallbackDeliveryOutcome,
    AnalysisCallbackDeliveryRequest,
)
from app.modules.report.adapters import (
    ReportTaskCommandCodec,
    SQLiteReportCallbackAdapter,
    SQLiteReportCallbackRecoverySource,
)
from app.modules.report.application import (
    RecoverReportCallbackSynchronously,
    SubmitReportTask,
)
from app.modules.tasks.adapters import (
    FileProcessSingletonGuard,
    InMemoryProgressAdapter,
    LegacyTaskCommandAdapter,
    LegacyTaskReadAdapter,
    LatestTaskProgressPublisherAdapter,
)
from app.modules.tasks.application import ProgressSubscriptionService
from app.modules.weaponry.adapters import (
    DatabaseServiceWeaponryDocumentScopeAdapter,
    SQLiteWeaponryCallbackAdapter,
    SQLiteWeaponryCallbackRecoverySource,
    SQLiteWeaponryInteractionAuditAdapter,
    SQLiteWeaponryResourceStoreAdapter,
    WeaponryInfrastructureConfig,
    WeaponryRuntimeCapabilities,
    WeaponryTaskCommandCodec,
)
from app.modules.weaponry.composition import compose_weaponry_application_services
from app.modules.chat import (
    ChatAbortService,
    ChatCleanupJobExecutor,
    ChatCommandService,
    ChatDeleteService,
    ChatHistoryService,
    ChatRunLockService,
    ChatStore,
    ChatTitleService,
    DatabaseChatDocumentResolver,
    InlineChatCleanupDispatcher,
    InlineChatRunDispatcher,
    SynchronousChatRunExecutor,
)
from app.services.core.config import (
    AnalysisInfrastructureConfig,
    AnythingLLMConfig,
    LLMIntegrationConfig,
    ReportInfrastructureConfig,
)
from app.services.core.database import DatabaseService
from app.services.core.progress_hub import LLMProgressHub
from app.services.llm_service.task_service import LLMTaskService
from tests.document_processing_fixtures import (
    build_test_document_preparer,
    build_test_rag_projector,
)
from tests.fakes import (
    FakeChatConversationFactory,
    FakeDocumentRagFactory,
    FakeKnowledgeIndexFactory,
    FakeReportDispatcherPort,
    FakeAuxiliaryGuidancePort,
    FakeEvidenceExtractionPort,
    FakeTargetEvidenceRetrievalPort,
    FakeWeaponryExternalResourceCleanupPort,
    FakeWeaponryTranslationPort,
    InvocationRecorder,
    WeaponryInvocationRecorder,
)


logger = logging.getLogger(__name__)


class _OfflineAnalysisTranslationService:
    """离线路由夹具的翻译替身，确保组合阶段不触发真实模型服务。"""

    def translate_document(self, *args: object, **kwargs: object) -> tuple[str, str]:
        """返回稳定空翻译；当前路由测试不会启动 Worker 调用该方法。"""

        return ("", "")


def _offline_analysis_callback_transport(
    request: AnalysisCallbackDeliveryRequest,
) -> AnalysisCallbackDelivery:
    """模拟严格成功投递，禁止离线路由测试访问真实 Callback 接收方。"""

    return AnalysisCallbackDelivery(
        execution=request.lease.execution,
        lease_token=request.lease.lease_token,
        lease_version=request.lease.lease_version,
        outcome=AnalysisCallbackDeliveryOutcome.DELIVERED,
    )


def _ignore_offline_analysis_callback_history(
    _payload: object,
    *,
    callback_context: object,
) -> None:
    """离线测试不写运行目录下的非权威回调历史副本。"""

    del callback_context


def build_offline_application_services(
    runtime_directory: str | Path,
    *,
    chat_stream_contents: Iterable[str] = ("第一段", "第二段"),
    max_upload_concurrency: int = 1,
    callback_url: str | None = None,
    analysis_callback_transport: Callable[
        [AnalysisCallbackDeliveryRequest],
        AnalysisCallbackDelivery,
    ]
    | None = None,
) -> ApplicationServices:
    """组装不访问网络的完整测试依赖容器。

    Args:
        runtime_directory: 本次测试独占的运行目录。任务、知识库和文件对话
            分别使用独立 SQLite 文件，避免用例间状态串扰。
        chat_stream_contents: Fake 对话端口返回的流式文本片段。
        max_upload_concurrency: 上传类任务的测试并发上限。
        callback_url: 仅用于测试 Analysis/Report/Weaponry 回调恢复的假地址。
        analysis_callback_transport: 可选的文件 Analysis 回调替身；缺省时稳定模拟严格
            成功投递，绝不建立真实网络连接。

    Returns:
        可以直接传给 ``create_app(services=...)`` 的完整依赖容器。

    Raises:
        ValueError: 运行目录为空，或并发上限不合法时由下层组件拒绝。
    """
    root = Path(runtime_directory)
    if not str(root).strip():
        raise ValueError("runtime_directory 不能为空")
    root.mkdir(parents=True, exist_ok=True)

    task_db_path = root / "tasks.sqlite3"
    knowledge_db_path = root / "knowledge.sqlite3"
    chat_db_path = root / "chat.sqlite3"

    logger.debug(
        "开始组装离线应用依赖: runtime_directory=%s max_upload_concurrency=%d",
        root,
        max_upload_concurrency,
    )

    task_service = LLMTaskService(db_path=str(task_db_path))
    progress_hub = LLMProgressHub()
    progress_adapter = InMemoryProgressAdapter(progress_hub)
    progress_subscription_service = ProgressSubscriptionService(
        progress_snapshots=progress_adapter,
        progress_subscriptions=progress_adapter,
        task_reader=LegacyTaskReadAdapter(task_service),
    )
    report_task_commands = LegacyTaskCommandAdapter(
        task_service,
        ReportTaskCommandCodec(),
    )
    report_dispatcher = FakeReportDispatcherPort(InvocationRecorder())
    report_submit = SubmitReportTask(
        task_commands=report_task_commands,
        progress_publisher=LatestTaskProgressPublisherAdapter(
            task_commands=report_task_commands,
            delegate=progress_adapter,
        ),
        dispatcher=report_dispatcher,
    )
    report_callback_adapter = SQLiteReportCallbackAdapter(
        task_service,
        callback_url=callback_url or "",
        callback_timeout=5.0,
        lease_seconds=30.0,
    )
    report_callback_recovery = RecoverReportCallbackSynchronously(
        source=SQLiteReportCallbackRecoverySource(task_service),
        callbacks=report_callback_adapter,
    )
    knowledge_service = DatabaseService(db_path=str(knowledge_db_path))
    chat_store = ChatStore(db_path=str(chat_db_path))
    chat_commands = ChatCommandService(ChatRunLockService(str(chat_db_path)))
    chat_history = ChatHistoryService(chat_store)
    chat_conversation_factory = FakeChatConversationFactory(
        stream_contents=tuple(chat_stream_contents),
    )

    chat_run_executor = SynchronousChatRunExecutor(
        store=chat_store,
        chat_commands=chat_commands,
        conversation_factory=chat_conversation_factory,
        document_resolver=DatabaseChatDocumentResolver(knowledge_service),
    )
    chat_cleanup_executor = ChatCleanupJobExecutor(
        store=chat_store,
        conversation_factory=chat_conversation_factory,
    )
    chat_cleanup_dispatcher = InlineChatCleanupDispatcher(
        execute=chat_cleanup_executor.execute_cleanup_job,
    )
    chat_dispatcher = InlineChatRunDispatcher(
        execute=chat_run_executor.execute_chat_run,
    )

    upload_task_limiter = UploadTaskLimiter(
        max_concurrency=max_upload_concurrency,
    )
    llm_integration_config = LLMIntegrationConfig(
        callback_url=callback_url,
        callback_timeout=5.0,
        task_db_path=str(task_db_path),
        download_timeout=5.0,
    )
    analysis_config = AnalysisInfrastructureConfig.single_instance()
    analysis_task_commands = SQLiteAnalysisBatchCommandAdapter(task_service)
    analysis_callbacks = SQLiteAnalysisCallbackAdapter(
        task_service,
        callback_timeout=analysis_config.callback_http_timeout_seconds,
        lease_seconds=analysis_config.callback_lease_seconds,
        transport=(
            analysis_callback_transport
            or _offline_analysis_callback_transport
        ),
        history_writer=_ignore_offline_analysis_callback_history,
    )
    # 显式注入的 Flask 测试容器不会调用 start_background_services，因此这里只构造
    # Dispatcher、受理和同步恢复链，不会创建后台线程或执行文件/RAG/模型 I/O。
    analysis_document_preparer = build_test_document_preparer(
        root / "analysis-document-processing"
    )
    analysis_services = compose_analysis_application_services(
        task_commands=analysis_task_commands,
        progress_publisher=LatestTaskProgressPublisherAdapter(
            task_commands=analysis_task_commands,
            delegate=progress_adapter,
        ),
        workspaces=LocalAnalysisTaskWorkspaceAdapter(str(root / "analysis-tasks")),
        files=LegacyAnalysisFilePreparationAdapter(
            download_timeout_seconds=llm_integration_config.download_timeout,
            document_preparer=analysis_document_preparer,
            rag_projector=build_test_rag_projector(
                analysis_document_preparer,
                root / "analysis-rag-projection",
            ),
        ),
        rag_factory=LegacyAnalysisRagAdapterFactory(FakeDocumentRagFactory()),
        knowledge=LegacyAnalysisKnowledgeAdapter(FakeKnowledgeIndexFactory()),
        audit=LegacyAnalysisAuditAdapter(task_service),
        translation=SerializedAnalysisTranslationAdapter(
            _OfflineAnalysisTranslationService(),
            AnalysisTranslationExecutionCoordinator(),
        ),
        callbacks=analysis_callbacks,
        callback_recovery_source=SQLiteAnalysisCallbackRecoverySource(
            task_service
        ),
        resources=SQLiteAnalysisResourceStoreAdapter(task_service),
        execution_limiter=upload_task_limiter,
        process_guard=FileProcessSingletonGuard(
            root / "locks" / "offline-analysis-dispatcher.lock",
            component_name="离线文件分析 Dispatcher",
        ),
        config=analysis_config,
        callback_url=callback_url or "",
    )
    weaponry_config = WeaponryInfrastructureConfig(
        runtime_mode="single_instance",
        scan_interval_seconds=0.02,
        accepted_batch_size=50,
        dispatch_failure_retry_seconds=1.0,
        maintenance_interval_seconds=0.05,
        maintenance_limit=50,
        running_sample_limit=10,
        stop_timeout_seconds=0.5,
        cleanup_http_timeout_seconds=1.0,
        cleanup_lease_seconds=7.0,
        provider_fingerprint="offline-provider-v1",
        embedding_fingerprint="offline-embedding-v1",
        document_processing_fingerprint="offline-processing-v1",
        extraction_model_fingerprint="offline-extraction-v1",
    )
    weaponry_capabilities = WeaponryRuntimeCapabilities(
        provider_fingerprint=weaponry_config.provider_fingerprint,
        embedding_fingerprint=weaponry_config.embedding_fingerprint,
        document_processing_fingerprint=(
            weaponry_config.document_processing_fingerprint
        ),
        extraction_model_fingerprint=(
            weaponry_config.extraction_model_fingerprint
        ),
        query_version=weaponry_config.query_version,
        score_semantics=weaponry_config.score_semantics,
        score_protocol=weaponry_config.score_protocol,
        ranking_strategy=weaponry_config.ranking_strategy,
        reference_filter_strategy=weaponry_config.reference_filter_strategy,
        extraction_context_strategy=weaponry_config.extraction_context_strategy,
    )
    weaponry_task_commands = LegacyTaskCommandAdapter(
        task_service,
        WeaponryTaskCommandCodec(),
    )
    weaponry_progress = LatestTaskProgressPublisherAdapter(
        task_commands=weaponry_task_commands,
        delegate=progress_adapter,
    )
    weaponry_callbacks = SQLiteWeaponryCallbackAdapter(
        task_service,
        callback_url=callback_url or "",
        callback_timeout=5.0,
        lease_seconds=30.0,
    )
    weaponry_resources = SQLiteWeaponryResourceStoreAdapter(
        str(task_db_path),
        cleanup_lease_seconds=7.0,
        retry_delay_seconds=0.05,
    )
    weaponry_recorder = WeaponryInvocationRecorder()
    weaponry_services = compose_weaponry_application_services(
        task_commands=weaponry_task_commands,
        progress_publisher=weaponry_progress,
        retrieval=FakeTargetEvidenceRetrievalPort(weaponry_recorder),
        extraction=FakeEvidenceExtractionPort(weaponry_recorder),
        guidance=FakeAuxiliaryGuidancePort(weaponry_recorder),
        translation=FakeWeaponryTranslationPort(weaponry_recorder),
        audit=SQLiteWeaponryInteractionAuditAdapter(str(task_db_path)),
        callbacks=weaponry_callbacks,
        callback_recovery_source=SQLiteWeaponryCallbackRecoverySource(
            task_service
        ),
        resources=weaponry_resources,
        resource_cleaner=FakeWeaponryExternalResourceCleanupPort(
            weaponry_recorder
        ),
        document_scope=DatabaseServiceWeaponryDocumentScopeAdapter(
            knowledge_service
        ),
        execution_limiter=upload_task_limiter,
        process_guard=FileProcessSingletonGuard(
            root / "locks" / "offline-weaponry-dispatcher.lock",
            component_name="离线武器谱 Dispatcher",
        ),
        config=weaponry_config,
        capabilities=weaponry_capabilities,
    )

    services = ApplicationServices(
        document_rag_factory=FakeDocumentRagFactory(),
        knowledge_index_factory=FakeKnowledgeIndexFactory(),
        chat_conversation_factory=chat_conversation_factory,
        task_service=task_service,
        kb_service=knowledge_service,
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
        llm_config=llm_integration_config,
        anythingllm_config=AnythingLLMConfig(
            base_url="http://anythingllm.invalid/api/v1",
            api_key="test-key",
            timeout=5.0,
            storage_root=None,
        ),
        report_infrastructure_config=ReportInfrastructureConfig.single_instance(),
        debug_services=compose_debug_application_services(
            chat_store=chat_store,
            kb_service=knowledge_service,
        ),
        analysis_submit=analysis_services.submit,
        analysis_callback_recovery=analysis_services.callback_recovery,
        analysis_dispatcher=analysis_services.dispatcher,
        analysis_runtime_config=analysis_config,
        weaponry_services=weaponry_services,
    )

    logger.debug("离线应用依赖组装完成: runtime_directory=%s", root)
    return services
