from __future__ import annotations

import unittest

from app.modules.analysis.domain import classification_rules as analysis_service
from app.modules.analysis.domain.architecture_tree import build_architecture_tree_index


DETAIL_KINDS = (
    "基础数据",
    "战技指标",
    "运用数据",
    "效能数据",
    "模型数据",
    "目特数据",
    "声像数据",
)


def _jane_text(
    title: str,
    *body_lines: str,
    metadata_label: str = "Date Posted",
) -> str:
    return "\n".join(
        (
            "© 2025 Jane’s Group UK Limited",
            "Page 1 of 8",
            title,
            f"{metadata_label}: 17 July 2026",
            *body_lines,
        )
    )


def _scope_tree() -> tuple[list[dict[str, object]], dict[int, tuple[int, ...]]]:
    nodes: list[dict[str, object]] = [
        {"id": 1, "name": "装备型号"},
        {"id": 2, "name": "水面装备", "parentId": 1},
        {"id": 3, "name": "水下装备", "parentId": 1},
        {"id": 4, "name": "探测装备", "parentId": 1},
        {"id": 28, "name": "空中装备", "parentId": 1},
        {"id": 242, "name": "美国级", "parentId": 2},
        {"id": 126, "name": "ⅡA型", "parentId": 2},
        {"id": 174, "name": "Ⅲ型", "parentId": 2},
        {"id": 458, "name": "第一批次", "parentId": 3},
        {"id": 468, "name": "第二批次", "parentId": 3},
        {"id": 100, "name": "数据标准"},
        {"id": 101, "name": "通用要求", "parentId": 100},
    ]
    entity_specs = (
        (240, "LHA-6", 242),
        (241, "LHA-7", 242),
        (119, "DDG-51", 126),
        (120, "DDG-53", 126),
        (121, "DDG-54", 126),
        (170, "DDG-125", 174),
        (171, "DDG-126", 174),
        (459, "SSN-774", 458),
        (460, "SSN-775", 458),
        (469, "SSN-778", 468),
        (470, "SSN-779", 468),
        (47, "P-8A", 28),
        (91, "AN-SPY-6", 4),
    )
    detail_ids_by_entity: dict[int, tuple[int, ...]] = {}
    next_detail_id = 10_000
    for entity_id, name, parent_id in entity_specs:
        nodes.append({"id": entity_id, "name": name, "parentId": parent_id})
        detail_ids: list[int] = []
        for kind in DETAIL_KINDS:
            nodes.append(
                {
                    "id": next_detail_id,
                    "name": f"{name}-{kind}",
                    "parentId": entity_id,
                }
            )
            detail_ids.append(next_detail_id)
            next_detail_id += 1
        detail_ids_by_entity[entity_id] = tuple(detail_ids)
    return nodes, detail_ids_by_entity


