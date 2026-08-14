"""阶段 2-4 Report Input v2 Canonical Profile 与最终 Registry 合同测试。"""

from __future__ import annotations

import json
from pathlib import Path
import unittest

from app.modules.report.adapters import ReportTaskCommandCodec
from app.modules.report.application import (
    REPORT_RECOVERY_MATRICES,
    REPORT_STEP_REGISTRY,
    resolve_report_step,
)
from app.modules.report.domain import (
    REPORT_EMPTY_RESULT_POLICY,
    REPORT_EXECUTION_PROFILE_SCHEMA_NAME,
    REPORT_EXECUTION_PROFILE_SCHEMA_VERSION,
    REPORT_INPUT_SCHEMA_VERSION,
    REPORT_INPUT_SCHEMA_VERSION_V2,
    ReportDomainValidationError,
    ReportExecutionProfile,
    ReportId,
    ReportSubmission,
)
from app.modules.tasks.domain import RecoveryClassification, StepReplayPolicy, TaskBusinessRef, TaskId
from app.modules.tasks.ports import TaskSubmissionCommand


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _profile() -> ReportExecutionProfile:
    return ReportExecutionProfile(
        schema_name=REPORT_EXECUTION_PROFILE_SCHEMA_NAME,
        schema_version=REPORT_EXECUTION_PROFILE_SCHEMA_VERSION,
        source_transport_profile_id="report-http-source.v1",
        max_download_bytes=512 * 1024 * 1024,
        document_processing_profile_id="report-document-pipeline.v1",
        document_processing_fingerprint="1" * 64,
        template_extractor_profile_id="docx-template-text.v1",
        rag_provider_id="anythingllm",
        rag_provider_fingerprint="2" * 64,
        rag_model_fingerprint="3" * 64,
        rag_workspace_settings_fingerprint="4" * 64,
        rag_upload_policy_fingerprint="5" * 64,
        prompt_profile_id="report-prompt.v1",
        sanitizer_profile_id="report-public-sanitizer.v1",
        renderer_profile_id="report-html-renderer.v1",
        empty_result_policy=REPORT_EMPTY_RESULT_POLICY,
    )


def _submission() -> ReportSubmission:
    return ReportSubmission(
        report_id=ReportId.from_public_value(132),
        source_urls=("https://example.invalid/a.pdf", "https://example.invalid/b.mhtml"),
        template_outline_url="https://example.invalid/template.docx",
        template_desc="模板说明",
        requirement="报告要求",
        trace_id="trace-report-v2",
    )


def _command(schema_version: int) -> TaskSubmissionCommand[ReportSubmission]:
    submission = _submission()
    return TaskSubmissionCommand(
        task_type="report",
        business_ref=TaskBusinessRef("report", submission.report_id.business_key),
        input_schema_version=schema_version,
        submission=submission,
        trace_id=submission.trace_id,
    )


class ReportExecutionProfileTests(unittest.TestCase):
    def test_canonical_json_and_fingerprint_ignore_mapping_order(self) -> None:
        profile = _profile()
        reversed_mapping = dict(reversed(tuple(profile.to_dict().items())))
        decoded = ReportExecutionProfile.from_dict(reversed_mapping)
        self.assertEqual(profile, decoded)
        self.assertEqual(profile.canonical_json, decoded.canonical_json)
        self.assertEqual(profile.fingerprint, decoded.fingerprint)
        self.assertEqual(64, len(profile.fingerprint))

    def test_profile_rejects_unknown_fields_secrets_and_invalid_policy(self) -> None:
        payload = _profile().to_dict()
        payload["api_key"] = "secret"
        with self.assertRaisesRegex(ReportDomainValidationError, "字段集合"):
            ReportExecutionProfile.from_dict(payload)
        invalid = _profile().to_dict()
        invalid["empty_result_policy"] = "fail_on_empty"
        with self.assertRaisesRegex(ReportDomainValidationError, "empty_result_policy"):
            ReportExecutionProfile.from_dict(invalid)


