"""阶段 1C-6/1C-7 报告公开契约、持久受理和遗留链隔离验收。"""

from __future__ import annotations

import ast
import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app
from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir
from tests.offline_application import build_offline_application_services


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "tests" / "contracts" / "stage0_contracts.json"


def _function_node(path: Path, name: str) -> ast.FunctionDef:
    """静态读取指定函数，避免通过 import 触发任何运行期装配。"""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"未找到函数: {path}:{name}")


class ReportRouteImplementedContractTests(unittest.TestCase):
    """使用离线容器证明批准的 202/409 已进入当前 Flask 路由。"""

    def setUp(self) -> None:
        self._tempdir = workspace_tempdir()
        self.runtime_directory = self._tempdir.__enter__()
        self.services = build_offline_application_services(self.runtime_directory)
        self.task_service = self.services.task_service
        self.progress_hub = self.services.progress_hub
        self.app = create_app(services=self.services)
        self.client = self.app.test_client()
        self.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    @staticmethod
    def _valid_payload(report_id: object = 132) -> dict[str, object]:
        return {
            "businessType": "report",
            "params": [
                {
                    "reportId": report_id,
                    "filePathList": ["http://files.invalid/source.pdf"],
                    "templateDesc": "模板说明",
                    "templateOutline": "http://files.invalid/template.docx",
                    "requirement": "生成报告",
                }
            ],
        }

    @patch("app.blueprints.llm.threading.Thread")
    def test_success_is_strict_empty_202_and_only_wakes_dispatcher(
        self,
        mock_thread,
    ) -> None:
        response = self.client.post(
            "/llm/generate-report",
            json=self._valid_payload(),
        )

        self.assertEqual(202, response.status_code)
        self.assertEqual(b"", response.get_data())
        self.assertIsNone(response.get_json(silent=True))
        current = self.contract["reportGenerationBaseline"]["current"]
        target = self.contract["reportGenerationBaseline"]["target"]
        self.assertEqual("", current["success"]["body"])
        self.assertEqual("", target["success"]["body"])
        mock_thread.assert_not_called()
        self.assertEqual(1, len(self.services.report_dispatcher.task_ids))

        latest = self.progress_hub.get_latest("report", "132")
        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(0.0, latest["data"]["progress"])

    @patch("app.blueprints.llm.threading.Thread")
    def test_active_duplicate_returns_409_without_new_execution_or_wakeup(
        self,
        mock_thread,
    ) -> None:
        first_response = self.client.post(
            "/llm/generate-report",
            json=self._valid_payload(),
        )
        first = self.task_service.get_task("report", "132")
        accepted_before = self.task_service.list_accepted_task_execution_ids(
            "report",
            limit=10,
        )

        response = self.client.post(
            "/llm/generate-report",
            json=self._valid_payload(),
        )
        latest = self.task_service.get_task("report", "132")

        self.assertEqual(202, first_response.status_code)
        self.assertEqual(409, response.status_code)
        self.assertEqual({"error": "任务正在处理中"}, response.get_json())
        self.assertIsNotNone(latest)
        assert latest is not None
        assert first is not None
        self.assertEqual(first["execution_id"], latest["execution_id"])
        self.assertEqual(
            accepted_before,
            self.task_service.list_accepted_task_execution_ids(
                "report",
                limit=10,
            ),
        )
        self.assertEqual(1, len(self.services.report_dispatcher.task_ids))
        mock_thread.assert_not_called()

    def test_callback_sending_and_unknown_both_map_to_same_409(self) -> None:
        with sqlite3.connect(self.task_service.db_path) as connection:
            connection.execute(
                """
                INSERT INTO callback_delivery_guards (
                    business_type, business_key, owner_execution_id,
                    state, updated_at
                ) VALUES ('report', '132', 'old-task', 'sending', 'now')
                """
            )

        sending = self.client.post(
            "/llm/generate-report",
            json=self._valid_payload(),
        )
        with sqlite3.connect(self.task_service.db_path) as connection:
            connection.execute(
                """
                UPDATE callback_delivery_guards
                SET state = 'outcome_unknown'
                WHERE business_type = 'report' AND business_key = '132'
                """
            )
        unknown = self.client.post(
            "/llm/generate-report",
            json=self._valid_payload(),
        )

        for response in (sending, unknown):
            self.assertEqual(409, response.status_code)
            self.assertEqual({"error": "任务正在处理中"}, response.get_json())
        self.assertEqual(
            (),
            self.task_service.list_accepted_task_execution_ids(
                "report",
                limit=10,
            ),
        )
        self.assertEqual([], self.services.report_dispatcher.task_ids)

    @patch("app.blueprints.llm.threading.Thread")
    def test_approved_params_policy_rejects_entire_mixed_request(
        self,
        mock_thread,
    ) -> None:
        valid_params = self._valid_payload()["params"][0]  # type: ignore[index]
        payload = {
            "businessType": "report",
            "params": ["ignored", valid_params, 123, {"reportId": 999}],
        }

        response = self.client.post("/llm/generate-report", json=payload)

        self.assertEqual(400, response.status_code)
        self.assertEqual({"error": "params元素必须是对象"}, response.get_json())
        self.assertIsNone(self.task_service.get_task("report", "132"))
        self.assertIsNone(self.task_service.get_task("report", "999"))
        self.assertIsNone(self.progress_hub.get_latest("report", "132"))
        self.assertEqual(
            "reject_entire_request_http_400",
            self.contract["reportGenerationBaseline"]["current"][
                "paramsPolicy"
            ]["nonObjectElements"],
        )
        mock_thread.assert_not_called()

    @patch("app.blueprints.llm.threading.Thread")
    def test_approved_file_path_policy_rejects_invalid_element_before_acceptance(
        self,
        mock_thread,
    ) -> None:
        payload = self._valid_payload()
        payload["params"][0]["filePathList"] = [123]  # type: ignore[index]

        response = self.client.post("/llm/generate-report", json=payload)

        self.assertEqual(400, response.status_code)
        self.assertEqual(
            {"error": "filePathList中第1项不是有效字符串"},
            response.get_json(),
        )
        self.assertIsNone(self.task_service.get_task("report", "132"))
        self.assertIsNone(self.progress_hub.get_latest("report", "132"))
        self.assertEqual(
            "non_empty_strings_or_reject_http_400",
            self.contract["reportGenerationBaseline"]["current"][
                "paramsPolicy"
            ]["filePathListElementTypes"],
        )
        mock_thread.assert_not_called()

    def test_current_validation_error_texts_are_frozen(self) -> None:
        valid_params = self._valid_payload()["params"][0]  # type: ignore[index]
        cases = (
            ({"businessType": "wrong", "params": [valid_params]}, "businessType必须为report"),
            ({"businessType": "report"}, "params不能为空"),
            ({"businessType": "report", "params": [{}]}, "reportId不能为空"),
            (
                {
                    "businessType": "report",
                    "params": [{"reportId": "132.0"}],
                },
                "reportId必须是整数或整数字符串",
            ),
            (
                {
                    "businessType": "report",
                    "params": [{"reportId": 132, "filePathList": []}],
                },
                "filePathList不能为空",
            ),
            (
                {
                    "businessType": "report",
                    "params": [
                        {
                            "reportId": 132,
                            "filePathList": ["http://files.invalid/a.pdf"],
                            "templateOutline": "   ",
                        }
                    ],
                },
                "templateOutline不能为空",
            ),
        )

        expected_errors = self.contract["reportGenerationBaseline"]["current"][
            "validationErrors"
        ]
        for payload, message in cases:
            with self.subTest(message=message):
                response = self.client.post("/llm/generate-report", json=payload)
                self.assertEqual(400, response.status_code)
                self.assertEqual({"error": message}, response.get_json())
                self.assertIn(message, expected_errors.values())

    @patch("app.blueprints.llm.threading.Thread")
    def test_approved_top_level_policy_rejects_every_non_object_json(
        self,
        mock_thread,
    ) -> None:
        invalid_payloads = ([{"x": 1}], [], "report", 132, True, None)
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self.client.post(
                    "/llm/generate-report",
                    json=payload,
                )

                self.assertEqual(400, response.status_code)
                self.assertEqual(
                    {"error": "请求体必须是JSON对象"},
                    response.get_json(),
                )
        malformed_response = self.client.post(
            "/llm/generate-report",
            data="{",
            content_type="application/json",
        )
        self.assertEqual(400, malformed_response.status_code)
        self.assertEqual(
            {"error": "请求体必须是JSON对象"},
            malformed_response.get_json(),
        )
        self.assertEqual(
            "reject_http_400",
            self.contract["reportGenerationBaseline"]["current"][
                "paramsPolicy"
            ]["topLevelNonObject"],
        )
        mock_thread.assert_not_called()


