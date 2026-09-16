"""阶段 6/8 应用容器、任务级 Factory 与路由注入的离线测试。"""

from __future__ import annotations

import ast
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from app import create_app
from app.integrations.anythingllm.factory import (
    AnythingLLMGatewayFactory,
    AnythingLLMKnowledgeIndexFactory,
)
from app.integrations.anythingllm.chat_factory import AnythingLLMChatFactory
from app.integrations.anythingllm.transport import AnythingLLMTransport
from app.ports import (
    ChatConversationFactory,
    ChatConversationPort,
    DocumentRagFactory,
    DocumentRagPort,
    KnowledgeIndexFactory,
    KnowledgeIndexPort,
)
from app.services.core.config import (
    ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE,
    ANALYSIS_CLASSIFICATION_MODE_TOPK_SINGLE,
    ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD,
    ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE,
    AnalysisClassificationConfig,
    AnalysisInfrastructureConfig,
    AnythingLLMConfig,
    LLMIntegrationConfig,
    ReportInfrastructureConfig,
    ReportInfrastructureConfigurationError,
    load_report_infrastructure_config,
)
from app.container import (
    APPLICATION_SERVICES_EXTENSION,
    ApplicationServices,
    UploadTaskLimiter,
)
from app.modules.analysis.adapters import (
    SQLiteAnalysisBatchCommandAdapter,
    SQLiteAnalysisCallbackAdapter,
    SQLiteAnalysisCallbackRecoverySource,
)
from app.modules.analysis.application import (
    RecoverAnalysisCallbackSynchronously,
    SubmitAnalysisBatch,
)
from app.modules.report.adapters import (
    AnythingLLMReportClientFactory,
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
    WeaponryInfrastructureConfig,
    WeaponryRuntimeCapabilities,
)
from app.modules.weaponry.composition import (
    compose_weaponry_application_services,
)
from app.services.chat import (
    ChatAbortService,
    ChatCleanupJobExecutor,
    ChatCommandService,
    ChatDeleteService,
    ChatHistoryService,
    ChatRunDispatcher,
    ChatPersistenceStore,
    ChatRunLockService,
    ChatStore,
    ChatTitleService,
    DatabaseChatDocumentResolver,
    SynchronousChatRunExecutor,
    InlineChatRunDispatcher,
    InlineChatCleanupDispatcher,
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
    FakeWeaponryCallbackPort,
    FakeWeaponryDocumentScopePort,
    FakeWeaponryExternalResourceCleanupPort,
    FakeWeaponryInteractionAuditPort,
    FakeWeaponryProgressPublisherPort,
    FakeWeaponryResourceStorePort,
    FakeWeaponryTaskCommandPort,
    FakeWeaponryTranslationPort,
    InvocationRecorder,
    WeaponryInvocationRecorder,
)


class _NoopWeaponryMaintenance:
    """1D-5 容器生命周期测试使用的显式有界维护替身。"""

    def run_once(self, *, limit: int) -> object:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        return {"limit": limit}


class _FailingAnalysisDispatcher:
    """显式离线 Dispatcher Fake：只验证容器回滚，不创建任何后台线程。"""

    def __init__(self) -> None:
        self.start_count = 0
        self.stop_count = 0
        self.close_count = 0

    def wake_up(self) -> None:
        return None

    def start(self) -> None:
        self.start_count += 1
        raise RuntimeError("forced analysis startup failure")

    def stop(self, *, timeout_seconds: float | None = None) -> bool:
        self.stop_count += 1
        return True

    def close(self) -> None:
        self.close_count += 1


class _RecordingAnalysisDispatcher:
    """文件分析路由测试使用的无后台 Dispatcher。

    该替身只记录 Application 在 SQLite 事务提交后发出的有界唤醒，不创建线程、
    不扫描任务、更不会访问外部模型或回调地址。它用于证明 HTTP 路由只做受理，
    Worker 生命周期仍由容器统一管理。
    """

    def __init__(self) -> None:
        self.wake_count = 0
        self.start_count = 0
        self.stop_count = 0
        self.close_count = 0

    def wake_up(self) -> None:
        self.wake_count += 1

    def start(self) -> None:
        self.start_count += 1

    def stop(self, *, timeout_seconds: float | None = None) -> bool:
        self.stop_count += 1
        return True

    def close(self) -> None:
        self.close_count += 1


