from __future__ import annotations

import unittest

from app.modules.analysis.domain.architecture_tree import build_architecture_tree_index
from app.modules.analysis.domain.errors import ArchitectureContractError
from app.modules.analysis.domain.classification_rules import (
    _build_equipment_identity_reselect_profile,
    _decide_identity_reselect_gate,
    _ordered_equipment_family_scope_ids,
    _parse_architecture_reselect_result,
)


_DETAIL_KINDS = (
    "基础数据",
    "战技指标",
    "运用数据",
    "效能数据",
    "模型数据",
    "目特数据",
    "声像数据",
)


class AnalysisIdentityReselectTests(unittest.TestCase):
    @staticmethod
    def _tree() -> list[dict]:
        nodes = [
            {"id": 1, "name": "领域树", "parentId": None},
            {"id": 10, "name": "航空母舰", "parentId": 1},
            {"id": 20, "name": "航空武器", "parentId": 1},
            {"id": 21, "name": "防空导弹", "parentId": 20},
            {"id": 56, "name": "CVN-68", "parentId": 10},
            {"id": 57, "name": "CVN-69", "parentId": 10},
            {"id": 58, "name": "CVN-70", "parentId": 10},
        ]
        for parent_id, parent_name, first_leaf_id in (
            (56, "CVN-68", 561),
            (57, "CVN-69", 571),
            (58, "CVN-70", 581),
        ):
            nodes.extend(
                {
                    "id": first_leaf_id + offset,
                    "name": f"{parent_name}-{kind}",
                    "parentId": parent_id,
                }
                for offset, kind in enumerate(_DETAIL_KINDS)
            )
        return nodes

    def setUp(self) -> None:
        self.architecture_list = self._tree()
        self.tree_index = build_architecture_tree_index(self.architecture_list)
        self.visible_ids = set(self.tree_index.nodes_by_id)

    def _profile(
        self,
        *,
        original_name: str = "USS Nimitz (CVN-68) profile.pdf",
        original_text: str = (
            "USS Nimitz (CVN-68) aircraft carrier\n"
            "CVN 68 entered service with the United States Navy."
        ),
        visible_ids: set[int] | None = None,
        jane_active: bool = False,
        data_standard_active: bool = False,
    ):
        return _build_equipment_identity_reselect_profile(
            requested_original_name=original_name,
            original_text=original_text,
            tree_index=self.tree_index,
            visible_ids=self.visible_ids if visible_ids is None else visible_ids,
            jane_active=jane_active,
            data_standard_active=data_standard_active,
        )

    def test_explicit_filename_and_opening_identity_activate_ordered_scope(self):
        profile = self._profile()

        self.assertTrue(profile.active)
        self.assertEqual(profile.identifier, "cvn68")
        self.assertEqual(profile.target_parent_id, 56)
        self.assertEqual(
            profile.evidence_sources,
            ("originalFileName", "openingIdentity"),
        )
        self.assertEqual(
            profile.candidate_ids,
            (561, 562, 563, 564, 565, 566, 567, 56),
        )
        self.assertEqual(
            _ordered_equipment_family_scope_ids(56, self.tree_index),
            (561, 562, 563, 564, 565, 566, 567, 56),
        )

    def test_missing_original_filename_does_not_fallback_to_opening_identity(self):
        profile = self._profile(original_name="")

        self.assertFalse(profile.active)
        self.assertEqual(profile.reason_code, "no_explicit_original_filename")
        self.assertEqual(profile.filename_identifiers, ())

    def test_filename_echo_alone_is_not_independent_opening_evidence(self):
        for opening in (
            "CVN-68.pdf\nGeneral naval information without a hull identifier.",
            "File name: CVN-68.pdf\nGeneral naval information without a hull identifier.",
            "file:///tmp/CVN-68.pdf\nGeneral naval information without a hull identifier.",
        ):
            with self.subTest(opening=opening):
                profile = self._profile(
                    original_name="CVN-68.pdf",
                    original_text=opening,
                )

                self.assertFalse(profile.active)
                self.assertEqual(profile.reason_code, "independent_identity_missing")
                self.assertEqual(profile.opening_identifiers, ())

    def test_filename_echo_plus_body_identity_keeps_independent_evidence(self):
        profile = self._profile(
            original_name="CVN-68.pdf",
            original_text=(
                "CVN-68.pdf\n"
                "USS Nimitz aircraft carrier\n"
                "Hull number: CVN-68"
            ),
        )

        self.assertTrue(profile.active)
        self.assertEqual(profile.identifier, "cvn68")
        self.assertEqual(profile.opening_identifiers, ("cvn68",))

    def test_other_owned_or_untrusted_document_scopes_are_skipped(self):
        cases = (
            (
                "non_descriptive",
                {"original_name": "technical_upload_abcdef1234.pdf"},
                "filename_not_descriptive",
            ),
            (
                "jane",
                {"jane_active": True},
                "jane_scope_owned",
            ),
            (
                "gjb",
                {"data_standard_active": True},
                "data_standard_scope_owned",
            ),
        )
        for label, kwargs, expected_reason in cases:
            with self.subTest(label=label):
                profile = self._profile(**kwargs)
                self.assertFalse(profile.active)
                self.assertEqual(profile.reason_code, expected_reason)

    def test_opening_sibling_identifier_with_same_prefix_blocks_profile(self):
        profile = self._profile(
            original_text=(
                "USS Nimitz (CVN-68) aircraft carrier\n"
                "The comparison also covers USS Dwight D. Eisenhower (CVN-69)."
            )
        )

        self.assertFalse(profile.active)
        self.assertEqual(profile.reason_code, "opening_identity_conflict")
        self.assertEqual(profile.shared_identifiers, ("cvn68",))
        self.assertEqual(profile.conflicting_identifiers, ("cvn69",))

    def test_incomplete_visible_family_scope_does_not_activate_profile(self):
        visible_ids = self.visible_ids - {567}

        profile = self._profile(visible_ids=visible_ids)

        self.assertFalse(profile.active)
        self.assertEqual(profile.reason_code, "equipment_family_scope_incomplete")
        self.assertEqual(
            profile.candidate_ids,
            (561, 562, 563, 564, 565, 566, 567, 56),
        )

    def test_gate_only_reselects_ancestor_sibling_or_cross_branch_results(self):
        profile = self._profile()
        cases = (
            (56, False, "in_target_branch", "initial_result_in_target_branch"),
            (561, False, "in_target_branch", "initial_result_in_target_branch"),
            (10, True, "target_ancestor", "branch_conflict_reselect"),
            (57, True, "sibling_equipment", "branch_conflict_reselect"),
            (21, True, "cross_branch", "branch_conflict_reselect"),
        )

        for architecture_id, should_reselect, relation, reason in cases:
            with self.subTest(architecture_id=architecture_id):
                decision = _decide_identity_reselect_gate(
                    architecture_id,
                    profile=profile,
                    tree_index=self.tree_index,
                )
                self.assertEqual(decision.should_reselect, should_reselect)
                self.assertEqual(decision.relation, relation)
                self.assertEqual(decision.reason_code, reason)

    def test_parse_reselect_result_accepts_only_scoped_minimal_contract(self):
        scoped_ids = set(self._profile().candidate_ids)
        parse_kwargs = {
            "scoped_ids": scoped_ids,
            "tree_index": self.tree_index,
            "architecture_list": self.architecture_list,
        }

        for raw_result, expected in (
            ('{"architectureId":561}', 561),
            ({"architectureId": 56}, 56),
            ('{"architectureId":null}', None),
        ):
            with self.subTest(raw_result=raw_result):
                self.assertEqual(
                    _parse_architecture_reselect_result(raw_result, **parse_kwargs),
                    expected,
                )

        with self.subTest(raw_result="out_of_scope"):
            with self.assertRaisesRegex(
                ArchitectureContractError,
                "architectureId 不属于模型可见候选",
            ):
                _parse_architecture_reselect_result(
                    '{"architectureId":57}',
                    **parse_kwargs,
                )

        with self.subTest(raw_result="extra_key"):
            with self.assertRaisesRegex(
                ArchitectureContractError,
                "仅含 architectureId",
            ):
                _parse_architecture_reselect_result(
                    '{"architectureId":561,"reason":"body match"}',
                    **parse_kwargs,
                )


if __name__ == "__main__":
    unittest.main()
