"""阶段 1D-3B：Schema v2 Query、Chunk 质量与 score/rank Selection 测试。"""

from __future__ import annotations

import unittest

from app.modules.weaponry.domain import (
    EVIDENCE_SCORE_MODE_RANK,
    EVIDENCE_SCORE_MODE_SCORE,
    EvidenceCandidate,
    EvidenceSelectionPolicy,
    RetrievalColumn,
    RetrievalField,
    WeaponryRetrievalValidationError,
    assess_chunk_quality,
    build_retrieval_query,
    normalize_evidence_text,
    select_evidence,
)


FINGERPRINT = "lancedb:native:multilingual-e5-small:test"
EMBEDDING_FINGERPRINT = "multilingual-e5-small:test"
PROFILE_ID = "weaponry-production-score-rank-v2"


def _candidate(
    candidate_id: str,
    *,
    document_key: str = "doc-a",
    text: str | None = None,
    provider_rank: object = 1,
    provider_score: object = 0.8,
    provider_score_present: bool = True,
    score_profile_id: str = PROFILE_ID,
) -> EvidenceCandidate:
    return EvidenceCandidate(
        candidate_id=candidate_id,
        document_key=document_key,
        text=(
            text
            if text is not None
            else f"这是长度足够且具备完整正文结构的武器装备候选证据：{candidate_id}。"
        ),
        provider_rank=provider_rank,
        provider_score=provider_score,
        provider_score_present=provider_score_present,
        score_profile_id=score_profile_id,
    )


def _profile(**overrides: object) -> EvidenceSelectionPolicy:
    values: dict[str, object] = {
        "profile_id": PROFILE_ID,
        "provider_fingerprint": FINGERPRINT,
        "embedding_fingerprint": EMBEDDING_FINGERPRINT,
        "document_processing_fingerprint": "legacy-normalized-artifact-v1:test",
    }
    values.update(overrides)
    return EvidenceSelectionPolicy(**values)  # type: ignore[arg-type]


class WeaponryRetrievalQueryTests(unittest.TestCase):
    def test_input_query_separates_retrieval_semantics_from_extraction_prompt(self) -> None:
        query = build_retrieval_query(
            RetrievalField(
                field_name="舰级名称",
                field_description="提取装备所属舰级的正式名称",
                expanded_terms=("ship class",),
            )
        )

        self.assertEqual(
            "字段：舰级名称\n"
            "语义说明：提取装备所属舰级的正式名称\n"
            "检索同义词：ship class",
            query.text,
        )
        self.assertIn("舰级", query.semantic_terms)
        self.assertIn("ship class", query.semantic_terms)
        self.assertNotIn("ship", query.semantic_terms)
        for extraction_instruction in ("只需回答", "未找到", "格式示例", "不要推测"):
            self.assertNotIn(extraction_instruction, query.text)

    def test_table_query_contains_table_and_column_semantics(self) -> None:
        query = build_retrieval_query(
            RetrievalField(
                field_name="雷达配置",
                field_description="按设备逐行提取",
                field_type="TABLE",
                columns=(
                    RetrievalColumn("雷达型号", "正式型号"),
                    RetrievalColumn("用途", "搜索、跟踪或火控"),
                ),
            )
        )
        self.assertIn("列：雷达型号（正式型号）；用途（搜索、跟踪或火控）", query.text)
        self.assertIn("雷达", query.semantic_terms)

    def test_single_character_and_symbol_field_names_have_no_hidden_gate(self) -> None:
        single_character = build_retrieval_query(
            RetrievalField(field_name="长", field_description="装备全长")
        )
        normalized_empty = build_retrieval_query(
            RetrievalField(field_name="\u200b", field_description="甲方定义的占位字段")
        )

        self.assertIn("长", single_character.semantic_terms)
        self.assertEqual((), normalized_empty.semantic_terms)
        self.assertIn("语义说明：甲方定义的占位字段", normalized_empty.text)

    def test_query_length_is_not_restricted_by_an_internal_character_limit(self) -> None:
        description = "完整字段语义" * 1000
        query = build_retrieval_query(
            RetrievalField(field_name="舰级", field_description=description)
        )

        self.assertGreater(len(query.text), 4096)
        self.assertIn(description, query.text)

    def test_invalid_field_shape_is_rejected_inside_domain(self) -> None:
        with self.assertRaisesRegex(WeaponryRetrievalValidationError, "TABLE 必须"):
            RetrievalField(field_name="雷达", field_type="TABLE")


class WeaponryChunkQualityTests(unittest.TestCase):
    def test_normalizer_removes_only_prefix_metadata_and_repairs_cjk_spacing(self) -> None:
        raw = (
            "<document_metadata>\nsourceDocument: demo.pdf\n</document_metadata>\n"
            "尼 米 茲 號 航 空 母 艦是正文。"
        )
        self.assertEqual("尼米茲號航空母艦是正文。", normalize_evidence_text(raw))

    def test_reference_dense_chunk_is_reported_without_exposing_content(self) -> None:
        quality = assess_chunk_quality(
            "<document_metadata>sourceDocument: demo.pdf</document_metadata>\n"
            "USNI News 2019 2020 2021 2022 2023 Archived "
            "https://one.example https://two.example 参考文献条目"
        )
        self.assertTrue(quality.reference_like)
        self.assertIn("reference-like-content", quality.rejection_reasons)

    def test_normal_body_chunk_is_not_mistaken_for_reference_list(self) -> None:
        quality = assess_chunk_quality(
            "尼米茲號航空母艦隸屬美國海軍，本文介紹其部署與艦載航空兵活動。"
        )
        self.assertFalse(quality.reference_like)


