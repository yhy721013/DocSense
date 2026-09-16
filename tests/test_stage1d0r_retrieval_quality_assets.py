"""阶段 1D-0R 检索质量修复资产、停止门禁和工具边界测试。"""

from __future__ import annotations

import ast
import json
import math
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import Mock

from app.integrations.anythingllm.models import AnythingLLMSource
from scripts.calibrate_weaponry_retrieval_quality import (
    CalibrationQuery,
    _query_result,
)


ROOT = Path(__file__).resolve().parents[1]
ASSET_PATH = ROOT / "tests" / "contracts" / "stage1d0r_retrieval_quality.json"
ORIGINAL_ASSET_PATH = ROOT / "tests" / "contracts" / "stage1d_weaponry_contracts.json"
CALIBRATION_SCRIPT = ROOT / "scripts" / "calibrate_weaponry_retrieval_quality.py"


def _reject_nonstandard_json(value: str) -> None:
    raise ValueError(f"非法 JSON 数字: {value}")


def _assert_finite_numbers(test_case: unittest.TestCase, value: Any) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _assert_finite_numbers(test_case, item)
        return
    if isinstance(value, list):
        for item in value:
            _assert_finite_numbers(test_case, item)
        return
    if isinstance(value, float):
        test_case.assertTrue(math.isfinite(value))


