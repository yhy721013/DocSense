"""阶段 1D-3B Weaponry 唯一 Schema v2 Codec、原子受理和任务隔离验收。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import patch

from app.adapters.web.flask.weaponry_requests import parse_weaponry_request
from app.modules.tasks.adapters import LegacyTaskCommandAdapter
from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.tasks.ports import (
    ExpectedTaskCompletion,
    TaskSubmissionCommand,
    TaskSubmissionOutcome,
)
from app.modules.weaponry.adapters import WeaponryTaskCommandCodec
from app.modules.weaponry.domain import (
    AUXILIARY_GUIDANCE_NONE,
    DOCUMENT_SCOPE_EXPLICIT,
    EVIDENCE_SCORE_SEMANTICS,
    EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1,
    EXTRACTION_PROMPT_VERSION,
    FILE_AGGREGATE_STRATEGY,
    MAX_TABLE_ROWS,
    TABLE_MERGE_POLICY_VERSION,
    WEAPONRY_INPUT_SCHEMA_VERSION,
    WEAPONRY_STATUS_SUCCEEDED,
    AuxiliaryGuidancePolicySnapshot,
    EvidenceSelectionPolicy,
    WeaponryDocumentScope,
    WeaponryDocumentSnapshot,
    WeaponryExecutionIdentity,
    WeaponryExecutionPolicySnapshot,
    WeaponryFieldResult,
    WeaponryInputSnapshot,
    WeaponryResult,
    WeaponrySubmission,
    WeaponryTableCellResult,
)
from app.services.core.database import DatabaseService
from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir


def _payload(
    architecture_id: int,
    marker: str,
    *,
    table: bool = False,
    field_count: int = 1,
) -> dict[str, object]:
    field: dict[str, object] = {
        "templateClassifyId": 1000 + architecture_id,
        "fieldName": f"字段-{marker}",
        "fieldType": "TABLE" if table else "INPUT",
        "fieldDescription": f"只提取 {marker} 对应内容",
        "analyseData": "",
        "analyseDataSource": [],
        "extension": {"marker": marker, "ordered": [3, 1, 2]},
    }
    if table:
        field["tableFieldList"] = [
            [
                {"fieldName": "型号", "fieldType": "INPUT"},
                {"fieldName": "用途", "fieldType": "INPUT"},
            ]
        ]
    fields = [field]
    for index in range(2, field_count + 1):
        additional = deepcopy(field)
        additional["templateClassifyId"] = 1000 + architecture_id + index - 1
        additional["fieldName"] = f"字段-{marker}-{index}"
        fields.append(additional)
    return {
        "businessType": "weaponry",
        "params": {
            "status": {"ignored": marker},
            "architectureId": f"{architecture_id:05d}",
            "filePathList": [f"http://files.local/{marker}.pdf?token=hidden"],
            "weaponryTemplateFieldList": fields,
            "unknownParam": {"marker": marker},
        },
        "unknownRoot": marker,
    }


def _profile(marker: str) -> EvidenceSelectionPolicy:
    # 这些 profile 仅用于离线结构/隔离测试，名称明确禁止被误当作生产阈值。
    return EvidenceSelectionPolicy(
        profile_id=f"test-only-stage1d2-{marker}",
        provider_fingerprint=f"test-provider-{marker}",
        embedding_fingerprint=f"test-embedding-{marker}",
        document_processing_fingerprint=f"test-processing-{marker}",
        input_candidate_top_n=9,
        table_candidate_top_n=17,
        reject_reference_like=True,
    )


def _execution_policy(marker: str) -> WeaponryExecutionPolicySnapshot:
    return WeaponryExecutionPolicySnapshot(
        extraction_strategy=FILE_AGGREGATE_STRATEGY,
        extraction_prompt_version=EXTRACTION_PROMPT_VERSION,
        extraction_context_strategy=EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1,
        extraction_model_fingerprint=f"test-extraction-model-{marker}",
        table_merge_policy_version=TABLE_MERGE_POLICY_VERSION,
        max_table_rows=MAX_TABLE_ROWS,
    )


def _auxiliary_policy() -> AuxiliaryGuidancePolicySnapshot:
    return AuxiliaryGuidancePolicySnapshot(
        policy_id=AUXILIARY_GUIDANCE_NONE,
        catalog_fingerprint="",
        top_n=0,
        max_context_chars=0,
    )


def _submission(
    architecture_id: int = 42,
    marker: str = "a",
    *,
    profile_marker: str | None = None,
    table: bool = False,
    field_count: int = 1,
) -> WeaponrySubmission:
    parsed = parse_weaponry_request(
        _payload(
            architecture_id,
            marker,
            table=table,
            field_count=field_count,
        )
    )
    document = WeaponryDocumentSnapshot(
        sequence_no=1,
        document_key=f"document-0001-{marker}",
        file_name=f"{marker}.pdf",
        original_name=f"甲方原名-{marker}.pdf",
        ingested_file_name=f"normalized-{marker}.pdf",
        source_architecture_id=architecture_id + 100,
        external_document_ref=f"custom-documents/{marker}.json",
        anything_document_id=f"anything-{marker}",
    )
    scope = WeaponryDocumentScope(
        mode=DOCUMENT_SCOPE_EXPLICIT,
        requested_file_names=parsed.selected_file_names,
        documents=(document,),
    )
    return parsed.to_submission(
        document_scope=scope,
        evidence_selection_policy=_profile(profile_marker or marker),
        execution_policy=_execution_policy(marker),
        auxiliary_guidance_policy=_auxiliary_policy(),
        trace_id=f"trace-{architecture_id}-{marker}",
    )


def _command(
    submission: WeaponrySubmission,
) -> TaskSubmissionCommand[WeaponrySubmission]:
    return TaskSubmissionCommand(
        task_type="weaponry",
        business_ref=TaskBusinessRef("weaponry", submission.business_key),
        input_schema_version=WEAPONRY_INPUT_SCHEMA_VERSION,
        submission=submission,
        trace_id=submission.trace_id,
    )


def _adapter(
    service: LLMTaskService,
) -> LegacyTaskCommandAdapter[WeaponrySubmission, WeaponryInputSnapshot, WeaponryResult]:
    return LegacyTaskCommandAdapter(service, WeaponryTaskCommandCodec())


class WeaponryTaskCodecTests(unittest.TestCase):
    def test_success_result_must_cover_snapshot_fields_in_order(self) -> None:
        codec = WeaponryTaskCommandCodec()
        encoded = codec.encode_submission(
            _command(_submission(field_count=2)),
            task_id=TaskId("weaponry-completeness-task"),
            accepted_at="2026-07-18T01:02:03+00:00",
        )
        snapshot = encoded.input_snapshot
        identity = WeaponryExecutionIdentity(snapshot.task_id, snapshot.architecture_id)

        with self.assertRaisesRegex(ValueError, "字段数量"):
            codec.validate_result(
                snapshot,
                WeaponryResult(
                    identity=identity,
                    status=WEAPONRY_STATUS_SUCCEEDED,
                    fields=(
                        WeaponryFieldResult(specification=snapshot.fields[0]),
                    ),
                ),
            )

        codec.validate_result(
            snapshot,
            WeaponryResult(
                identity=identity,
                status=WEAPONRY_STATUS_SUCCEEDED,
                fields=tuple(
                    WeaponryFieldResult(specification=field)
                    for field in snapshot.fields
                ),
            ),
        )

    def test_success_table_rows_must_have_every_snapshot_column_in_order(self) -> None:
        codec = WeaponryTaskCommandCodec()
        encoded = codec.encode_submission(
            _command(_submission(marker="table", table=True)),
            task_id=TaskId("weaponry-table-completeness-task"),
            accepted_at="2026-07-18T01:02:03+00:00",
        )
        snapshot = encoded.input_snapshot
        specification = snapshot.fields[0]
        malformed_row = (
            WeaponryTableCellResult(
                specification=specification.columns[0],
                analyse_data="AN/SPY-1",
                sources=(),
            ),
        )

        with self.assertRaisesRegex(ValueError, "列定义不完整"):
            codec.validate_result(
                snapshot,
                WeaponryResult(
                    identity=WeaponryExecutionIdentity(
                        snapshot.task_id,
                        snapshot.architecture_id,
                    ),
                    status=WEAPONRY_STATUS_SUCCEEDED,
                    fields=(
                        WeaponryFieldResult(
                            specification=specification,
                            table_rows=(malformed_row,),
                        ),
                    ),
                ),
            )

    def test_schema_v1_round_trip_freezes_all_execution_semantics(self) -> None:
        codec = WeaponryTaskCommandCodec()
        submission = _submission()
        encoded = codec.encode_submission(
            _command(submission),
            task_id=TaskId("weaponry-codec-task"),
            accepted_at="2026-07-18T01:02:03+00:00",
        )

        decoded = codec.decode_input(
            schema_version=WEAPONRY_INPUT_SCHEMA_VERSION,
            payload=encoded.input_payload,
        )

        self.assertEqual(encoded.input_snapshot, decoded)
        self.assertEqual(FILE_AGGREGATE_STRATEGY, decoded.extraction_strategy)
        self.assertEqual(AUXILIARY_GUIDANCE_NONE, decoded.auxiliary_guidance_policy_id)
        self.assertEqual("document-0001-a", decoded.document_scope.documents[0].document_key)
        self.assertEqual(
            EVIDENCE_SCORE_SEMANTICS,
            decoded.evidence_selection_policy.score_semantics,
        )
        self.assertEqual(9, decoded.evidence_selection_policy.input_candidate_top_n)
        self.assertEqual(17, decoded.evidence_selection_policy.table_candidate_top_n)
        self.assertEqual(
            "test-processing-a",
            decoded.evidence_selection_policy.document_processing_fingerprint,
        )
        self.assertEqual(
            EXTRACTION_PROMPT_VERSION,
            decoded.execution_policy.extraction_prompt_version,
        )
        self.assertEqual(
            "test-extraction-model-a",
            decoded.execution_policy.extraction_model_fingerprint,
        )
        self.assertEqual(AUXILIARY_GUIDANCE_NONE, decoded.auxiliary_guidance_policy.policy_id)
        self.assertNotIn("request_projection", encoded.input_payload)
        self.assertEqual(
            "00042",
            encoded.projection_request_payload["params"]["architectureId"],
        )
        json.dumps(encoded.input_payload, allow_nan=False)

    def test_strict_decoder_rejects_missing_unknown_mismatch_and_non_finite_values(self) -> None:
        codec = WeaponryTaskCommandCodec()
        encoded = codec.encode_submission(
            _command(_submission()),
            task_id=TaskId("weaponry-corrupt-task"),
            accepted_at="2026-07-18T01:02:03+00:00",
        )
        base = deepcopy(dict(encoded.input_payload))

        corrupted_payloads: list[dict[str, object]] = []
        missing_top = deepcopy(base)
        missing_top.pop("trace_id")
        corrupted_payloads.append(missing_top)
        unknown_top = deepcopy(base)
        unknown_top["future_default"] = True
        corrupted_payloads.append(unknown_top)
        mismatched_business_key = deepcopy(base)
        mismatched_business_key["business_key"] = "0042"
        corrupted_payloads.append(mismatched_business_key)
        missing_profile_key = deepcopy(base)
        missing_profile_key["evidence_selection_policy"].pop("provider_fingerprint")  # type: ignore[union-attr]
        corrupted_payloads.append(missing_profile_key)
        unknown_score_semantics = deepcopy(base)
        unknown_score_semantics["evidence_selection_policy"]["score_semantics"] = "distance-lower-is-better"  # type: ignore[index]
        corrupted_payloads.append(unknown_score_semantics)
        unknown_score_protocol = deepcopy(base)
        unknown_score_protocol["evidence_selection_policy"]["score_protocol"] = "unknown"  # type: ignore[index]
        corrupted_payloads.append(unknown_score_protocol)
        broken_sequence = deepcopy(base)
        broken_sequence["document_scope"]["documents"][0]["sequence_no"] = 2  # type: ignore[index]
        corrupted_payloads.append(broken_sequence)
        unknown_document_key = deepcopy(base)
        unknown_document_key["document_scope"]["documents"][0]["new_ref"] = "x"  # type: ignore[index]
        corrupted_payloads.append(unknown_document_key)
        non_empty_analysis = deepcopy(base)
        non_empty_analysis["field_templates"][0]["analyseData"] = "污染"  # type: ignore[index]
        corrupted_payloads.append(non_empty_analysis)
        boolean_payload_schema = deepcopy(base)
        boolean_payload_schema["schema_version"] = True
        corrupted_payloads.append(boolean_payload_schema)

        for payload in corrupted_payloads:
            with self.subTest(keys=tuple(payload)):
                with self.assertRaises((TypeError, ValueError)):
                    codec.decode_input(
                        schema_version=WEAPONRY_INPUT_SCHEMA_VERSION,
                        payload=payload,
                    )

    def test_schema_row_version_mismatch_is_rejected(self) -> None:
        codec = WeaponryTaskCommandCodec()
        encoded = codec.encode_submission(
            _command(_submission()),
            task_id=TaskId("weaponry-version-task"),
            accepted_at="2026-07-18T01:02:03+00:00",
        )
        payload = deepcopy(dict(encoded.input_payload))
        payload["schema_version"] = 1

        with self.assertRaisesRegex(ValueError, "Schema"):
            codec.decode_input(
                schema_version=WEAPONRY_INPUT_SCHEMA_VERSION,
                payload=payload,
            )
        with self.assertRaisesRegex(ValueError, "不支持"):
            codec.decode_input(schema_version=1, payload=payload)
        with self.assertRaisesRegex(ValueError, "不支持"):
            codec.decode_input(schema_version=True, payload=payload)

    def test_result_codec_keeps_internal_fact_separate_from_public_callback(self) -> None:
        result = WeaponryResult(
            identity=WeaponryExecutionIdentity(
                task_id="weaponry-result-task",
                architecture_id=42,
            ),
            status=WEAPONRY_STATUS_SUCCEEDED,
            fields=(
                WeaponryFieldResult(
                    specification=_submission().fields[0],
                ),
            ),
        )

        encoded = WeaponryTaskCommandCodec().encode_result(result)

        self.assertEqual(
            {"schema_version", "task_id", "architecture_id", "status", "message"},
            set(encoded.execution_result_payload),
        )
        self.assertEqual(
            WEAPONRY_INPUT_SCHEMA_VERSION,
            encoded.execution_result_payload["schema_version"],
        )
        self.assertEqual(
            {"businessType", "data", "msg"},
            set(encoded.projection_result_payload),
        )
        self.assertNotIn("task_id", encoded.projection_result_payload)


class WeaponryTaskAdapterPersistenceTests(unittest.TestCase):
    def test_result_status_must_match_completion_terminal_metadata(self) -> None:
        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            adapter = _adapter(service)
            created = adapter.create_if_allowed(_command(_submission()))
            assert created.execution is not None
            claimed = adapter.claim(created.execution.task_id)
            assert claimed.execution is not None
            snapshot = claimed.execution.input_snapshot

            with self.assertRaisesRegex(ValueError, "状态与"):
                adapter.finish_if_current(
                    ExpectedTaskCompletion(
                        expected_task_id=claimed.execution.task_id,
                        business_ref=claimed.execution.business_ref,
                        execution_state="failed",
                        public_status="3",
                        message="错误的终态组合",
                        result=WeaponryResult(
                            identity=WeaponryExecutionIdentity(
                                task_id=snapshot.task_id,
                                architecture_id=snapshot.architecture_id,
                            ),
                            status=WEAPONRY_STATUS_SUCCEEDED,
                            fields=(
                                WeaponryFieldResult(
                                    specification=snapshot.fields[0],
                                ),
                            ),
                        ),
                    )
                )
            reloaded = adapter.get_execution(claimed.execution.task_id)

        assert reloaded is not None
        self.assertEqual("running", reloaded.execution_state)

    def test_incomplete_success_result_cannot_reach_terminal_cas(self) -> None:
        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            adapter = _adapter(service)
            created = adapter.create_if_allowed(
                _command(_submission(field_count=2))
            )
            assert created.execution is not None
            claimed = adapter.claim(created.execution.task_id)
            assert claimed.execution is not None

            with self.assertRaisesRegex(ValueError, "字段数量"):
                adapter.finish_if_current(
                    ExpectedTaskCompletion(
                        expected_task_id=claimed.execution.task_id,
                        business_ref=claimed.execution.business_ref,
                        execution_state="succeeded",
                        public_status="2",
                        message="不完整结果",
                        result=WeaponryResult(
                            identity=WeaponryExecutionIdentity(
                                task_id=claimed.execution.task_id.value,
                                architecture_id=42,
                            ),
                            status=WEAPONRY_STATUS_SUCCEEDED,
                            fields=(
                                WeaponryFieldResult(
                                    specification=claimed.execution.input_snapshot.fields[0]
                                ),
                            ),
                        ),
                    )
                )
            reloaded = adapter.get_execution(claimed.execution.task_id)

        assert reloaded is not None
        self.assertEqual("running", reloaded.execution_state)

    def test_atomic_acceptance_persists_internal_snapshot_without_old_selection_table(self) -> None:
        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            service = LLMTaskService(database)
            adapter = _adapter(service)

            created = adapter.create_if_allowed(_command(_submission()))
            assert created.execution is not None
            projection = service.get_task("weaponry", "42")
            raw_execution = service.get_task_execution(created.execution.task_id.value)
            with sqlite3.connect(database) as connection:
                legacy_snapshot_count = connection.execute(
                    "SELECT COUNT(*) FROM weaponry_task_document_snapshots"
                ).fetchone()[0]

        self.assertEqual(TaskSubmissionOutcome.ACCEPTED, created.outcome)
        self.assertEqual(created.execution, adapter.get_execution(created.execution.task_id))
        assert projection is not None and raw_execution is not None
        self.assertEqual("00042", projection["request_payload"]["params"]["architectureId"])
        self.assertEqual("1", projection["status"])
        self.assertNotIn("request_projection", raw_execution["input_payload"])
        self.assertEqual(
            "document-0001-a",
            raw_execution["input_payload"]["document_scope"]["documents"][0]["document_key"],
        )
        self.assertEqual(0, legacy_snapshot_count)

    def test_acceptance_transaction_does_not_requery_documents_or_read_environment(self) -> None:
        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            adapter = _adapter(service)
            submission = _submission()

            with patch.object(
                DatabaseService,
                "list_document_records",
                side_effect=AssertionError("受理事务不得解析文档范围"),
            ), patch.dict(
                os.environ,
                {
                    "WEAPONRY_RETRIEVAL_PROFILE": "changed-after-snapshot",
                    "WEAPONRY_PROVIDER_FINGERPRINT": "changed-provider",
                },
                clear=False,
            ):
                created = adapter.create_if_allowed(_command(submission))
                assert created.execution is not None
                reloaded = adapter.get_execution(created.execution.task_id)

        assert reloaded is not None
        self.assertEqual(submission.evidence_selection_policy, reloaded.input_snapshot.evidence_selection_policy)

    def test_projection_failure_rolls_back_execution_without_orphan_or_legacy_snapshot(self) -> None:
        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            service = LLMTaskService(database)
            adapter = _adapter(service)
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TRIGGER reject_weaponry_projection
                    BEFORE INSERT ON llm_tasks
                    WHEN NEW.business_type = 'weaponry'
                    BEGIN
                        SELECT RAISE(ABORT, 'forced weaponry projection failure');
                    END
                    """
                )

            with self.assertRaises(sqlite3.IntegrityError):
                adapter.create_if_allowed(_command(_submission()))

            with sqlite3.connect(database) as connection:
                execution_count = connection.execute(
                    "SELECT COUNT(*) FROM llm_task_executions WHERE business_type = 'weaponry'"
                ).fetchone()[0]
                projection_count = connection.execute(
                    "SELECT COUNT(*) FROM llm_tasks WHERE business_type = 'weaponry'"
                ).fetchone()[0]
                legacy_snapshot_count = connection.execute(
                    "SELECT COUNT(*) FROM weaponry_task_document_snapshots"
                ).fetchone()[0]
        self.assertEqual(0, execution_count)
        self.assertEqual(0, projection_count)
        self.assertEqual(0, legacy_snapshot_count)

    def test_new_same_key_execution_cannot_change_old_input_or_profile(self) -> None:
        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            adapter = _adapter(service)
            first = adapter.create_if_allowed(
                _command(_submission(42, "old", profile_marker="old-profile"))
            )
            assert first.execution is not None
            adapter.claim(first.execution.task_id)
            self.assertTrue(
                adapter.finish_if_current(
                    ExpectedTaskCompletion(
                        expected_task_id=first.execution.task_id,
                        business_ref=first.execution.business_ref,
                        execution_state="succeeded",
                        public_status="2",
                        message="第一次解析完成",
                        result=WeaponryResult(
                            identity=WeaponryExecutionIdentity(
                                task_id=first.execution.task_id.value,
                                architecture_id=42,
                            ),
                            status=WEAPONRY_STATUS_SUCCEEDED,
                            fields=(
                                WeaponryFieldResult(
                                    specification=first.execution.input_snapshot.fields[0],
                                ),
                            ),
                        ),
                    )
                )
            )
            second = adapter.create_if_allowed(
                _command(_submission(42, "new", profile_marker="new-profile"))
            )
            assert second.execution is not None

            first_reloaded = adapter.get_execution(first.execution.task_id)
            second_reloaded = adapter.get_execution(second.execution.task_id)

        assert first_reloaded is not None and second_reloaded is not None
        self.assertNotEqual(first_reloaded.task_id, second_reloaded.task_id)
        self.assertEqual(
            "test-only-stage1d2-old-profile",
            first_reloaded.input_snapshot.evidence_selection_policy.profile_id,
        )
        self.assertEqual(
            "test-only-stage1d2-new-profile",
            second_reloaded.input_snapshot.evidence_selection_policy.profile_id,
        )
        self.assertEqual(
            "old.pdf",
            first_reloaded.input_snapshot.document_scope.documents[0].file_name,
        )
        self.assertEqual(
            "new.pdf",
            second_reloaded.input_snapshot.document_scope.documents[0].file_name,
        )
        self.assertEqual("字段-old", first_reloaded.input_snapshot.fields[0].field_name)
        self.assertEqual("字段-new", second_reloaded.input_snapshot.fields[0].field_name)


