"""阶段 1F-1：文件分析 Domain 迁移与兼容导出的离线门禁。

本模块只导入无副作用的 Domain 和既有兼容模块，不构造 Flask 应用、不启动 run.py，也不访问
真实网络、文件或数据库。公开接口契约仍由阶段 1F-0 的黄金资产单独冻结。
"""

from __future__ import annotations

import ast
import importlib
import unittest
from pathlib import Path

from app.modules.analysis.domain import (
    architecture_recall as domain_recall,
    architecture_tree as domain_tree,
    callback_payloads,
    classification_rules,
    models,
    prompts,
    ranges,
    result_mapping,
)
from app.services.core import architecture_tree as legacy_tree
from app.services.core import prompts as legacy_prompts
from app.services.core import config as legacy_config
from app.services.llm_service import analysis_service
from app.services.llm_service import architecture_recall_service as legacy_recall
from tests.architecture.import_rules import (
    DOMAIN_RULE,
    collect_violations,
    describe_violations,
)


ROOT = Path(__file__).resolve().parents[1]
ANALYSIS_DOMAIN_ROOT = ROOT / "app" / "modules" / "analysis" / "domain"
LEGACY_ANALYSIS_SERVICE_PATH = (
    ROOT / "app" / "services" / "llm_service" / "analysis_service.py"
)


