"""阶段 1A-2：模块骨架与依赖方向架构测试。

这些测试只静态读取源码，不导入 ``app`` 中的生产模块，因此不会构造容器、初始化
SQLite、连接 AnythingLLM 或启动 ``run.py``。除扫描当前仓库外，自测用例还会在
临时目录注入违规导入，证明每条门禁确实可以失败，而不是因骨架暂时为空自然通过。
"""

from __future__ import annotations

import ast
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests.architecture.import_rules import (
    APPLICATION_RULE,
    DOMAIN_RULE,
    PORTS_RULE,
    PRESENTER_RULE,
    TASKS_MODULE_RULE,
    ArchitectureRule,
    ImportViolation,
    collect_violations,
    describe_violations,
)


ROOT = Path(__file__).resolve().parents[1]
MODULES_ROOT = ROOT / "app" / "modules"
TASKS_ROOT = MODULES_ROOT / "tasks"
REPORT_ROOT = MODULES_ROOT / "report"
WEAPONRY_ROOT = MODULES_ROOT / "weaponry"
PRESENTERS_ROOT = ROOT / "app" / "presenters"


def _module_layer_dirs(layer_name: str) -> tuple[Path, ...]:
    """返回所有业务模块中指定分层目录，避免规则只绑定 tasks 一个模块。"""

    return tuple(
        sorted(
            (
                module_dir / layer_name
                for module_dir in MODULES_ROOT.iterdir()
                if module_dir.is_dir() and (module_dir / layer_name).is_dir()
            ),
            key=lambda path: path.as_posix(),
        )
    )


