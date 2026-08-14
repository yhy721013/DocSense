"""阶段 2-4 第 8 步 Report 旧 SQL 与巨型 Service 入口永久收口门禁。"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

from app.services.llm_service.task_service import LLMTaskService


_ROOT = Path(__file__).resolve().parents[1]
_TASK_SERVICE_PATH = _ROOT / "app/services/llm_service/task_service.py"


def _task_service_class() -> ast.ClassDef:
    """解析生产源码，避免仅凭运行期 monkeypatch 形成虚假的删除证据。"""

    tree = ast.parse(_TASK_SERVICE_PATH.read_text(encoding="utf-8-sig"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "LLMTaskService":
            return node
    raise AssertionError("未找到 LLMTaskService 类定义")


class ReportLegacySqlCleanupTests(unittest.TestCase):
    """锁定 Report 单向迁移完成后的职责边界。"""

    def test_report_specific_task_and_resource_entries_cannot_return(self) -> None:
        removed = {
            "create_report_task",
            "create_report_resource_record",
            "get_report_resource_record",
            "save_report_resource_record",
            "prepare_report_resource_cleanup",
            "defer_report_resource_recovery",
            "list_recoverable_report_resource_ids",
        }
        class_node = _task_service_class()
        methods = {
            node.name
            for node in class_node.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        self.assertTrue(removed.isdisjoint(methods))
        for method_name in removed:
            with self.subTest(method=method_name):
                self.assertFalse(hasattr(LLMTaskService, method_name))

    def test_task_service_no_longer_owns_report_resource_ddl(self) -> None:
        """物理表只归 v2 Report Store；这里同时禁止旧索引和迁移逻辑复活。"""

        source = _TASK_SERVICE_PATH.read_text(encoding="utf-8-sig")
        self.assertNotIn("report_resource_records", source)
        self.assertNotIn("idx_report_resource_records", source)

    def test_interaction_audit_writes_only_delegate_to_shared_store(self) -> None:
        """保留 Analysis 所需兼容入口，但禁止在入口后藏回不可达的内联 SQL。"""

        class_node = _task_service_class()
        methods = {
            node.name: node
            for node in class_node.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for method_name in (
            "create_llm_interaction_with_trace",
            "append_llm_interaction_lifecycle_events",
        ):
            method = methods[method_name]
            attributes = {
                node.attr for node in ast.walk(method) if isinstance(node, ast.Attribute)
            }
            sql_text = "\n".join(
                node.value
                for node in ast.walk(method)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            ).upper()
            with self.subTest(method=method_name):
                self.assertIn("_interaction_store", attributes)
                self.assertNotIn("_connection", attributes)
                self.assertNotIn("_audit_executor", attributes)
                self.assertNotIn("INSERT INTO", sql_text)
                self.assertNotIn("UPDATE ", sql_text)

        self.assertFalse(hasattr(LLMTaskService, "create_llm_interaction"))
        self.assertFalse(hasattr(LLMTaskService, "update_llm_interaction_cleanup"))

    def test_shared_analysis_weaponry_and_audit_compatibility_remains(self) -> None:
        """第 8 步不是清空旧 Service，尚未迁移业务仍须按原契约运行。"""

        retained = {
            "create_analysis_batch_if_allowed",
            "create_analysis_resource_record",
            "get_analysis_resource_record",
            "acquire_callback_delivery_guard",
            "complete_callback_delivery_guard",
            "freeze_expired_callback_delivery_guards",
            "create_llm_interaction_with_trace",
            "append_llm_interaction_lifecycle_events",
            "get_llm_interaction_by_execution",
        }
        missing = sorted(name for name in retained if not hasattr(LLMTaskService, name))
        self.assertEqual([], missing)


if __name__ == "__main__":
    unittest.main()
