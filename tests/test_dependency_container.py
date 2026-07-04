"""阶段 6 应用容器、任务级 Factory 与路由注入的离线测试。"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from app import create_app
from app.integrations.anythingllm.factory import AnythingLLMGatewayFactory
from app.integrations.anythingllm.transport import AnythingLLMTransport
from app.ports import DocumentRagFactory, DocumentRagPort
from app.services.core.config import AnythingLLMConfig, LLMIntegrationConfig
from app.container import (
    APPLICATION_SERVICES_EXTENSION,
    ApplicationServices,
    UploadTaskLimiter,
)
from app.services.core.database import ChatDatabaseService, DatabaseService
from app.services.core.progress_hub import LLMProgressHub
from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir
from tests.fakes import FakeDocumentRagFactory


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
        """Transport 尚未创建时，Factory 必须原样传播构造异常。"""
        factory = AnythingLLMGatewayFactory(
            self._config(),
            transport_factory=Mock(side_effect=ValueError("配置无效")),
        )

        with self.assertRaisesRegex(ValueError, "配置无效"):
            with factory.create():
                self.fail("Transport 构造失败后不应进入任务作用域")


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
        self._temp_directory = workspace_tempdir()
        self.runtime_directory = self._temp_directory.__enter__()
        self.document_rag_factory = FakeDocumentRagFactory()
        self.services = ApplicationServices(
            document_rag_factory=self.document_rag_factory,
            knowledge_index_factory=None,
            task_service=LLMTaskService(
                db_path=f"{self.runtime_directory}/tasks.sqlite3"
            ),
            kb_service=DatabaseService(
                db_path=f"{self.runtime_directory}/knowledge.sqlite3"
            ),
            chat_db=ChatDatabaseService(
                db_path=f"{self.runtime_directory}/chat.sqlite3"
            ),
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
        """Blueprint 只能解析应用容器，不能重新引入模块级数据库或信号量单例。"""
        blueprint_source = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "blueprints"
            / "llm.py"
        ).read_text(encoding="utf-8")
        forbidden_constructors = (
            "LLMTaskService(",
            "DatabaseService(",
            "ChatDatabaseService(",
            "LLMProgressHub(",
            "threading.Semaphore(",
        )

        for constructor in forbidden_constructors:
            with self.subTest(constructor=constructor):
                self.assertNotIn(constructor, blueprint_source)

    @patch("app.blueprints.llm.threading.Thread")
    def test_analysis_route_injects_factory_without_entering_lease(
        self,
        thread_type: MagicMock,
    ) -> None:
        """路由只把无状态 Factory 交给线程，不在请求线程创建 Gateway 或 Transport。"""
        app = create_app(services=self.services)

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
        target = thread_type.call_args.kwargs["target"]
        self.assertIs(
            self.services.upload_task_limiter,
            target.__self__,
        )
        self.assertEqual(0, len(self.document_rag_factory.ports))


if __name__ == "__main__":
    unittest.main()