class CurrentArchitectureBoundaryTests(unittest.TestCase):
    """对当前工作树执行长期生效的架构门禁。"""

    def assert_rule_clean(
        self,
        paths: tuple[Path, ...],
        rule: ArchitectureRule,
    ) -> None:
        violations = collect_violations(paths, project_root=ROOT, rule=rule)
        self.assertFalse(
            violations,
            "发现架构边界违规:\n"
            + describe_violations(violations, project_root=ROOT),
        )

    def test_module_package_skeletons_are_complete(self) -> None:
        """已建立的模块分层必须具备包标识和职责文档，不能留下无说明空目录。"""

        required_directories = (
            MODULES_ROOT,
            TASKS_ROOT,
            TASKS_ROOT / "domain",
            TASKS_ROOT / "application",
            TASKS_ROOT / "ports",
            TASKS_ROOT / "adapters",
            REPORT_ROOT,
            REPORT_ROOT / "domain",
            REPORT_ROOT / "application",
            REPORT_ROOT / "ports",
            WEAPONRY_ROOT,
            WEAPONRY_ROOT / "domain",
            WEAPONRY_ROOT / "application",
            WEAPONRY_ROOT / "ports",
            WEAPONRY_ROOT / "adapters",
            ROOT / "app" / "adapters",
            ROOT / "app" / "adapters" / "web",
            ROOT / "app" / "adapters" / "web" / "flask",
        )
        for directory in required_directories:
            with self.subTest(directory=directory.relative_to(ROOT)):
                self.assertTrue((directory / "__init__.py").is_file())
                self.assertTrue((directory / "README.md").is_file())

    def test_all_module_domain_layers_are_framework_and_infrastructure_free(self) -> None:
        self.assert_rule_clean(_module_layer_dirs("domain"), DOMAIN_RULE)

    def test_all_module_ports_remain_abstract(self) -> None:
        self.assert_rule_clean(_module_layer_dirs("ports"), PORTS_RULE)

    def test_all_module_application_layers_depend_inward(self) -> None:
        self.assert_rule_clean(_module_layer_dirs("application"), APPLICATION_RULE)

    def test_tasks_module_does_not_reach_chat_persistence_or_foreign_modules(self) -> None:
        self.assert_rule_clean((TASKS_ROOT,), TASKS_MODULE_RULE)

    def test_presenters_do_not_read_database_or_anythingllm_client(self) -> None:
        self.assert_rule_clean((PRESENTERS_ROOT,), PRESENTER_RULE)

    def test_route_tests_do_not_construct_uninjected_production_applications(self) -> None:
        """只有显式生命周期用例可以调用无参 create_app，其他测试必须注入离线容器。"""

        allowed = {
            (
                "test_dependency_container.py",
                "test_production_owned_container_starts_once_and_registers_close",
            )
        }
        violations: list[str] = []
        for source_path in sorted((ROOT / "tests").glob("test_*.py")):
            # 仓库少量历史测试仍带 UTF-8 BOM；AST 门禁应兼容读取，而不是要求本轮
            # 顺带机械改写无关文件编码。
            source = source_path.read_text(encoding="utf-8-sig")
            tree = ast.parse(source, filename=str(source_path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for child in ast.walk(node):
                    if not isinstance(child, ast.Call):
                        continue
                    if not isinstance(child.func, ast.Name):
                        continue
                    if child.func.id != "create_app":
                        continue
                    has_services = any(
                        keyword.arg == "services" for keyword in child.keywords
                    )
                    if has_services:
                        continue
                    identity = (source_path.name, node.name)
                    if identity not in allowed:
                        violations.append(
                            f"{source_path.name}:{child.lineno} {node.name}"
                        )
        self.assertEqual(
            [],
            violations,
            "路由测试禁止无参 create_app()，请注入完全离线 ApplicationServices",
        )


class ArchitectureRuleSelfTests(unittest.TestCase):
    """用临时源码验证规则本身能识别真实违规。"""

    def _scan_source(
        self,
        relative_path: str,
        source_text: str,
        rule: ArchitectureRule,
    ) -> tuple[ImportViolation, ...]:
        with tempfile.TemporaryDirectory(prefix="docsense-architecture-") as temp_dir:
            project_root = Path(temp_dir)
            source_path = project_root / relative_path
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text(
                textwrap.dedent(source_text).lstrip(),
                encoding="utf-8",
            )
            return collect_violations(
                (source_path,),
                project_root=project_root,
                rule=rule,
            )

    @staticmethod
    def _targets(violations: tuple[ImportViolation, ...]) -> set[str]:
        return {violation.target for violation in violations}

    def test_domain_rule_rejects_web_database_and_http_libraries(self) -> None:
        violations = self._scan_source(
            "app/modules/tasks/domain/models.py",
            """
            from flask import request
            import sqlite3
            import requests
            """,
            DOMAIN_RULE,
        )
        self.assertEqual({"flask", "sqlite3", "requests"}, self._targets(violations))

    def test_ports_rule_rejects_reverse_application_dependency(self) -> None:
        violations = self._scan_source(
            "app/modules/tasks/ports/task_read.py",
            """
            from app.modules.tasks.application import CheckTaskStatusService
            import sqlalchemy
            """,
            PORTS_RULE,
        )
        self.assertEqual(
            {"app.modules.tasks.application", "sqlalchemy"},
            self._targets(violations),
        )

    def test_application_rule_rejects_flask_legacy_service_and_relative_adapter(self) -> None:
        violations = self._scan_source(
            "app/modules/tasks/application/check_status.py",
            """
            from flask import Blueprint, current_app, request
            from app.services.llm_service.task_service import LLMTaskService
            from ..adapters import legacy_task_service
            """,
            APPLICATION_RULE,
        )
        self.assertEqual(
            {
                "flask",
                "app.services.llm_service.task_service",
                "app.modules.tasks.adapters",
            },
            self._targets(violations),
        )

    def test_report_layers_can_only_reuse_tasks_public_control_plane(self) -> None:
        """Report 可使用 TaskId/Port，但不能穿透到通用任务具体 Adapter。"""

        allowed_ports = self._scan_source(
            "app/modules/report/ports/dispatcher.py",
            "from app.modules.tasks.domain import TaskId\n",
            PORTS_RULE,
        )
        allowed_application = self._scan_source(
            "app/modules/report/application/run_report.py",
            "from app.modules.tasks.ports import TaskCommandPort\n",
            APPLICATION_RULE,
        )
        rejected = self._scan_source(
            "app/modules/report/application/run_report.py",
            "from app.modules.tasks.adapters import LegacyTaskReadAdapter\n",
            APPLICATION_RULE,
        )

        self.assertEqual((), allowed_ports)
        self.assertEqual((), allowed_application)
        self.assertIn("app.modules.tasks.adapters", self._targets(rejected))

    def test_tasks_rule_rejects_chat_persistence_and_any_foreign_business_layer(self) -> None:
        violations = self._scan_source(
            "app/modules/tasks/adapters/legacy_task_service.py",
            """
            from app.services.chat.persistence import ChatStore
            from app.modules.report.adapters.mysql import ReportRepository
            from app.modules.weaponry.domain import WeaponryTask
            """,
            TASKS_MODULE_RULE,
        )
        self.assertEqual(
            {
                "app.services.chat.persistence",
                "app.modules.report.adapters.mysql",
                "app.modules.weaponry.domain",
            },
            self._targets(violations),
        )

    def test_positive_allowlists_reject_unlisted_client_libraries(self) -> None:
        cases = (
            (
                "app/modules/tasks/domain/models.py",
                "import httpx\nimport redis\n",
                DOMAIN_RULE,
                {"httpx", "redis"},
            ),
            (
                "app/modules/tasks/ports/task_read.py",
                "import pika\nimport minio\n",
                PORTS_RULE,
                {"pika", "minio"},
            ),
            (
                "app/modules/tasks/application/check_status.py",
                "import boto3\nimport os\n",
                APPLICATION_RULE,
                {"boto3", "os"},
            ),
        )
        for relative_path, source_text, rule, expected in cases:
            with self.subTest(path=relative_path, rule=rule.name):
                self.assertEqual(
                    expected,
                    self._targets(self._scan_source(relative_path, source_text, rule)),
                )

    def test_dynamic_imports_cannot_bypass_protected_layers(self) -> None:
        violations = self._scan_source(
            "app/modules/tasks/application/check_status.py",
            "client = __import__('httpx')\n",
            APPLICATION_RULE,
        )

        self.assertEqual(
            {"<dynamic-import>:httpx"},
            self._targets(violations),
        )

    def test_importlib_alias_is_detected_as_dynamic_import(self) -> None:
        violations = self._scan_source(
            "app/modules/tasks/domain/models.py",
            """
            import importlib as loader
            client = loader.import_module("redis")
            """,
            DOMAIN_RULE,
        )

        self.assertEqual(
            {"importlib", "<dynamic-import>:redis"},
            self._targets(violations),
        )

    def test_presenter_rule_rejects_database_and_anythingllm_dependencies(self) -> None:
        violations = self._scan_source(
            "app/presenters/task_status.py",
            """
            from app.services.core.database import DatabaseService
            from app.integrations.anythingllm.transport import AnythingLLMTransport
            """,
            PRESENTER_RULE,
        )
        self.assertEqual(
            {
                "app.services.core.database",
                "app.integrations.anythingllm.transport",
            },
            self._targets(violations),
        )

    def test_rules_allow_expected_inward_dependencies(self) -> None:
        allowed_cases = (
            (
                "app/modules/tasks/domain/models.py",
                "from dataclasses import dataclass\n",
                DOMAIN_RULE,
            ),
            (
                "app/modules/tasks/ports/task_read.py",
                "from typing import Protocol\nfrom app.modules.tasks.domain import TaskSnapshot\n",
                PORTS_RULE,
            ),
            (
                "app/modules/tasks/application/check_status.py",
                "from app.modules.tasks.domain import TaskSnapshot\n"
                "from app.modules.tasks.ports import TaskReadPort\n",
                APPLICATION_RULE,
            ),
            (
                "app/modules/weaponry/application/field_execution.py",
                "import hashlib\nimport json\n",
                APPLICATION_RULE,
            ),
            (
                "app/modules/tasks/adapters/legacy_task_service.py",
                "from app.services.llm_service.task_service import LLMTaskService\n"
                "from app.modules.tasks.ports import TaskReadPort\n",
                TASKS_MODULE_RULE,
            ),
            (
                "app/presenters/task_status.py",
                "from app.services.chat.domain.events import ChatStreamEvent\n",
                PRESENTER_RULE,
            ),
        )
        for relative_path, source_text, rule in allowed_cases:
            with self.subTest(path=relative_path, rule=rule.name):
                self.assertEqual(
                    (),
                    self._scan_source(relative_path, source_text, rule),
                )


if __name__ == "__main__":
    unittest.main()
