"""阶段 1H 的永久依赖方向与迁移期消费者清单门禁。"""

from __future__ import annotations

import ast
import json
import unittest
from pathlib import Path


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_MODULE_ROOT = (
    _REPOSITORY_ROOT / "app" / "modules" / "document_processing"
)
_TRANSLATION_ROOT = (
    _REPOSITORY_ROOT / "app" / "modules" / "translation"
)
_BASELINE_PATH = (
    Path(__file__).resolve().parent
    / "assets"
    / "document_processing"
    / "stage1h_baseline.json"
)
_FORBIDDEN_DOCUMENT_PROCESSING_PREFIXES = (
    "flask",
    "app.blueprints",
    "app.services",
    "app.modules.analysis",
    "app.modules.report",
    "app.modules.weaponry",
    "app.modules.chat",
    "app.modules.translation",
)
_FORBIDDEN_INNER_LAYER_PREFIXES = (
    "flask",
    "sqlite3",
    "subprocess",
    "app.services",
    "app.modules.document_processing.adapters",
    "app.modules.document_processing.libreoffice",
)
_FORBIDDEN_TRANSLATION_FORMAT_PREFIXES = (
    "app.modules.document_processing.adapters",
    "app.modules.document_processing.libreoffice",
    "app.modules.document_processing.ooxml_validator",
    "app.services",
)
_FORBIDDEN_LEGACY_CONVERSION_IMPORTS = {
    "app.services.translator.MinerUConverter",
    "app.services.translator.mhtml2pdf",
}


def _absolute_import(source: Path, node: ast.ImportFrom) -> str:
    """将相对导入解析成绝对模块名，避免简单文本搜索误判注释。"""

    module_parts = node.module.split(".") if node.module else []
    if node.level == 0:
        return ".".join(module_parts)
    relative_source = source.relative_to(_REPOSITORY_ROOT)
    package_parts = list(relative_source.with_suffix("").parts[:-1])
    parent_count = node.level - 1
    if parent_count > len(package_parts):
        return ""
    if parent_count:
        package_parts = package_parts[:-parent_count]
    return ".".join(package_parts + module_parts)


def _imports(source: Path) -> set[str]:
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            target = _absolute_import(source, node)
            if target:
                imported.add(target)
    return imported