def _weaponry_infrastructure_config() -> WeaponryInfrastructureConfig:
    """构造不访问环境变量或外部服务的离线单实例配置。"""

    return WeaponryInfrastructureConfig(
        runtime_mode="single_instance",
        scan_interval_seconds=0.02,
        accepted_batch_size=10,
        dispatch_failure_retry_seconds=1.0,
        maintenance_interval_seconds=0.02,
        maintenance_limit=5,
        running_sample_limit=5,
        stop_timeout_seconds=0.5,
        cleanup_http_timeout_seconds=1.0,
        cleanup_lease_seconds=7.0,
        provider_fingerprint="container-provider-v1",
        embedding_fingerprint="container-embedding-v1",
        document_processing_fingerprint="container-processing-v1",
        extraction_model_fingerprint="container-extraction-v1",
    )


def _weaponry_capabilities(
    config: WeaponryInfrastructureConfig,
) -> WeaponryRuntimeCapabilities:
    """由离线 Fake 明确声明能力，不能从生产期望配置自动推断。"""

    return WeaponryRuntimeCapabilities(
        provider_fingerprint=config.provider_fingerprint,
        embedding_fingerprint=config.embedding_fingerprint,
        document_processing_fingerprint=(
            config.document_processing_fingerprint
        ),
        extraction_model_fingerprint=config.extraction_model_fingerprint,
        query_version=config.query_version,
        score_semantics=config.score_semantics,
        score_protocol=config.score_protocol,
        ranking_strategy=config.ranking_strategy,
        reference_filter_strategy=config.reference_filter_strategy,
        extraction_context_strategy=config.extraction_context_strategy,
    )


class AnythingLLMGatewayFactoryTests(unittest.TestCase):
    """验证生产 Factory 的惰性创建、任务隔离和确定性资源关闭。"""

    @staticmethod
    def _config() -> AnythingLLMConfig:
        """返回不连接网络的合法测试配置。"""
        return AnythingLLMConfig(
            base_url="http://anythingllm.invalid/api/v1",
            api_key="test-key",
            timeout=5.0,
            storage_root=None,
        )

    def test_each_lease_builds_an_independent_transport_and_gateway(self) -> None:
        """两个任务租约必须使用不同 Transport，退出时分别关闭且不能相互复用。"""
        first_transport = MagicMock(spec=AnythingLLMTransport)
        second_transport = MagicMock(spec=AnythingLLMTransport)
        transport_factory = Mock(
            side_effect=(first_transport, second_transport),
        )
        factory = AnythingLLMGatewayFactory(
            self._config(),
            transport_factory=transport_factory,
        )

        with factory.create() as first_gateway:
            self.assertIsInstance(first_gateway, DocumentRagPort)
            self.assertEqual(1, first_gateway._workspace_settings["openAiHistory"])
            first_transport.close.assert_not_called()
        with factory.create() as second_gateway:
            self.assertIsInstance(second_gateway, DocumentRagPort)
            self.assertIsNot(first_gateway, second_gateway)

        self.assertEqual(2, transport_factory.call_count)
        first_transport.close.assert_called_once_with()
        second_transport.close.assert_called_once_with()

    def test_factory_is_lazy_until_context_is_entered(self) -> None:
        """仅取得上下文管理器不得创建 requests Session 或 Transport。"""
        transport_factory = Mock()
        factory = AnythingLLMGatewayFactory(
            self._config(),
            transport_factory=transport_factory,
        )

        lease = factory.create()

        self.assertIsInstance(factory, DocumentRagFactory)
        self.assertIsNotNone(lease)
        transport_factory.assert_not_called()

    def test_close_failure_does_not_mask_active_business_exception(self) -> None:
        """任务和关闭同时失败时必须保留原始任务异常，避免错误归因。"""
        transport = MagicMock(spec=AnythingLLMTransport)
        transport.close.side_effect = RuntimeError("关闭失败")
        factory = AnythingLLMGatewayFactory(
            self._config(),
            transport_factory=Mock(return_value=transport),
        )

        with self.assertLogs(
            "app.integrations.anythingllm.factory",
            level="ERROR",
        ):
            with self.assertRaisesRegex(ValueError, "业务失败"):
                with factory.create():
                    raise ValueError("业务失败")

        transport.close.assert_called_once_with()

    def test_transport_construction_failure_is_not_suppressed(self) -> None:
        """传输对象尚未创建时，工厂必须原样传播构造异常。"""
        factory = AnythingLLMGatewayFactory(
            self._config(),
            transport_factory=Mock(side_effect=ValueError("配置无效")),
        )

        with self.assertRaisesRegex(ValueError, "配置无效"):
            with factory.create():
                self.fail("Transport 构造失败后不应进入任务作用域")

    def test_report_cleanup_timeout_is_finite_without_changing_generation(self) -> None:
        """生成可继续无限等待，但清理 DELETE 必须使用批准的 60 秒有限超时。"""

        generation_config = replace(self._config(), timeout=None)
        cleanup_config = replace(generation_config, timeout=60.0)
        generation_transport = MagicMock(spec=AnythingLLMTransport)
        cleanup_transport = MagicMock(spec=AnythingLLMTransport)
        transport_factory = Mock(
            side_effect=(generation_transport, cleanup_transport),
        )
        generation_factory = AnythingLLMReportClientFactory(
            generation_config,
            transport_factory=transport_factory,
        )
        cleanup_factory = AnythingLLMReportClientFactory(
            cleanup_config,
            transport_factory=transport_factory,
        )

        with generation_factory.create():
            pass
        with cleanup_factory.create():
            pass

        self.assertIsNone(transport_factory.call_args_list[0].kwargs["timeout"])
        self.assertEqual(
            60.0,
            transport_factory.call_args_list[1].kwargs["timeout"],
        )
        generation_transport.close.assert_called_once_with()
        cleanup_transport.close.assert_called_once_with()


