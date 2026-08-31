"""旧路径式 OCR/MinerU 入口删除后的永久结构门禁。"""

from __future__ import annotations

from dataclasses import fields
import inspect
import unittest

from app.modules.document_processing.adapters import builtin_ocr
from app.modules.document_processing.adapters.builtin_ocr import (
    BuiltinOCRDocumentProcessorAdapter,
    build_builtin_ocr_profile,
)
from app.services.core.config import LLMIntegrationConfig, OCRConfig


class OCRPreprocessorRemovalTests(unittest.TestCase):
    """禁止旧缓存路径、自由函数或 Config 字段重新进入当前实现。"""

    def test_removed_path_functions_are_not_available(self) -> None:
        removed_names = {
            "build_mineru_cache_key",
            "build_ocr_cache_key",
            "mineru_pdf_to_markdown",
            "ocr_pdf_to_markdown",
            "prepare_analysis_file_for_upload",
            "prepare_file_for_upload",
        }

        self.assertTrue(removed_names.isdisjoint(vars(builtin_ocr)))
        self.assertTrue(removed_names.isdisjoint(set(builtin_ocr.__all__)))

    def test_core_config_contains_no_removed_directory_fields(self) -> None:
        self.assertNotIn("download_dir", {item.name for item in fields(LLMIntegrationConfig)})
        self.assertTrue(
            {"cache_dir", "mineru_cache_dir"}.isdisjoint(
                {item.name for item in fields(OCRConfig)}
            )
        )

    def test_current_builtin_ocr_adapter_owns_materialization_root(self) -> None:
        parameters = inspect.signature(
            BuiltinOCRDocumentProcessorAdapter
        ).parameters
        self.assertEqual(
            {"source_store", "materialization_root", "renderer"},
            set(parameters),
        )
        profile = build_builtin_ocr_profile(languages="chi_sim+eng", dpi=300)
        self.assertEqual(
            {"dpi", "languages", "sourceSuffix"},
            set(profile.to_dict()["parameters"]),
        )


if __name__ == "__main__":
    unittest.main()
