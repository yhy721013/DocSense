from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError

from app.services.core.architecture_tree import build_architecture_tree_index
from app.services.llm_service.architecture_recall_service import (
    BM25_LIMIT,
    DATA_STANDARD_KINDS,
    DETAIL_KINDS,
    MAX_BODY_CHARS,
    MAX_FINAL_CANDIDATES,
    MAX_HEADINGS,
    MAX_IDENTIFIERS,
    MAX_REMARK_CHARS,
    RRF_K,
    ArchitecturePromptBudgetError,
    ArchitectureRecallError,
    ArchitectureRecallService,
    build_document_architecture_signals,
    identifier_aliases,
    recall_architecture_candidates,
)


def _full_feature_tree() -> list[dict[str, object]]:
    nodes: list[dict[str, object]] = [
        {"id": 1, "name": "装备目标", "pathName": "装备目标"},
        {"id": 2, "name": "水面装备", "parentId": 1},
        {"id": 10, "name": "CVN-78", "parentId": 2},
        {"id": 20, "name": "CVN-68", "parentId": 2},
        {"id": 100, "name": "数据标准", "pathName": "数据标准"},
    ]
    for offset, kind in enumerate(DETAIL_KINDS, start=1):
        nodes.append(
            {
                "id": 10 + offset,
                "name": f"CVN-78-{kind}",
                "parentId": 10,
                "remark": "福特级航母技术资料" if offset == 1 else "",
            }
        )
        nodes.append(
            {
                "id": 20 + offset,
                "name": f"CVN-68-{kind}",
                "parentId": 20,
            }
        )
    for offset, kind in enumerate(DATA_STANDARD_KINDS, start=1):
        nodes.append({"id": 100 + offset, "name": kind, "parentId": 100})
    return nodes


def _channel(decision, name: str) -> tuple[int, ...]:
    return next(
        ranking.node_ids
        for ranking in decision.channel_rankings
        if ranking.channel == name
    )


class DocumentArchitectureSignalsTests(unittest.TestCase):
    def test_bounds_all_variable_length_signals_and_extracts_identifiers(self):
        signals = build_document_architecture_signals(
            filename="Gerald R Ford CVN78.pdf",
            original_filename="原始 CVN-78.pdf",
            title="GJB 9001C-2017",
            headings=(f"标题{index}" for index in range(100)),
            identifiers=(f"ID-{index}" for index in range(200)),
            body="正文" * (MAX_BODY_CHARS + 10),
        )

        self.assertEqual(len(signals.headings), MAX_HEADINGS)
        self.assertEqual(len(signals.identifiers), MAX_IDENTIFIERS)
        self.assertEqual(len(signals.body_excerpt), MAX_BODY_CHARS)
        self.assertEqual(signals.headings[0], "标题0")

    def test_identifier_aliases_cover_compact_dash_and_space_variants(self):
        self.assertEqual(
            identifier_aliases("ＣＶＮ 78"),
            ("cvn78", "cvn-78", "cvn 78"),
        )
        aliases = identifier_aliases("GJB 9001C-2017")
        self.assertIn("gjb9001c2017", aliases)
        self.assertIn("gjb-9001c-2017", aliases)
        self.assertIn("gjb 9001c 2017", aliases)


