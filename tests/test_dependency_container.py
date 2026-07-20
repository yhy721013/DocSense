"""阶段 6/8 应用容器、任务级 Factory 与路由注入的离线测试。"""

from __future__ import annotations

import tempfile
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
    AnalysisClassificationConfig,
    AnythingLLMConfig,
    LLMIntegrationConfig,
)
from app.container import (
    APPLICATION_SERVICES_EXTENSION,
    ApplicationServices,
    UploadTaskLimiter,
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
        self.services = ApplicationServices(
            document_rag_factory=self.document_rag_factory,
            knowledge_index_factory=self.knowledge_index_factory,
            chat_conversation_factory=self.chat_conversation_factory,
            task_service=LLMTaskService(
                db_path=f"{self.runtime_directory}/tasks.sqlite3"
            ),
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
            progress_hub=LLMProgressHub(),
            upload_task_limiter=UploadTaskLimiter(max_concurrency=1),
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
        )

    def tearDown(self) -> None:
        """释放测试创建的临时 SQLite 目录。"""
        self._temp_directory.__exit__(None, None, None)

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

    @patch("app.blueprints.llm.threading.Thread")
    def test_analysis_route_injects_factories_without_entering_leases(
        self,
        thread_type: MagicMock,
    ) -> None:
        """路由只把两个无状态 Factory 交给线程，不在请求线程创建 Gateway 或 Transport。"""
        configured_services = replace(
            self.services,
            analysis_classification_config=AnalysisClassificationConfig(
                mode=ANALYSIS_CLASSIFICATION_MODE_TOPK_SINGLE,
                filename_constraint_mode=(
                    ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
                ),
            ),
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
        task_kwargs = thread_type.call_args.kwargs["kwargs"]
        self.assertIs(
            self.document_rag_factory,
            task_kwargs["document_rag_factory"],
        )
        self.assertIs(
            self.knowledge_index_factory,
            task_kwargs["knowledge_index_factory"],
        )
        self.assertEqual(
            ANALYSIS_CLASSIFICATION_MODE_TOPK_SINGLE,
            task_kwargs["analysis_classification_mode"],
        )
        self.assertEqual(
            ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD,
            task_kwargs["analysis_filename_constraint_mode"],
        )
        target = thread_type.call_args.kwargs["target"]
        self.assertIs(
            self.services.upload_task_limiter,
            target.__self__,
        )
        self.assertEqual(0, len(self.document_rag_factory.ports))
        self.assertEqual(0, len(self.knowledge_index_factory.ports))


if __name__ == "__main__":
    unittest.main()