class ReportLegacyCompatibilityIsolationTests(unittest.TestCase):
    """保留旧 API 风险证明，同时确认生产报告路由不再引用它。"""

    def test_old_execution_result_can_mutate_new_latest_row(self) -> None:
        with workspace_tempdir() as runtime_directory:
            task_service = LLMTaskService(
                db_path=f"{runtime_directory}/tasks.sqlite3"
            )
            first = task_service.create_report_task(
                132,
                {"businessType": "report", "version": "old"},
            )
            second = task_service.create_report_task(
                132,
                {"businessType": "report", "version": "new"},
            )

            task_service.mark_business_result(
                "report",
                "132",
                {
                    "ownerExecutionId": first["execution_id"],
                    "details": "旧执行结果",
                },
                status="1",
            )
            latest = task_service.get_task("report", "132")

        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(second["execution_id"], latest["execution_id"])
        self.assertEqual("1", latest["status"])
        self.assertEqual(
            first["execution_id"],
            latest["result_payload"]["ownerExecutionId"],
        )


class ReportStaticImplementedContractTests(unittest.TestCase):
    """通过 AST 固定薄路由和遗留执行链隔离事实。"""

    def test_report_route_creates_no_thread_and_uses_submit_presenter(self) -> None:
        function = _function_node(
            ROOT / "app" / "blueprints" / "llm.py",
            "llm_generate_report",
        )
        thread_calls = [
            node
            for node in ast.walk(function)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "threading"
            and node.func.attr == "Thread"
        ]

        self.assertEqual(0, len(thread_calls))
        attributes = {
            node.attr
            for node in ast.walk(function)
            if isinstance(node, ast.Attribute)
        }
        self.assertIn("to_submission", attributes)
        self.assertIn("execute", attributes)
        self.assertIn("present_success", attributes)

    def test_report_route_no_longer_references_legacy_report_worker(self) -> None:
        blueprint_source = (
            ROOT / "app" / "blueprints" / "llm.py"
        ).read_text(encoding="utf-8")
        function = _function_node(
            ROOT / "app" / "blueprints" / "llm.py",
            "llm_generate_report",
        )
        function_source = ast.unparse(function)

        self.assertNotIn("run_report_task", blueprint_source)
        self.assertNotIn("create_report_task", function_source)
        self.assertNotIn("mark_business_result", function_source)

    def test_production_code_cannot_reenter_legacy_report_worker(self) -> None:
        """永久禁止生产模块重新导入迁移期报告 Worker。

        旧实现本身仍由兼容测试直接调用，因此不能在 1C-7 删除。这里扫描 ``app`` 与
        ``run.py`` 的其余生产源码，覆盖路由、组合根和启动入口，防止未来通过新的导入点
        绕开 Report Application/Dispatcher。静态解析不会构造容器、启动线程或连接服务。
        """

        legacy_module = "app.services.llm_service.report_service"
        legacy_source = ROOT / "app" / "services" / "llm_service" / "report_service.py"
        production_sources = [
            source
            for source in (ROOT / "app").rglob("*.py")
            if source != legacy_source
        ]
        production_sources.append(ROOT / "run.py")
        violations: list[str] = []

        for source in sorted(production_sources):
            tree = ast.parse(
                source.read_text(encoding="utf-8-sig"),
                filename=str(source),
            )
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    imported_names = {alias.name for alias in node.names}
                    imports_legacy_module = node.module == legacy_module
                    imports_legacy_from_package = (
                        node.module == "app.services.llm_service"
                        and "report_service" in imported_names
                    )
                    imports_legacy_relatively = (
                        source.parent == legacy_source.parent
                        and node.level > 0
                        and (
                            node.module == "report_service"
                            or (
                                node.module is None
                                and "report_service" in imported_names
                            )
                        )
                    )
                    if (
                        imports_legacy_module
                        or imports_legacy_from_package
                        or imports_legacy_relatively
                    ):
                        violations.append(
                            f"{source.relative_to(ROOT)}:{node.lineno} "
                            f"legacy report import"
                        )
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == legacy_module:
                            violations.append(
                                f"{source.relative_to(ROOT)}:{node.lineno} import {legacy_module}"
                            )

        self.assertEqual(
            [],
            violations,
            "生产代码禁止重新导入遗留报告执行链，请依赖 Report Application/Port",
        )


if __name__ == "__main__":
    unittest.main()
