"""阶段 1D-7：武器谱关闭验收的永久静态门禁。

本文件只读取源码/配置文本，或使用显式字典构造基础设施配置；不会创建生产容器、连接
AnythingLLM、启动 Dispatcher 或执行 ``run.py``。这些断言用于防止后续改造把正式路由重新
接回遗留 Worker、模式选择器、共享父 Thread 或绕过 Evidence Selection 的路径。
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

from app.modules.weaponry.adapters import (
    WeaponryInfrastructureConfigurationError,
    load_weaponry_infrastructure_config,
)


ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT / "app"
LEGACY_WEAPONRY_MODULE = "app.services.llm_service.weaponry_service"
LEGACY_WEAPONRY_PATH = (
    APP_ROOT / "services" / "llm_service" / "weaponry_service.py"
)


def _source(path: Path) -> str:
    """统一兼容历史 UTF-8 BOM 文件，并为失败信息保留明确路径。"""

    return path.read_text(encoding="utf-8-sig")


def _tree(path: Path) -> ast.Module:
    return ast.parse(_source(path), filename=str(path))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(matches) != 1:
        raise AssertionError(f"函数 {name} 应存在且只能定义一次，实际为 {len(matches)}")
    return matches[0]


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        if isinstance(child.func, ast.Name):
            names.add(child.func.id)
        elif isinstance(child.func, ast.Attribute):
            names.add(child.func.attr)
    return names


class WeaponryStage1D7ClosureTests(unittest.TestCase):
    """对正式运行入口、核心链路与遗留边界执行长期关闭检查。"""

    def test_production_python_never_imports_legacy_weaponry_worker(self) -> None:
        """遗留文件可保留，但任何其他生产 Python 都不得重新依赖它。"""

        violations: list[str] = []
        sources = tuple(sorted(APP_ROOT.rglob("*.py"))) + (ROOT / "run.py",)
        for path in sources:
            if path == LEGACY_WEAPONRY_PATH:
                continue
            for node in ast.walk(_tree(path)):
                if isinstance(node, ast.ImportFrom) and (
                    node.module == LEGACY_WEAPONRY_MODULE
                    or (node.module or "").startswith(
                        f"{LEGACY_WEAPONRY_MODULE}."
                    )
                ):
                    violations.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} import-from"
                    )
                elif isinstance(node, ast.Import) and any(
                    alias.name == LEGACY_WEAPONRY_MODULE
                    or alias.name.startswith(f"{LEGACY_WEAPONRY_MODULE}.")
                    for alias in node.names
                ):
                    violations.append(
                        f"{path.relative_to(ROOT)}:{node.lineno} import"
                    )
        self.assertEqual([], violations)

    def test_public_weaponry_route_is_thin_and_has_no_worker_or_client(self) -> None:
        """只检查 weaponry 函数体，避免同文件的 analysis 遗留线程产生假阳性。"""

        route = _function(_tree(APP_ROOT / "blueprints" / "llm.py"), "llm_weaponry")
        names = {
            node.id for node in ast.walk(route) if isinstance(node, ast.Name)
        }
        calls = _called_names(route)
        forbidden = {
            "threading",
            "Thread",
            "AnythingLLMClient",
            "run_weaponry_task",
            "run_file_analysis_task",
            "run_file_analysis_batch_task",
        }
        self.assertEqual(set(), forbidden & (names | calls))
        self.assertIn("parse_weaponry_request", calls)
        self.assertIn("execute", calls)
        self.assertIn("present_success", calls)
        # 文档范围解析属于 Application 用例职责，蓝图只允许执行统一提交入口。
        self.assertNotIn("resolve", calls)

    def test_mode_selector_has_no_active_deployment_assignment(self) -> None:
        """部署样例不得继续要求模式 2；Loader 兼容不等于仍提供运行时选择器。"""

        active_assignments: list[str] = []
        for path in (ROOT / ".env.example", ROOT / "docker" / ".env.docker"):
            for line_number, line in enumerate(_source(path).splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if re.match(r"^WEAPONRY_ANALYSE_MODE\s*=", stripped):
                    active_assignments.append(
                        f"{path.relative_to(ROOT)}:{line_number}"
                    )
        self.assertEqual([], active_assignments)
        self.assertNotIn(
            "WEAPONRY_ANALYSE_MODE=2",
            _source(ROOT / "scripts" / "run_llm_weaponry_directory.py"),
        )
        self.assertNotIn("WEAPONRY_ANALYSE_MODE=2 python run.py", _source(ROOT / "README.md"))

    def test_legacy_mode_one_is_rejected_and_two_cannot_change_strategy(self) -> None:
        """迁移期值 2 只允许启动，配置对象中不再暴露可选择的 analyse_mode。"""

        baseline = {
            "DOCSENSE_WEAPONRY_PROVIDER_FINGERPRINT": "provider-v1",
            "DOCSENSE_WEAPONRY_EMBEDDING_FINGERPRINT": "embedding-v1",
            "DOCSENSE_WEAPONRY_DOCUMENT_PROCESSING_FINGERPRINT": "processing-v1",
            "DOCSENSE_WEAPONRY_EXTRACTION_MODEL_FINGERPRINT": "model-v1",
            "WEAPONRY_TERMS_RULE_CONTEXT_ENABLED": "false",
        }
        for invalid in ("1", "0", "3", "file_aggregate_v1"):
            with self.subTest(value=invalid), self.assertRaises(
                WeaponryInfrastructureConfigurationError
            ):
                load_weaponry_infrastructure_config(
                    {**baseline, "WEAPONRY_ANALYSE_MODE": invalid}
                )
        config = load_weaponry_infrastructure_config(
            {**baseline, "WEAPONRY_ANALYSE_MODE": "2"}
        )
        self.assertFalse(hasattr(config, "analyse_mode"))

    def test_domain_and_application_do_not_depend_on_terms_infrastructure(self) -> None:
        """未来删除 Terms Provider 时不得修改核心领域或执行用例。"""

        forbidden = (
            "WEAPONRY_TERMS_",
            "weaponry-terms-rules",
            "TermsRuleGuidanceAdapter",
            "NoAuxiliaryGuidanceAdapter",
            "terms_workspace_name",
            "terms_dir",
        )
        violations: list[str] = []
        for layer in ("domain", "application"):
            for path in sorted((APP_ROOT / "modules" / "weaponry" / layer).rglob("*.py")):
                content = _source(path)
                for marker in forbidden:
                    if marker in content:
                        violations.append(
                            f"{path.relative_to(ROOT)} contains {marker}"
                        )
        self.assertEqual([], violations)

    def test_application_has_single_retrieval_selection_extraction_flow(self) -> None:
        """Candidate 必须经过 Selection；抽取函数不能反向执行 Retrieval。"""

        path = APP_ROOT / "modules" / "weaponry" / "application" / "field_execution.py"
        tree = _tree(path)
        retrieval = _function(tree, "_retrieve_and_select")
        extraction = _function(tree, "_extract_source")
        retrieval_calls = _called_names(retrieval)
        extraction_calls = _called_names(extraction)
        self.assertIn("build_retrieval_query", retrieval_calls)
        self.assertIn("search_target", retrieval_calls)
        self.assertIn("select_evidence", retrieval_calls)
        self.assertTrue(
            {"build_input_extraction_prompt", "build_table_extraction_prompt"}
            <= extraction_calls
        )
        self.assertTrue(
            {"search_target", "select_evidence", "build_retrieval_query"}.isdisjoint(
                extraction_calls
            )
        )

    def test_provided_evidence_adapter_cannot_reach_target_rag(self) -> None:
        """生产抽取只能向空 workspace 发送当前 Evidence，不能执行文档二次 RAG。"""

        path = (
            APP_ROOT
            / "modules"
            / "weaponry"
            / "adapters"
            / "provided_evidence_extraction.py"
        )
        tree = _tree(path)
        calls = _called_names(tree)
        self.assertTrue(
            {
                "vector_search",
                "update_embeddings",
                "search_target",
                "send_prompt_to_thread",
            }.isdisjoint(calls)
        )
        ask_calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "ask"
        ]
        self.assertEqual(1, len(ask_calls))
        document_ids = next(
            (
                keyword.value
                for keyword in ask_calls[0].keywords
                if keyword.arg == "document_ids"
            ),
            None,
        )
        self.assertIsInstance(document_ids, ast.Tuple)
        self.assertEqual([], document_ids.elts)  # type: ignore[union-attr]

    def test_only_compatibility_tests_call_legacy_worker(self) -> None:
        """1G-4 后测试必须验证现行分层，不得再执行旧 Worker。"""

        callers = []
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            tree = _tree(path)
            imports_legacy_worker = any(
                isinstance(node, ast.ImportFrom)
                and node.module == LEGACY_WEAPONRY_MODULE
                and any(alias.name == "run_weaponry_task" for alias in node.names)
                for node in ast.walk(tree)
            )
            calls_legacy_worker = any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "run_weaponry_task"
                for node in ast.walk(tree)
            )
            if imports_legacy_worker or calls_legacy_worker:
                callers.append(path.name)
        self.assertEqual([], callers)


if __name__ == "__main__":
    unittest.main()