class AnythingLLMKnowledgeIndexFactoryTests(unittest.TestCase):
    """验证永久知识库 Factory 同样保持惰性和任务级 Transport 生命周期。"""

    def test_knowledge_factory_is_lazy_and_closes_transport(self) -> None:
        """创建租约不联网，进入后返回 Port，退出时关闭本次独立 Transport。"""
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            database_service = DatabaseService(
                db_path=f"{tmp}/knowledge.sqlite3"
            )
            transport = MagicMock(spec=AnythingLLMTransport)
            transport_factory = Mock(return_value=transport)
            factory = AnythingLLMKnowledgeIndexFactory(
                AnythingLLMConfig(
                    base_url="http://anythingllm.invalid/api/v1",
                    api_key="test-key",
                    timeout=5.0,
                    storage_root=None,
                ),
                task_service.knowledge_index_operations,
                database_service,
                transport_factory=transport_factory,
            )

            lease = factory.create()
            self.assertIsInstance(factory, KnowledgeIndexFactory)
            transport_factory.assert_not_called()

            with lease as gateway:
                self.assertIsInstance(gateway, KnowledgeIndexPort)
                self.assertEqual(1, gateway._workspace_settings["openAiHistory"])
                transport.close.assert_not_called()

            transport.close.assert_called_once_with()


class AnythingLLMChatFactoryTests(unittest.TestCase):
    """验证文件对话工厂的隔离性与传输对象惰性生命周期。"""

    def test_chat_factory_is_lazy_and_closes_transport(self) -> None:
        transport = MagicMock(spec=AnythingLLMTransport)
        transport_factory = Mock(return_value=transport)
        factory = AnythingLLMChatFactory(
            AnythingLLMConfig(
                base_url="http://anythingllm.invalid/api/v1",
                api_key="test-key",
                timeout=5.0,
                storage_root=None,
            ),
            transport_factory=transport_factory,
        )

        lease = factory.create()
        self.assertIsInstance(factory, ChatConversationFactory)
        transport_factory.assert_not_called()

        with lease as gateway:
            self.assertIsInstance(gateway, ChatConversationPort)
            transport.close.assert_not_called()

        transport.close.assert_called_once_with()