class Stage1HDocumentProcessingArchitectureTests(unittest.TestCase):
    """确保共享处理模块保持向内依赖，并冻结正式迁移前的消费者集合。"""

    def test_document_processing_never_imports_business_or_translation_layers(
        self,
    ) -> None:
        violations: list[str] = []
        for source in _MODULE_ROOT.rglob("*.py"):
            if "__pycache__" in source.parts:
                continue
            for imported in _imports(source):
                if imported.startswith(
                    _FORBIDDEN_DOCUMENT_PROCESSING_PREFIXES
                ):
                    violations.append(
                        f"{source.relative_to(_REPOSITORY_ROOT).as_posix()}"
                        f" -> {imported}"
                    )
        self.assertEqual([], sorted(violations))

    def test_current_production_consumers_match_stage1h_inventory(self) -> None:
        """任何新增消费者都必须经过对应迁移波次，而不是绕过计划直接接线。"""

        baseline = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
        expected = set(baseline["currentDocumentProcessingConsumers"])
        actual: set[str] = set()
        app_root = _REPOSITORY_ROOT / "app"
        for source in app_root.rglob("*.py"):
            if (
                "__pycache__" in source.parts
                or _MODULE_ROOT in source.parents
            ):
                continue
            if any(
                imported == "app.modules.document_processing"
                or imported.startswith("app.modules.document_processing.")
                for imported in _imports(source)
            ):
                actual.add(source.relative_to(_REPOSITORY_ROOT).as_posix())

        self.assertEqual(expected, actual)

    def test_domain_and_application_do_not_import_infrastructure(self) -> None:
        """内核不得因本地 SQLite/文件实现而反向依赖 Adapter。"""

        violations: list[str] = []
        for relative_root in ("domain", "application"):
            for source in (_MODULE_ROOT / relative_root).rglob("*.py"):
                for imported in _imports(source):
                    if imported.startswith(_FORBIDDEN_INNER_LAYER_PREFIXES):
                        violations.append(
                            f"{source.relative_to(_REPOSITORY_ROOT).as_posix()}"
                            f" -> {imported}"
                        )
        self.assertEqual([], sorted(violations))

    def test_translation_never_imports_document_format_adapters(self) -> None:
        """Translation 只能消费 Artifact 合同，不能重新取得格式转换能力。"""

        violations: list[str] = []
        for source in _TRANSLATION_ROOT.rglob("*.py"):
            for imported in _imports(source):
                if imported.startswith(_FORBIDDEN_TRANSLATION_FORMAT_PREFIXES):
                    violations.append(
                        f"{source.relative_to(_REPOSITORY_ROOT).as_posix()}"
                        f" -> {imported}"
                    )
        self.assertEqual([], sorted(violations))

    def test_business_application_never_imports_document_adapters(self) -> None:
        """业务 Application 只依赖 Port/DTO，真实路径解析必须留在 Adapter。"""

        violations: list[str] = []
        modules_root = _REPOSITORY_ROOT / "app" / "modules"
        for application_root in modules_root.glob("*/application"):
            if application_root.parent == _MODULE_ROOT:
                continue
            for source in application_root.rglob("*.py"):
                for imported in _imports(source):
                    if (
                        imported.startswith(
                            "app.modules.document_processing.adapters"
                        )
                        or imported
                        == "app.modules.document_processing.adapters.path_compat"
                    ):
                        violations.append(
                            f"{source.relative_to(_REPOSITORY_ROOT).as_posix()}"
                            f" -> {imported}"
                        )
        self.assertEqual([], sorted(violations))

    def test_production_code_never_imports_legacy_converter_paths(self) -> None:
        """旧模块可作为兼容入口存在，但生产代码不得再从旧路径取得转换器。"""

        violations: list[str] = []
        for source in (_REPOSITORY_ROOT / "app").rglob("*.py"):
            for imported in _imports(source):
                if imported in _FORBIDDEN_LEGACY_CONVERSION_IMPORTS:
                    violations.append(
                        f"{source.relative_to(_REPOSITORY_ROOT).as_posix()}"
                        f" -> {imported}"
                    )
        self.assertEqual([], sorted(violations))

    def test_legacy_format_compatibility_surface_cannot_expand(self) -> None:
        """冻结旧目录到新格式 Adapter 的桥接文件，新增桥必须先经过迁移审查。"""

        baseline = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
        expected = set(baseline["legacyFormatCompatibilityFiles"])
        actual: set[str] = set()
        for relative_root in (
            Path("app/services/translator"),
            Path("app/services/utils"),
        ):
            for source in (_REPOSITORY_ROOT / relative_root).rglob("*.py"):
                if any(
                    imported.startswith(
                        "app.modules.document_processing.adapters"
                    )
                    for imported in _imports(source)
                ):
                    actual.add(
                        source.relative_to(_REPOSITORY_ROOT).as_posix()
                    )
        self.assertEqual(expected, actual)

    def test_processing_record_adapter_never_imports_legacy_task_service(
        self,
    ) -> None:
        source = (
            _MODULE_ROOT / "adapters" / "sqlite_records.py"
        )
        self.assertNotIn(
            "app.services.llm_service.task_service",
            _imports(source),
        )

    def test_legacy_office_facades_do_not_duplicate_conversion_implementation(
        self,
    ) -> None:
        """旧路径只能转发，唯一进程/转换/校验实现必须位于 Adapter 包。"""

        libreoffice_facade = _MODULE_ROOT / "libreoffice.py"
        validator_facade = _MODULE_ROOT / "ooxml_validator.py"
        forbidden_facade_imports = {"subprocess", "shutil", "tempfile"}
        self.assertTrue(
            forbidden_facade_imports.isdisjoint(_imports(libreoffice_facade))
        )
        facade_tree = ast.parse(
            libreoffice_facade.read_text(encoding="utf-8")
        )
        validator_tree = ast.parse(
            validator_facade.read_text(encoding="utf-8")
        )
        self.assertFalse(
            any(
                isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                for node in ast.walk(facade_tree)
            )
        )
        self.assertFalse(
            any(
                isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                for node in ast.walk(validator_tree)
            )
        )

    def test_mhtml_legacy_facades_never_import_translator_conversion(self) -> None:
        sources = (
            _REPOSITORY_ROOT / "app" / "services" / "utils" / "mhtml_normalizer.py",
            _REPOSITORY_ROOT / "app" / "services" / "translator" / "mhtml_handler.py",
        )
        violations = [
            f"{source.relative_to(_REPOSITORY_ROOT).as_posix()} -> {imported}"
            for source in sources
            for imported in _imports(source)
            if imported == "app.services.translator.mhtml2pdf"
        ]
        self.assertEqual([], violations)

    def test_mineru_and_ocr_legacy_files_are_thin_facades(self) -> None:
        """旧路径只允许兼容导出，禁止重新长出供应商或 OCR 实现。"""

        facades = (
            _REPOSITORY_ROOT
            / "app"
            / "services"
            / "translator"
            / "MinerUConverter.py",
            _REPOSITORY_ROOT
            / "app"
            / "services"
            / "utils"
            / "ocr_preprocessor.py",
        )
        forbidden_imports = {
            "asyncio",
            "fitz",
            "httpx",
            "mineru.cli.api_client",
            "subprocess",
            "tempfile",
        }
        for facade in facades:
            with self.subTest(facade=facade.name):
                self.assertTrue(forbidden_imports.isdisjoint(_imports(facade)))
                tree = ast.parse(facade.read_text(encoding="utf-8"))
                self.assertFalse(
                    any(
                        isinstance(
                            node,
                            (
                                ast.ClassDef,
                                ast.FunctionDef,
                                ast.AsyncFunctionDef,
                            ),
                        )
                        for node in ast.walk(tree)
                    )
                )


if __name__ == "__main__":
    unittest.main()