class AnalysisDomainBoundaryTests(unittest.TestCase):
    """锁定 1F-1 Domain 只依赖自身和批准标准库的架构边界。"""

    def test_legacy_office_internal_basename_is_removed_from_final_public_result(self) -> None:
        """只替换本任务精确内部名，并覆盖翻译等后置填充字段。"""

        internal_name = f"prepared-{'a' * 32}.xlsx"
        other_name = f"prepared-{'b' * 32}.xlsx"
        public = result_mapping.sanitize_analysis_public_result(
            {
                "fileDataItem": {
                    "source": f"sheet from {internal_name}",
                    "documentTranslationOne": f"translated {internal_name}",
                    "documentTranslationTwo": other_name,
                }
            },
            internal_prepared_basename=internal_name,
            business_file_name="业务原名.xls",
        )

        self.assertEqual(
            "sheet from 业务原名.xls",
            public["fileDataItem"]["source"],
        )
        self.assertEqual(
            "translated 业务原名.xls",
            public["fileDataItem"]["documentTranslationOne"],
        )
        self.assertEqual(other_name, public["fileDataItem"]["documentTranslationTwo"])

    def test_domain_imports_are_framework_and_legacy_service_free(self) -> None:
        """静态检查禁止框架、数据库、HTTP、文件解析和旧服务反向依赖。"""

        violations = collect_violations(
            (ANALYSIS_DOMAIN_ROOT,),
            project_root=ROOT,
            rule=DOMAIN_RULE,
        )
        self.assertFalse(
            violations,
            "Analysis Domain 发现依赖边界违规:\n"
            + describe_violations(violations, project_root=ROOT),
        )

        forbidden_prefixes = (
            "flask",
            "sqlite3",
            "requests",
            "fitz",
            "app.services",
            "app.integrations",
        )
        for source_path in sorted(ANALYSIS_DOMAIN_ROOT.glob("*.py")):
            source = source_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(source_path))
            imported_modules = {
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            }
            imported_modules.update(
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            )
            for forbidden in forbidden_prefixes:
                with self.subTest(source=source_path.name, forbidden=forbidden):
                    self.assertFalse(
                        any(
                            target == forbidden
                            or target.startswith(f"{forbidden}.")
                            for target in imported_modules
                        ),
                    )

    def test_domain_does_not_call_file_io_apis(self) -> None:
        """导入白名单之外，还要禁止内置 open 和常见 Path 文件 I/O。"""

        forbidden_attribute_calls = {
            "chmod",
            "exists",
            "glob",
            "is_dir",
            "is_file",
            "iterdir",
            "lstat",
            "mkdir",
            "open",
            "read_bytes",
            "read_text",
            "rename",
            "rglob",
            "rmdir",
            "stat",
            "touch",
            "unlink",
            "write_bytes",
            "write_text",
        }
        violations: list[str] = []
        for source_path in sorted(ANALYSIS_DOMAIN_ROOT.glob("*.py")):
            source = source_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(source_path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Name) and node.func.id == "open":
                    violations.append(f"{source_path.name}:{node.lineno}: open")
                elif (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in forbidden_attribute_calls
                ):
                    violations.append(
                        f"{source_path.name}:{node.lineno}: {node.func.attr}"
                    )

        self.assertFalse(
            violations,
            "Analysis Domain 禁止执行文件系统 I/O:\n" + "\n".join(violations),
        )

    def test_legacy_analysis_service_only_keeps_io_and_compatibility_symbols(self) -> None:
        """迁移后的旧 Service 不能重新定义已迁移的领域算法。"""

        source = LEGACY_ANALYSIS_SERVICE_PATH.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(LEGACY_ANALYSIS_SERVICE_PATH))
        top_level_definitions = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        migrated_symbols = {
            "AnalysisContractError",
            "ArchitectureContractError",
            "DataStandardParentContractError",
            "build_effective_analysis_ranges",
            "validate_analysis_architecture_ranges",
            "build_file_callback_payload",
            "_parse_strict_json_object",
            "_decide_topk_deterministic_architecture_constraint",
        }
        self.assertFalse(
            migrated_symbols & top_level_definitions,
            "旧 Analysis Service 不得重新定义已迁移的 Domain 纯规则",
        )
        for required_import in (
            "app.modules.analysis.domain.classification_rules",
            "app.modules.analysis.domain.ranges",
            "app.modules.analysis.domain.result_mapping",
        ):
            with self.subTest(required_import=required_import):
                self.assertIn(required_import, source)
        self.assertIn("@wraps(_domain_map_analysis_result)", source)

    def test_domain_export_surfaces_are_explicit_literals(self) -> None:
        """兼容导出必须显式冻结，新增私有函数不能自动泄漏到旧 Service。"""

        modules_with_compatibility_exports = {
            "architecture_recall.py",
            "classification_rules.py",
            "models.py",
            "prompts.py",
            "result_mapping.py",
        }
        for file_name in sorted(modules_with_compatibility_exports):
            source_path = ANALYSIS_DOMAIN_ROOT / file_name
            tree = ast.parse(
                source_path.read_text(encoding="utf-8"),
                filename=str(source_path),
            )
            assignments = [
                node
                for node in tree.body
                if isinstance(node, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "__all__"
                    for target in node.targets
                )
            ]
            with self.subTest(source=file_name):
                self.assertEqual(1, len(assignments))
                self.assertIsInstance(assignments[0].value, ast.Tuple)
                self.assertTrue(
                    all(
                        isinstance(item, ast.Constant)
                        and isinstance(item.value, str)
                        for item in assignments[0].value.elts
                    )
                )
        self.assertNotIn(
            "_RelatedTechnologySanitization",
            result_mapping.__all__,
        )
        self.assertNotIn(
            "_sanitize_related_technologies_with_diagnostics",
            result_mapping.__all__,
        )

    def test_tree_and_recall_legacy_modules_are_same_implementation_modules(self) -> None:
        """模块别名保留私有测试替身、缓存身份和旧导入路径的一致性。"""

        self.assertIs(
            importlib.import_module("app.services.core.architecture_tree"),
            domain_tree,
        )
        self.assertIs(
            importlib.import_module(
                "app.services.llm_service.architecture_recall_service"
            ),
            domain_recall,
        )
        self.assertIs(legacy_tree, domain_tree)
        self.assertIs(legacy_recall, domain_recall)

    def test_legacy_exports_delegate_to_domain_implementations(self) -> None:
        """兼容层必须复用同一实现；仅日志适配允许保留薄包装。"""

        self.assertIs(
            analysis_service.build_effective_analysis_ranges,
            ranges.build_effective_analysis_ranges,
        )
        self.assertIs(
            analysis_service.validate_analysis_architecture_ranges,
            ranges.validate_analysis_architecture_ranges,
        )
        self.assertIs(
            analysis_service.map_analysis_result.__wrapped__,
            result_mapping.map_analysis_result,
        )
        self.assertIs(
            analysis_service.build_file_callback_payload,
            callback_payloads.build_file_callback_payload,
        )
        self.assertIs(
            analysis_service._decide_topk_deterministic_architecture_constraint,
            classification_rules._decide_topk_deterministic_architecture_constraint,
        )
        self.assertIs(
            legacy_prompts.build_file_extraction_prompt,
            prompts.build_file_extraction_prompt,
        )
        self.assertIs(
            legacy_config.ANALYSIS_CLASSIFICATION_MODES,
            models.ANALYSIS_CLASSIFICATION_MODES,
        )

    def test_effective_ranges_are_deeply_isolated_snapshots(self) -> None:
        """修改一次范围快照不能污染请求、全局默认值或后续任务。"""

        request_country = [
            {
                "label": "测试国家",
                "value": "测试国家",
                "metadata": {"aliases": ["测试别名"]},
            }
        ]
        request = {"country": request_country}
        first = ranges.build_effective_analysis_ranges(request)
        first["country"][0]["metadata"]["aliases"].append("污染标记")
        first["country"][0]["value"] = "污染值"

        second = ranges.build_effective_analysis_ranges(request)
        self.assertEqual("测试国家", request_country[0]["value"])
        self.assertEqual(["测试别名"], request_country[0]["metadata"]["aliases"])
        self.assertEqual("测试国家", second["country"][0]["value"])
        self.assertEqual(
            ["测试别名"],
            second["country"][0]["metadata"]["aliases"],
        )

        default_first = ranges.build_effective_analysis_ranges({})
        original_default = models.DEFAULT_COUNTRY_OPTIONS[0]["value"]
        default_first["country"][0]["value"] = "污染默认值"
        default_second = ranges.build_effective_analysis_ranges({})
        self.assertEqual(original_default, models.DEFAULT_COUNTRY_OPTIONS[0]["value"])
        self.assertEqual(original_default, default_second["country"][0]["value"])

    def test_callback_payload_is_a_deep_terminal_snapshot(self) -> None:
        """构造后的回调事实不能跟随映射结果的嵌套修改而变化。"""

        mapped_result = {
            "fileDataItem": {
                "summary": "构造时摘要",
                "keywords": ["甲", "乙"],
            }
        }
        payload = callback_payloads.build_file_callback_payload(
            "demo.pdf",
            mapped_result,
            "2",
        )
        mapped_result["fileDataItem"]["summary"] = "构造后修改"
        mapped_result["fileDataItem"]["keywords"].append("丙")

        self.assertEqual(
            "构造时摘要",
            payload["data"]["fileDataItem"]["summary"],
        )
        self.assertEqual(
            ["甲", "乙"],
            payload["data"]["fileDataItem"]["keywords"],
        )

    def test_prompt_normalization_preserves_legacy_type_error(self) -> None:
        """非字符串 Prompt 继续使用旧 Port 的明确 TypeError 契约。"""

        with self.assertRaisesRegex(TypeError, "prompt 必须是 str"):
            classification_rules._normalize_bounded_analysis_prompt(None)

    def test_related_technology_overflow_log_has_exact_reason(self) -> None:
        """证据充分的第 11 项只能记为数量截断，不能误报证据缺失。"""

        terms = [
            "技术甲",
            "技术乙",
            "技术丙",
            "技术丁",
            "技术戊",
            "技术己",
            "技术庚",
            "技术辛",
            "技术壬",
            "技术癸",
            "技术子",
        ]
        parsed_result = {
            "fileDataItem": {"relatedTechnology": ",".join(terms)}
        }
        with self.assertLogs(
            "app.services.llm_service.analysis_service",
            level="WARNING",
        ) as captured:
            mapped_result = analysis_service.map_analysis_result(
                parsed_result,
                {"fileName": "demo.txt"},
                original_text=" ".join(terms),
            )

        messages = "\n".join(captured.output)
        self.assertIn("所属技术数量超过上限", messages)
        self.assertNotIn("缺少可核验原文术语映射", messages)
        self.assertEqual(
            ", ".join(terms[:10]),
            mapped_result["fileDataItem"]["relatedTechnology"],
        )


if __name__ == "__main__":
    unittest.main()