class ReportInputV2CodecTests(unittest.TestCase):
    def test_v2_new_write_and_round_trip_include_exact_profile(self) -> None:
        profile = _profile()
        codec = ReportTaskCommandCodec.for_v2(profile)
        encoded = codec.encode_submission(
            _command(REPORT_INPUT_SCHEMA_VERSION_V2),
            task_id=TaskId("report-v2-task"),
            accepted_at="2026-08-13T00:00:00.000000Z",
        )
        self.assertEqual(REPORT_INPUT_SCHEMA_VERSION_V2, codec.write_schema_version)
        self.assertEqual(profile.to_dict(), encoded.input_payload["execution_profile"])
        decoded = codec.decode_input(
            schema_version=REPORT_INPUT_SCHEMA_VERSION_V2,
            payload=encoded.input_payload,
        )
        self.assertEqual(encoded.input_snapshot, decoded)
        self.assertEqual(profile.fingerprint, decoded.execution_profile.fingerprint)  # type: ignore[union-attr]

    def test_v1_read_is_strict_and_never_fills_current_profile(self) -> None:
        legacy_codec = ReportTaskCommandCodec()
        encoded = legacy_codec.encode_submission(
            _command(REPORT_INPUT_SCHEMA_VERSION),
            task_id=TaskId("report-v1-task"),
            accepted_at="2026-08-13T00:00:00.000000Z",
        )
        decoded_by_v2 = ReportTaskCommandCodec.for_v2(_profile()).decode_input(
            schema_version=REPORT_INPUT_SCHEMA_VERSION,
            payload=encoded.input_payload,
        )
        self.assertIsNone(decoded_by_v2.execution_profile)
        poisoned = dict(encoded.input_payload)
        poisoned["execution_profile"] = _profile().to_dict()
        with self.assertRaisesRegex(ValueError, "字段集合"):
            legacy_codec.decode_input(
                schema_version=REPORT_INPUT_SCHEMA_VERSION,
                payload=poisoned,
            )

    def test_codec_instances_cannot_silently_cross_write_schema(self) -> None:
        with self.assertRaisesRegex(ValueError, "Schema"):
            ReportTaskCommandCodec().encode_submission(
                _command(REPORT_INPUT_SCHEMA_VERSION_V2),
                task_id=TaskId("wrong-v1-writer"),
                accepted_at="2026-08-13T00:00:00.000000Z",
            )
        with self.assertRaisesRegex(ValueError, "Schema"):
            ReportTaskCommandCodec.for_v2(_profile()).encode_submission(
                _command(REPORT_INPUT_SCHEMA_VERSION),
                task_id=TaskId("wrong-v2-writer"),
                accepted_at="2026-08-13T00:00:00.000000Z",
            )


class ReportStepRegistryTests(unittest.TestCase):
    def test_code_registry_exactly_matches_frozen_machine_registry(self) -> None:
        contract = json.loads(
            (PROJECT_ROOT / "tests/contracts/stage2_business_step_registry.json").read_text(
                encoding="utf-8"
            )
        )["businesses"]["report"]["steps"]
        expected = {
            item["stepKey"]: (
                item["definitionVersion"],
                item["effectKind"],
                item["replayPolicy"],
                item["schemaRef"],
                item["recoveryMatrixRef"],
                item["successResultRef"],
            )
            for item in contract
        }
        actual = {
            item.key_pattern: (
                item.definition_version,
                item.effect_kind.value,
                item.replay_policy.value,
                item.schema_ref,
                item.recovery_matrix_ref,
                item.success_result_ref,
            )
            for item in REPORT_STEP_REGISTRY
        }
        self.assertEqual(expected, actual)

    def test_unknown_or_noncanonical_parameterized_steps_fail_closed(self) -> None:
        self.assertEqual(
            "source.download:{source_sequence}",
            resolve_report_step("source.download:1").key_pattern,
        )
        for step_key in ("source.download:0", "source.download:01", "source.download:-1", "rag.unknown"):
            with self.subTest(step_key=step_key):
                with self.assertRaises(ReportDomainValidationError):
                    resolve_report_step(step_key)

    def test_every_step_has_complete_recovery_matrix_and_generation_never_auto_replays(self) -> None:
        self.assertEqual(
            set(RecoveryClassification),
            set(REPORT_RECOVERY_MATRICES["external_write_reconcile.v1"].rules),
        )
        for definition in REPORT_STEP_REGISTRY:
            self.assertIn(definition.recovery_matrix_ref, REPORT_RECOVERY_MATRICES)
        generation = resolve_report_step("rag.generate")
        self.assertIs(StepReplayPolicy.NEVER_AUTO, generation.replay_policy)


if __name__ == "__main__":
    unittest.main()
