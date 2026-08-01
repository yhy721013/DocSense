"""阶段 1G 遗留引用检查器的离线正负例。"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import textwrap
import unittest

from scripts.inspect_stage1g_references import (
    inspect_stage1g_references,
    main,
)


class Stage1GReferenceInspectorTests(unittest.TestCase):
    """用最小临时仓库证明引用分类和 fail-closed 语义。"""

    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(
            prefix="docsense-stage1g-reference-",
            ignore_cleanup_errors=True,
        )
        self.root = Path(self._tempdir.__enter__())
        (self.root / "app").mkdir()
        (self.root / "tests").mkdir()

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    def _write(self, relative_path: str, content: str) -> None:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            textwrap.dedent(content).lstrip(),
            encoding="utf-8",
        )

    def _report_findings(
        self,
        report: dict[str, object],
        candidate_id: str,
    ) -> list[dict[str, object]]:
        findings = report["findings"]
        self.assertIsInstance(findings, list)
        return [
            item
            for item in findings
            if isinstance(item, dict)
            and item.get("candidateId") == candidate_id
        ]

    def test_scan_separates_runtime_test_guard_script_and_history(self) -> None:
        self._write(
            "app/services/llm_service/report_service.py",
            """
            def run_report_task():
                return None
            """,
        )
        self._write(
            "app/modules/report/current_consumer.py",
            """
            from app.services.llm_service.report_service import run_report_task

            run_report_task()
            """,
        )
        self._write(
            "tests/test_legacy_execution.py",
            """
            from app.services.llm_service.report_service import run_report_task
            from unittest.mock import patch

            @patch("app.services.llm_service.report_service.run_report_task")
            def test_old(mocked):
                run_report_task()
            """,
        )
        self._write(
            "tests/test_guard.py",
            """
            def test_guard(self):
                self.assertNotIn("run_report_task", "new route source")
            """,
        )
        self._write(
            "scripts/legacy_tool.py",
            """
            from app.services.llm_service.report_service import run_report_task
            """,
        )
        self._write(
            "docs/更新记录/history.md",
            "历史执行入口 app.services.llm_service.report_service.run_report_task",
        )
        self._write(
            "README.md",
            "当前仍调用 app.services.llm_service.report_service.run_report_task",
        )
        self._write(
            ".env",
            "OLD_RUNNER=app.services.llm_service.report_service",
        )

        report = inspect_stage1g_references(self.root)
        findings = self._report_findings(
            report,
            "report_legacy_executor",
        )
        categories = {str(item["category"]) for item in findings}

        self.assertTrue(report["inventoryComplete"])
        self.assertIn("compatibility_source", categories)
        self.assertIn("production_runtime", categories)
        self.assertIn("test_execution", categories)
        self.assertIn("test_guard_string", categories)
        self.assertIn("script_execution", categories)
        self.assertIn("historical_documentation", categories)
        self.assertIn("current_documentation", categories)
        self.assertFalse(any(item["path"] == ".env" for item in findings))

    def test_non_literal_dynamic_import_fails_closed_without_exposing_value(self) -> None:
        self._write(
            "app/dynamic_loader.py",
            """
            import importlib

            def load(name):
                return importlib.import_module(name)
            """,
        )

        report = inspect_stage1g_references(self.root)

        self.assertFalse(report["inventoryComplete"])
        self.assertEqual(
            [
                {
                    "path": "app/dynamic_loader.py",
                    "line": 4,
                    "call": "import_module",
                }
            ],
            report["unknownDynamicImports"],
        )

    def test_literal_dynamic_import_is_classified_as_execution(self) -> None:
        self._write(
            "scripts/legacy_loader.py",
            """
            import importlib

            legacy = importlib.import_module(
                "app.services.llm_service.weaponry_service"
            )
            """,
        )

        report = inspect_stage1g_references(self.root)
        findings = self._report_findings(
            report,
            "weaponry_legacy_executor",
        )

        self.assertTrue(report["inventoryComplete"])
        self.assertTrue(
            any(
                item["referenceKind"] == "dynamic_import"
                and item["category"] == "script_execution"
                for item in findings
            )
        )

    def test_relative_import_is_resolved_to_absolute_candidate_module(self) -> None:
        """相对导入必须阻止候选模块被错误判定为可删除。"""

        self._write(
            "app/services/llm_service/consumer.py",
            """
            from . import architecture_recall_service

            VALUE = architecture_recall_service
            """,
        )

        report = inspect_stage1g_references(self.root)
        findings = self._report_findings(report, "analysis_legacy_recall")

        self.assertTrue(report["inventoryComplete"])
        self.assertTrue(
            any(
                item["category"] == "production_runtime"
                and item["referenceKind"] == "import"
                and item["target"]
                == "app.services.llm_service.architecture_recall_service"
                for item in findings
            )
        )

    def test_extensionless_deployment_configuration_is_scanned(self) -> None:
        """Dockerfile 等无后缀配置同样可能选择旧运行入口。"""

        self._write(
            "docker/Dockerfile",
            "ENV LEGACY_RUNNER=app.services.llm_service.report_service",
        )

        report = inspect_stage1g_references(self.root)
        findings = self._report_findings(report, "report_legacy_executor")

        self.assertTrue(
            any(
                item["path"] == "docker/Dockerfile"
                and item["category"] == "script_or_configuration"
                for item in findings
            )
        )

    def test_active_stage1g_design_is_current_documentation(self) -> None:
        self._write(
            "docs/重构记录/260801-阶段1G删除设计.md",
            "当前仍调用 app.services.llm_service.report_service.run_report_task",
        )

        report = inspect_stage1g_references(self.root)
        findings = self._report_findings(report, "report_legacy_executor")

        self.assertTrue(
            any(
                item["path"] == "docs/重构记录/260801-阶段1G删除设计.md"
                and item["category"] == "current_documentation"
                for item in findings
            )
        )

    def test_legacy_symbol_text_match_requires_identifier_boundary(self) -> None:
        """现行 Client 集合或工厂名称不得被旧聚合 Client 子串误报。"""

        self._write(
            "app/modules/weaponry/adapters/client_bundle.py",
            """
            class WeaponryAnythingLLMClients:
                pass

            class AnythingLLMClientFactory:
                pass
            """,
        )

        report = inspect_stage1g_references(self.root)
        findings = self._report_findings(
            report,
            "anythingllm_legacy_wrapper",
        )
        self.assertEqual([], findings)

    def test_frozen_assets_are_manifest_not_current_documentation(self) -> None:
        """历史契约字面量必须保留，但不能冒充当前调用说明。"""

        self._write(
            "tests/contracts/stage1-history.json",
            '{"legacy": "app.services.llm_service.report_service"}',
        )

        report = inspect_stage1g_references(self.root)
        findings = self._report_findings(report, "report_legacy_executor")
        self.assertTrue(findings)
        self.assertTrue(
            all(item["category"] == "manifest_definition" for item in findings)
        )

    def test_ignored_pycache_does_not_keep_deleted_directory_candidate_alive(
        self,
    ) -> None:
        """只剩解释器缓存的旧目录应视为源码已经物理退出。"""

        self._write(
            "app/services/translator/__pycache__/core.cpython-312.pyc",
            "ignored-cache",
        )

        report = inspect_stage1g_references(self.root)
        candidate = next(
            item
            for item in report["candidates"]
            if item["candidateId"] == "translator_legacy_package"
        )
        self.assertEqual(0, candidate["blockingReferenceCount"])
        self.assertTrue(candidate["deletionReady"])

    def test_json_cli_output_is_stable_and_contains_no_absolute_root(self) -> None:
        self._write("app/current.py", "VALUE = 1")
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(
                [
                    "--root",
                    str(self.root),
                    "--format",
                    "json",
                ]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(0, exit_code)
        self.assertEqual(1, payload["schemaVersion"])
        self.assertTrue(payload["inventoryComplete"])
        self.assertNotIn(str(self.root), stdout.getvalue())
        self.assertEqual("", stderr.getvalue())

    def test_invalid_root_returns_error_without_path_or_file_content(self) -> None:
        invalid = self.root / "missing"
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(["--root", str(invalid)])

        self.assertEqual(1, exit_code)
        self.assertEqual("", stdout.getvalue())
        self.assertIn("error_type=ValueError", stderr.getvalue())
        self.assertNotIn(str(invalid), stderr.getvalue())

    def test_unreadable_selected_text_file_fails_closed(self) -> None:
        path = self.root / "docker" / "Dockerfile"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\xff\xfe\x00\x00")
        stdout = io.StringIO()
        stderr = io.StringIO()

        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(["--root", str(self.root)])

        self.assertEqual(1, exit_code)
        self.assertEqual("", stdout.getvalue())
        self.assertIn("error_type=UnicodeDecodeError", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
