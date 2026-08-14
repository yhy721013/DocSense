"""阶段 2-5 Weaponry v2 受理、Execution Runtime 与终态原子链验收。"""

from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from app.modules.tasks.adapters import (
    CodecTaskExecutionSnapshotLoader,
    SQLiteTaskControlReadAdapter,
)
from app.modules.tasks.adapters.sqlite import (
    SQLiteCallbackControlStore,
    SQLiteConnectionFactory,
    SQLiteTaskControlStore,
    SQLiteTransactionManager,
    build_sqlite_task_control_uow_factories,
)
from app.modules.tasks.application import TaskExecutionRuntime
from app.modules.tasks.domain import (
    TaskId,
    TaskLeaseRuntimeSettings,
    TaskOwnerIdentity,
)
from app.modules.tasks.ports import (
    TaskExecutionRuntimeOutcome,
)
from app.modules.weaponry.adapters import (
    SQLiteWeaponryV2CallbackRecoverySource,
    TaskControlWeaponryCallbackAdapter,
    WeaponryTaskCommandCodec,
    WeaponryV2ResultMetrics,
)
from app.modules.weaponry.adapters.sqlite import (
    SQLiteWeaponryAdmissionUnitOfWorkFactory,
    SQLiteWeaponryCreationIntentStoreAdapter,
    SQLiteWeaponryExecutionUnitOfWorkFactory,
    SQLiteWeaponryInteractionAuditAdapter,
    SQLiteWeaponryResourceStoreAdapter,
    SQLiteWeaponryResultSnapshotStore,
    SQLiteWeaponryTaskDocumentSnapshotStore,
    bootstrap_weaponry_task_control_database,
)
from app.modules.weaponry.application import (
    RunWeaponryOutcome,
    RunWeaponryResult,
    RunWeaponryV2Workflow,
    SubmitWeaponryV2Task,
    WeaponryFieldExecutor,
    WeaponryStepRuntime,
)
from app.modules.weaponry.domain import (
    EVIDENCE_SCORE_MODE_SCORE,
    EvidenceCandidate,
)
from app.modules.weaponry.ports import (
    ExtractionAnswer,
    ExtractionValidationOutcome,
    TargetEvidenceSearchResult,
    WeaponryCallbackDeliveryOutcome,
    WeaponryCallbackDeliveryResult,
    WeaponryCallIdentity,
    WeaponryExternalOperationError,
    WeaponryExternalOutcome,
    WeaponryOperation,
    WeaponryTranslationOutcome,
    WeaponryTranslationResult,
)
from tests import workspace_tempdir
from tests.fakes import (
    FakeAuxiliaryGuidancePort,
    FakeClock,
    FakeEvidenceExtractionPort,
    FakeLeaseHeartbeatSupervisor,
    FakeTargetEvidenceRetrievalPort,
    FakeWeaponryCallbackPort,
    FakeWeaponryDispatcherPort,
    FakeWeaponryProgressPublisherPort,
    FakeWeaponryTranslationPort,
    FixedTaskLeaseTokenFactory,
    WeaponryInvocationRecorder,
)
from tests.test_weaponry_application import _submission


_T0 = "2026-08-14T03:00:00.000000Z"


