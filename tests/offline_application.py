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
from typing import Iterable

from app.container import ApplicationServices, UploadTaskLimiter
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
from app.services.chat import (
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
    AnythingLLMConfig,
    LLMIntegrationConfig,
    ReportInfrastructureConfig,
)
from app.services.core.database import DatabaseService
from app.services.core.progress_hub import LLMProgressHub
from app.services.llm_service.task_service import LLMTaskService
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


def build_offline_application_services(
    runtime_directory: str | Path,
    *,
    chat_stream_contents: Iterable[str] = ("第一段", "第二段"),
    max_upload_concurrency: int = 1,
    callback_url: str | None = None,
) -> ApplicationServices:
    """组装不访问网络的完整测试依赖容器。

    Args:
        runtime_directory: 本次测试独占的运行目录。任务、知识库和文件对话
            分别使用独立 SQLite 文件，避免用例间状态串扰。
        chat_stream_contents: Fake 对话端口返回的流式文本片段。
        max_upload_concurrency: 上传类任务的测试并发上限。

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
        llm_config=LLMIntegrationConfig(
            callback_url=callback_url,
            callback_timeout=5.0,
            task_db_path=str(task_db_path),
            download_timeout=5.0,
            download_dir=str(root),
        ),
        anythingllm_config=AnythingLLMConfig(
            base_url="http://anythingllm.invalid/api/v1",
            api_key="test-key",
            timeout=5.0,
            storage_root=None,
        ),
        report_infrastructure_config=ReportInfrastructureConfig.single_instance(),
        weaponry_services=weaponry_services,
    )

    logger.debug("离线应用依赖组装完成: runtime_directory=%s", root)
    return services