class WeaponryEvidenceSelectionV2Tests(unittest.TestCase):
    def _query(self):
        return build_retrieval_query(
            RetrievalField(field_name="舰级名称", field_description="提取所属舰级")
        )

    def _select(self, candidates, *, score_mode=EVIDENCE_SCORE_MODE_SCORE):
        return select_evidence(
            candidates,
            score_mode=score_mode,
            query=self._query(),
            profile=_profile(),
            provider_fingerprint=FINGERPRINT,
            embedding_fingerprint=EMBEDDING_FINGERPRINT,
            expected_document_keys=("doc-a", "doc-b"),
        )

    def test_score_mode_uses_score_desc_then_stable_tiebreakers(self) -> None:
        candidates = (
            _candidate("lower", provider_rank=1, provider_score=0.7),
            _candidate("higher-b", document_key="doc-b", provider_rank=3, provider_score=0.9),
            _candidate("higher-a", provider_rank=2, provider_score=0.9),
        )
        first = self._select(candidates)
        second = self._select(tuple(reversed(candidates)))
        expected = ("higher-a", "higher-b", "lower")
        self.assertEqual(expected, tuple(item.candidate_id for item in first.selected))
        self.assertEqual(expected, tuple(item.candidate_id for item in second.selected))
        self.assertTrue(all(item.score_mode == "score" for item in first.selected))

    def test_rank_only_mode_accepts_explicitly_missing_scores(self) -> None:
        result = self._select(
            (
                _candidate(
                    "rank-two",
                    provider_rank=2,
                    provider_score=None,
                    provider_score_present=False,
                ),
                _candidate(
                    "rank-one",
                    provider_rank=1,
                    provider_score=None,
                    provider_score_present=False,
                ),
            ),
            score_mode=EVIDENCE_SCORE_MODE_RANK,
        )
        self.assertEqual(
            ("rank-one", "rank-two"),
            tuple(item.candidate_id for item in result.selected),
        )
        self.assertTrue(all(item.provider_score is None for item in result.selected))

    def test_invalid_or_mixed_score_rejects_whole_batch(self) -> None:
        invalid = self._select(
            (
                _candidate("valid", provider_rank=1, provider_score=0.8),
                _candidate("invalid", provider_rank=2, provider_score="bad"),
            )
        )
        self.assertEqual((), invalid.selected)
        self.assertEqual(
            {"invalid-provider-score"},
            {item.reason for item in invalid.rejected},
        )

        mixed = self._select(
            (
                _candidate("scored", provider_rank=1),
                _candidate(
                    "unscored",
                    provider_rank=2,
                    provider_score=None,
                    provider_score_present=False,
                ),
            )
        )
        self.assertEqual((), mixed.selected)
        self.assertEqual(
            {"mixed-provider-score-mode"},
            {item.reason for item in mixed.rejected},
        )

    def test_invalid_or_duplicate_rank_rejects_whole_batch(self) -> None:
        invalid = self._select((_candidate("a", provider_rank=True),))
        self.assertEqual("invalid-provider-rank", invalid.rejected[0].reason)
        duplicate = self._select(
            (_candidate("a", provider_rank=1), _candidate("b", provider_rank=1))
        )
        self.assertEqual(
            {"duplicate-provider-rank"},
            {item.reason for item in duplicate.rejected},
        )

    def test_no_absolute_relevance_or_anchor_gate_is_applied(self) -> None:
        result = self._select(
            (
                _candidate(
                    "low-score-unrelated-text",
                    provider_score=0.0001,
                    text="农业灌溉面积与粮食产量统计，正文结构完整但没有字段关键词。",
                ),
            )
        )
        self.assertEqual(("low-score-unrelated-text",), tuple(
            item.candidate_id for item in result.selected
        ))

    def test_source_quality_and_profile_gates_remain(self) -> None:
        result = self._select(
            (
                _candidate("foreign", document_key="doc-x", provider_rank=1),
                _candidate(
                    "old-profile",
                    provider_rank=2,
                    score_profile_id="other-profile",
                ),
                _candidate("short", provider_rank=3, text="过短"),
            )
        )
        reasons = {item.candidate_id: item.reason for item in result.rejected}
        self.assertEqual("unexpected-document", reasons["foreign"])
        self.assertEqual("score-profile-mismatch", reasons["old-profile"])
        self.assertEqual("content-empty-or-too-short", reasons["short"])

    def test_exact_dedup_is_within_document_and_keeps_full_text(self) -> None:
        full_text = "完整 Evidence " + "A" * 80
        result = self._select(
            (
                _candidate("winner", provider_rank=1, provider_score=0.9, text=full_text),
                _candidate("duplicate", provider_rank=2, provider_score=0.8, text=full_text),
                _candidate(
                    "other-document",
                    document_key="doc-b",
                    provider_rank=3,
                    provider_score=0.7,
                    text=full_text,
                ),
            )
        )
        self.assertEqual(
            ("winner", "other-document"),
            tuple(item.candidate_id for item in result.selected),
        )
        self.assertEqual(full_text, result.selected[0].text)
        self.assertIn("duplicate-in-document", {item.reason for item in result.rejected})

    def test_fingerprint_mismatch_stops_selection(self) -> None:
        with self.assertRaisesRegex(WeaponryRetrievalValidationError, "provider fingerprint"):
            select_evidence(
                (),
                score_mode=EVIDENCE_SCORE_MODE_SCORE,
                query=self._query(),
                profile=_profile(),
                provider_fingerprint="different",
                embedding_fingerprint=EMBEDDING_FINGERPRINT,
                expected_document_keys=("doc-a",),
            )


if __name__ == "__main__":
    unittest.main()