class UploadTaskLimiterTests(unittest.TestCase):
    """验证上传并发许可在异常路径也会归还。"""

    def test_exception_releases_permit_for_next_task(self) -> None:
        """首个任务异常后，后续任务仍应立即取得同一许可并正常执行。"""
        limiter = UploadTaskLimiter(max_concurrency=1)

        with self.assertRaisesRegex(RuntimeError, "任务失败"):
            limiter.run(lambda: (_ for _ in ()).throw(RuntimeError("任务失败")))

        self.assertEqual("完成", limiter.run(lambda: "完成"))

    def test_waiting_tasks_acquire_in_fifo_order(self) -> None:
        """已等待的业务必须先于刚归还许可后重新排队的忙碌业务执行。"""

        limiter = UploadTaskLimiter(max_concurrency=1)
        self.assertTrue(
            limiter.acquire_interruptibly(
                lambda: False,
                poll_interval_seconds=0.01,
            )
        )
        completed: list[str] = []

        def run(label: str) -> None:
            limiter.run(lambda: completed.append(label))

        threads: list[threading.Thread] = []
        for expected_waiters, label in enumerate(
            ("report", "weaponry", "analysis"),
            start=1,
        ):
            thread = threading.Thread(target=run, args=(label,))
            threads.append(thread)
            thread.start()
            deadline = time.monotonic() + 2.0
            while limiter.waiting_count < expected_waiters:
                if time.monotonic() >= deadline:
                    self.fail("等待任务未在限定时间内进入 FIFO 队列")
                time.sleep(0.005)

        limiter.release()
        for thread in threads:
            thread.join(timeout=2.0)
            self.assertFalse(thread.is_alive())

        self.assertEqual(["report", "weaponry", "analysis"], completed)

    def test_cancelled_waiter_is_removed_without_blocking_queue_head(self) -> None:
        """停机取消必须移除 ticket，否则后续任务会被幽灵队首永久阻塞。"""

        limiter = UploadTaskLimiter(max_concurrency=1)
        self.assertTrue(
            limiter.acquire_interruptibly(
                lambda: False,
                poll_interval_seconds=0.01,
            )
        )
        cancel = threading.Event()
        outcome: list[bool] = []
        waiter = threading.Thread(
            target=lambda: outcome.append(
                limiter.acquire_interruptibly(
                    cancel.is_set,
                    poll_interval_seconds=0.01,
                )
            )
        )
        waiter.start()
        deadline = time.monotonic() + 2.0
        while limiter.waiting_count != 1:
            if time.monotonic() >= deadline:
                self.fail("取消用例未在限定时间内进入 FIFO 队列")
            time.sleep(0.005)

        cancel.set()
        waiter.join(timeout=2.0)
        self.assertEqual([False], outcome)
        self.assertEqual(0, limiter.waiting_count)
        limiter.release()
        self.assertEqual("完成", limiter.run(lambda: "完成"))


class ReportInfrastructureConfigTests(unittest.TestCase):
    """验证清理 HTTP 超时与接管租约的必要安全关系。"""

    def test_safe_defaults_keep_cleanup_finite_and_generation_independent(self) -> None:
        config = ReportInfrastructureConfig.single_instance()

        self.assertEqual(60.0, config.cleanup_http_timeout_seconds)
        self.assertEqual(130.0, config.cleanup_lease_seconds)
        self.assertEqual(30.0, config.dispatch_failure_retry_seconds)
        self.assertEqual(512 * 1024 * 1024, config.max_download_bytes)
        self.assertGreater(
            config.cleanup_lease_seconds,
            config.cleanup_http_timeout_seconds * 2,
        )

    def test_cleanup_lease_must_cover_connect_read_and_margin(self) -> None:
        for lease_seconds in (60.0, 120.0, 124.9):
            with self.subTest(lease_seconds=lease_seconds):
                with self.assertRaisesRegex(
                    ReportInfrastructureConfigurationError,
                    "必须覆盖连接、响应读取和安全余量",
                ):
                    ReportInfrastructureConfig(
                        cleanup_http_timeout_seconds=60.0,
                        cleanup_lease_seconds=lease_seconds,
                    )

    def test_non_finite_cleanup_timeout_is_rejected(self) -> None:
        with self.assertRaises(ReportInfrastructureConfigurationError):
            ReportInfrastructureConfig(
                cleanup_http_timeout_seconds=float("inf"),
            )

    def test_environment_loader_is_strict_and_preserves_approved_values(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DOCSENSE_REPORT_CLEANUP_HTTP_TIMEOUT_SECONDS": "60",
                "DOCSENSE_REPORT_CLEANUP_LEASE_SECONDS": "130",
                "DOCSENSE_REPORT_ACCEPTED_BATCH_SIZE": "50",
                "DOCSENSE_REPORT_DISPATCH_FAILURE_RETRY_SECONDS": "30",
                "DOCSENSE_REPORT_MAX_DOWNLOAD_BYTES": "1048576",
            },
        ):
            config = load_report_infrastructure_config()

        self.assertEqual(60.0, config.cleanup_http_timeout_seconds)
        self.assertEqual(130.0, config.cleanup_lease_seconds)
        self.assertEqual(50, config.accepted_batch_size)
        self.assertEqual(30.0, config.dispatch_failure_retry_seconds)
        self.assertEqual(1048576, config.max_download_bytes)

        with patch.dict(
            os.environ,
            {"DOCSENSE_REPORT_RESOURCE_SWEEP_LIMIT": "unbounded"},
        ):
            with self.assertRaises(ReportInfrastructureConfigurationError):
                load_report_infrastructure_config()

        for invalid in ("0", str(10 * 1024**4 + 1), "1.5"):
            with self.subTest(max_download_bytes=invalid), patch.dict(
                os.environ,
                {"DOCSENSE_REPORT_MAX_DOWNLOAD_BYTES": invalid},
            ):
                with self.assertRaises(ReportInfrastructureConfigurationError):
                    load_report_infrastructure_config()