class WeaponryTaskAdapterConcurrencyTests(unittest.TestCase):
    """使用精确 Barrier 验证 50 并发，不以随机 sleep 推测并发。"""

    def test_fifty_same_keys_have_one_accepted_and_forty_nine_conflicts(self) -> None:
        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            service = LLMTaskService(database)
            adapter = _adapter(service)
            command = _command(_submission())
            barrier = threading.Barrier(50)

            def submit() -> TaskSubmissionOutcome:
                barrier.wait(timeout=20)
                return adapter.create_if_allowed(command).outcome

            with ThreadPoolExecutor(max_workers=50) as executor:
                futures = [executor.submit(submit) for _ in range(50)]
                outcomes = [future.result(timeout=60) for future in futures]

            with sqlite3.connect(database) as connection:
                execution_count = connection.execute(
                    "SELECT COUNT(*) FROM llm_task_executions WHERE business_type = 'weaponry'"
                ).fetchone()[0]
                projection_count = connection.execute(
                    "SELECT COUNT(*) FROM llm_tasks WHERE business_type = 'weaponry'"
                ).fetchone()[0]
                legacy_snapshot_count = connection.execute(
                    "SELECT COUNT(*) FROM weaponry_task_document_snapshots"
                ).fetchone()[0]
                # 所有并发调用返回后必须能够立即取得新的写事务，显式证明没有连接或
                # BEGIN IMMEDIATE 锁泄漏。
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()

        self.assertEqual(1, outcomes.count(TaskSubmissionOutcome.ACCEPTED))
        self.assertEqual(49, outcomes.count(TaskSubmissionOutcome.ACTIVE_CONFLICT))
        self.assertEqual(1, execution_count)
        self.assertEqual(1, projection_count)
        self.assertEqual(0, legacy_snapshot_count)

    def test_fifty_distinct_tasks_keep_fields_documents_and_profiles_isolated(self) -> None:
        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            service = LLMTaskService(database)
            adapter = _adapter(service)
            commands = tuple(
                _command(_submission(1000 + index, f"doc-{index}"))
                for index in range(50)
            )
            barrier = threading.Barrier(50)

            def submit(index: int) -> tuple[int, TaskSubmissionOutcome, TaskId]:
                barrier.wait(timeout=20)
                created = adapter.create_if_allowed(commands[index])
                assert created.execution is not None
                return index, created.outcome, created.execution.task_id

            with ThreadPoolExecutor(max_workers=50) as executor:
                futures = [executor.submit(submit, index) for index in range(50)]
                accepted = [future.result(timeout=60) for future in futures]

            for index, outcome, task_id in accepted:
                self.assertEqual(TaskSubmissionOutcome.ACCEPTED, outcome)
                execution = adapter.get_execution(task_id)
                assert execution is not None
                marker = f"doc-{index}"
                self.assertEqual(f"字段-{marker}", execution.input_snapshot.fields[0].field_name)
                self.assertEqual(
                    f"{marker}.pdf",
                    execution.input_snapshot.document_scope.documents[0].file_name,
                )
                self.assertEqual(
                    f"test-only-stage1d2-{marker}",
                    execution.input_snapshot.evidence_selection_policy.profile_id,
                )
            with sqlite3.connect(database) as connection:
                counts = connection.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM llm_task_executions WHERE business_type='weaponry'),
                        (SELECT COUNT(*) FROM llm_tasks WHERE business_type='weaponry'),
                        (SELECT COUNT(*) FROM weaponry_task_document_snapshots)
                    """
                ).fetchone()

        self.assertEqual((50, 50, 0), counts)


if __name__ == "__main__":
    unittest.main()