class WeaponryV2RuntimeTests(unittest.TestCase):
    """只使用临时 SQLite/Fake，覆盖真实 v2 控制面而不启动后台线程。"""

    def setUp(self) -> None:
        self._directory = workspace_tempdir()
        root = Path(self._directory.__enter__())
        old_path = root / "old.sqlite3"
        sqlite3.connect(old_path).close()
        bootstrap = bootstrap_weaponry_task_control_database(
            old_path,
            root / "task-control.sqlite3",
        )
        self.manager = SQLiteTransactionManager(
            SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100)
        )
        self.task_uows = build_sqlite_task_control_uow_factories(self.manager)
        self.codec = WeaponryTaskCommandCodec()
        self.clock = FakeClock(_T0)
        self.recorder = WeaponryInvocationRecorder()
        self.progress = FakeWeaponryProgressPublisherPort(self.recorder)
        self.dispatcher = FakeWeaponryDispatcherPort(self.recorder)
        self.task_id = TaskId("weaponry-v2-runtime-task")
        self.submission = _submission(document_count=1)

        self.admission_uows = SQLiteWeaponryAdmissionUnitOfWorkFactory(
            self.manager,
            admission_builder=SQLiteTaskControlStore,
            callback_conflict_builder=SQLiteTaskControlStore,
            document_snapshot_builder=(
                SQLiteWeaponryTaskDocumentSnapshotStore.from_connection
            ),
        )
        self.execution_uows = SQLiteWeaponryExecutionUnitOfWorkFactory(
            self.manager,
            execution_builder=SQLiteTaskControlStore,
            callback_delivery_builder=SQLiteCallbackControlStore,
            document_snapshot_builder=(
                SQLiteWeaponryTaskDocumentSnapshotStore.from_connection
            ),
            creation_intent_builder=(
                SQLiteWeaponryCreationIntentStoreAdapter.from_connection
            ),
            interaction_audit_builder=(
                SQLiteWeaponryInteractionAuditAdapter.from_connection
            ),
            resource_builder=SQLiteWeaponryResourceStoreAdapter.from_connection,
            result_snapshot_builder=SQLiteWeaponryResultSnapshotStore.from_connection,
        )
        self.documents = SQLiteWeaponryTaskDocumentSnapshotStore(self.manager)
        self.results = SQLiteWeaponryResultSnapshotStore(self.manager)
        self.resources = SQLiteWeaponryResourceStoreAdapter(
            transaction_manager=self.manager,
        )
        self.audit = SQLiteWeaponryInteractionAuditAdapter(
            transaction_manager=self.manager,
        )

        submitted = SubmitWeaponryV2Task(
            admission_uow_factory=self.admission_uows,
            codec=self.codec,
            clock=self.clock,
            progress_publisher=self.progress,
            dispatcher=self.dispatcher,
            task_id_factory=lambda: self.task_id,
        ).execute(self.submission)
        self.assertEqual(self.task_id, submitted.task_id)

        self.retrieval = FakeTargetEvidenceRetrievalPort(
            self.recorder,
            enforce_call_order=False,
        )
        self.extraction = FakeEvidenceExtractionPort(
            self.recorder,
            enforce_call_order=False,
        )
        self.guidance = FakeAuxiliaryGuidancePort(
            self.recorder,
            enforce_call_order=False,
        )
        self.translation = FakeWeaponryTranslationPort(
            self.recorder,
            enforce_call_order=False,
        )
        self.callbacks = FakeWeaponryCallbackPort(self.recorder)
        self.callbacks.set_latest(self.task_id, self.submission.architecture_id)
        self.callbacks.delivery_results[self.task_id] = WeaponryCallbackDeliveryResult(
            WeaponryCallbackDeliveryOutcome.SUCCESS
        )
        self._configure_successful_field_calls()
        self.workflow = self._workflow()

    def tearDown(self) -> None:
        self._directory.__exit__(None, None, None)

    def _call(
        self,
        operation: WeaponryOperation,
        *,
        document_sequence: int | None = None,
        item_sequence: int | None = None,
    ) -> WeaponryCallIdentity:
        return WeaponryCallIdentity(
            task_id=self.task_id,
            field_sequence=1,
            document_sequence=document_sequence,
            operation=operation,
            attempt_no=1,
            item_sequence=item_sequence,
        )

    def _configure_successful_field_calls(self) -> None:
        profile = self.submission.evidence_selection_policy
        retrieval_call = self._call(WeaponryOperation.TARGET_RETRIEVAL)
        candidate = EvidenceCandidate(
            candidate_id="candidate-1",
            document_key="doc-a",
            # 使用足够长且与字段语义直接相关的正文，避免质量门禁把短句判定为
            # 标题式/引用式噪声；测试关注的是 v2 Runtime，不放宽 Evidence 规则。
            text=(
                "甲舰是甲级首舰，承担远洋警戒、防空指挥和编队协同等多项任务，"
                "其正式舰级名称为甲级。"
            ),
            provider_rank=1,
            provider_score=0.99,
            provider_score_present=True,
            score_profile_id=profile.profile_id,
        )
        self.retrieval.search_results[retrieval_call.attempt_key] = (
            TargetEvidenceSearchResult(
                scope_ref=f"fake-retrieval-scope:{self.task_id.value}",
                call=retrieval_call,
                candidates=(candidate,),
                score_mode=EVIDENCE_SCORE_MODE_SCORE,
                provider_fingerprint=profile.provider_fingerprint,
                embedding_fingerprint=profile.embedding_fingerprint,
            )
        )

        extraction_call = self._call(
            WeaponryOperation.EVIDENCE_EXTRACTION,
            document_sequence=1,
        )
        answer = "甲级"
        self.extraction.results[extraction_call.attempt_key] = ExtractionAnswer(
            call=extraction_call,
            text=answer,
            raw_response_digest=hashlib.sha256(answer.encode("utf-8")).hexdigest(),
            raw_response_chars=len(answer),
            evidence_ids=(candidate.candidate_id,),
            sources=(),
            validation_outcome=ExtractionValidationOutcome.MATCHED,
        )

        translation_call = self._call(
            WeaponryOperation.TRANSLATION,
            document_sequence=1,
            item_sequence=1,
        )
        self.translation.results[translation_call.attempt_key] = (
            WeaponryTranslationResult(
                call=translation_call,
                text="甲级",
                outcome=WeaponryTranslationOutcome.SUCCEEDED,
            )
        )

    def _workflow(self, *, callbacks=None) -> RunWeaponryV2Workflow:
        return RunWeaponryV2Workflow(
            steps=WeaponryStepRuntime(
                uow_factory=self.execution_uows,
                clock=self.clock,
            ),
            clock=self.clock,
            progress_publisher=self.progress,
            retrieval=self.retrieval,
            field_executor=WeaponryFieldExecutor(
                retrieval=self.retrieval,
                extraction=self.extraction,
                guidance=self.guidance,
                translation=self.translation,
                audit=self.audit,
            ),
            callbacks=callbacks or self.callbacks,
            resources=self.resources,
            document_snapshots=self.documents,
        )

    def _runtime(self, workflow: RunWeaponryV2Workflow) -> TaskExecutionRuntime:
        return TaskExecutionRuntime(
            task_type="weaponry",
            owner=TaskOwnerIdentity(
                instance_start_id="12345678-1234-4234-8234-123456789abc",
                process_id=405,
                executor_name="WeaponryExecutor",
                worker_slot="worker-0",
            ),
            clock=self.clock,
            execution_uow_factory=self.task_uows.execution,
            lease_token_factory=FixedTaskLeaseTokenFactory(("weaponry-lease",)),
            heartbeat_supervisor_factory=FakeLeaseHeartbeatSupervisor,
            workflow_runner=workflow,
            snapshot_loader=CodecTaskExecutionSnapshotLoader(
                query_uow_factory=self.task_uows.queries,
                codec=self.codec,
            ),
            lease_settings=TaskLeaseRuntimeSettings(),
        )

    def _task_and_steps(self):
        with self.execution_uows() as unit_of_work:
            task = unit_of_work.execution.get_task(self.task_id)
        with self.manager.begin(read_only=True) as transaction:
            rows = transaction.connection.execute(
                "SELECT step_key, state FROM task_steps WHERE task_id = ?",
                (self.task_id.value,),
            ).fetchall()
            transaction.commit()
        return task, {str(row["step_key"]): str(row["state"]) for row in rows}

    def test_full_runtime_commits_result_terminal_and_callback_eligibility(self) -> None:
        delivered_payloads: list[dict[str, object]] = []

        def deliver(payload: dict[str, object]) -> WeaponryCallbackDeliveryResult:
            delivered_payloads.append(payload)
            return WeaponryCallbackDeliveryResult(
                WeaponryCallbackDeliveryOutcome.SUCCESS,
                "http_status=200",
            )

        callbacks = TaskControlWeaponryCallbackAdapter(
            self.task_uows.callback_delivery,
            clock=self.clock,
            callback_url="https://callback.invalid/weaponry",
            callback_timeout=1.0,
            lease_seconds=30.0,
            token_factory=lambda: "weaponry-callback-lease",
            transport=deliver,
        )
        workflow = self._workflow(callbacks=callbacks)
        with patch(
            "app.modules.weaponry.adapters.v2_callback.save_callback_history_payload"
        ):
            runtime_result = self._runtime(workflow).run(self.task_id)

        self.assertIs(
            TaskExecutionRuntimeOutcome.WORKFLOW_RETURNED,
            runtime_result.outcome,
        )
        self.assertIs(RunWeaponryOutcome.SUCCEEDED, workflow.last_result.outcome)
        task, steps = self._task_and_steps()
        result = self.results.get(self.task_id)
        assert task is not None and result is not None
        self.assertEqual("succeeded", task.state.value)
        self.assertEqual(self.submission.architecture_id, result.payload.architecture_id)
        self.assertEqual("2", result.payload.status)
        self.assertEqual({"succeeded"}, set(steps.values()))
        self.assertIn("terminal.commit", steps)
        self.assertIn("result.map", steps)
        self.assertIn("field_model.execute:1:1:1", steps)
        self.assertIn("translation.execute:1:1:1", steps)
        self.assertIn(
            f"interaction_audit.commit:{self._call(WeaponryOperation.EVIDENCE_EXTRACTION, document_sequence=1).call_id}",
            steps,
        )
        self.assertEqual(1, len(self.extraction.calls))

        with self.manager.begin(read_only=True) as transaction:
            callback_state = transaction.connection.execute(
                "SELECT state, owner_execution_id, last_outcome "
                "FROM callback_delivery_guards "
                "WHERE business_type = 'weaponry' AND business_key = ?",
                (str(self.submission.architecture_id),),
            ).fetchone()
            attempt = transaction.connection.execute(
                "SELECT attempt_no, fencing_token, lease_token, lease_expires_at "
                "FROM task_attempts WHERE task_id = ?",
                (self.task_id.value,),
            ).fetchone()
            transaction.commit()
        assert callback_state is not None and attempt is not None
        self.assertEqual("idle", callback_state["state"])
        self.assertEqual(self.task_id.value, callback_state["owner_execution_id"])
        self.assertEqual("success", callback_state["last_outcome"])
        self.assertEqual([result.payload.to_public_dict()], delivered_payloads)
        self.assertEqual(1, attempt["attempt_no"])
        self.assertEqual(1, attempt["fencing_token"])
        self.assertEqual("weaponry-lease", attempt["lease_token"])
        self.assertTrue(str(attempt["lease_expires_at"]).endswith("Z"))

    def test_model_outcome_unknown_commits_audit_before_task_isolation(self) -> None:
        extraction_call = self._call(
            WeaponryOperation.EVIDENCE_EXTRACTION,
            document_sequence=1,
        )
        self.extraction.errors[extraction_call.attempt_key] = (
            WeaponryExternalOperationError(
                "model_response_outcome_unknown",
                "模拟模型请求发送后连接中断",
                outcome=WeaponryExternalOutcome.OUTCOME_UNKNOWN,
            )
        )

        runtime_result = self._runtime(self.workflow).run(self.task_id)

        self.assertIs(
            TaskExecutionRuntimeOutcome.WORKFLOW_RETURNED,
            runtime_result.outcome,
        )
        self.assertIs(
            RunWeaponryOutcome.RECOVERY_REQUIRED,
            self.workflow.last_result.outcome,
        )
        task, steps = self._task_and_steps()
        assert task is not None
        self.assertEqual("recovery_required", task.state.value)
        self.assertEqual(
            "outcome_unknown",
            steps["field_model.execute:1:1:1"],
        )
        self.assertEqual(
            "succeeded",
            steps[f"interaction_audit.commit:{extraction_call.call_id}"],
        )
        self.assertNotIn("terminal.commit", steps)
        self.assertIsNone(self.results.get(self.task_id))
        self.assertEqual(1, len(self.extraction.calls))

    def test_failed_callback_recovery_rebuilds_exact_terminal_payload(self) -> None:
        callbacks = TaskControlWeaponryCallbackAdapter(
            self.task_uows.callback_delivery,
            clock=self.clock,
            callback_url="https://callback.invalid/weaponry",
            callback_timeout=1.0,
            lease_seconds=30.0,
            token_factory=lambda: "weaponry-callback-failed-lease",
            transport=lambda _payload: WeaponryCallbackDeliveryResult(
                WeaponryCallbackDeliveryOutcome.DEFINITELY_NOT_SENT,
                "connect_timeout",
            ),
        )
        workflow = self._workflow(callbacks=callbacks)
        with patch(
            "app.modules.weaponry.adapters.v2_callback.save_callback_history_payload"
        ):
            runtime_result = self._runtime(workflow).run(self.task_id)

        self.assertIs(
            TaskExecutionRuntimeOutcome.WORKFLOW_RETURNED,
            runtime_result.outcome,
        )
        result = self.results.get(self.task_id)
        assert result is not None
        source = SQLiteWeaponryV2CallbackRecoverySource(
            task_reader=SQLiteTaskControlReadAdapter(self.manager),
            results=self.results,
        )

        candidate = source.load_recoverable(self.submission.architecture_id)

        assert candidate is not None
        self.assertEqual(self.task_id, candidate.task_id)
        self.assertEqual(result.payload, candidate.payload)
        self.assertEqual(1, candidate.callback_attempts)

    def _assert_audited_outcome_unknown(
        self,
        *,
        call: WeaponryCallIdentity,
        step_key: str,
    ) -> None:
        runtime_result = self._runtime(self.workflow).run(self.task_id)

        self.assertIs(
            TaskExecutionRuntimeOutcome.WORKFLOW_RETURNED,
            runtime_result.outcome,
        )
        self.assertIs(
            RunWeaponryOutcome.RECOVERY_REQUIRED,
            self.workflow.last_result.outcome,
        )
        task, steps = self._task_and_steps()
        assert task is not None
        self.assertEqual("recovery_required", task.state.value)
        self.assertEqual("outcome_unknown", steps[step_key])
        self.assertEqual(
            "succeeded",
            steps[f"interaction_audit.commit:{call.call_id}"],
        )
        self.assertNotIn("terminal.commit", steps)
        self.assertIsNone(self.results.get(self.task_id))

    def test_guidance_outcome_unknown_cannot_degrade_to_business_success(self) -> None:
        call = self._call(WeaponryOperation.AUXILIARY_GUIDANCE)
        self.guidance.errors[call.attempt_key] = WeaponryExternalOperationError(
            "guidance_outcome_unknown",
            "模拟辅助语境请求结果未知",
            outcome=WeaponryExternalOutcome.OUTCOME_UNKNOWN,
        )

        self._assert_audited_outcome_unknown(
            call=call,
            step_key="auxiliary_guidance.load:1",
        )

    def test_retrieval_outcome_unknown_cannot_degrade_to_empty_result(self) -> None:
        call = self._call(WeaponryOperation.TARGET_RETRIEVAL)
        self.retrieval.search_errors[call.attempt_key] = (
            WeaponryExternalOperationError(
                "retrieval_outcome_unknown",
                "模拟检索请求结果未知",
                outcome=WeaponryExternalOutcome.OUTCOME_UNKNOWN,
            )
        )

        self._assert_audited_outcome_unknown(
            call=call,
            step_key="retrieval.execute:1",
        )

    def test_translation_outcome_unknown_cannot_degrade_to_empty_text(self) -> None:
        call = self._call(
            WeaponryOperation.TRANSLATION,
            document_sequence=1,
            item_sequence=1,
        )
        self.translation.errors[call.attempt_key] = WeaponryExternalOperationError(
            "translation_outcome_unknown",
            "模拟翻译请求结果未知",
            outcome=WeaponryExternalOutcome.OUTCOME_UNKNOWN,
        )

        self._assert_audited_outcome_unknown(
            call=call,
            step_key="translation.execute:1:1:1",
        )

    def test_admission_rolls_back_task_when_document_snapshot_write_fails(self) -> None:
        rollback_task_id = TaskId("weaponry-v2-admission-rollback")

        class _FailingDocumentStore:
            def replace_for_task(self, **_kwargs):
                raise OSError("simulated document snapshot write failure")

            def list_for_task(self, _task_id):
                return ()

        failing_uows = SQLiteWeaponryAdmissionUnitOfWorkFactory(
            self.manager,
            admission_builder=SQLiteTaskControlStore,
            callback_conflict_builder=SQLiteTaskControlStore,
            document_snapshot_builder=lambda _connection: _FailingDocumentStore(),
        )
        submit = SubmitWeaponryV2Task(
            admission_uow_factory=failing_uows,
            codec=self.codec,
            clock=self.clock,
            progress_publisher=self.progress,
            dispatcher=self.dispatcher,
            task_id_factory=lambda: rollback_task_id,
        )

        with self.assertRaises(OSError):
            submit.execute(_submission(document_count=1, architecture_id=10503))

        with self.task_uows.queries() as unit_of_work:
            persisted = unit_of_work.queries.load_execution_input(rollback_task_id)
        self.assertIsNone(persisted)


class WeaponryV2ResultMetricsTests(unittest.TestCase):
    """隔离结果也必须进入执行健康度，避免 unknown 现场在指标中隐身。"""

    def test_recovery_required_counts_as_unsuccessful_execution(self) -> None:
        metrics = WeaponryV2ResultMetrics()
        metrics.observe(
            RunWeaponryResult(
                TaskId("weaponry-v2-metrics-recovery"),
                RunWeaponryOutcome.RECOVERY_REQUIRED,
                error_code="retrieval_outcome_unknown",
            )
        )

        (
            execution_count,
            execution_failures,
            succeeded,
            provider,
            business_zero,
            input_contract,
            other_failed,
        ) = metrics.snapshot()
        self.assertEqual(1, execution_count)
        self.assertEqual(1, execution_failures)
        self.assertEqual(0, succeeded)
        self.assertEqual(0, provider)
        self.assertEqual(0, business_zero)
        self.assertEqual(0, input_contract)
        self.assertEqual(0, other_failed)


if __name__ == "__main__":
    unittest.main()
