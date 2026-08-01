from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import patch

from app.modules.analysis.domain.architecture_tree import build_architecture_tree_index
from app.modules.analysis.domain import architecture_recall as recall_module
from app.modules.analysis.domain.architecture_recall import (
    BM25_LIMIT,
    DATA_STANDARD_KINDS,
    DETAIL_KINDS,
    MAX_BODY_CHARS,
    MAX_FINAL_CANDIDATES,
    MAX_HEADINGS,
    MAX_IDENTIFIERS,
    MAX_PARENT_CANDIDATES,
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

    def test_generic_cjk_parent_does_not_starve_specific_bm25_leaf(self):
        """通用中文父节点命中不能用受保护后代挤出具体 BM25/树候选。"""
        nodes: list[dict[str, object]] = [
            {"id": 1, "name": "领域根"},
            {"id": 2, "name": "海军", "parentId": 1},
            {"id": 3, "name": "雷达装备", "parentId": 1},
            {
                "id": 4,
                "name": "宙斯盾雷达系统",
                "parentId": 3,
                "remark": "有源相控阵火控技术指标",
            },
        ]
        nodes.extend(
            {
                "id": 1_000 + index,
                "name": f"通用资料{index}",
                "parentId": 2,
            }
            for index in range(100)
        )
        index = build_architecture_tree_index(nodes)

        decision = recall_architecture_candidates(
            index,
            build_document_architecture_signals(
                body="海军有源相控阵火控技术指标"
            ),
        )
        generic_leaf_ids = set(range(1_000, 1_100))

        self.assertIn(2, decision.direct_exact_ids)
        self.assertEqual(_channel(decision, "lexical")[0], 4)
        self.assertIn(4, decision.base_leaf_ids)
        self.assertFalse(
            generic_leaf_ids & set(dict(decision.protected_reasons))
        )

    def test_direct_leaf_precedes_generic_parent_descendants(self):
        """即使普通父节点在请求顺序更早，直接叶命中仍必须先进入 exact 通道。"""
        nodes: list[dict[str, object]] = [
            {"id": 1, "name": "领域根"},
            {"id": 2, "name": "海军", "parentId": 1},
            {"id": 3, "name": "精确目标", "parentId": 1},
        ]
        nodes.extend(
            {
                "id": 2_000 + index,
                "name": f"海军资料{index}",
                "parentId": 2,
            }
            for index in range(BM25_LIMIT)
        )
        decision = recall_architecture_candidates(
            build_architecture_tree_index(nodes),
            build_document_architecture_signals(title="海军 精确目标"),
        )

        self.assertEqual(_channel(decision, "exact")[0], 3)
        self.assertEqual(decision.base_leaf_ids[0], 3)
        self.assertEqual(
            dict(decision.protected_reasons)[3],
            ("exact:3",),
        )

    def test_tree_route_is_bounded_and_records_direct_nodes(self):
        decision = recall_architecture_candidates(
            self.index,
            build_document_architecture_signals(title="CVN-78 航母"),
        )

        self.assertLessEqual(len(_channel(decision, "tree")), 100)
        self.assertIn(10, decision.direct_tree_ids)
        self.assertTrue(set(range(11, 18)) & set(_channel(decision, "tree")))

    def test_tree_route_handles_the_maximum_legal_depth_iteratively(self):
        nodes = [
            {
                "id": depth,
                "name": (
                    "深层目标雷达"
                    if depth == 128
                    else f"层级{depth}"
                ),
                "parentId": depth - 1 if depth > 1 else None,
            }
            for depth in range(1, 129)
        ]
        index = build_architecture_tree_index(nodes)

        decision = recall_architecture_candidates(
            index,
            build_document_architecture_signals(body="深层目标雷达"),
        )

        self.assertIn(128, decision.base_leaf_ids)

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

    def test_data_standard_scope_filters_body_generic_exact_and_enriches_candidates(self):
        index = build_architecture_tree_index(
            [
                *_full_feature_tree(),
                {"id": 200, "name": "基地目标"},
                {"id": 201, "name": "海军", "parentId": 200},
                {"id": 202, "name": "海军基地甲", "parentId": 201},
                {"id": 203, "name": "海军基地乙", "parentId": 201},
            ]
        )
        scope_ids = tuple(range(101, 107))
        decision = recall_architecture_candidates(
            index,
            build_document_architecture_signals(
                filename="GJB 9001C-2017.pdf",
                title="质量管理体系要求",
                body=(
                    "本标准起草单位包括海军装备研究院和海军驻地代表局。"
                    "正文规定质量管理体系要求。"
                ),
            ),
            candidate_scope_ids=scope_ids,
            candidate_scope_reason="data-standard-scope",
            candidate_remark_overrides={
                105: "质量管理及其他综合性标准要求。",
            },
        )

        self.assertEqual(set(decision.final_candidate_ids), set(scope_ids))
        self.assertNotIn(201, decision.direct_exact_ids)
        self.assertNotIn(202, decision.final_candidate_ids)
        self.assertEqual(_channel(decision, "scope"), scope_ids)
        general = next(item for item in decision.candidates if item.id == 105)
        self.assertEqual(general.remark, "质量管理及其他综合性标准要求。")
        self.assertIn("data-standard-scope", general.protected_reasons)

    def test_candidate_scope_rejects_parent_nodes(self):
        with self.assertRaisesRegex(
            ArchitectureRecallError,
            "只能包含叶子节点",
        ):
            recall_architecture_candidates(
                self.index,
                build_document_architecture_signals(
                    filename="GJB 9001C-2017.pdf"
                ),
                candidate_scope_ids=(100,),
            )

    def test_equipment_rule_completes_all_seven_detail_categories(self):
        decision = recall_architecture_candidates(
            self.index,
            build_document_architecture_signals(title="CVN78 基础数据"),
        )

        self.assertTrue(set(range(11, 18)).issubset(decision.final_candidate_ids))

    def test_scope_guard_keeps_body_identifier_lexical_but_not_strongly_protected(self):
        signals = build_document_architecture_signals(
            body="Fleetlist\nCVN-78 Gerald R. Ford\n其他同级舰艇",
        )
        with patch.object(
            recall_module,
            "_tree_rank",
            wraps=recall_module._tree_rank,
        ) as tree_rank:
            decision = recall_architecture_candidates(
                self.index,
                signals,
                strong_evidence_only=True,
            )

        self.assertEqual(decision.direct_exact_ids, ())
        self.assertEqual(_channel(decision, "rule"), ())
        self.assertEqual(decision.protected_reasons, ())
        self.assertEqual(tree_rank.call_args.args[2], set())
        self.assertTrue(set(range(11, 18)) & set(_channel(decision, "lexical")))

    def test_scope_guard_keeps_body_component_lexical_without_exact_protection(self):
        index = build_architecture_tree_index(
            [
                {"id": 1, "name": "装备目标"},
                {"id": 2, "name": "航空母舰", "parentId": 1},
                {"id": 3, "name": "CVN-71", "parentId": 2},
                {"id": 4, "name": "雷达", "parentId": 1},
                {"id": 5, "name": "AN/SPS-48E", "parentId": 4},
            ]
        )
        signals = build_document_architecture_signals(
            original_filename="CVN-71 基本情况.mhtml",
            title="USS Theodore Roosevelt (CVN-71)",
            body="舰载设备包括 AN/SPS-48E 三坐标雷达。",
        )

        decision = recall_architecture_candidates(
            index,
            signals,
            strong_evidence_only=True,
        )

        self.assertIn(3, decision.direct_exact_ids)
        self.assertNotIn(5, decision.direct_exact_ids)
        self.assertNotIn(5, dict(decision.protected_reasons))
        self.assertIn(5, _channel(decision, "lexical"))

    def test_scope_guard_conflict_disables_both_identity_exact_matches(self):
        signals = build_document_architecture_signals(
            filename="technical-upload.pdf",
            original_filename="CVN-78 class.pdf",
            title="CVN-68 class",
            body="Fleetlist CVN-78 CVN-68",
        )
        with patch.object(
            recall_module,
            "_tree_rank",
            wraps=recall_module._tree_rank,
        ) as tree_rank:
            decision = recall_architecture_candidates(
                self.index,
                signals,
                strong_evidence_only=True,
                strong_identity_enabled=False,
            )

        self.assertEqual(decision.direct_exact_ids, ())
        self.assertEqual(decision.protected_reasons, ())
        self.assertEqual(_channel(decision, "rule"), ())
        self.assertEqual(tree_rank.call_args.args[2], set())
        self.assertTrue(
            _channel(decision, "lexical") or _channel(decision, "tree")
        )

    def test_scope_guard_prefers_original_filename_over_technical_filename(self):
        index = build_architecture_tree_index(
            [
                {"id": 1, "name": "装备根"},
                {"id": 2, "name": "DDG-1000", "parentId": 1},
                {"id": 3, "name": "CVN-78", "parentId": 1},
            ]
        )
        signals = build_document_architecture_signals(
            filename="technical-DDG-1000.pdf",
            original_filename="source-CVN-78.pdf",
        )
        decision = recall_architecture_candidates(
            index,
            signals,
            strong_evidence_only=True,
        )

        self.assertEqual(signals.strong_identifiers, ("cvn-78",))
        self.assertIn(3, decision.direct_exact_ids)
        self.assertNotIn(2, decision.direct_exact_ids)
        self.assertIn(2, _channel(decision, "lexical"))

    def test_scope_guard_protects_unique_uuv_type_from_trusted_title_and_body(self):
        index = build_architecture_tree_index(
            [
                {"id": 1, "name": "装备目标"},
                {"id": 2, "name": "水下装备", "parentId": 1},
                {"id": 3, "name": "无人潜航器", "parentId": 2},
                {"id": 4, "name": "常规潜艇", "parentId": 2},
            ]
        )

        for title, description in (
            (
                "Echo Voyager/Orca XLUUV",
                "Type: Extra-large unmanned underwater vehicle (XLUUV).",
            ),
            (
                "Future UUV",
                "Type: uncrewed undersea vehicle.",
            ),
        ):
            with self.subTest(title=title):
                decision = recall_architecture_candidates(
                    index,
                    build_document_architecture_signals(
                        title=title,
                        body=description,
                    ),
                    strong_evidence_only=True,
                    strong_identity_enabled=False,
                )

                self.assertEqual(_channel(decision, "rule"), (3,))
                self.assertIn(3, decision.final_candidate_ids)
                self.assertEqual(
                    dict(decision.protected_reasons)[3],
                    ("jane_title_type_alias",),
                )
                candidate = next(
                    candidate
                    for candidate in decision.candidates
                    if candidate.id == 3
                )
                self.assertEqual(
                    candidate.protected_reasons,
                    ("jane_title_type_alias",),
                )
                self.assertLessEqual(
                    len(decision.candidates),
                    MAX_FINAL_CANDIDATES,
                )
                self.assertLessEqual(decision.prompt_chars, 32_000)

    def test_uuv_type_alias_requires_scope_guard_title_body_and_unique_leaf(self):
        base_nodes = [
            {"id": 1, "name": "装备目标"},
            {"id": 2, "name": "水下装备", "parentId": 1},
            {"id": 3, "name": "无人潜航器", "parentId": 2},
            {"id": 4, "name": "payload", "parentId": 2},
        ]
        cases = (
            {
                "name": "legacy",
                "nodes": base_nodes,
                "title": "Orca XLUUV",
                "body": "unmanned underwater vehicle payload",
                "strong_evidence_only": False,
            },
            {
                "name": "title_only",
                "nodes": base_nodes,
                "title": "Orca XLUUV",
                "body": "payload",
                "strong_evidence_only": True,
            },
            {
                "name": "body_only",
                "nodes": base_nodes,
                "title": "Orca",
                "body": "unmanned underwater vehicle payload",
                "strong_evidence_only": True,
            },
            {
                "name": "duplicate_canonical_leaf",
                "nodes": [
                    *base_nodes,
                    {"id": 5, "name": "无人潜航器", "parentId": 2},
                ],
                "title": "Orca XLUUV",
                "body": "unmanned underwater vehicle payload",
                "strong_evidence_only": True,
            },
        )

        for case in cases:
            with self.subTest(case=case["name"]):
                decision = recall_architecture_candidates(
                    build_architecture_tree_index(case["nodes"]),
                    build_document_architecture_signals(
                        title=case["title"],
                        body=case["body"],
                    ),
                    strong_evidence_only=case["strong_evidence_only"],
                    strong_identity_enabled=False,
                )

                self.assertNotIn(3, _channel(decision, "rule"))
                self.assertNotIn(3, dict(decision.protected_reasons))
                if case["name"] == "duplicate_canonical_leaf":
                    self.assertNotIn(5, _channel(decision, "rule"))
                    self.assertNotIn(5, dict(decision.protected_reasons))

    def test_non_jane_scope_guard_does_not_enable_jane_title_body_rule(self):
        index = build_architecture_tree_index(
            [
                {"id": 1, "name": "装备目标"},
                {"id": 2, "name": "水下装备", "parentId": 1},
                {"id": 3, "name": "无人潜航器", "parentId": 2},
                {"id": 4, "name": "payload", "parentId": 2},
            ]
        )
        decision = recall_architecture_candidates(
            index,
            build_document_architecture_signals(
                title="Orca XLUUV",
                body="unmanned underwater vehicle payload",
            ),
            strong_evidence_only=True,
            jane_title_type_alias_enabled=False,
        )

        self.assertNotIn(3, _channel(decision, "rule"))
        self.assertNotIn(3, dict(decision.protected_reasons))
        self.assertTrue(_channel(decision, "lexical"))

    def test_legacy_mode_keeps_body_identifier_exact_and_family_expansion(self):
        signals = build_document_architecture_signals(
            body="Fleetlist\nCVN-78 Gerald R. Ford",
        )
        implicit_legacy = recall_architecture_candidates(self.index, signals)
        explicit_legacy = recall_architecture_candidates(
            self.index,
            signals,
            strong_evidence_only=False,
        )

        self.assertEqual(
            implicit_legacy.direct_exact_ids,
            explicit_legacy.direct_exact_ids,
        )
        self.assertEqual(
            implicit_legacy.final_candidate_ids,
            explicit_legacy.final_candidate_ids,
        )
        self.assertIn(10, explicit_legacy.direct_exact_ids)
        self.assertEqual(
            set(_channel(explicit_legacy, "rule")),
            set(range(11, 18)),
        )
        protected = dict(explicit_legacy.protected_reasons)
        self.assertEqual(protected[10], ("exact:10",))
        self.assertTrue(set(range(11, 18)).issubset(protected))

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
            build_document_architecture_signals(
                title="直接命中根 候选叶甲"
            ),
        )
        protected_ids = {node_id for node_id, _ in decision.protected_reasons}

        self.assertNotIn(1, decision.final_candidate_ids)
        self.assertNotIn(1, protected_ids)
        self.assertTrue(protected_ids)
        self.assertIn(2, protected_ids)
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

    def test_finite_tree_boundary_parent_is_eligible(self):
        """外部父节点未随有限树传入时，边界非叶节点仍是合法父候选。"""
        index = build_architecture_tree_index(
            [
                {"id": 10, "name": "有限边界分类", "parentId": 999},
                {"id": 11, "name": "边界候选甲", "parentId": 10},
                {"id": 12, "name": "边界候选乙", "parentId": 10},
            ]
        )
        decision = recall_architecture_candidates(
            index,
            build_document_architecture_signals(title="有限边界分类"),
        )

        parent_ids = {
            candidate.id
            for candidate in decision.candidates
            if candidate.node_type == "parent"
        }
        self.assertEqual(index.require(10).depth, 1)
        self.assertEqual(index.require(10).parent_id, 999)
        self.assertIn(10, parent_ids)

    def test_preferred_parent_bypasses_depth_gate_and_takes_first_parent_slot(self):
        nodes: list[dict[str, object]] = [
            {"id": 1, "name": "根"},
            {"id": 2, "name": "空中装备", "parentId": 1},
            {"id": 3, "name": "普通容器", "parentId": 1},
            {"id": 20, "name": "空中普通叶", "parentId": 2},
        ]
        for offset in range(20):
            parent_id = 100 + offset * 10
            nodes.extend(
                [
                    {"id": parent_id, "name": "共同父", "parentId": 3},
                    {
                        "id": parent_id + 1,
                        "name": f"候选甲{offset}",
                        "parentId": parent_id,
                    },
                    {
                        "id": parent_id + 2,
                        "name": f"候选乙{offset}",
                        "parentId": parent_id,
                    },
                ]
            )
        index = build_architecture_tree_index(nodes)
        signals = build_document_architecture_signals(title="共同父")

        without_preference = recall_architecture_candidates(
            index,
            signals,
            strong_evidence_only=True,
        )
        self.assertNotIn(2, without_preference.final_candidate_ids)

        decision = recall_architecture_candidates(
            index,
            signals,
            strong_evidence_only=True,
            preferred_parent_reasons={2: ("jane_high_level_branch",)},
        )
        parent_candidates = [
            candidate
            for candidate in decision.candidates
            if candidate.node_type == "parent"
        ]

        self.assertEqual(len(parent_candidates), MAX_PARENT_CANDIDATES)
        self.assertEqual(parent_candidates[0].id, 2)
        self.assertEqual(
            parent_candidates[0].protected_reasons,
            ("jane_high_level_branch",),
        )
        self.assertEqual(
            dict(decision.protected_reasons)[2],
            ("jane_high_level_branch",),
        )
        self.assertLessEqual(len(decision.candidates), MAX_FINAL_CANDIDATES)

    def test_preferred_equipment_parent_adds_all_seven_detail_leaves(self):
        index = build_architecture_tree_index(_full_feature_tree())
        decision = recall_architecture_candidates(
            index,
            build_document_architecture_signals(body="unmatched prose token"),
            strong_evidence_only=True,
            preferred_parent_reasons={10: ("jane_branch_guard",)},
        )

        self.assertTrue(set(range(11, 18)).issubset(decision.final_candidate_ids))
        self.assertIn(10, decision.final_candidate_ids)
        self.assertEqual(
            dict(decision.protected_reasons)[10],
            ("jane_branch_guard",),
        )

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

    def test_runtime_candidate_limits_shape_recall_instead_of_only_rejecting_it(self):
        """冻结的小阈值必须参与召回裁剪，不能先按默认 128 生成后再整批拒绝。"""

        nodes = [{"id": 1, "name": "共同信号根"}]
        nodes.extend(
            {
                "id": item + 2,
                "name": f"共同信号叶{item}",
                "parentId": 1,
            }
            for item in range(30)
        )
        index = build_architecture_tree_index(nodes)
        decision = recall_architecture_candidates(
            index,
            build_document_architecture_signals(body="共同信号"),
            base_leaf_limit=6,
            parent_candidate_limit=2,
            model_candidate_limit=8,
        )

        self.assertLessEqual(len(decision.base_leaf_ids), 6)
        self.assertLessEqual(
            len([item for item in decision.candidates if item.node_type == "parent"]),
            2,
        )
        self.assertLessEqual(len(decision.candidates), 8)

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
