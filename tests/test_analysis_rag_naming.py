"""文件分析 RAG 命名策略的纯领域离线测试。"""

from __future__ import annotations

import unittest

from app.modules.analysis.domain.errors import AnalysisContractError
from app.modules.analysis.domain.rag_naming import (
    AnalysisRagNamingSnapshot,
    RAG_NAMING_SOURCE_FILE_NAME_FALLBACK,
    RAG_NAMING_SOURCE_ORIGINAL_FILE_NAME,
    RAG_REPRESENTATION_MARKDOWN,
    RAG_REPRESENTATION_PDF,
    RagNameValidationError,
    derive_rag_transport_file_name,
    select_rag_business_name,
    validate_rag_transport_name_candidate,
)


class AnalysisRagNamingPolicyTests(unittest.TestCase):
    """锁定原值保留、回退、跨平台合法性与表示名派生规则。"""

    def test_selects_original_value_without_rewriting_and_falls_back_together(self) -> None:
        selected = select_rag_business_name(
            " Nimitz (CVN 68) class.pdf",
            "business-hash.pdf",
        )
        self.assertEqual(" Nimitz (CVN 68) class.pdf", selected.value)
        self.assertEqual(RAG_NAMING_SOURCE_ORIGINAL_FILE_NAME, selected.source)

        for original in (None, "", "   "):
            with self.subTest(original=original):
                fallback = select_rag_business_name(original, "business-hash.pdf")
                self.assertEqual("business-hash.pdf", fallback.value)
                self.assertEqual(
                    RAG_NAMING_SOURCE_FILE_NAME_FALLBACK,
                    fallback.source,
                )

    def test_derives_names_by_replacing_only_the_last_suffix(self) -> None:
        cases = (
            ("资料.v2.pdf", "资料.v2.md", "资料.v2.pdf"),
            ("无后缀", "无后缀.md", "无后缀.pdf"),
            ("archive.tar.gz", "archive.tar.md", "archive.tar.pdf"),
        )
        for candidate, expected_markdown, expected_pdf in cases:
            with self.subTest(candidate=candidate):
                self.assertEqual(
                    expected_markdown,
                    derive_rag_transport_file_name(
                        candidate,
                        RAG_REPRESENTATION_MARKDOWN,
                    ),
                )
                self.assertEqual(
                    expected_pdf,
                    derive_rag_transport_file_name(
                        candidate,
                        RAG_REPRESENTATION_PDF,
                    ),
                )

    def test_rejects_every_frozen_illegal_name_category(self) -> None:
        cases = (
            ("non-string", ["name.pdf"], "candidate_not_string"),
            ("slash", "folder/name.pdf", "forbidden_character"),
            ("backslash", r"folder\name.pdf", "forbidden_character"),
            ("windows-character", "report?.pdf", "forbidden_character"),
            ("ascii-control", "report\x01.pdf", "control_character"),
            ("crlf", "report\r\nforged.pdf", "control_character"),
            ("invalid-unicode", "\ud800.pdf", "invalid_unicode"),
            ("relative-dot", ".", "relative_path_name"),
            ("relative-dot-dot", "..", "relative_path_name"),
            ("empty-stem", ".pdf", "empty_stem"),
            ("trailing-space", "report.pdf ", "trailing_space_or_dot"),
            ("trailing-dot", "report.", "trailing_space_or_dot"),
            ("reserved", "CON.txt", "windows_reserved_name"),
            ("reserved-extension", "lpt1.report.pdf", "windows_reserved_name"),
            (
                "utf8-too-long",
                f"{'a' * 252}.x",
                "derived_name_too_long",
            ),
        )
        for case_id, candidate, reason in cases:
            with self.subTest(case=case_id):
                with self.assertRaises(RagNameValidationError) as captured:
                    validate_rag_transport_name_candidate(candidate)
                self.assertEqual(reason, captured.exception.reason_code)

    def test_accepts_exact_255_byte_longest_derived_name(self) -> None:
        candidate = f"{'a' * 251}.x"
        snapshot = AnalysisRagNamingSnapshot.from_public_names(
            original_file_name="展示标题.pdf",
            file_name=candidate,
        )
        self.assertEqual(
            254,
            len(snapshot.markdown_transport_file_name.encode("utf-8")),
        )
        self.assertEqual(
            255,
            len(snapshot.pdf_transport_file_name.encode("utf-8")),
        )

    def test_business_keys_make_same_stem_and_same_title_uploads_unique(self) -> None:
        composed = AnalysisRagNamingSnapshot.from_public_names(
            original_file_name="é.pdf",
            file_name="one.pdf",
        )
        decomposed = AnalysisRagNamingSnapshot.from_public_names(
            original_file_name="e\u0301.pdf",
            file_name="two.pdf",
        )
        self.assertNotEqual(composed.display_title, decomposed.display_title)
        self.assertNotEqual(composed.candidate_sha256, decomposed.candidate_sha256)
        self.assertNotEqual(
            composed.markdown_transport_file_name,
            decomposed.markdown_transport_file_name,
        )

        pdf = AnalysisRagNamingSnapshot.from_public_names(
            original_file_name="资料.pdf",
            file_name="one.pdf",
        )
        docx = AnalysisRagNamingSnapshot.from_public_names(
            original_file_name="资料.docx",
            file_name="two.docx",
        )
        self.assertNotEqual(
            pdf.markdown_transport_file_name,
            docx.markdown_transport_file_name,
        )
        self.assertNotEqual(pdf.display_title, docx.display_title)

        first_same_title = AnalysisRagNamingSnapshot.from_public_names(
            original_file_name="完全同名.pdf",
            file_name="global-key-a.pdf",
        )
        second_same_title = AnalysisRagNamingSnapshot.from_public_names(
            original_file_name="完全同名.pdf",
            file_name="global-key-b.pdf",
        )
        self.assertEqual(
            first_same_title.display_title,
            second_same_title.display_title,
        )
        self.assertNotEqual(
            first_same_title.markdown_transport_file_name,
            second_same_title.markdown_transport_file_name,
        )

    def test_snapshot_mapping_rejects_tampered_derived_name(self) -> None:
        snapshot = AnalysisRagNamingSnapshot.from_public_names(
            original_file_name="资料.pdf",
            file_name="business.pdf",
        )
        payload = snapshot.to_dict()
        payload["markdown_transport_file_name"] = "shadow.md"
        with self.assertRaises(AnalysisContractError):
            AnalysisRagNamingSnapshot.from_mapping(payload)


if __name__ == "__main__":
    unittest.main()
