"""阶段 1D-0 武器谱契约、黄金样例和校准门禁的离线测试。

本模块故意不创建 Flask 应用、不启动 ``run.py``，也不连接 AnythingLLM、模型、
回调服务或生产数据库。测试只读取已经脱敏的冻结资产，并用测试侧参考算法证明
Evidence Selection 规则是确定性的；真正的领域实现将在 1D-1 后按同一资产落地。
"""

from __future__ import annotations

import json
import math
import unittest
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from app.services.llm_service.weaponry_service import (
    _build_weaponry_callback_payload,
)


_CONTRACT_PATH = (
    Path(__file__).with_name("contracts") / "stage1d_weaponry_contracts.json"
)
_ARCHITECTURE_ID_ERROR = (
    "architectureId必须为1到9223372036854775807之间的正整数"
)
_MAX_ARCHITECTURE_ID = 9_223_372_036_854_775_807


def _normalize_architecture_id_for_contract(value: Any) -> tuple[int, str]:
    """按 D02 的冻结口径规范化 ID；这里只是测试 Oracle，不供生产代码调用。"""

    if isinstance(value, bool):
        raise ValueError(_ARCHITECTURE_ID_ERROR)

    if isinstance(value, int):
        normalized = value
    elif (
        isinstance(value, str)
        and value
        and value.isascii()
        and all("0" <= character <= "9" for character in value)
    ):
        normalized = int(value)
    else:
        raise ValueError(_ARCHITECTURE_ID_ERROR)

    if normalized < 1 or normalized > _MAX_ARCHITECTURE_ID:
        raise ValueError(_ARCHITECTURE_ID_ERROR)
    return normalized, str(normalized)


def _normalize_evidence_text(text: str) -> str:
    """只统一换行并去除首尾空白，避免测试误引入模糊去重。"""

    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def _is_finite_number(value: Any) -> bool:
    """布尔值在 JSON 中虽是数字子类，但不属于合法相关性分数。"""

    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _select_fixture_evidence(
    candidates: Iterable[dict[str, Any]],
    *,
    allowed_document_keys: set[str],
    profile_id: str,
    minimum_score: float,
) -> tuple[list[dict[str, Any]], dict[str, str], Counter[str]]:
    """执行测试侧选择规则，返回选中项、逐项拒绝原因和原因计数。

    这里的实现只用于证明冻结资产自洽。生产代码不得从 ``tests`` 导入它；1D-1
    应在领域层重新实现同一顺序，并由本资产进行黑盒校验。
    """

    rejected: dict[str, str] = {}
    valid: list[dict[str, Any]] = []

    for raw_candidate in candidates:
        candidate = dict(raw_candidate)
        candidate_id = str(candidate["id"])
        document_key = str(candidate.get("documentKey") or "")
        if document_key not in allowed_document_keys:
            rejected[candidate_id] = "document-not-allowed"
            continue
        if candidate.get("profileId") != profile_id:
            rejected[candidate_id] = "profile-mismatch"
            continue

        normalized_text = _normalize_evidence_text(str(candidate.get("text") or ""))
        if not normalized_text:
            rejected[candidate_id] = "empty-text"
            continue
        score = candidate.get("normalizedScore")
        if not _is_finite_number(score):
            rejected[candidate_id] = "missing-or-invalid-score"
            continue

        candidate["normalizedText"] = normalized_text
        candidate["normalizedScore"] = float(score)
        valid.append(candidate)

    # 同一文档、规范化全文相同的候选只保留稳定优胜项。不同文档绝不互相去重。
    deduplicated: dict[tuple[str, str], dict[str, Any]] = {}
    for candidate in valid:
        key = (candidate["documentKey"], candidate["normalizedText"])
        previous = deduplicated.get(key)
        winner_key = (
            -candidate["normalizedScore"],
            int(candidate["rank"]),
            str(candidate["id"]),
        )
        if previous is None:
            deduplicated[key] = candidate
            continue
        previous_key = (
            -previous["normalizedScore"],
            int(previous["rank"]),
            str(previous["id"]),
        )
        if winner_key < previous_key:
            rejected[str(previous["id"])] = "duplicate-within-document"
            deduplicated[key] = candidate
        else:
            rejected[str(candidate["id"])] = "duplicate-within-document"

    ordered = sorted(
        deduplicated.values(),
        key=lambda item: (
            -item["normalizedScore"],
            int(item["rank"]),
            str(item["id"]),
        ),
    )

    selected: list[dict[str, Any]] = []
    for candidate in ordered:
        candidate_id = str(candidate["id"])
        if candidate["normalizedScore"] < minimum_score:
            rejected[candidate_id] = "below-threshold"
            continue

        selected_item = dict(candidate)
        selected_item["selectedText"] = candidate["normalizedText"]
        selected.append(selected_item)

    return selected, rejected, Counter(rejected.values())


