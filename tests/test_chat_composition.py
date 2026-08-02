"""Chat 模块生产组合根的离线装配门禁。"""

from __future__ import annotations

import ast
import tempfile
import os
import subprocess
import sys
import unittest
from pathlib import Path

from app.modules.chat.adapters.anythingllm_factory import AnythingLLMChatFactory
from app.modules.chat.adapters.sqlite.store import ChatStore
from app.modules.chat.application.document_resolver import ResolvedChatDocument
from app.modules.chat.composition import (
    ChatApplicationServices,
    compose_chat_application_services,
)
from app.services.core.config import AnythingLLMConfig


class _EmptyDocumentResolver:
    """组合根测试使用的只读 Resolver；不会接触知识库或供应商。"""

    def resolve_many(
        self,
        file_names: tuple[str, ...],
    ) -> tuple[ResolvedChatDocument, ...]:
        return ()

    def resolve_all_available(self) -> tuple[ResolvedChatDocument, ...]:
        return ()


class ChatCompositionTests(unittest.TestCase):
    """证明全局 Container 无需了解 Chat 内部构造顺序。"""

    def test_compose_builds_one_shared_object_graph_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            services = compose_chat_application_services(
                db_path=str(Path(temp_dir) / "chat.sqlite3"),
                anythingllm_config=AnythingLLMConfig(
                    base_url="http://127.0.0.1:1",
                    api_key="test-secret-not-used",
                    timeout=0.1,
                    storage_root=None,
                ),
                document_resolver=_EmptyDocumentResolver(),
            )

            self.assertIsInstance(services, ChatApplicationServices)
            self.assertIsInstance(services.store, ChatStore)
            self.assertIsInstance(
                services.conversation_factory,
                AnythingLLMChatFactory,
            )
            # 各用例必须共享同一 Store/Factory，避免同一请求落入彼此隔离的内存或
            # 数据库对象图。这里不进入 Factory 租约，因此不会创建 HTTP Transport。
            self.assertIs(services.history._store, services.store)
            self.assertIs(services.abort._store, services.store)
            self.assertIs(services.delete._store, services.store)
            self.assertIs(
                services.delete._conversation_factory,
                services.conversation_factory,
            )
            self.assertTrue((Path(temp_dir) / "chat.sqlite3").is_file())

    def test_importing_chat_package_does_not_create_runtime_resources(self) -> None:
        """仅导入模块不得建库、联网或启动后台线程。"""
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "import-only.sqlite3"
            script = (
                "import socket, threading\n"
                "def reject_connect(*args, **kwargs):\n"
                "    raise AssertionError('network connect during import')\n"
                "socket.socket.connect = reject_connect\n"
                "import app.modules.chat\n"
                "assert len(threading.enumerate()) == 1, threading.enumerate()\n"
            )
            environment = os.environ.copy()
            environment["DOCSENSE_CHAT_DB"] = str(db_path)
            completed = subprocess.run(
                [sys.executable, "-B", "-c", script],
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(
                0,
                completed.returncode,
                msg=completed.stdout + completed.stderr,
            )
            self.assertFalse(db_path.exists())

    def test_global_container_delegates_chat_construction_to_composition(self) -> None:
        """永久阻止全局 Container 再次了解 Chat 内部对象的构造顺序。"""
        project_root = Path(__file__).resolve().parents[1]
        container_path = project_root / "app" / "container.py"
        source = container_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(container_path))
        factory = next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "create_application_services"
        )
        factory_source = ast.get_source_segment(source, factory) or ""

        self.assertEqual(
            1,
            factory_source.count("compose_chat_application_services("),
            "生产工厂必须且只能调用一次 Chat 组合根",
        )
        self.assertIn("chat_services=chat_services", factory_source)
        for forbidden_constructor in (
            "ChatStore(",
            "ChatRunLockService(",
            "ChatCommandService(",
            "SynchronousChatRunExecutor(",
            "InlineChatRunDispatcher(",
            "ChatHistoryService(",
            "ChatTitleService(",
            "ChatAbortService(",
            "ChatDeleteService(",
            "ChatCleanupJobExecutor(",
            "AnythingLLMChatFactory(",
        ):
            with self.subTest(forbidden_constructor=forbidden_constructor):
                self.assertNotIn(forbidden_constructor, factory_source)


if __name__ == "__main__":
    unittest.main()
