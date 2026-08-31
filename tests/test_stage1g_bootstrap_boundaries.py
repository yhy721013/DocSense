"""阶段 1G-2 框架无关组合根与 Flask 生命周期边界测试。"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask

from app import create_app
from app.blueprints.dependencies import get_application_services
from app.container import APPLICATION_SERVICES_EXTENSION, ApplicationServices
from tests import workspace_tempdir
from tests.offline_application import build_offline_application_services


ROOT = Path(__file__).resolve().parents[1]


class Stage1GBootstrapBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = workspace_tempdir()
        self.root = Path(self._tempdir.__enter__())
        self.services = build_offline_application_services(self.root / "application")

    def tearDown(self) -> None:
        self.services.close()
        self._tempdir.__exit__(None, None, None)

    def test_container_ast_has_no_web_framework_reference(self) -> None:
        source_path = ROOT / "app" / "container.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        imported_roots = {
            alias.name.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported_roots.update(
            node.module.split(".", 1)[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        self.assertTrue(
            {"flask", "werkzeug", "fastapi", "starlette"}.isdisjoint(imported_roots)
        )
        self.assertNotIn("current_app", source_path.read_text(encoding="utf-8"))

    def test_injected_services_are_not_started_or_rebuilt(self) -> None:
        with (
            patch("app.create_application_services") as production_builder,
            patch.object(ApplicationServices, "start_background_services") as start,
        ):
            app = create_app(services=self.services)

        self.assertIs(
            self.services,
            app.extensions[APPLICATION_SERVICES_EXTENSION],
        )
        production_builder.assert_not_called()
        start.assert_not_called()

    def test_owned_services_start_once_and_registered_close_runs_once(self) -> None:
        owned = MagicMock(spec=ApplicationServices)
        with (
            patch("app.create_application_services", return_value=owned),
            patch("app.atexit.register") as register,
        ):
            # 显式传入 None 表达本用例有意验证生产所有权分支，同时满足仓库中
            # “测试不得无参构造生产应用”的永久静态门禁。
            create_app(services=None)

        owned.start_background_services.assert_called_once_with()
        register.assert_called_once_with(owned.close)
        register.call_args.args[0]()
        owned.close.assert_called_once_with()

    def test_flask_dependency_adapter_fails_closed_without_valid_container(self) -> None:
        app = Flask(__name__)
        with app.app_context():
            with self.assertRaisesRegex(RuntimeError, "尚未安装"):
                get_application_services()
            app.extensions[APPLICATION_SERVICES_EXTENSION] = object()
            with self.assertRaisesRegex(RuntimeError, "类型无效"):
                get_application_services()

    def test_create_app_with_injected_services_does_not_create_transport(self) -> None:
        with patch(
            "app.integrations.anythingllm.transport.AnythingLLMTransport.__init__",
            side_effect=AssertionError("create_app 不得创建网络 Transport"),
        ) as constructor:
            create_app(services=self.services)
        constructor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