def _assert_all_numbers_are_finite(test_case: unittest.TestCase, value: Any) -> None:
    """递归防止 JSON 黄金资产混入 NaN/Infinity。"""

    if isinstance(value, dict):
        for child in value.values():
            _assert_all_numbers_are_finite(test_case, child)
    elif isinstance(value, list):
        for child in value:
            _assert_all_numbers_are_finite(test_case, child)
    elif isinstance(value, float):
        test_case.assertTrue(math.isfinite(value))


class Stage1DWeaponryContractAssetTests(unittest.TestCase):
    """冻结 1D 已批准契约，并核验 1D-6 的公开路由实施状态。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract: dict[str, Any] = json.loads(
            _CONTRACT_PATH.read_text(encoding="utf-8")
        )

    def test_stage_1d6_switches_only_approved_route_behavior(self) -> None:
        self.assertEqual(1, self.contract["schemaVersion"])
        self.assertEqual("1D-6", self.contract["stage"])
        self.assertEqual(
            "weaponry-application-route",
            self.contract["productionCodeSwitch"],
        )
        self.assertFalse(self.contract["interfaceParametersChanged"])
        self.assertEqual(
            "new-application-route-active-since-1D-6",
            self.contract["publicSubmission"]["current"]["implementationStatus"],
        )
        self.assertEqual(
            "",
            self.contract["publicSubmission"]["current"]["successBody"],
        )

    def test_approved_d01_to_d05_are_all_frozen_without_pending_branches(self) -> None:
        decisions = {
            item["id"]: item for item in self.contract["approvedDecisions"]
        }
        self.assertEqual(
            {"1D-D01", "1D-D02", "1D-D03", "1D-D04", "1D-D05"},
            set(decisions),
        )
        self.assertEqual(
            "file-aggregate-v1",
            decisions["1D-D04"]["onlyStrategy"],
        )
        self.assertEqual(
            "skip-network-send",
            decisions["1D-D05"]["staleCallback"],
        )

    def test_target_http_contract_is_202_empty_and_existing_error_shape(self) -> None:
        target = self.contract["publicSubmission"]["target"]
        self.assertEqual(202, target["successStatus"])
        self.assertEqual("", target["successBody"])
        self.assertEqual(
            {"status": 409, "body": {"error": "任务正在处理中"}},
            target["activeConflict"],
        )
        self.assertEqual(["error"], target["errorBodyShape"]["requiredKeys"])
        self.assertFalse(target["errorBodyShape"]["additionalKeys"])

    def test_architecture_id_matrix_matches_d02_oracle(self) -> None:
        for case in self.contract["architectureIdCases"]:
            with self.subTest(value=case["input"]):
                if case["valid"]:
                    self.assertEqual(
                        (case["normalizedNumber"], case["businessKey"]),
                        _normalize_architecture_id_for_contract(case["input"]),
                    )
                else:
                    if case["input"] is None:
                        self.assertEqual("architectureId不能为空", case["error"])
                        continue
                    with self.assertRaisesRegex(ValueError, _ARCHITECTURE_ID_ERROR):
                        _normalize_architecture_id_for_contract(case["input"])
                    self.assertEqual(_ARCHITECTURE_ID_ERROR, case["error"])

    def test_validation_errors_are_exact_single_field_contracts(self) -> None:
        matrix = self.contract["validationMatrix"]
        case_names = [item["case"] for item in matrix]
        self.assertEqual(len(case_names), len(set(case_names)))
        self.assertIn("field-item-not-object", case_names)
        self.assertIn("field-type-unknown", case_names)
        self.assertIn("table-cell-not-object", case_names)
        self.assertIn("selected-file-not-found", case_names)
        for item in matrix:
            with self.subTest(case=item["case"]):
                self.assertIn(item["status"], {400, 404, 409})
                self.assertIsInstance(item["error"], str)
                self.assertTrue(item["error"])

    def test_validation_errors_are_present_in_authoritative_interface_document(self) -> None:
        """机器契约中的公开错误文本必须全部回写接口文档，避免形成双重真相源。"""

        interface_document = (
            Path(__file__).parents[1] / "docs" / "接口文档" / "知识谱系解析.md"
        ).read_text(encoding="utf-8")
        for item in self.contract["validationMatrix"]:
            with self.subTest(case=item["case"]):
                self.assertIn(item["error"], interface_document)

    def test_document_scope_matrix_freezes_explicit_and_category_paths(self) -> None:
        observed = {
            item["case"]: item["result"]
            for item in self.contract["documentScopeMatrix"]
        }
        self.assertEqual(
            "freeze-current-category-documents-at-acceptance",
            observed["file-path-list-missing"],
        )
        self.assertEqual(
            "freeze-current-category-documents-at-acceptance",
            observed["file-path-list-empty"],
        )
        self.assertEqual(
            "http-202-then-failure-callback",
            observed["category-empty-at-acceptance"],
        )
        self.assertEqual(
            "http-400-ambiguity-without-task-side-effects",
            observed["same-file-name-multiple-records"],
        )

    def test_only_file_aggregate_mode_two_is_a_compatibility_target(self) -> None:
        mode = self.contract["modePolicy"]
        self.assertEqual(["file-aggregate-v1"], mode["supported"])
        self.assertEqual(["chunk-question-mode-1"], mode["removed"])
        self.assertFalse(mode["environmentSelectionDuringExecution"])

    def test_input_and_table_golden_callbacks_match_existing_root_builder(self) -> None:
        callbacks = self.contract["goldenCallbacks"]
        for name in ("inputSuccess", "tableSuccess"):
            expected = callbacks[name]
            with self.subTest(name=name):
                self.assertEqual(
                    expected,
                    _build_weaponry_callback_payload(
                        expected["data"]["architectureId"],
                        expected["data"]["weaponryTemplateFieldList"],
                        status="2",
                    ),
                )
        failure = callbacks["failure"]
        self.assertEqual(
            failure,
            _build_weaponry_callback_payload(
                failure["data"]["architectureId"],
                [],
                status="3",
            ),
        )
        self.assertEqual(
            {"content", "source", "time", "fileName", "rows", "translate"},
            set(callbacks["emptySource"]),
        )
        self.assertEqual([], callbacks["emptySource"]["rows"])

    def test_field_description_is_required_in_both_retrieval_and_extraction(self) -> None:
        dual_stage = self.contract["fieldDescriptionDualStage"]
        input_contract = dual_stage["input"]
        retrieval_query = input_contract["retrievalQuery"]
        for fragment in input_contract["retrievalQueryRequiredFragments"]:
            self.assertIn(fragment, retrieval_query)
        for fragment in input_contract["retrievalQueryForbiddenFragments"]:
            self.assertNotIn(fragment, retrieval_query)
        self.assertIn(
            input_contract["fieldDescription"],
            input_contract["extractionPromptRequiredFragments"],
        )

        table_contract = dual_stage["table"]
        retrieval_fragments = set(table_contract["retrievalQueryRequiredFragments"])
        extraction_fragments = set(table_contract["extractionPromptRequiredFragments"])
        self.assertTrue(retrieval_fragments.issubset(extraction_fragments))
        for column in table_contract["columns"]:
            self.assertIn(column["fieldName"], retrieval_fragments)
            self.assertIn(column["fieldDescription"], retrieval_fragments)

    def test_terms_disabled_is_zero_io_and_misleading_guidance_cannot_win(self) -> None:
        matrix = {
            item["policy"]: item
            for item in self.contract["auxiliaryGuidanceMatrix"]
        }
        disabled = matrix["none"]
        for key in (
            "directoryScanCalls",
            "workspaceCalls",
            "uploadCalls",
            "embeddingCalls",
            "guidanceSearchCalls",
            "promptGuidanceCount",
        ):
            self.assertEqual(0, disabled[key], msg=key)
        self.assertEqual(1, disabled["targetSearchCallsPerField"])

        misleading = self.contract["misleadingGuidanceCase"]
        self.assertEqual("31节", misleading["expectedContent"])
        self.assertEqual("35节", misleading["forbiddenContent"])
        self.assertFalse(misleading["guidanceMayAppearInRows"])
        self.assertTrue(misleading["targetEvidenceMustAppearInRows"])

    def test_context_matrix_forbids_cross_document_and_parent_thread_fallback(self) -> None:
        matrix = {
            item["case"]: item
            for item in self.contract["contextIsolationMatrix"]
        }
        self.assertFalse(
            matrix["document-b-call-contains-document-a-evidence"]["answerAccepted"]
        )
        self.assertTrue(
            matrix["two-documents-legitimately-return-same-content"]["answerAccepted"]
        )
        for case in (
            "child-thread-create-returns-none",
            "child-thread-create-raises",
        ):
            self.assertFalse(matrix[case]["answerAccepted"])
            self.assertEqual(0, matrix[case]["parentThreadCalls"])

        contamination = self.contract["crossDocumentContaminationRegression"]
        self.assertEqual("乙级", contamination["documentB"]["expectedContent"])
        self.assertEqual("甲级", contamination["documentB"]["forbiddenContent"])
        self.assertFalse(contamination["sameSessionAllowed"])
        self.assertFalse(contamination["parentSessionFallbackAllowed"])

    def test_provider_capability_matrix_selects_no_unproven_production_strategy(self) -> None:
        matrix = {
            item["provider"]: item
            for item in self.contract["providerCapabilityMatrix"]
        }
        current = matrix["current-anythingllm-adapter"]
        self.assertEqual([], current["eligibleExtractionStrategies"])
        self.assertFalse(current["productionEligible"])
        for provider in matrix.values():
            self.assertFalse(provider["targetWorkspaceSecondRagAllowed"])
            self.assertFalse(provider["parentThreadFallbackAllowed"])

    def test_fixture_evidence_selection_is_deterministic_and_never_coerces_scores(self) -> None:
        selection = self.contract["evidenceSelection"]
        profile = selection["fixtureOnlySelectionProfile"]
        candidates = [
            item
            for item in selection["fixtureCandidates"]
            if "allowedDocumentKeysOverride" not in item
        ]
        result = _select_fixture_evidence(
            candidates,
            allowed_document_keys={"doc-a", "doc-b"},
            profile_id=profile["profileId"],
            minimum_score=profile["minimumScore"],
        )
        selected, rejected, reason_counts = result
        self.assertEqual(
            ["strong-a", "same-text-other-document"],
            [item["id"] for item in selected],
        )
        self.assertEqual("duplicate-within-document", rejected["duplicate-a-lower"])
        self.assertEqual("below-threshold", rejected["near-synonym-wrong"])
        self.assertEqual("below-threshold", rejected["homonym-wrong"])
        self.assertEqual("below-threshold", rejected["unrelated"])
        self.assertEqual("empty-text", rejected["blank"])
        self.assertEqual("missing-or-invalid-score", rejected["missing-score"])
        self.assertEqual("missing-or-invalid-score", rejected["invalid-score"])
        self.assertEqual("profile-mismatch", rejected["profile-mismatch"])
        self.assertEqual(2, reason_counts["missing-or-invalid-score"])

        # 反转输入后结果仍完全一致，证明不依赖供应商返回容器的偶然顺序。
        reversed_result = _select_fixture_evidence(
            reversed(candidates),
            allowed_document_keys={"doc-a", "doc-b"},
            profile_id=profile["profileId"],
            minimum_score=profile["minimumScore"],
        )
        self.assertEqual(
            [item["id"] for item in selected],
            [item["id"] for item in reversed_result[0]],
        )
        self.assertEqual(rejected, reversed_result[1])

    def test_cross_document_candidate_is_rejected_even_with_a_higher_score(self) -> None:
        selection = self.contract["evidenceSelection"]
        profile = selection["fixtureOnlySelectionProfile"]
        candidate = next(
            item
            for item in selection["fixtureCandidates"]
            if item["id"] == "cross-document-a-in-b-call"
        )
        selected, rejected, _ = _select_fixture_evidence(
            [candidate],
            allowed_document_keys=set(candidate["allowedDocumentKeysOverride"]),
            profile_id=profile["profileId"],
            minimum_score=profile["minimumScore"],
        )
        self.assertEqual([], selected)
        self.assertEqual(
            "document-not-allowed",
            rejected["cross-document-a-in-b-call"],
        )

    def test_selected_evidence_preserves_all_characters_deterministically(self) -> None:
        limit_case = self.contract["evidenceSelection"]["determinismCases"][
            "fullTextPreservation"
        ]
        candidates = [
            {
                "id": f"long-{index}",
                "documentKey": limit_case["documentKey"],
                "text": chr(ord("甲") + index) * length,
                "rank": index + 1,
                "normalizedScore": 0.95 - index * 0.01,
                "profileId": "stage1d-selection-oracle-fixture-v1",
            }
            for index, length in enumerate(limit_case["inputTextLengths"])
        ]
        selected, _, _ = _select_fixture_evidence(
            candidates,
            allowed_document_keys={limit_case["documentKey"]},
            profile_id="stage1d-selection-oracle-fixture-v1",
            minimum_score=0.82,
        )
        lengths = [len(item["selectedText"]) for item in selected]
        self.assertEqual(limit_case["expectedSelectedTextLengths"], lengths)
        self.assertEqual(limit_case["expectedTotalCharacters"], sum(lengths))

    def test_live_calibration_rejects_fake_production_threshold(self) -> None:
        calibration = self.contract["liveCalibration"]
        self.assertFalse(calibration["remoteMutation"])
        self.assertFalse(calibration["secretsRecorded"])
        self.assertEqual("lancedb", calibration["providerFingerprint"]["vectorDatabase"])
        self.assertEqual(
            "MintplexLabs/multilingual-e5-small",
            calibration["providerFingerprint"]["embeddingModel"],
        )
        self.assertEqual(
            "score-plus-distance-equals-one-within-float-tolerance",
            calibration["scoreProtocol"]["identity"],
        )
        self.assertEqual(
            "higher-score-is-returned-first",
            calibration["scoreProtocol"]["direction"],
        )

        observations = calibration["queryLevelObservations"]
        positives = [item for item in observations if item["label"] == "strong-relevant"]
        natural_negatives = [
            item
            for item in observations
            if item["label"] == "natural-language-negative"
        ]
        self.assertLess(
            min(item["topScore"] for item in positives),
            max(item["topScore"] for item in natural_negatives),
        )
        decision = calibration["productionProfileDecision"]
        self.assertEqual("rejected-for-direct-score-thresholding", decision["status"])
        self.assertIsNone(decision["minimumRelevanceScore"])
        self.assertFalse(decision["rawProviderScoreAloneProvesFieldRelevance"])
        self.assertEqual([], decision["blocks"])
        self.assertIn("1D-6-public-route-switch", decision["historicalBlocks"])
        self.assertEqual(
            "approved-score-or-rank-selection-without-absolute-threshold",
            decision["supersededBy"],
        )

    def test_live_calibration_records_stage1d0r_ground_truth_correction(self) -> None:
        calibration = self.contract["liveCalibration"]
        reaudit = calibration["groundTruthReaudit"]
        observations = {
            item["id"]: item for item in calibration["queryLevelObservations"]
        }

        self.assertEqual("wrong-missile", reaudit["correctedQueryId"])
        self.assertEqual("positive-present-in-document", reaudit["newLabel"])
        self.assertTrue(reaudit["originalRawScoreDecisionStillRejected"])
        self.assertEqual("strong-relevant", observations["wrong-missile"]["label"])
        self.assertNotIn(
            "wrong-missile",
            calibration["productionProfileDecision"]["knownFalsePositiveQueryIds"],
        )
        self.assertEqual(
            "stage1d0r_retrieval_quality.json",
            reaudit["supersededBy"],
        )

    def test_fault_matrix_covers_every_planned_boundary(self) -> None:
        observed_steps = {item["step"] for item in self.contract["faultMatrix"]}
        required_steps = {
            "document-scope-repository",
            "submission-repository",
            "workspace",
            "document-binding",
            "thread",
            "retrieval",
            "retrieval-score",
            "model",
            "table-parser",
            "translation",
            "interaction-audit",
            "terminal-write",
            "callback",
            "cleanup",
        }
        self.assertTrue(required_steps.issubset(observed_steps))

    def test_progress_and_check_task_remain_internal_compatibility_only(self) -> None:
        boundary = self.contract["progressAndCheckTask"]
        self.assertFalse(boundary["progress"]["newPublicFields"])
        self.assertFalse(boundary["checkTask"]["newPublicFields"])
        self.assertEqual(
            "must-remain",
            boundary["checkTask"]["synchronousCallbackRecoverySideEffect"],
        )

    def test_asset_is_strict_json_and_records_the_open_exit_gate(self) -> None:
        _assert_all_numbers_are_finite(self, self.contract)
        strict_round_trip = json.loads(
            json.dumps(self.contract, ensure_ascii=False, allow_nan=False)
        )
        self.assertEqual(self.contract, strict_round_trip)
        stage_exit = self.contract["stageExit"]
        self.assertEqual(
            "complete",
            stage_exit["contractsGoldenFaultAndIsolationAssets"],
        )
        self.assertEqual("verified", stage_exit["liveScoreSemantics"])
        self.assertEqual(
            "schema-v2-score-or-stable-rank-frozen",
            stage_exit["productionEvidenceSelectionProfile"],
        )
        self.assertFalse(stage_exit["stageCanClose"])


if __name__ == "__main__":
    unittest.main()
