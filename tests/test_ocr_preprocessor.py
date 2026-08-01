from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.core.config import OCRConfig
from app.modules.document_processing.adapters.builtin_ocr import (
    mineru_pdf_to_markdown,
    prepare_analysis_file_for_upload,
    prepare_file_for_upload,
)
from tests import workspace_tempdir


def _ocr_config(tmp: str, *, engine: str = "mineru", enabled: bool = True) -> OCRConfig:
    return OCRConfig(
        enabled=enabled,
        languages="chi_sim+eng",
        dpi=300,
        sample_pages=3,
        text_threshold=50,
        cache_dir=str(Path(tmp) / "ocr-cache"),
        analysis_scanned_pdf_engine=engine,
        mineru_cache_dir=str(Path(tmp) / "mineru-cache"),
        mineru_lang="ch",
        mineru_api_url=None,
        tessdata_prefix=None,
    )


class OCRPreprocessorTests(unittest.TestCase):
    @patch("app.modules.document_processing.adapters.builtin_ocr.is_scanned_pdf", return_value=True)
    @patch("app.modules.document_processing.adapters.builtin_ocr.mineru_pdf_to_markdown")
    @patch("app.modules.document_processing.adapters.builtin_ocr.ocr_pdf_to_markdown")
    def test_analysis_scanned_pdf_uses_mineru_first(self, mock_ocr, mock_mineru, _mock_scanned):
        with workspace_tempdir() as tmp:
            pdf_path = Path(tmp) / "scan.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            mineru_md = Path(tmp) / "mineru.md"
            mineru_md.write_text("mineru text", encoding="utf-8")
            mock_mineru.return_value = mineru_md

            result = prepare_analysis_file_for_upload(str(pdf_path), _ocr_config(tmp))

        self.assertEqual(result, str(mineru_md))
        mock_mineru.assert_called_once()
        mock_ocr.assert_not_called()

    @patch("app.modules.document_processing.adapters.builtin_ocr.is_scanned_pdf", return_value=True)
    @patch("app.modules.document_processing.adapters.builtin_ocr.mineru_pdf_to_markdown", side_effect=RuntimeError("mineru failed"))
    @patch("app.modules.document_processing.adapters.builtin_ocr.ocr_pdf_to_markdown")
    def test_analysis_scanned_pdf_falls_back_to_builtin_ocr(self, mock_ocr, _mock_mineru, _mock_scanned):
        with workspace_tempdir() as tmp:
            pdf_path = Path(tmp) / "scan.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            ocr_md = Path(tmp) / "ocr.md"
            ocr_md.write_text("ocr text", encoding="utf-8")
            mock_ocr.return_value = ocr_md

            result = prepare_analysis_file_for_upload(str(pdf_path), _ocr_config(tmp))

        self.assertEqual(result, str(ocr_md))
        mock_ocr.assert_called_once()

    @patch("app.modules.document_processing.adapters.builtin_ocr.is_scanned_pdf", return_value=True)
    @patch("app.modules.document_processing.adapters.builtin_ocr.mineru_pdf_to_markdown", side_effect=RuntimeError("mineru failed"))
    @patch("app.modules.document_processing.adapters.builtin_ocr.ocr_pdf_to_markdown", side_effect=RuntimeError("ocr failed"))
    def test_analysis_scanned_pdf_falls_back_to_original_pdf(self, _mock_ocr, _mock_mineru, _mock_scanned):
        with workspace_tempdir() as tmp:
            pdf_path = Path(tmp) / "scan.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")

            result = prepare_analysis_file_for_upload(str(pdf_path), _ocr_config(tmp))

        self.assertEqual(result, str(pdf_path))

    @patch("app.modules.document_processing.adapters.builtin_ocr.is_scanned_pdf", return_value=True)
    @patch("app.modules.document_processing.adapters.builtin_ocr.mineru_pdf_to_markdown")
    @patch("app.modules.document_processing.adapters.builtin_ocr.ocr_pdf_to_markdown")
    def test_analysis_engine_ocr_skips_mineru(self, mock_ocr, mock_mineru, _mock_scanned):
        with workspace_tempdir() as tmp:
            pdf_path = Path(tmp) / "scan.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            ocr_md = Path(tmp) / "ocr.md"
            ocr_md.write_text("ocr text", encoding="utf-8")
            mock_ocr.return_value = ocr_md

            result = prepare_analysis_file_for_upload(str(pdf_path), _ocr_config(tmp, engine="ocr"))

        self.assertEqual(result, str(ocr_md))
        mock_mineru.assert_not_called()

    @patch("app.modules.document_processing.adapters.builtin_ocr.is_scanned_pdf", return_value=False)
    @patch("app.modules.document_processing.adapters.builtin_ocr.mineru_pdf_to_markdown")
    @patch("app.modules.document_processing.adapters.builtin_ocr.ocr_pdf_to_markdown")
    def test_non_scanned_pdf_keeps_original(self, mock_ocr, mock_mineru, _mock_scanned):
        with workspace_tempdir() as tmp:
            pdf_path = Path(tmp) / "text.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")

            result = prepare_analysis_file_for_upload(str(pdf_path), _ocr_config(tmp))

        self.assertEqual(result, str(pdf_path))
        mock_mineru.assert_not_called()
        mock_ocr.assert_not_called()

    @patch("app.modules.document_processing.adapters.builtin_ocr.is_scanned_pdf", return_value=True)
    @patch("app.modules.document_processing.adapters.builtin_ocr.mineru_pdf_to_markdown")
    @patch("app.modules.document_processing.adapters.builtin_ocr.ocr_pdf_to_markdown")
    def test_default_prepare_file_for_upload_keeps_builtin_ocr(self, mock_ocr, mock_mineru, _mock_scanned):
        with workspace_tempdir() as tmp:
            pdf_path = Path(tmp) / "scan.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            ocr_md = Path(tmp) / "ocr.md"
            ocr_md.write_text("ocr text", encoding="utf-8")
            mock_ocr.return_value = ocr_md

            result = prepare_file_for_upload(str(pdf_path), _ocr_config(tmp))

        self.assertEqual(result, str(ocr_md))
        mock_mineru.assert_not_called()

    @patch("app.modules.document_processing.adapters.mineru.MinerUConverter")
    def test_mineru_pdf_to_markdown_caches_markdown_with_metadata(self, MockConverter):
        with workspace_tempdir() as tmp:
            pdf_path = Path(tmp) / "scan.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            mineru_result = Path(tmp) / "result.md"
            mineru_result.write_text("parsed body", encoding="utf-8")
            MockConverter.return_value.convert_to_markdown.return_value = str(mineru_result)

            output = mineru_pdf_to_markdown(pdf_path, _ocr_config(tmp))
            text = output.read_text(encoding="utf-8")

        self.assertIn("# MinerU Markdown", text)
        self.assertIn("scan.pdf", text)
        self.assertIn("parsed body", text)
        MockConverter.return_value.convert_to_markdown.assert_called_once()


if __name__ == "__main__":
    unittest.main()
