"""阶段 1G-6 关闭资产的长期一致性门禁。"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OWNERSHIP_MATRIX = (
    PROJECT_ROOT
    / "docs"
    / "重构记录"
    / "阶段0资产"
    / "260801-阶段1关闭模块所有权与遗留适配矩阵.md"
)

# 这里只扫描静态 SQL 声明。运行期动态表名不能在没有明确所有权的情况下
# 悄悄加入生产代码；如将来确有需要，应先把它改为可审计的固定 Schema。
CREATE_TABLE_PATTERN = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?!IF\b)([a-zA-Z_][a-zA-Z0-9_]*)",
    re.IGNORECASE,
)


class Stage1GCloseoutAssetTests(unittest.TestCase):
    """保证关闭文档不会在后续改造中与现行代码静默漂移。"""

    def setUp(self) -> None:
        """每个用例读取一次 UTF-8 资产，避免依赖平台默认编码。"""

        self.matrix_text = OWNERSHIP_MATRIX.read_text(encoding="utf-8")

    def test_ownership_matrix_covers_every_static_database_table(self) -> None:
        """生产代码中每个固定表名都必须出现在所有权矩阵。"""

        production_roots = (
            PROJECT_ROOT / "app" / "modules",
            PROJECT_ROOT / "app" / "services",
        )
        discovered_tables: set[str] = set()
        for production_root in production_roots:
            for source_path in production_root.rglob("*.py"):
                source = source_path.read_text(encoding="utf-8")
                discovered_tables.update(CREATE_TABLE_PATTERN.findall(source))

        self.assertTrue(discovered_tables, "未发现任何静态数据库表，扫描规则可能失效")
        missing_tables = sorted(
            table_name
            for table_name in discovered_tables
            if f"`{table_name}`" not in self.matrix_text
        )
        self.assertEqual(
            [],
            missing_tables,
            f"以下数据库表尚未声明所有权：{missing_tables}",
        )

    def test_matrix_names_all_current_stage_one_module_boundaries(self) -> None:
        """阶段 1 已建立的业务模块必须有目录、说明和矩阵归属。"""

        module_names = (
            "analysis",
            "chat",
            "debug",
            "document_processing",
            "reassign",
            "report",
            "tasks",
            "translation",
            "weaponry",
        )
        for module_name in module_names:
            with self.subTest(module=module_name):
                module_root = PROJECT_ROOT / "app" / "modules" / module_name
                self.assertTrue(module_root.is_dir())
                self.assertTrue((module_root / "README.md").is_file())
                self.assertIn(f"`app/modules/{module_name}`", self.matrix_text)

    def test_deleted_legacy_runtime_paths_cannot_reappear(self) -> None:
        """已通过 1G-5 门禁的旧运行入口不得在后续阶段被顺手恢复。"""

        deleted_paths = (
            "app/services/llm_service/analysis_service.py",
            "app/services/llm_service/architecture_recall_service.py",
            "app/services/llm_service/report_service.py",
            "app/services/llm_service/translation_service.py",
            "app/services/llm_service/weaponry_service.py",
            "app/services/translator",
            "app/services/utils/anythingllm_client.py",
            "app/services/utils/callback_preview.py",
            "app/services/utils/chat_debug_preview.py",
            "app/services/utils/mhtml_normalizer.py",
            "app/services/utils/ocr_preprocessor.py",
            "app/services/utils/rag_pipeline.py",
        )
        for relative_path in deleted_paths:
            with self.subTest(path=relative_path):
                candidate_path = PROJECT_ROOT / relative_path
                if candidate_path.is_dir():
                    # compileall 可能为已删除源码保留被 Git 忽略的 __pycache__。
                    # 门禁关心的是可执行源码/资产是否回流，而不是解释器缓存目录本身。
                    active_files = [
                        path
                        for path in candidate_path.rglob("*")
                        if path.is_file() and "__pycache__" not in path.parts
                    ]
                    self.assertEqual([], active_files)
                else:
                    self.assertFalse(candidate_path.exists())


if __name__ == "__main__":
    unittest.main()