class JaneClassificationProfileTests(unittest.TestCase):
    def test_extracts_title_after_page_marker_instead_of_copyright(self) -> None:
        active, title = analysis_service._extract_jane_title(
            _jane_text(
                "America class (LHA-6)",
                "Fleetlist",
                "LHA-6",
                metadata_label="Publication",
            )
        )

        self.assertTrue(active)
        self.assertEqual(title, "America class (LHA-6)")
        self.assertNotIn("Jane", title)
        self.assertNotIn("©", title)

    def test_requires_copyright_page_one_and_metadata_marker(self) -> None:
        valid = _jane_text("America class (LHA-6)")
        invalid_documents = (
            valid.replace("© 2025 Jane’s Group UK Limited\n", ""),
            valid.replace("Page 1 of 8\n", ""),
            valid.replace("Date Posted: 17 July 2026", ""),
            valid.replace("Page 1 of 8", "Page 2 of 8"),
        )

        for document in invalid_documents:
            with self.subTest(document=document):
                self.assertEqual(
                    analysis_service._extract_jane_title(document),
                    (False, ""),
                )

    def test_original_filename_has_priority_and_technical_name_is_fallback_only(
        self,
    ) -> None:
        text = _jane_text("America class (LHA-6)")
        original_profile = analysis_service._build_jane_classification_profile(
            file_name="9f23ab44f18c.pdf",
            original_name="America class LHA-6.pdf",
            original_text=text,
        )
        fallback_profile = analysis_service._build_jane_classification_profile(
            file_name="America class LHA-6.pdf",
            original_name="",
            original_text=text,
        )
        hash_original_profile = analysis_service._build_jane_classification_profile(
            file_name="America class LHA-6.pdf",
            original_name="upload-9f23ab44.pdf",
            original_text=text,
        )

        self.assertEqual(
            original_profile.identity_filename,
            "America class LHA-6.pdf",
        )
        self.assertTrue(original_profile.identity_confirmed)
        self.assertEqual(fallback_profile.identity_filename, "America class LHA-6.pdf")
        self.assertTrue(fallback_profile.identity_confirmed)
        self.assertEqual(
            hash_original_profile.identity_filename,
            "upload-9f23ab44.pdf",
        )
        self.assertEqual(hash_original_profile.filename_identity_kind, "opaque")
        self.assertEqual(hash_original_profile.trusted_filename_identifiers, ())
        self.assertEqual(hash_original_profile.primary_identifier, "lha6")
        self.assertTrue(hash_original_profile.recall_identity_enabled)
        self.assertFalse(hash_original_profile.identity_confirmed)
        self.assertFalse(hash_original_profile.identity_conflict)

    def test_catalog_and_opaque_names_use_jane_title_for_recall_only(self) -> None:
        cases = (
            ("JFS_3526-JFS_-16-Aug-2023.pdf", "catalog"),
            ("JAEM1026-JC4IA-25-May-2023.pdf", "catalog"),
            ("JAWA1185-JAWA-09-Jul-2024.pdf", "catalog"),
            ("JUMV0235-JUMV-07-Jun-2024.pdf", "catalog"),
            ("upload-9f23ab44.pdf", "opaque"),
            ("9f23ab44f18c.pdf", "opaque"),
        )
        for original_name, expected_kind in cases:
            with self.subTest(original_name=original_name):
                profile = analysis_service._build_jane_classification_profile(
                    file_name="technical-upload.pdf",
                    original_name=original_name,
                    original_text=_jane_text("America class (LHA-6)"),
                )

                self.assertEqual(
                    profile.filename_identity_kind,
                    expected_kind,
                )
                self.assertEqual(profile.trusted_filename_identifiers, ())
                self.assertEqual(profile.title_identifiers, ("lha6",))
                self.assertEqual(profile.primary_identifier, "lha6")
                self.assertTrue(profile.recall_identity_enabled)
                self.assertFalse(profile.identity_confirmed)
                self.assertFalse(profile.identity_conflict)

    def test_scope_guard_removes_technical_names_from_recall_signals(self) -> None:
        catalog_name = "JFS_3526-JFS_-16-Aug-2023.pdf"
        profile = analysis_service._build_jane_classification_profile(
            file_name="equipment-models-20260719-01.pdf",
            original_name=catalog_name,
            original_text=_jane_text("Nimitz (CVN 68) class (CVNM)"),
        )
        file_signal, original_signal = (
            analysis_service._jane_recall_filename_signals(
                file_name="equipment-models-20260719-01.pdf",
                original_name=catalog_name,
                profile=profile,
                scope_guard_active=True,
            )
        )
        signals = analysis_service._build_analysis_architecture_signals(
            file_name=file_signal,
            original_name=original_signal,
            original_text=_jane_text("Nimitz (CVN 68) class (CVNM)"),
            title_override=profile.title,
        )

        self.assertEqual((file_signal, original_signal), ("", ""))
        self.assertEqual(signals.strong_identifiers, ("cvn 68",))
        self.assertNotIn("jfs", signals.query_text.casefold())
        self.assertNotIn("equipment-models", signals.query_text.casefold())

    def test_short_specifications_document_has_dominant_detail_hint(self) -> None:
        short_specifications = _jane_text(
            "Northrop Grumman E-2D Advanced Hawkeye",
            "UPDATED",
            "Specifications",
            "Aircraft totals",
            "Operational speed: 300 kt",
        ).replace("Page 1 of 8", "Page 1 of 2")
        long_contents_document = _jane_text(
            "Boeing P-8A Poseidon",
            "UPDATED",
            "Contents",
            "Country and classification",
            "Specifications",
        ).replace("Page 1 of 8", "Page 1 of 24")

        e2d_profile = analysis_service._build_jane_classification_profile(
            file_name="technical-upload.pdf",
            original_name="E-2D.pdf",
            original_text=short_specifications,
        )
        p8_profile = analysis_service._build_jane_classification_profile(
            file_name="technical-upload.pdf",
            original_name="P-8A.pdf",
            original_text=long_contents_document,
        )

        self.assertEqual(
            e2d_profile.dominant_detail_kind,
            "technical_specifications",
        )
        self.assertEqual(p8_profile.dominant_detail_kind, "")

    def test_single_model_prompt_context_does_not_expose_branch_parent(self) -> None:
        text = _jane_text(
            "Northrop Grumman E-2D Advanced Hawkeye",
            "Specifications",
        ).replace("Page 1 of 8", "Page 1 of 2")
        profile = analysis_service._build_jane_classification_profile(
            file_name="technical-upload.pdf",
            original_name="E-2D.pdf",
            original_text=text,
        )
        context = analysis_service._jane_classification_prompt_context(
            profile,
            analysis_service._ArchitectureScopeResolution(
                matched_scope_parent_id=44,
                matched_branch_parent_id=44,
                reason_code="jane_branch_guard",
            ),
        )

        self.assertNotIn("matchedScopeParentId", context)
        self.assertEqual(
            context["dominantDetailKind"],
            "technical_specifications",
        )

    def test_filename_and_title_identifier_conflict_disables_identity(self) -> None:
        profile = analysis_service._build_jane_classification_profile(
            file_name="technical-upload.pdf",
            original_name="DDG-51 Flight III.pdf",
            original_text=_jane_text("America class (LHA-6)"),
        )

        self.assertTrue(profile.active)
        self.assertEqual(profile.filename_identity_kind, "descriptive")
        self.assertTrue(profile.identity_conflict)
        self.assertFalse(profile.recall_identity_enabled)
        self.assertFalse(profile.identity_confirmed)
        self.assertEqual(profile.primary_identifier, "")

    def test_flight_qualifier_takes_precedence_over_class_scope(self) -> None:
        for qualifier in ("Flight IIA", "Flight III"):
            with self.subTest(qualifier=qualifier):
                profile = analysis_service._build_jane_classification_profile(
                    file_name="technical-upload.pdf",
                    original_name=f"DDG-51 {qualifier}.pdf",
                    original_text=_jane_text(
                        f"DDG-51 {qualifier} class",
                    ),
                )

                self.assertEqual(profile.primary_identifier, "ddg51")
                self.assertEqual(profile.qualifier, qualifier)
                self.assertEqual(profile.scope_kind, "flight")

    def test_qualifier_parser_supports_suffixes_without_consuming_class(self) -> None:
        expected = {
            "DDG-51 Flight IIA class": "Flight IIA",
            "DDG-51 Flight III class": "Flight III",
            "LHA-6 Flight 0 I class": "Flight 0 I",
            "LHA-6 Flight 0/I class": "Flight 0/I",
            "Aircraft Block 4A class": "Block 4A",
            "Aircraft Batch 2A class": "Batch 2A",
        }
        for text, qualifier in expected.items():
            with self.subTest(text=text):
                self.assertEqual(
                    analysis_service._extract_scope_qualifier(text),
                    qualifier,
                )


class JaneScopeResolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tree, cls.details = _scope_tree()
        cls.index = build_architecture_tree_index(cls.tree)

    def _profile(self, original_name: str, original_text: str):
        return analysis_service._build_jane_classification_profile(
            file_name="technical-upload.pdf",
            original_name=original_name,
            original_text=original_text,
        )

    def test_fleetlist_requires_two_distinct_same_series_members(self) -> None:
        repeated_text = _jane_text(
            "America class (LHA-6)",
            "Fleetlist",
            "LHA-6",
            "LHA-6",
            "LHA-6",
        )
        profile = self._profile("America class LHA-6.pdf", repeated_text)
        entities = analysis_service._equipment_entities_by_identifier(self.index)

        self.assertEqual(
            analysis_service._scope_parent_clusters(
                profile,
                repeated_text,
                self.index,
                entities,
            ),
            (),
        )

        two_member_text = f"{repeated_text}\nLHA-7"
        self.assertEqual(
            analysis_service._scope_parent_clusters(
                profile,
                two_member_text,
                self.index,
                entities,
            ),
            ((242, (240, 241)),),
        )

    def test_catalog_filename_title_can_protect_recall_without_final_override(
        self,
    ) -> None:
        text = _jane_text(
            "America class (LHA-6)",
            "Fleetlist",
            "LHA-6",
            "LHA-7",
        )
        profile = self._profile(
            "JFS_3567-JFS_-17-Jul-2024.pdf",
            text,
        )
        resolution = analysis_service._resolve_jane_architecture_scope(
            profile,
            original_text=text,
            tree_index=self.index,
        )
        radar_detail = self.details[91][0]
        decision = analysis_service._decide_topk_deterministic_architecture_constraint(
            radar_detail,
            file_name="technical-upload.pdf",
            original_name="JFS_3567-JFS_-17-Jul-2024.pdf",
            visible_ids={radar_detail, 242},
            tree_index=self.index,
            architecture_list=self.tree,
            filename_constraint_mode="scope_guard",
            jane_profile=profile,
            scope_resolution=resolution,
        )

        self.assertTrue(profile.recall_identity_enabled)
        self.assertFalse(profile.identity_confirmed)
        self.assertEqual(resolution.matched_scope_parent_id, 242)
        self.assertEqual(
            resolution.preferred_parent_reasons,
            {242: ("jane_scope_parent",)},
        )
        self.assertEqual(decision.post_architecture_id, radar_detail)
        self.assertEqual(decision.matched_scope_parent_id, 242)
        self.assertEqual(
            decision.reason_code,
            "no_constraint_insufficient_evidence",
        )

    def test_flight_qualifier_selects_matching_parent_cluster(self) -> None:
        for qualifier, expected_parent in (("Flight IIA", 126), ("Flight III", 174)):
            with self.subTest(qualifier=qualifier):
                text = _jane_text(
                    f"DDG-51 {qualifier} class",
                    "Fleetlist",
                    "DDG-53",
                    "DDG-54",
                    "DDG-125",
                    "DDG-126",
                )
                profile = self._profile(f"DDG-51 {qualifier}.pdf", text)
                resolution = analysis_service._resolve_jane_architecture_scope(
                    profile,
                    original_text=text,
                    tree_index=self.index,
                )

                self.assertEqual(resolution.matched_scope_parent_id, expected_parent)
                self.assertFalse(resolution.tree_gap)
                self.assertEqual(resolution.reason_code, "jane_scope_parent")

    def test_flight_qualifier_mismatch_does_not_use_unique_wrong_cluster(self) -> None:
        text = _jane_text(
            "DDG-51 Flight III class",
            "Fleetlist",
            "DDG-53",
            "DDG-54",
        )
        profile = self._profile("DDG-51 Flight III.pdf", text)
        resolution = analysis_service._resolve_jane_architecture_scope(
            profile,
            original_text=text,
            tree_index=self.index,
        )

        self.assertEqual(resolution.clustered_parent_ids, (126,))
        self.assertIsNone(resolution.matched_scope_parent_id)
        self.assertEqual(
            resolution.reason_code,
            "no_constraint_insufficient_evidence",
        )

    def test_unique_class_cluster_promotes_detail_to_scope_parent(self) -> None:
        text = _jane_text(
            "America class (LHA-6)",
            "Fleetlist",
            "LHA-6",
            "LHA-7",
        )
        profile = self._profile("America class LHA-6.pdf", text)
        resolution = analysis_service._resolve_jane_architecture_scope(
            profile,
            original_text=text,
            tree_index=self.index,
        )
        selected_detail = self.details[240][0]
        decision = analysis_service._decide_topk_deterministic_architecture_constraint(
            selected_detail,
            file_name="technical-upload.pdf",
            original_name="America class LHA-6.pdf",
            visible_ids={selected_detail, 242},
            tree_index=self.index,
            architecture_list=self.tree,
            filename_constraint_mode="scope_guard",
            jane_profile=profile,
            scope_resolution=resolution,
        )

        self.assertEqual(resolution.matched_scope_parent_id, 242)
        self.assertEqual(resolution.preferred_parent_reasons, {242: ("jane_scope_parent",)})
        self.assertEqual(decision.pre_architecture_id, selected_detail)
        self.assertEqual(decision.post_architecture_id, 242)
        self.assertEqual(decision.reason_code, "jane_scope_parent")

    def test_cross_branch_class_result_is_guarded_to_scope_parent(self) -> None:
        text = _jane_text(
            "America class (LHA-6)",
            "Fleetlist",
            "LHA-6",
            "LHA-7",
        )
        profile = self._profile("America class LHA-6.pdf", text)
        resolution = analysis_service._resolve_jane_architecture_scope(
            profile,
            original_text=text,
            tree_index=self.index,
        )
        radar_detail = self.details[91][0]
        decision = analysis_service._decide_topk_deterministic_architecture_constraint(
            radar_detail,
            file_name="technical-upload.pdf",
            original_name="America class LHA-6.pdf",
            visible_ids={radar_detail, 242},
            tree_index=self.index,
            architecture_list=self.tree,
            filename_constraint_mode="scope_guard",
            jane_profile=profile,
            scope_resolution=resolution,
        )

        self.assertEqual(decision.post_architecture_id, 242)
        self.assertEqual(decision.reason_code, "jane_branch_guard")

    def test_virginia_tree_gap_selects_lead_identifier_parent(self) -> None:
        text = _jane_text(
            "Virginia class (SSN-774)",
            "Fleetlist",
            "SSN-774",
            "SSN-775",
            "SSN-778",
            "SSN-779",
        )
        profile = self._profile("Virginia class SSN-774.pdf", text)
        resolution = analysis_service._resolve_jane_architecture_scope(
            profile,
            original_text=text,
            tree_index=self.index,
        )

        self.assertEqual(resolution.clustered_parent_ids, (458, 468))
        self.assertEqual(resolution.matched_scope_parent_id, 458)
        self.assertTrue(resolution.tree_gap)
        self.assertEqual(resolution.reason_code, "jane_tree_gap_lead_parent")
        self.assertEqual(
            resolution.preferred_parent_reasons,
            {458: ("jane_tree_gap_lead_parent",)},
        )

    def test_mh60r_aircraft_totals_protects_high_level_air_parent(self) -> None:
        text = _jane_text(
            "MH-60R",
            "Aircraft totals",
            "Fleet",
            "MH-60R helicopters",
        )
        profile = self._profile("MH-60R.pdf", text)
        resolution = analysis_service._resolve_jane_architecture_scope(
            profile,
            original_text=text,
            tree_index=self.index,
        )
        radar_detail = self.details[91][0]

        self.assertEqual(profile.high_level_branch_hint, "air_equipment")
        self.assertEqual(resolution.matched_scope_parent_id, 28)
        self.assertEqual(
            resolution.preferred_parent_reasons,
            {28: ("jane_high_level_branch",)},
        )
        decision = analysis_service._decide_topk_deterministic_architecture_constraint(
            radar_detail,
            file_name="technical-upload.pdf",
            original_name="MH-60R.pdf",
            visible_ids={radar_detail, 28},
            tree_index=self.index,
            architecture_list=self.tree,
            filename_constraint_mode="scope_guard",
            jane_profile=profile,
            scope_resolution=resolution,
        )
        self.assertEqual(decision.post_architecture_id, 28)
        self.assertEqual(decision.reason_code, "jane_high_level_branch")

    def test_single_model_preserves_compatible_result_and_guards_cross_branch(
        self,
    ) -> None:
        text = _jane_text("P-8A", "Specifications")
        profile = self._profile("P-8A.pdf", text)
        resolution = analysis_service._resolve_jane_architecture_scope(
            profile,
            original_text=text,
            tree_index=self.index,
        )
        p8_detail = self.details[47][0]
        radar_detail = self.details[91][0]
        visible_ids = {47, p8_detail, radar_detail}

        compatible = analysis_service._decide_topk_deterministic_architecture_constraint(
            p8_detail,
            file_name="technical-upload.pdf",
            original_name="P-8A.pdf",
            visible_ids=visible_ids,
            tree_index=self.index,
            architecture_list=self.tree,
            filename_constraint_mode="scope_guard",
            jane_profile=profile,
            scope_resolution=resolution,
        )
        cross_branch = analysis_service._decide_topk_deterministic_architecture_constraint(
            radar_detail,
            file_name="technical-upload.pdf",
            original_name="P-8A.pdf",
            visible_ids=visible_ids,
            tree_index=self.index,
            architecture_list=self.tree,
            filename_constraint_mode="scope_guard",
            jane_profile=profile,
            scope_resolution=resolution,
        )

        self.assertEqual(profile.scope_kind, "single_model")
        self.assertEqual(resolution.matched_branch_parent_id, 47)
        self.assertEqual(compatible.post_architecture_id, p8_detail)
        self.assertEqual(compatible.reason_code, "jane_branch_guard")
        self.assertEqual(cross_branch.post_architecture_id, 47)
        self.assertEqual(cross_branch.reason_code, "jane_branch_guard")

    def test_conflict_and_insufficient_evidence_do_not_override_model_result(
        self,
    ) -> None:
        radar_detail = self.details[91][0]
        cases = (
            (
                "DDG-51.pdf",
                _jane_text("America class (LHA-6)"),
                "no_constraint_conflict",
            ),
            (
                "general-report.pdf",
                _jane_text("America class (LHA-6)"),
                "no_constraint_insufficient_evidence",
            ),
        )
        for original_name, text, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                profile = self._profile(original_name, text)
                resolution = analysis_service._resolve_jane_architecture_scope(
                    profile,
                    original_text=text,
                    tree_index=self.index,
                )
                decision = (
                    analysis_service._decide_topk_deterministic_architecture_constraint(
                        radar_detail,
                        file_name="technical-upload.pdf",
                        original_name=original_name,
                        visible_ids={radar_detail},
                        tree_index=self.index,
                        architecture_list=self.tree,
                        filename_constraint_mode="scope_guard",
                        jane_profile=profile,
                        scope_resolution=resolution,
                    )
                )

                self.assertEqual(decision.post_architecture_id, radar_detail)
                self.assertEqual(decision.reason_code, expected_reason)

    def test_confirmed_gjb_constraint_is_identical_across_filename_modes(
        self,
    ) -> None:
        radar_detail = self.details[91][0]
        standard_profile = (
            analysis_service._build_data_standard_classification_profile(
                file_name="GJB9001C-2026.pdf",
                original_name="GJB9001C-2026.pdf",
                original_text="\n".join(
                    (
                        "中华人民共和国国家军用标准",
                        "GJB 9001C-2026",
                        "质量管理体系要求",
                        "1 范围",
                        "2 规范性引用文件",
                    )
                ),
            )
        )
        results = []
        for mode in ("legacy", "scope_guard"):
            decision = analysis_service._decide_topk_deterministic_architecture_constraint(
                radar_detail,
                file_name="GJB9001C-2026.pdf",
                original_name="GJB9001C-2026.pdf",
                visible_ids={radar_detail, 101},
                tree_index=self.index,
                architecture_list=self.tree,
                filename_constraint_mode=mode,
                data_standard_profile=standard_profile,
            )
            results.append(decision.post_architecture_id)

        self.assertEqual(results, [101, 101])

    def test_non_jane_scope_guard_does_not_fall_back_to_legacy_constraint(self) -> None:
        """非 Jane 文件仅在显式 legacy 模式下允许文件名单源硬覆盖。"""
        profile = self._profile(
            "P-8A.pdf",
            "P-8A aircraft specifications without Jane's page metadata",
        )
        radar_detail = self.details[91][0]
        results = []
        for mode in ("legacy", "scope_guard"):
            decision = analysis_service._decide_topk_deterministic_architecture_constraint(
                radar_detail,
                file_name="technical-upload.pdf",
                original_name="P-8A.pdf",
                visible_ids={radar_detail, 47},
                tree_index=self.index,
                architecture_list=self.tree,
                filename_constraint_mode=mode,
                jane_profile=profile,
            )
            results.append((decision.post_architecture_id, decision.reason_code))

        self.assertFalse(profile.active)
        self.assertEqual(
            results,
            [
                (47, "legacy_identifier_parent"),
                (radar_detail, "no_constraint_insufficient_evidence"),
            ],
        )

    def test_legacy_wrapper_matches_decision_and_keeps_reason_code(self) -> None:
        radar_detail = self.details[91][0]
        kwargs = {
            "file_name": "technical-upload.pdf",
            "original_name": "P-8A.pdf",
            "visible_ids": {radar_detail, 47},
            "tree_index": self.index,
            "architecture_list": self.tree,
            "filename_constraint_mode": "legacy",
        }
        decision = analysis_service._decide_topk_deterministic_architecture_constraint(
            radar_detail,
            **kwargs,
        )
        wrapped_id = analysis_service._apply_topk_deterministic_architecture_constraints(
            radar_detail,
            **kwargs,
        )

        self.assertEqual(decision.reason_code, "legacy_identifier_parent")
        self.assertEqual(decision.post_architecture_id, 47)
        self.assertEqual(wrapped_id, decision.post_architecture_id)


if __name__ == "__main__":
    unittest.main()
