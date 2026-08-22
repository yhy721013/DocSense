"""阶段 2-6 步骤 1：Analysis Input v5 与 Canonical Profile 契约。"""

from __future__ import annotations

import copy
import unittest

from app.modules.analysis.adapters.task_codec import (
    AnalysisTaskInputCodec,
    AnalysisTaskInputCodecError,
)
from app.modules.analysis.domain.execution_profile import (
    ANALYSIS_EXECUTION_PROFILE_SCHEMA_NAME,
    ANALYSIS_EXECUTION_PROFILE_SCHEMA_VERSION,
    AnalysisExecutionProfile,
)
from app.modules.analysis.domain.task_inputs import (
    ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V5,
    AnalysisPolicySnapshot,
    AnalysisSubmissionSnapshot,
    AnalysisTaskInputV5,
)
from app.modules.translation.domain import (
    TranslationFailurePolicy,
    TranslationMode,
    TranslationProfile,
)


def _sha(character: str) -> str:
    return character * 64


def _execution_profile() -> AnalysisExecutionProfile:
    return AnalysisExecutionProfile(
        schema_name=ANALYSIS_EXECUTION_PROFILE_SCHEMA_NAME,
        schema_version=ANALYSIS_EXECUTION_PROFILE_SCHEMA_VERSION,
        source_transport_profile_id="http-source-v1",
        max_download_bytes=512 * 1024 * 1024,
        rag_provider_id="anythingllm",
        rag_provider_fingerprint=_sha("1"),
        rag_model_fingerprint=_sha("2"),
        rag_workspace_profile_id="analysis-workspace-v1",
        rag_projection_profile_id=_sha("3"),
        prompt_profile_id="analysis-prompts-v1",
        knowledge_provider_id="anythingllm",
        knowledge_provider_fingerprint=_sha("4"),
        knowledge_protocol_version="v1.15",
    )


def _translation_profile() -> TranslationProfile:
    return TranslationProfile.create(
        engine_id="fake-translation-engine",
        engine_fingerprint=_sha("5"),
        renderer_id="bilingual-html-v1",
        renderer_fingerprint=_sha("6"),
        mode=TranslationMode.MACHINE,
        failure_policy=TranslationFailurePolicy.PLACEHOLDER,
        parameters={"segmentationPolicy": "blank-line-v1"},
    )


def _task_input() -> AnalysisTaskInputV5:
    submission = AnalysisSubmissionSnapshot.from_request_params(
        {
            "fileName": "analysis-business-key.txt",
            "originalFileName": "原始文件.txt",
            "filePath": "https://example.invalid/source.txt",
            "unknownExtension": {"mustRemain": [1, 2, 3]},
        },
        policy_snapshot=AnalysisPolicySnapshot.default(),
    )
    return AnalysisTaskInputV5.from_submission(
        submission,
        task_id="analysis-v5-task",
        batch_id="a" * 32,
        batch_sequence=1,
        accepted_at="2026-08-15T10:00:00+08:00",
        trace_id="analysis-v5-trace",
        execution_profile=_execution_profile(),
        translation_profile=_translation_profile(),
    )


class AnalysisInputV5Tests(unittest.TestCase):
    def test_v5_round_trip_preserves_profiles_and_public_extensions(self) -> None:
        task_input = _task_input()
        payload = AnalysisTaskInputCodec.encode(task_input)
        decoded = AnalysisTaskInputCodec.decode(
            payload,
            expected_task_id=task_input.task_id,
            expected_business_key=task_input.file_name,
            expected_batch_id=task_input.batch_id,
        )

        self.assertEqual(ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V5, payload["schema_version"])
        self.assertEqual(task_input, decoded)
        self.assertEqual(
            {"mustRemain": [1, 2, 3]},
            decoded.raw_params.to_dict()["unknownExtension"],
        )
        self.assertEqual(_execution_profile().fingerprint, decoded.execution_profile.fingerprint)
        self.assertEqual(_translation_profile().profile_id, decoded.translation_profile.profile_id)

    def test_v5_rejects_profile_tampering_and_unknown_fields(self) -> None:
        payload = AnalysisTaskInputCodec.encode(_task_input())
        tampered = copy.deepcopy(payload)
        tampered["execution_profile"]["max_download_bytes"] = 0
        with self.assertRaises(AnalysisTaskInputCodecError):
            AnalysisTaskInputCodec.decode(tampered)

        unknown = copy.deepcopy(payload)
        unknown["translation_profile"]["futureField"] = True
        with self.assertRaises(AnalysisTaskInputCodecError):
            AnalysisTaskInputCodec.decode(unknown)

    def test_v1_to_v4_remain_readable_but_never_gain_profiles(self) -> None:
        # 历史读取的等价性由既有完整 Codec 回归覆盖；这里额外锁定 v4 envelope 不能
        # 偷带 Profile，否则旧 Worker 可能静默忽略新的执行决策。
        payload = AnalysisTaskInputCodec.encode(_task_input())
        payload["schema_version"] = 4
        with self.assertRaises(AnalysisTaskInputCodecError):
            AnalysisTaskInputCodec.decode(payload)


if __name__ == "__main__":
    unittest.main()