class ArchitectureRecallChannelTests(unittest.TestCase):
    def setUp(self):
        self.index = build_architecture_tree_index(_full_feature_tree())

    def test_all_cvn78_variants_protect_the_same_seven_leaf_family(self):
        expected = set(range(11, 18))
        for variant in ("CVN78", "CVN-78", "CVN 78", "ＣＶＮ－７８"):
            with self.subTest(variant=variant):
                decision = recall_architecture_candidates(
                    self.index,
                    build_document_architecture_signals(filename=f"{variant}.pdf"),
                )
                self.assertEqual(set(decision.base_leaf_ids[:7]), expected)
                self.assertIn(10, decision.direct_exact_ids)
                self.assertTrue(expected.issubset(decision.final_candidate_ids))
                self.assertIn(10, decision.final_candidate_ids)
                parent = next(candidate for candidate in decision.candidates if candidate.id == 10)
                self.assertEqual(parent.protected_reasons, ("exact:10",))

    def test_same_name_exact_hits_keep_request_order(self):
        index = build_architecture_tree_index(
            [
                {"id": 1, "name": "根"},
                {"id": 2, "name": "共同型号", "parentId": 1},
                {"id": 3, "name": "共同型号", "parentId": 1},
            ]
        )
        decision = recall_architecture_candidates(
            index,
            build_document_architecture_signals(title="共同型号"),
        )

        self.assertEqual(decision.direct_exact_ids, (2, 3))
        self.assertEqual(decision.base_leaf_ids[:2], (2, 3))

    def test_bm25_prefers_specific_leaf_and_is_bounded(self):
        index = build_architecture_tree_index(
            [
                {"id": 1, "name": "装备根"},
                {"id": 2, "name": "雷达火控制导系统", "parentId": 1},
                {"id": 3, "name": "后勤保障系统", "parentId": 1},
                *(
                    {"id": 1000 + i, "name": f"雷达资料{i}", "parentId": 1}
                    for i in range(250)
                ),
            ]
        )
        decision = recall_architecture_candidates(
            index,
            build_document_architecture_signals(body="火控制导雷达技术指标"),
        )
        lexical = _channel(decision, "lexical")

        self.assertEqual(lexical[0], 2)
        self.assertLessEqual(len(lexical), BM25_LIMIT)

    def test_tree_route_is_bounded_and_records_direct_nodes(self):
        decision = recall_architecture_candidates(
            self.index,
            build_document_architecture_signals(title="CVN-78 航母"),
        )

        self.assertLessEqual(len(_channel(decision, "tree")), 100)
        self.assertIn(10, decision.direct_tree_ids)
        self.assertTrue(set(range(11, 18)) & set(_channel(decision, "tree")))

    def test_rrf_uses_declared_rule_weight_and_k(self):
        decision = recall_architecture_candidates(
            self.index,
            build_document_architecture_signals(filename="GJB 9001C-2017.pdf"),
        )
        scores = dict(decision.rrf_scores)

        self.assertAlmostEqual(scores[101], 0.8 / (RRF_K + 1))
        self.assertEqual(_channel(decision, "rule")[:6], tuple(range(101, 107)))

    def test_gjb_rule_completes_six_data_standard_leaves(self):
        decision = recall_architecture_candidates(
            self.index,
            build_document_architecture_signals(filename="GJB9001C.pdf"),
        )

        self.assertEqual(decision.base_leaf_ids[:6], tuple(range(101, 107)))
        self.assertTrue(set(range(101, 107)).issubset(decision.final_candidate_ids))
        self.assertNotIn(100, decision.final_candidate_ids)

    def test_equipment_rule_completes_all_seven_detail_categories(self):
        decision = recall_architecture_candidates(
            self.index,
            build_document_architecture_signals(title="CVN78 基础数据"),
        )

        self.assertTrue(set(range(11, 18)).issubset(decision.final_candidate_ids))

    def test_small_root_diversity_adds_one_leaf_from_each_direct_branch(self):
        index = build_architecture_tree_index(
            [
                {"id": 1, "name": "小根"},
                {"id": 2, "name": "雷达分支", "parentId": 1},
                {"id": 3, "name": "通信分支", "parentId": 1},
                {"id": 4, "name": "雷达目标叶", "parentId": 2},
                {"id": 5, "name": "雷达备用叶", "parentId": 2},
                {"id": 6, "name": "通信普通叶", "parentId": 3},
            ]
        )
        decision = recall_architecture_candidates(
            index,
            build_document_architecture_signals(title="雷达目标叶"),
        )

        self.assertIn(4, decision.final_candidate_ids)
        self.assertIn(6, decision.final_candidate_ids)


