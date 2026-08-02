"""阶段 1F-5A：Analysis 配置与组合根的离线验收。"""

from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest.mock import patch

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
from app.modules.analysis.composition import (
    compose_analysis_application_services,
)
from app.modules.tasks.adapters import (
    FileProcessSingletonGuard,
    InMemoryProgressAdapter,
    LatestTaskProgressPublisherAdapter,
    UploadTaskLimiter,
)
from app.services.core.config import (
    AnalysisInfrastructureConfig,
    AnalysisInfrastructureConfigurationError,
    load_analysis_infrastructure_config,
)
from app.services.core.database import DatabaseService
from app.services.core.progress_hub import LLMProgressHub
from app.services.llm_service.task_service import LLMTaskService
from app.modules.tasks.http_deadlines import required_http_lease_seconds
from tests import workspace_tempdir
from tests.fakes import FakeDocumentRagFactory, FakeKnowledgeIndexFactory


class _TranslationServiceFake:
    """组合测试使用的无 I/O 翻译替身；Dispatcher 构造不能触发真实模型调用。"""

    def translate_document(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return ("双语结果", "单语结果")


class AnalysisInfrastructureConfigTests(unittest.TestCase):
    """验证单实例配置、严格环境解析和 Callback lease 安全关系。"""

    def test_safe_defaults_are_single_instance_and_strictly_cover_http_budget(self) -> None:
        config = AnalysisInfrastructureConfig.single_instance()

        self.assertEqual("single_instance", config.runtime_mode)
        self.assertEqual(5.0, config.dispatch_retry_base_seconds)
        self.assertEqual(300.0, config.dispatch_retry_max_seconds)
        self.assertEqual(300.0, config.resource_close_running_grace_seconds)
        self.assertGreater(
            config.callback_lease_seconds,
            required_http_lease_seconds(config.callback_http_timeout_seconds),
        )

    def test_invalid_mode_backoff_range_and_equal_lease_are_rejected(self) -> None:
        with self.assertRaises(AnalysisInfrastructureConfigurationError):
            AnalysisInfrastructureConfig(runtime_mode="multi_instance")
        with self.assertRaises(AnalysisInfrastructureConfigurationError):
            AnalysisInfrastructureConfig(
                dispatch_retry_base_seconds=30.0,
                dispatch_retry_max_seconds=5.0,
            )
        with self.assertRaises(AnalysisInfrastructureConfigurationError):
            AnalysisInfrastructureConfig(
                callback_http_timeout_seconds=10.0,
                callback_lease_seconds=required_http_lease_seconds(10.0),
            )
        with self.assertRaises(AnalysisInfrastructureConfigurationError):
            AnalysisInfrastructureConfig(
                resource_close_running_grace_seconds=0.0,
            )

    def test_environment_loader_fails_fast_instead_of_silently_falling_back(self) -> None:
        with patch.dict(
            os.environ,
            {
                "DOCSENSE_ANALYSIS_DISPATCH_SCAN_INTERVAL_SECONDS": "0.5",
                "DOCSENSE_ANALYSIS_DISPATCH_BATCH_SIZE": "12",
                "DOCSENSE_ANALYSIS_DISPATCH_RETRY_BASE_SECONDS": "2",
                "DOCSENSE_ANALYSIS_DISPATCH_RETRY_MAX_SECONDS": "20",
                "DOCSENSE_ANALYSIS_RESOURCE_SWEEP_INTERVAL_SECONDS": "15",
                "DOCSENSE_ANALYSIS_RESOURCE_SWEEP_BATCH_SIZE": "8",
                "DOCSENSE_ANALYSIS_RESOURCE_CLOSE_RUNNING_GRACE_SECONDS": "90",
                "DOCSENSE_ANALYSIS_RUNNING_ALERT_SECONDS": "10",
                "DOCSENSE_ANALYSIS_STOP_TIMEOUT_SECONDS": "4",
                "DOCSENSE_ANALYSIS_CALLBACK_HTTP_TIMEOUT_SECONDS": "2",
                "DOCSENSE_ANALYSIS_CALLBACK_LEASE_SECONDS": "10",
            },
        ):
            config = load_analysis_infrastructure_config()

        self.assertEqual(12, config.accepted_batch_size)
        self.assertEqual(2.0, config.dispatch_retry_base_seconds)
        self.assertEqual(20.0, config.dispatch_retry_max_seconds)
        self.assertEqual(90.0, config.resource_close_running_grace_seconds)

        with patch.dict(
            os.environ,
            {"DOCSENSE_ANALYSIS_RESOURCE_SWEEP_BATCH_SIZE": "unbounded"},
        ):
            with self.assertRaises(AnalysisInfrastructureConfigurationError):
                load_analysis_infrastructure_config()


class AnalysisCompositionTests(unittest.TestCase):
    """验证唯一组合根共享依赖且构造阶段不启动线程、不访问供应商。"""

    def test_composition_reuses_shared_limiter_and_keeps_dispatcher_new(self) -> None:
        with workspace_tempdir() as runtime_directory:
            root = Path(runtime_directory)
            task_service = LLMTaskService(str(root / "tasks.sqlite3"))
            knowledge_service = DatabaseService(str(root / "knowledge.sqlite3"))
            task_commands = SQLiteAnalysisBatchCommandAdapter(task_service)
            progress_adapter = InMemoryProgressAdapter(LLMProgressHub())
            progress_publisher = LatestTaskProgressPublisherAdapter(
                task_commands=task_commands,
                delegate=progress_adapter,
            )
            limiter = UploadTaskLimiter(max_concurrency=1)
            callbacks = SQLiteAnalysisCallbackAdapter(
                task_service,
                callback_timeout=1.0,
                lease_seconds=10.0,
            )

            services = compose_analysis_application_services(
                task_commands=task_commands,
                progress_publisher=progress_publisher,
                workspaces=LocalAnalysisTaskWorkspaceAdapter(
                    str(root / "tasks")
                ),
                files=LegacyAnalysisFilePreparationAdapter(
                    download_timeout_seconds=1.0,
                ),
                rag_factory=LegacyAnalysisRagAdapterFactory(
                    FakeDocumentRagFactory()
                ),
                knowledge=LegacyAnalysisKnowledgeAdapter(
                    FakeKnowledgeIndexFactory()
                ),
                audit=LegacyAnalysisAuditAdapter(task_service),
                translation=SerializedAnalysisTranslationAdapter(
                    _TranslationServiceFake(),
                    AnalysisTranslationExecutionCoordinator(),
                ),
                callbacks=callbacks,
                callback_recovery_source=SQLiteAnalysisCallbackRecoverySource(
                    task_service
                ),
                resources=SQLiteAnalysisResourceStoreAdapter(task_service),
                execution_limiter=limiter,
                process_guard=FileProcessSingletonGuard(
                    root / "locks" / "analysis-dispatcher.lock",
                    component_name="Analysis组合测试 Dispatcher",
                ),
                config=AnalysisInfrastructureConfig(
                    callback_http_timeout_seconds=1.0,
                    callback_lease_seconds=10.0,
                ),
                callback_url="",
            )

            self.assertIs(services.task_commands, task_commands)
            self.assertIs(services.execution_limiter, limiter)
            self.assertIs(services.submit.dispatcher, services.dispatcher)
            self.assertIs(services.callback_recovery.callbacks, callbacks)
            self.assertIs(services.dispatcher.task_commands, task_commands)
            self.assertIs(services.dispatcher.execution_limiter, limiter)
            self.assertIs(
                services.resource_activity,
                services.runner._resource_activity,
            )
            self.assertIs(
                services.resource_activity,
                services.resource_recovery._resource_activity,
            )

            snapshot = services.dispatcher.snapshot()
            self.assertEqual("new", snapshot.lifecycle_state)
            self.assertEqual(0, snapshot.worker_thread_count)
            self.assertEqual(0, snapshot.maintenance_thread_count)
            self.assertFalse(snapshot.ready)


if __name__ == "__main__":
    unittest.main()
