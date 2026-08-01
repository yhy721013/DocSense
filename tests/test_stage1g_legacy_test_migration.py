"""阶段 1G-4R 逐测试方法迁移清单与引用归零门禁。"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import unittest

from scripts.inspect_stage1g_references import inspect_stage1g_references


_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST_PATH = _ROOT / "tests/contracts/stage1g_legacy_test_migration.json"
_LEGACY_MODULE_CANDIDATES = {
    "analysis_legacy_executor",
    "analysis_legacy_recall",
    "report_legacy_executor",
    "weaponry_legacy_executor",
    "translation_legacy_service",
    "translator_legacy_package",
    "document_processing_legacy_facades",
    "debug_preview_legacy_facades",
    "anythingllm_legacy_wrapper",
}


def _test_method_ids(path: Path) -> set[str]:
    """读取目标测试方法，不 import 测试模块或构造生产基础设施。"""

    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    return {
        f"{class_node.name}.{method.name}"
        for class_node in tree.body
        if isinstance(class_node, ast.ClassDef)
        for method in class_node.body
        if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
        and method.name.startswith("test_")
    }


class Stage1GLegacyTestMigrationTests(unittest.TestCase):
    """禁止用文件级概述伪装逐方法迁移，也禁止重新执行待删实现。"""

    def setUp(self) -> None:
        self.manifest = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_every_source_method_has_one_auditable_disposition(self) -> None:
        self.assertEqual(2, self.manifest["schemaVersion"])
        self.assertEqual("1G-4R", self.manifest["stage"])
        policy = self.manifest["policy"]
        self.assertTrue(policy["migrationComplete"])
        self.assertTrue(policy["legacyExecutionReferencesRemoved"])
        self.assertFalse(policy["publicContractChanged"])
        self.assertEqual("test_method", policy["mappingUnit"])
        self.assertEqual(
            "1G-5E",
            policy["taskServiceCompatibilityMethodsDeferredTo"],
        )

        all_mappings: list[dict[str, object]] = []
        for migration in self.manifest["migrations"]:
            with self.subTest(source=migration["source"]):
                self.assertRegex(migration["sourceSnapshotSha256"], r"^[0-9a-f]{64}$")
                mappings = migration["methodMappings"]
                self.assertEqual(migration["sourceMethodCount"], len(mappings))
                self.assertGreater(len(mappings), 0)
                all_mappings.extend(mappings)

        self.assertEqual(200, self.manifest["sourceMethodCount"])
        self.assertEqual(200, len(all_mappings))
        source_tests = [item["sourceTest"] for item in all_mappings]
        self.assertEqual(len(source_tests), len(set(source_tests)))

        retired_count = 0
        target_cache: dict[Path, set[str]] = {}
        for mapping in all_mappings:
            with self.subTest(source_test=mapping["sourceTest"]):
                self.assertTrue(mapping["semanticReason"])
                disposition = mapping["disposition"]
                targets = mapping["targetTests"]
                if disposition == "retired_unstable_implementation_detail":
                    retired_count += 1
                    self.assertEqual([], targets)
                    continue
                self.assertEqual("migrated", disposition)
                self.assertTrue(targets)
                for target in targets:
                    relative_path, separator, method_id = target.partition("::")
                    self.assertEqual("::", separator)
                    target_path = _ROOT / relative_path
                    self.assertTrue(target_path.is_file(), target)
                    method_ids = target_cache.setdefault(
                        target_path,
                        _test_method_ids(target_path),
                    )
                    self.assertIn(method_id, method_ids, target)

        # 仅三条 Argos 私有启发式被明确退休；不得把未映射测试批量归入“实现细节”。
        self.assertEqual(3, retired_count)

    def test_cutover_and_removed_source_files_match_manifest(self) -> None:
        policy = self.manifest["policy"]
        for source in policy["sourceFilesCutOverInPlace"]:
            with self.subTest(cutover=source):
                self.assertTrue((_ROOT / source).is_file())
        for source in policy["sourceFilesRemovedAfterMigration"]:
            with self.subTest(removed=source):
                self.assertFalse((_ROOT / source).exists())

    def test_legacy_modules_have_no_test_execution_references(self) -> None:
        report = inspect_stage1g_references(_ROOT)
        self.assertTrue(report["inventoryComplete"])
        candidates = {item["candidateId"]: item for item in report["candidates"]}
        for candidate_id in sorted(_LEGACY_MODULE_CANDIDATES):
            with self.subTest(candidate_id=candidate_id):
                self.assertEqual(
                    0,
                    candidates[candidate_id]["counts"].get("test_execution", 0),
                )


if __name__ == "__main__":
    unittest.main()