class ApplicationContainerRouteTests(unittest.TestCase):
    """验证 Flask 应用可以注入完全离线容器并把 Factory 传给后台任务。"""

    def setUp(self) -> None:
        """创建隔离 SQLite 服务和不访问网络的 Fake Factory。"""
        self._temp_directory = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        )
        self.runtime_directory = self._temp_directory.__enter__()
        self.document_rag_factory = FakeDocumentRagFactory()
        self.knowledge_index_factory = FakeKnowledgeIndexFactory()
        self.chat_conversation_factory = FakeChatConversationFactory()
        chat_db_path = f"{self.runtime_directory}/chat.sqlite3"
        chat_store = ChatStore(db_path=chat_db_path)
        chat_commands = ChatCommandService(ChatRunLockService(chat_db_path))
        chat_history = ChatHistoryService(chat_store)
        kb_service = DatabaseService(
            db_path=f"{self.runtime_directory}/knowledge.sqlite3"
        )
        chat_run_executor = SynchronousChatRunExecutor(
            store=chat_store,
            chat_commands=chat_commands,
            conversation_factory=self.chat_conversation_factory,
            document_resolver=DatabaseChatDocumentResolver(kb_service),
        )
        chat_cleanup_executor = ChatCleanupJobExecutor(
            store=chat_store,
            conversation_factory=self.chat_conversation_factory,
        )
        chat_cleanup_dispatcher = InlineChatCleanupDispatcher(
            execute=chat_cleanup_executor.execute_cleanup_job,
        )
        chat_dispatcher = InlineChatRunDispatcher(
            execute=chat_run_executor.execute_chat_run,
        )
        task_service = LLMTaskService(
            db_path=f"{self.runtime_directory}/tasks.sqlite3"
        )
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
        report_callback_recovery = RecoverReportCallbackSynchronously(
            source=SQLiteReportCallbackRecoverySource(task_service),
            callbacks=SQLiteReportCallbackAdapter(
                task_service,
                callback_url="",
                callback_timeout=5.0,
                lease_seconds=30.0,
            ),
        )
        self.services = ApplicationServices(
            document_rag_factory=self.document_rag_factory,
            knowledge_index_factory=self.knowledge_index_factory,
            chat_conversation_factory=self.chat_conversation_factory,
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
                conversation_factory=self.chat_conversation_factory,
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
                conversation_factory=self.chat_conversation_factory,
                cleanup_dispatcher=chat_cleanup_dispatcher,
                cleanup_executor=chat_cleanup_executor,
            ),
            chat_cleanup_executor=chat_cleanup_executor,
            progress_hub=progress_hub,
            progress_subscription_service=progress_subscription_service,
            upload_task_limiter=UploadTaskLimiter(max_concurrency=1),
            report_submit=report_submit,
            report_callback_recovery=report_callback_recovery,
            report_dispatcher=report_dispatcher,
            llm_config=LLMIntegrationConfig(
                callback_url=None,
                callback_timeout=5.0,
                task_db_path=f"{self.runtime_directory}/tasks.sqlite3",
                download_timeout=5.0,
                download_dir=self.runtime_directory,
            ),
            anythingllm_config=AnythingLLMConfig(
                base_url="http://anythingllm.invalid/api/v1",
                api_key="test-key",
                timeout=5.0,
                storage_root=None,
            ),
            report_infrastructure_config=(
                ReportInfrastructureConfig.single_instance()
            ),
        )

    def tearDown(self) -> None:
        """释放测试创建的临时 SQLite 目录。"""
        self._temp_directory.__exit__(None, None, None)

    def _compose_weaponry_services(self):
        """构造不启动线程、不访问真实供应商的 1D-5 Weaponry 实例链。"""

        recorder = WeaponryInvocationRecorder()
        config = _weaponry_infrastructure_config()
        callbacks = FakeWeaponryCallbackPort(recorder)
        return compose_weaponry_application_services(
            task_commands=FakeWeaponryTaskCommandPort(recorder),
            progress_publisher=FakeWeaponryProgressPublisherPort(recorder),
            retrieval=FakeTargetEvidenceRetrievalPort(recorder),
            extraction=FakeEvidenceExtractionPort(recorder),
            guidance=FakeAuxiliaryGuidancePort(recorder),
            translation=FakeWeaponryTranslationPort(recorder),
            audit=FakeWeaponryInteractionAuditPort(recorder),
            callbacks=callbacks,
            callback_recovery_source=callbacks,
            resources=FakeWeaponryResourceStorePort(recorder),
            resource_cleaner=FakeWeaponryExternalResourceCleanupPort(recorder),
            document_scope=FakeWeaponryDocumentScopePort(),
            execution_limiter=self.services.upload_task_limiter,
            process_guard=FileProcessSingletonGuard(
                Path(self.runtime_directory)
                / "locks"
                / "weaponry-container.lock",
                component_name="武器谱容器测试 Dispatcher",
            ),
            config=config,
            capabilities=_weaponry_capabilities(config),
        )

    def test_create_app_uses_injected_services_without_building_production(self) -> None:
        """显式注入时应用必须原样保存容器，且不得构建生产依赖。"""
        with patch("app.create_application_services") as production_builder:
            app = create_app(services=self.services)

        self.assertIs(
            self.services,
            app.extensions[APPLICATION_SERVICES_EXTENSION],
        )
        self.assertIsInstance(self.services.chat_store, ChatPersistenceStore)
        self.assertIsInstance(self.services.chat_store, ChatStore)
        self.assertIsInstance(self.services.chat_commands, ChatCommandService)
        self.assertIsInstance(self.services.chat_dispatcher, ChatRunDispatcher)
        self.assertIsInstance(self.services.chat_history, ChatHistoryService)
        self.assertIsInstance(self.services.chat_title, ChatTitleService)
        self.assertIsInstance(self.services.chat_abort, ChatAbortService)
        self.assertIsInstance(self.services.chat_delete, ChatDeleteService)
        self.assertEqual(
            AnalysisClassificationConfig.topk_two_stage(),
            self.services.analysis_classification_config,
        )
        production_builder.assert_not_called()
        self.assertEqual(0, len(self.document_rag_factory.ports))
        self.assertEqual(0, self.services.report_dispatcher.start_count)

    def test_explicit_weaponry_bundle_shares_limiter_and_container_lifecycle(self) -> None:
        """ApplicationServices 统一启动、停止、关闭同一 Weaponry 实例链。"""

        weaponry = self._compose_weaponry_services()
        services = replace(self.services, weaponry_services=weaponry)

        self.assertIs(
            services.upload_task_limiter,
            weaponry.execution_limiter,
        )
        self.assertEqual("new", weaponry.snapshot().lifecycle_state)
        try:
            services.start_background_services()
            self.assertEqual("running", weaponry.snapshot().lifecycle_state)
            self.assertEqual(1, services.report_dispatcher.start_count)
            self.assertTrue(services.stop_background_services(timeout_seconds=0.5))
            self.assertEqual("stopped", weaponry.snapshot().lifecycle_state)
            self.assertEqual(1, services.report_dispatcher.stop_count)
        finally:
            services.close()

        self.assertEqual("closed", weaponry.snapshot().lifecycle_state)
        self.assertEqual(1, services.report_dispatcher.close_count)

    def test_analysis_start_failure_rolls_back_only_components_started_this_call(self) -> None:
        """Analysis 启动失败时，Report 必须逆序停机且不能留下半启动 Worker。"""

        dispatcher = _FailingAnalysisDispatcher()
        task_commands = SQLiteAnalysisBatchCommandAdapter(
            self.services.task_service
        )
        callbacks = SQLiteAnalysisCallbackAdapter(
            self.services.task_service,
            callback_timeout=1.0,
            lease_seconds=10.0,
        )
        services = replace(
            self.services,
            analysis_submit=SubmitAnalysisBatch(
                batch_commands=task_commands,
                dispatcher=dispatcher,
            ),
            analysis_callback_recovery=RecoverAnalysisCallbackSynchronously(
                source=SQLiteAnalysisCallbackRecoverySource(
                    self.services.task_service
                ),
                callbacks=callbacks,
                callback_url="",
            ),
            analysis_dispatcher=dispatcher,
            analysis_runtime_config=AnalysisInfrastructureConfig(
                callback_http_timeout_seconds=1.0,
                callback_lease_seconds=10.0,
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "forced analysis startup failure"):
            services.start_background_services()

        self.assertEqual(1, dispatcher.start_count)
        self.assertEqual(0, dispatcher.stop_count)
        self.assertEqual(1, services.report_dispatcher.start_count)
        self.assertEqual(1, services.report_dispatcher.stop_count)

    def test_production_owned_container_starts_once_and_registers_close(self) -> None:
        """仅无参应用工厂拥有后台线程，显式注入测试保持手动生命周期。"""

        with (
            patch("app.create_application_services", return_value=self.services),
            patch("app.atexit.register") as register,
        ):
            app = create_app()

        self.assertIs(
            self.services,
            app.extensions[APPLICATION_SERVICES_EXTENSION],
        )
        self.assertEqual(1, self.services.report_dispatcher.start_count)
        register.assert_called_once_with(self.services.close)
        self.services.close()

    def test_production_start_failure_closes_owned_container_without_atexit(
        self,
    ) -> None:
        """生产启动失败必须最终关闭容器，且不得注册一个失效的退出钩子。"""

        owned_services = MagicMock(spec=ApplicationServices)
        owned_services.start_background_services.side_effect = RuntimeError(
            "forced production startup failure"
        )
        owned_services.close.side_effect = ValueError("forced close failure")

        with (
            patch("app.create_application_services", return_value=owned_services),
            patch("app.atexit.register") as register,
            self.assertLogs("app", level="ERROR"),
            self.assertRaisesRegex(RuntimeError, "forced production startup failure"),
        ):
            create_app()

        owned_services.close.assert_called_once_with()
        register.assert_not_called()

    def test_production_container_constructs_one_shared_office_preparer(self) -> None:
        """Report 与 Analysis 必须引用同一个进程级转换容量许可。"""

        source_path = Path(__file__).resolve().parents[1] / "app" / "container.py"
        module = ast.parse(source_path.read_text(encoding="utf-8"))
        production_factory = next(
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "create_application_services"
        )
        constructor_calls = [
            node
            for node in ast.walk(production_factory)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "LibreOfficeLegacyOfficePreparer"
        ]
        # 生产组合根内部只能构造一次，随后把同一局部变量注入 Report、Analysis
        # 和 ApplicationServices；离线默认依赖工厂不属于这个函数作用域。
        self.assertEqual(1, len(constructor_calls))

        injected_targets: dict[str, str] = {}
        for node in ast.walk(production_factory):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in {
                "LegacyReportFileAdapter",
                "LegacyAnalysisFilePreparationAdapter",
                "ApplicationServices",
            }:
                continue
            keyword = next(
                (
                    item
                    for item in node.keywords
                    if item.arg == "legacy_office_preparer"
                ),
                None,
            )
            if keyword is not None and isinstance(keyword.value, ast.Name):
                injected_targets[node.func.id] = keyword.value.id

        self.assertEqual(
            {
                "LegacyReportFileAdapter": "legacy_office_preparer",
                "LegacyAnalysisFilePreparationAdapter": (
                    "legacy_office_preparer"
                ),
                "ApplicationServices": "legacy_office_preparer",
            },
            injected_targets,
        )

    def test_run_entrypoint_disables_debug_reloader_for_single_worker(self) -> None:
        source = (Path(__file__).resolve().parents[1] / "run.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("use_reloader=False", source)

    def test_application_container_source_does_not_construct_transport(self) -> None:
        """应用级容器不得持有或直接创建带网络 Session 的任务级对象。"""
        container_source = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "container.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("AnythingLLMTransport(", container_source)
        self.assertNotIn("requests.Session(", container_source)

    def test_blueprint_source_has_no_module_level_service_construction(self) -> None:
        """蓝图只能解析应用容器，不能重新引入模块级数据库或信号量单例。"""
        blueprint_source = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "blueprints"
            / "llm.py"
        ).read_text(encoding="utf-8")
        forbidden_constructors = (
            "LLMTaskService(",
            "DatabaseService(",
            "ChatRunLockService(",
            "LLMProgressHub(",
            "threading.Semaphore(",
        )

        for constructor in forbidden_constructors:
            with self.subTest(constructor=constructor):
                self.assertNotIn(constructor, blueprint_source)
        self.assertNotIn("record_chat_run_events", blueprint_source)
        self.assertNotIn("stream_chat_run(chat_run_request)", blueprint_source)

    def test_analysis_route_persists_policy_and_only_wakes_dispatcher(
        self,
    ) -> None:
        """路由只冻结策略并原子受理，Gateway/Worker 均不能进入请求线程。"""
        dispatcher = _RecordingAnalysisDispatcher()
        infrastructure_config = AnalysisInfrastructureConfig.single_instance()
        task_commands = SQLiteAnalysisBatchCommandAdapter(
            self.services.task_service
        )
        callbacks = SQLiteAnalysisCallbackAdapter(
            self.services.task_service,
            callback_timeout=infrastructure_config.callback_http_timeout_seconds,
            lease_seconds=infrastructure_config.callback_lease_seconds,
        )
        configured_services = replace(
            self.services,
            analysis_classification_config=AnalysisClassificationConfig(
                mode=ANALYSIS_CLASSIFICATION_MODE_TOPK_SINGLE,
                filename_constraint_mode=(
                    ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
                ),
            ),
            analysis_submit=SubmitAnalysisBatch(
                batch_commands=task_commands,
                dispatcher=dispatcher,
            ),
            analysis_callback_recovery=RecoverAnalysisCallbackSynchronously(
                source=SQLiteAnalysisCallbackRecoverySource(
                    self.services.task_service
                ),
                callbacks=callbacks,
                callback_url="",
            ),
            analysis_dispatcher=dispatcher,
            analysis_runtime_config=infrastructure_config,
        )
        app = create_app(services=configured_services)

        response = app.test_client().post(
            "/llm/analysis",
            json={
                "businessType": "file",
                "params": [
                    {
                        "fileName": "sample.txt",
                        "filePath": "http://files.invalid/sample.txt",
                    }
                ],
            },
        )

        self.assertEqual(202, response.status_code)
        self.assertEqual(b"", response.get_data())
        self.assertEqual(1, dispatcher.wake_count)
        self.assertEqual(0, dispatcher.start_count)
        latest_task = self.services.task_service.get_task("file", "sample.txt")
        self.assertIsNotNone(latest_task)
        assert latest_task is not None
        execution = self.services.task_service.get_task_execution(
            latest_task["execution_id"]
        )
        self.assertIsNotNone(execution)
        assert execution is not None
        policy_snapshot = execution["input_payload"]["policy_snapshot"]
        self.assertEqual(
            ANALYSIS_CLASSIFICATION_MODE_TOPK_SINGLE,
            policy_snapshot["classification_mode"],
        )
        self.assertEqual(
            ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD,
            policy_snapshot["filename_constraint_mode"],
        )
        self.assertEqual(
            ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE,
            policy_snapshot["identity_reselect_mode"],
        )
        # 路由受理不得提前取得 RAG/知识库租约；真实访问只发生在 Dispatcher Worker 内。
        self.assertEqual(0, len(self.document_rag_factory.ports))
        self.assertEqual(0, len(self.knowledge_index_factory.ports))


if __name__ == "__main__":
    unittest.main()