class Stage1D0RRetrievalQualityAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.asset = json.loads(
            ASSET_PATH.read_text(encoding="utf-8"),
            parse_constant=_reject_nonstandard_json,
        )
        cls.original_asset = json.loads(
            ORIGINAL_ASSET_PATH.read_text(encoding="utf-8"),
            parse_constant=_reject_nonstandard_json,
        )

    def test_asset_is_strict_json_and_keeps_public_contract_unchanged(self) -> None:
        self.assertEqual(1, self.asset["schemaVersion"])
        self.assertEqual("1D-0R", self.asset["stage"])
        self.assertFalse(self.asset["publicInterfaceParametersChanged"])
        self.assertFalse(self.asset["productionRouteSwitched"])
        self.assertTrue(self.asset["remoteMutationPerformed"])
        self.assertEqual(
            "temporary-isolated-resources-only",
            self.asset["remoteMutationScope"],
        )
        self.assertFalse(self.asset["existingAnythingLlmResourcesModified"])
        _assert_finite_numbers(self, self.asset)

    def test_live_corpus_is_not_misreported_as_three_documents(self) -> None:
        audit = self.asset["diagnosis"]["liveWorkspaceAudit"]

        self.assertEqual(3, audit["workspaceCount"])
        self.assertEqual(3, audit["documentCount"])
        self.assertEqual(1, audit["uniqueBusinessDocumentContentCount"])
        self.assertFalse(audit["representativeMultiDocumentCorpus"])

    def test_wrong_missile_ground_truth_is_corrected_in_both_assets(self) -> None:
        correction = self.asset["diagnosis"]["groundTruthCorrection"]
        original_reaudit = self.original_asset["liveCalibration"][
            "groundTruthReaudit"
        ]

        self.assertEqual("wrong-missile", correction["previousQueryId"])
        self.assertEqual(
            "positive-present-in-document",
            correction["correctedLabel"],
        )
        self.assertEqual(
            correction["previousQueryId"],
            original_reaudit["correctedQueryId"],
        )
        self.assertTrue(
            original_reaudit["originalRawScoreDecisionStillRejected"]
        )

    def test_mhtml_cleaner_removes_measured_reference_noise(self) -> None:
        audit = self.asset["mhtmlCleanerAudit"]
        legacy = audit["legacyText"]
        retrieval = audit["retrievalText"]

        self.assertGreater(legacy["chars"], retrieval["chars"])
        self.assertGreater(legacy["yearTokens"], retrieval["yearTokens"])
        self.assertGreater(legacy["urlTokens"], retrieval["urlTokens"])
        self.assertGreater(legacy["usniNewsOccurrences"], 0)
        self.assertEqual(0, retrieval["usniNewsOccurrences"])
        self.assertGreater(audit["removedCharacterRatio"], 0.5)
        self.assertFalse(audit["productionIngestionWired"])

    def test_historical_candidate_profile_is_not_reused_as_schema_v2_policy(self) -> None:
        value = self.asset["candidateProfile"]
        # 1D-0R 资产是不可改写的历史诊断证据，其中包含已经删除的 threshold/reranker
        # 字段。测试只验证其历史结论，禁止再把它构造成当前唯一 Schema v2 Policy。
        self.assertIn("rerankerFingerprint", value)
        self.assertIn("minimumProviderScore", value)
        self.assertEqual("field-semantic-v2", value["queryVersion"])
        self.assertIsNone(value["minimumProviderScore"])
        self.assertFalse(value["productionEligible"])
        self.assertFalse(value["minimumRerankerScoreCalibrated"])

    def test_query_policy_does_not_create_unreviewed_weak_tokens(self) -> None:
        query_policy = self.asset["repairImplementation"]["retrievalQuery"]

        self.assertFalse(query_policy["expandedPhraseAutoSplitIntoWeakTokens"])
        self.assertTrue(query_policy["asciiAnchorUsesWordBoundary"])

    def test_diagnostic_experiment_cannot_be_promoted_to_profile(self) -> None:
        experiment = self.asset["diagnosticTranslationAnchorExperiment"]

        self.assertEqual(7, experiment["positiveQueryCount"])
        self.assertEqual(6, experiment["positiveQueriesWithEvidence"])
        self.assertEqual(["propulsion"], experiment["missedPositiveQueryIds"])
        self.assertFalse(experiment["productionEligible"])

    def test_quality_gate_gap_is_recorded_as_provisional_only(self) -> None:
        calibration = self.asset["readOnlyRecalibrationAfterQualityGate"]

        self.assertGreater(
            calibration["minimumPositiveTopScore"],
            calibration["maximumNegativeTopScore"],
        )
        self.assertGreater(calibration["provisionalQueryLevelGap"], 0.0)
        self.assertIsNone(
            calibration["positiveExpectedTermFirstRankAfterQuality"][
                "propulsion"
            ]
        )
        self.assertTrue(calibration["groundTruthSupersededByIsolatedCalibration"])
        self.assertFalse(calibration["productionEligible"])

    def test_calibration_queries_have_ground_truth_classes(self) -> None:
        queries = self.asset["calibrationQueries"]
        ids = [item["id"] for item in queries]
        labels = {item["label"] for item in queries}

        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(labels, {"positive", "negative"})
        self.assertIn("missile-launcher", ids)
        self.assertNotIn("wrong-missile", ids)

    def test_calibration_script_has_only_read_operations(self) -> None:
        tree = ast.parse(
            CALIBRATION_SCRIPT.read_text(encoding="utf-8"),
            filename=str(CALIBRATION_SCRIPT),
        )
        called_attributes = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        forbidden = {
            "add_documents",
            "create_thread",
            "create_workspace",
            "delete_document",
            "delete_workspace",
            "update_embeddings",
            "update_workspace",
            "upload_document",
        }

        self.assertTrue(
            {"list_documents", "vector_search"}.issubset(
                called_attributes
            )
        )
        self.assertFalse(forbidden & called_attributes)

    def test_calibration_does_not_turn_remote_failure_into_zero_hits(self) -> None:
        workspace_client = Mock()
        workspace_client.vector_search.side_effect = RuntimeError("remote failed")
        query = CalibrationQuery(
            query_id="failure-probe",
            label="unknown",
            text="字段：故障探针",
            expected_terms=(),
        )

        with self.assertRaisesRegex(RuntimeError, "remote failed"):
            _query_result(
                workspace_client,
                workspace_slug="redacted-workspace",
                query=query,
                top_n=10,
                user_id=1,
            )

    def test_calibration_output_contains_hashes_not_chunk_body(self) -> None:
        workspace_client = Mock()
        workspace_client.vector_search.return_value = [
            AnythingLLMSource(
                document_ref="name:redacted.pdf",
                text="舰级名称为尼米兹级，正文只用于验证脱敏输出。",
                score=0.9,
            )
        ]
        query = CalibrationQuery(
            query_id="safe-output",
            label="positive",
            text="字段：舰级名称",
            expected_terms=("尼米兹级",),
        )

        result, digests = _query_result(
            workspace_client,
            workspace_slug="redacted-workspace",
            query=query,
            top_n=10,
            user_id=1,
        )

        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("正文只用于验证脱敏输出", serialized)
        self.assertEqual(digests[0], result["candidates"][0]["contentHash"])

    def test_exit_gate_requires_remote_reindex_and_representative_corpus(self) -> None:
        eligibility = self.asset["productionEligibility"]
        stage_exit = self.asset["stageExit"]

        self.assertIn(
            "reacquire-original-mhtml-or-equivalent-source-and-reindex-from-mhtml-main-content-v1-with-a-new-execution",
            eligibility["requiredBeforeApproval"],
        )
        self.assertIn(
            "user-approved-representative-multi-document-corpus",
            eligibility["requiredBeforeApproval"],
        )
        self.assertTrue(stage_exit["isolatedTemporaryReindexComplete"])
        self.assertFalse(stage_exit["existingSourceFaithfulReindexComplete"])
        self.assertFalse(stage_exit["remoteReindexComplete"])
        self.assertFalse(stage_exit["stageCanClose"])

    def test_isolated_reindex_was_cleaned_without_promoting_production_profile(self) -> None:
        calibration = self.asset["isolatedCleanedCopyCalibration"]
        retrieval = calibration["retrieval"]
        cleanup = calibration["cleanup"]

        self.assertEqual(4, calibration["executionCount"])
        self.assertEqual(2, calibration["postMetadataFixExecutionCount"])
        self.assertFalse(calibration["source"]["equivalentToMhtmlMainContentV1"])
        self.assertEqual(0, retrieval["referenceLikeCandidatesPerQuery"])
        self.assertGreater(
            retrieval["minimumPositiveTopScore"],
            retrieval["maximumNegativeTopScore"],
        )
        self.assertEqual(
            {1},
            set(retrieval["positiveExpectedTermFirstRankAfterQuality"].values()),
        )
        self.assertTrue(cleanup["baselineSnapshotRestoredForAllExecutions"])
        self.assertEqual(0, cleanup["temporaryWorkspacePrefixCountAfterCleanup"])
        self.assertEqual(0, cleanup["executionTokenResidualNameCountAfterCleanup"])
        repeatability = calibration["postFixRepeatability"]
        self.assertTrue(repeatability["candidateContentHashSetsIdenticalForAllQueries"])
        self.assertFalse(repeatability["candidateOrderIdenticalForAllQueries"])
        self.assertGreater(repeatability["maximumTopScoreDrift"], 0.0)
        self.assertFalse(calibration["productionEligible"])


if __name__ == "__main__":
    unittest.main()