class ArchitectureRecallCandidateContractTests(unittest.TestCase):
    def test_protected_audit_only_contains_model_visible_candidates(self):
        index = build_architecture_tree_index(
            [
                {"id": 1, "name": "直接命中根"},
                {"id": 2, "name": "候选叶甲", "parentId": 1},
                {"id": 3, "name": "候选叶乙", "parentId": 1},
            ]
        )
        decision = recall_architecture_candidates(
            index,
            build_document_architecture_signals(title="直接命中根"),
        )
        protected_ids = {node_id for node_id, _ in decision.protected_reasons}

        self.assertNotIn(1, decision.final_candidate_ids)
        self.assertNotIn(1, protected_ids)
        self.assertTrue(protected_ids)
        self.assertTrue(protected_ids.issubset(set(decision.final_candidate_ids)))

    def test_prompt_budget_constructor_rejects_bool_and_non_integers(self):
        index = build_architecture_tree_index([{"id": 1, "name": "候选"}])
        for value in (True, False, 1.0, "32000", None, 0, -1):
            with self.subTest(prompt_char_limit=value):
                with self.assertRaises(ValueError):
                    ArchitectureRecallService(index, prompt_char_limit=value)  # type: ignore[arg-type]
        for value in (True, False, 1.0, "1024", None, -1):
            with self.subTest(prompt_overhead_chars=value):
                with self.assertRaises(ValueError):
                    ArchitectureRecallService(index, prompt_overhead_chars=value)  # type: ignore[arg-type]

        ArchitectureRecallService(index, prompt_char_limit=1, prompt_overhead_chars=0)

    def test_parent_eligibility_accepts_direct_or_two_top16_descendants(self):
        index = build_architecture_tree_index(
            [
                {"id": 1, "name": "根节点"},
                {"id": 2, "name": "分类容器", "parentId": 1},
                {"id": 3, "name": "共同信号甲", "parentId": 2},
                {"id": 4, "name": "共同信号乙", "parentId": 2},
                {"id": 5, "name": "后勤保障", "parentId": 1},
                {"id": 6, "name": "油料补给", "parentId": 5},
                {"id": 100, "name": "数据标准"},
                {"id": 101, "name": "通用要求", "parentId": 100},
            ]
        )
        decision = recall_architecture_candidates(
            index,
            build_document_architecture_signals(title="共同信号甲 共同信号乙"),
        )
        parent_ids = {
            candidate.id
            for candidate in decision.candidates
            if candidate.node_type == "parent"
        }

        self.assertIn(2, parent_ids)
        self.assertNotIn(1, parent_ids)
        self.assertNotIn(5, parent_ids)
        self.assertNotIn(100, parent_ids)

    def test_empty_and_unmatched_signals_fail_without_tree_fallback(self):
        index = build_architecture_tree_index([{"id": 1, "name": "唯一领域"}])
        with self.assertRaisesRegex(ArchitectureRecallError, "有效信号"):
            recall_architecture_candidates(
                index,
                build_document_architecture_signals(),
            )
        with self.assertRaisesRegex(ArchitectureRecallError, "未命中"):
            recall_architecture_candidates(
                index,
                build_document_architecture_signals(body="totally unrelated prose"),
            )

    def test_candidate_and_prompt_limits_are_enforced(self):
        nodes = [{"id": 1, "name": "根"}]
        nodes.extend(
            {
                "id": index + 2,
                "name": f"共同信号叶{index}",
                "parentId": 1,
            }
            for index in range(300)
        )
        index = build_architecture_tree_index(nodes)
        signals = build_document_architecture_signals(body="共同信号")
        decision = recall_architecture_candidates(index, signals)

        self.assertLessEqual(len(decision.candidates), MAX_FINAL_CANDIDATES)
        self.assertTrue(
            all(len(candidate.remark) <= MAX_REMARK_CHARS for candidate in decision.candidates)
        )

        capped_remark_index = build_architecture_tree_index(
            [{"id": 1, "name": "精确目标", "remark": "长备注" * 300}]
        )
        capped_remark_decision = recall_architecture_candidates(
            capped_remark_index,
            build_document_architecture_signals(title="精确目标"),
        )
        self.assertEqual(
            len(capped_remark_decision.candidates[0].remark),
            MAX_REMARK_CHARS,
        )

        long_remark_index = build_architecture_tree_index(
            [
                {"id": 1, "name": "根"},
                *(
                    {
                        "id": item + 2,
                        "name": f"共同信号叶{item}",
                        "parentId": 1,
                        "remark": "长备注" * 300,
                    }
                    for item in range(10)
                ),
            ]
        )
        with self.assertRaises(ArchitecturePromptBudgetError):
            ArchitectureRecallService(
                long_remark_index,
                prompt_char_limit=500,
                prompt_overhead_chars=0,
            ).recall(signals)

    def test_audit_is_body_free_and_deterministic_except_elapsed_time(self):
        index = build_architecture_tree_index(_full_feature_tree())
        unique_body = "不可写入审计的唯一正文 CVN-78"
        signals = build_document_architecture_signals(body=unique_body)
        first = recall_architecture_candidates(index, signals)
        second = recall_architecture_candidates(index, signals)

        self.assertEqual(first.final_candidate_ids, second.final_candidate_ids)
        self.assertEqual(first.channel_rankings, second.channel_rankings)
        self.assertEqual(first.rrf_scores, second.rrf_scores)
        self.assertNotIn(unique_body, json.dumps(first.to_audit_dict(), ensure_ascii=False))
        self.assertEqual(len(first.query_digest), 64)
        self.assertLessEqual(first.prompt_chars, 32_000)

    def test_dtos_are_frozen_and_projection_omits_empty_remark(self):
        index = build_architecture_tree_index([{"id": 1, "name": "目标叶"}])
        decision = recall_architecture_candidates(
            index,
            build_document_architecture_signals(title="目标叶"),
        )
        candidate = decision.candidates[0]

        self.assertNotIn("remark", candidate.to_prompt_dict())
        with self.assertRaises(FrozenInstanceError):
            candidate.rank = 99  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
