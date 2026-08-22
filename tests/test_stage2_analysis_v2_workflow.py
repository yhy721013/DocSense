"""阶段 2-6 步骤 5：真实 Analysis v2 Workflow 的离线 Authority 验证。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from app.modules.analysis.adapters import (
    AnalysisV5TaskCommandCodec,
    SQLiteAnalysisV2CallbackRecoverySource,
    SQLiteAnalysisV2BatchAdmissionAdapter,
    TaskControlAnalysisCallbackAdapter,
)
from app.modules.analysis.adapters.sqlite import (
    SQLiteAnalysisExecutionUnitOfWorkFactory,
    SQLiteAnalysisResultSnapshotStore,
    SQLiteAnalysisV2ResourceStoreAdapter,
    bootstrap_analysis_task_control_database,
)
from app.modules.analysis.application import (
    AnalysisStepRuntime,
    RecoverAnalysisResources,
    RecoverAnalysisCallbackSynchronously,
    RunAnalysisOutcome,
    RunAnalysisV2Workflow,
)
from app.modules.analysis.domain.task_inputs import (
    AnalysisPolicySnapshot,
    AnalysisSubmissionSnapshot,
    FrozenJsonArray,
    FrozenJsonObject,
)
from app.modules.analysis.ports import (
    AnalysisBatchCommand,
    AnalysisCallbackAcquireOutcome,
    AnalysisCallbackAcquireResult,
    AnalysisCallbackDelivery,
    AnalysisCallbackDeliveryOutcome,
    AnalysisCallbackGuardLease,
    AnalysisExecutionRef,
    AnalysisInteractionAuditReceipt,
    AnalysisKnowledgeWriteOutcome,
    AnalysisKnowledgeWriteResult,
    AnalysisRagCloseOutcome,
    AnalysisRagCloseResult,
    AnalysisRagLifecycleEvent,
    AnalysisRagLifecycleOutcome,
    AnalysisRagOperation,
    AnalysisRagResult,
    AnalysisRagSessionOpenResult,
    AnalysisRagSessionOpenError,
    AnalysisRagSessionOpenStage,
    AnalysisRagSessionRef,
    AnalysisRecallAuditReceipt,
    AnalysisTaskWorkspace,
    AnalysisTranslationOutcome,
    AnalysisTranslationResult,
    PreparedAnalysisDocument,
    AcquiredAnalysisSource,
)
from app.modules.document_processing.domain import (
    ArtifactKind,
    ArtifactMetadata,
    ArtifactRef,
    DocumentRepresentation,
)
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
from app.modules.tasks.domain import TaskId, TaskLeaseRuntimeSettings, TaskOwnerIdentity
from app.modules.tasks.ports import ProgressPublisherPort, TaskExecutionRuntimeOutcome
from tests import workspace_tempdir
from tests.fakes import FakeClock, FakeLeaseHeartbeatSupervisor, FixedTaskLeaseTokenFactory
from tests.fakes.analysis import (
    StrictAnalysisFakeScript,
    StrictAnalysisPortFake,
    StrictAnalysisRagFactoryFake,
    StrictAnalysisTaskWorkspaceFake,
)
from tests.test_stage2_analysis_v2_admission import (
    _IdentityFactory,
    _execution_profile,
    _translation_profile,
)


_T0 = "2026-08-15T00:00:00.000000Z"


class _ProgressSink(ProgressPublisherPort):
    def __init__(self) -> None:
        self.publications = []

    def publish(self, publication):  # type: ignore[no-untyped-def]
        self.publications.append(publication)


def _command() -> AnalysisBatchCommand:
    projection = FrozenJsonObject.from_mapping(
        {
            "businessType": "file",
            "params": [
                {
                    "fileName": "workflow-demo.txt",
                    "filePath": "https://example.invalid/workflow-demo.txt",
                    "architectureList": [
                        {
                            "id": 103,
                            "name": "装备型号",
                            "parentId": None,
                            "path": "103",
                            "pathName": "装备型号",
                            "remark": "装备型号资料",
                        }
                    ],
                }
            ],
        },
        name="analysis_v2_workflow_batch",
    )
    params = projection.get("params")
    assert isinstance(params, FrozenJsonArray)
    item = params.values[0]
    assert isinstance(item, FrozenJsonObject)
    submission = AnalysisSubmissionSnapshot.from_frozen_params(
        item,
        policy_snapshot=AnalysisPolicySnapshot.default(),
    )
    return AnalysisBatchCommand(projection, (submission,), "trace-analysis-v2-workflow")


class AnalysisV2WorkflowTests(unittest.TestCase):
    def _runtime(
        self,
        root: Path,
        *,
        mismatched_profile: bool = False,
        callback_url: str = "",
        callback_transport=None,  # type: ignore[no-untyped-def]
    ):
        old_path = root / "old.sqlite3"
        database_path = root / "task-control.sqlite3"
        sqlite3.connect(old_path).close()
        bootstrap = bootstrap_analysis_task_control_database(old_path, database_path)
        manager = SQLiteTransactionManager(
            SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100)
        )
        root_uows = build_sqlite_task_control_uow_factories(manager)
        clock = FakeClock(_T0)
        profile = _execution_profile()
        translation_profile = _translation_profile()
        codec = AnalysisV5TaskCommandCodec(
            execution_profile=profile,
            translation_profile=translation_profile,
        )
        admission = SQLiteAnalysisV2BatchAdmissionAdapter(
            admission_uow_factory=root_uows.admission,
            codec=codec,
            clock=clock,
            task_id_factory=_IdentityFactory().task_id,
            batch_id_factory=lambda: "1" * 32,
        ).create_batch_if_allowed(_command())
        task_id = admission.executions[0].task_id
        execution = AnalysisExecutionRef(
            task_id,
            "workflow-demo.txt",
            "1" * 32,
            1,
        )
        script = StrictAnalysisFakeScript()
        ports = StrictAnalysisPortFake(script)
        progress = _ProgressSink()
        steps = AnalysisStepRuntime(
            uow_factory=SQLiteAnalysisExecutionUnitOfWorkFactory(
                manager,
                execution_builder=SQLiteTaskControlStore,
                callback_delivery_builder=SQLiteCallbackControlStore,
                resource_builder=SQLiteAnalysisV2ResourceStoreAdapter.from_connection,
                result_snapshot_builder=SQLiteAnalysisResultSnapshotStore.from_connection,
            ),
            clock=clock,
        )
        active_profile = replace(profile, prompt_profile_id="mismatch-v1") if mismatched_profile else profile
        callback = TaskControlAnalysisCallbackAdapter(
            root_uows.callback_delivery,
            clock=clock,
            callback_timeout=1.0,
            lease_seconds=30.0,
            token_factory=lambda: "analysis-v2-callback-lease",
            transport=callback_transport,
        )
        workflow = RunAnalysisV2Workflow(
            steps=steps,
            progress_publisher=progress,
            workspaces=StrictAnalysisTaskWorkspaceFake(script),
            files=ports,
            rag_factory=StrictAnalysisRagFactoryFake(script, ports),
            knowledge=ports,
            audit=ports,
            translation=ports,
            resources=SQLiteAnalysisV2ResourceStoreAdapter(manager),
            callbacks=callback,
            # 空地址仍需经过 Task Control Guard 并收敛为 skipped；测试不触网。
            callback_url=callback_url,
            execution_profile=active_profile,
            translation_profile=translation_profile,
        )
        runtime = TaskExecutionRuntime(
            task_type="file",
            owner=TaskOwnerIdentity(
                instance_start_id="12345678-1234-4234-8234-123456789abc",
                process_id=100,
                executor_name="file",
                worker_slot="analysis-v2-workflow",
            ),
            clock=clock,
            execution_uow_factory=root_uows.execution,
            lease_token_factory=FixedTaskLeaseTokenFactory(("analysis-v2-lease-token",)),
            heartbeat_supervisor_factory=lambda: FakeLeaseHeartbeatSupervisor(),
            workflow_runner=workflow,
            snapshot_loader=CodecTaskExecutionSnapshotLoader(
                query_uow_factory=root_uows.queries,
                codec=codec,
            ),
            lease_settings=TaskLeaseRuntimeSettings(
                lease_duration_seconds=60.0,
                heartbeat_interval_seconds=10.0,
                stop_grace_seconds=15.0,
            ),
        )
        return database_path, task_id, execution, script, workflow, runtime, progress

    @staticmethod
    def _expect_happy_workflow(
        script: StrictAnalysisFakeScript,
        execution: AnalysisExecutionRef,
        *,
        close_audit_result: object = None,
    ) -> None:
        """登记一条完整成功脚本，供终态与 Callback handoff 用例共享。"""

        key = execution.task_id.value
        prepared_artifact = ArtifactRef(
            task_id=execution.task_id,
            artifact_id="a" * 64,
            step_key="b" * 64,
            kind=ArtifactKind.PREPARED,
            representation=DocumentRepresentation.MARKDOWN,
            metadata=ArtifactMetadata("text/markdown; charset=utf-8", 20, "c" * 64),
        )
        rag_artifact = ArtifactRef(
            task_id=execution.task_id,
            artifact_id="d" * 64,
            step_key="e" * 64,
            kind=ArtifactKind.RAG_PROJECTION,
            representation=DocumentRepresentation.MARKDOWN,
            metadata=ArtifactMetadata("text/markdown; charset=utf-8", 20, "f" * 64),
        )
        prepared = PreparedAnalysisDocument(
            execution=execution,
            source_path="C:/analysis/source.txt",
            processing_path="C:/analysis/prepared.md",
            upload_path="C:/analysis/prepared.md",
            original_text="装备型号资料\n摘要正文",
            internal_prepared_basename="prepared.md",
            prepared_artifact=prepared_artifact,
            rag_upload_artifact=rag_artifact,
            rag_projection_profile_id=_execution_profile().rag_projection_profile_id,
            source_sha256="9" * 64,
        )
        pending = AnalysisRagSessionRef(
            execution,
            "context:v2::conversation:v2",
            "context:v2",
            "conversation:v2",
        )
        bound = pending.with_bound_document(
            document_ref="document:v2",
            document_location="location:v2",
            content_sha256="f" * 64,
            ingested_file_name="workflow-demo.md",
            structured_source_key="docsense_ref:" + "7" * 32,
        )
        open_events = (
            AnalysisRagLifecycleEvent(1, "context_create", 1, AnalysisRagLifecycleOutcome.SUCCEEDED, "context:v2"),
            AnalysisRagLifecycleEvent(2, "conversation_create", 1, AnalysisRagLifecycleOutcome.SUCCEEDED, "conversation:v2"),
        )
        execute_events = (
            AnalysisRagLifecycleEvent(3, "document_upload", 1, AnalysisRagLifecycleOutcome.SUCCEEDED, "location:v2"),
            AnalysisRagLifecycleEvent(4, "document_bind", 1, AnalysisRagLifecycleOutcome.SUCCEEDED, "document:v2"),
        )
        script.expect_for(key, "workspace.create", AnalysisTaskWorkspace(execution, "C:/analysis/task-v2"))
        script.expect_for(
            key,
            "file.acquire",
            AcquiredAnalysisSource(execution, "C:/analysis/source.txt", "source.txt", "9" * 64),
        )
        script.expect_for(key, "file.prepare_document", prepared)
        recall = AnalysisRecallAuditReceipt(execution, f"analysis-recall:{key}", "recall:v2", 0)
        script.expect_for(key, "audit.reserve_recall", recall)
        script.expect_for(key, "rag.factory.create", None)
        script.expect_for(key, "rag.open_session", AnalysisRagSessionOpenResult(pending, open_events))
        script.expect_for(
            key,
            "rag.execute",
            AnalysisRagResult(
                execution,
                bound,
                AnalysisRagOperation.EXTRACTION,
                1,
                '{"architectureId":103,"fileDataItem":{"summary":"摘要","keyword":"装备"}}',
                lifecycle_events=execute_events,
            ),
        )
        script.expect_for(
            key,
            "audit.finalize_recall",
            AnalysisRecallAuditReceipt(execution, recall.idempotency_key, recall.audit_id, 1, True),
        )
        script.expect_for(
            key,
            "audit.persist_interaction",
            AnalysisInteractionAuditReceipt(execution, f"analysis-rag:{key}", "interaction:v2"),
        )
        knowledge_key = "document:v1:" + __import__("hashlib").sha256(
            f"workflow-demo.txt\0{103}\0{'f' * 64}".encode("utf-8")
        ).hexdigest()
        script.expect_for(
            key,
            "knowledge.persist",
            AnalysisKnowledgeWriteResult(
                execution,
                knowledge_key,
                AnalysisKnowledgeWriteOutcome.COMMITTED,
                external_ref="knowledge:v2",
            ),
        )
        script.expect_for(
            key,
            "translation.translate",
            AnalysisTranslationResult(
                execution,
                AnalysisTranslationOutcome.SUCCEEDED,
                document_translation_one="单语",
                document_translation_two="双语",
            ),
        )
        close_event = AnalysisRagLifecycleEvent(
            5,
            "context_delete",
            1,
            AnalysisRagLifecycleOutcome.SUCCEEDED,
            "context:v2",
        )
        script.expect_for(
            key,
            "rag.close_session",
            AnalysisRagCloseResult(
                execution,
                bound,
                AnalysisRagCloseOutcome.CONFIRMED,
                (close_event,),
            ),
        )
        script.expect_for(key, "audit.append_lifecycle_events", close_audit_result)

    def test_happy_path_uses_v5_profile_all_steps_and_atomic_terminal_snapshot(self) -> None:
        with workspace_tempdir() as temporary_root:
            root = Path(temporary_root)
            database_path, task_id, execution, script, workflow, runtime, progress = self._runtime(root)
            key = task_id.value
            self._expect_happy_workflow(script, execution)

            result = runtime.run(task_id)

            self.assertIs(TaskExecutionRuntimeOutcome.WORKFLOW_RETURNED, result.outcome)
            self.assertIs(RunAnalysisOutcome.SUCCEEDED, workflow.last_result.outcome)
            script.assert_exhausted()
            self.assertEqual([0.35, 0.65, 0.95, 1.0], [item.progress for item in progress.publications])
            with sqlite3.connect(database_path) as connection:
                task = connection.execute(
                    "SELECT execution_state, public_status FROM llm_task_executions WHERE execution_id = ?",
                    (key,),
                ).fetchone()
                result_count = connection.execute(
                    "SELECT COUNT(*) FROM analysis_result_snapshots WHERE task_id = ?",
                    (key,),
                ).fetchone()[0]
                succeeded_steps = connection.execute(
                    "SELECT COUNT(*) FROM task_steps WHERE task_id = ? AND state = 'succeeded'",
                    (key,),
                ).fetchone()[0]
                callback = connection.execute(
                    "SELECT state, last_outcome, lease_version FROM callback_delivery_guards "
                    "WHERE business_type = 'file' AND business_key = ?",
                    (execution.file_name,),
                ).fetchone()
                resource = connection.execute(
                    "SELECT state FROM analysis_resource_records WHERE execution_id = ?",
                    (key,),
                ).fetchone()
            self.assertEqual(("succeeded", "2"), task)
            self.assertEqual(1, result_count)
            # 单候选直接抽取不会创建 classification/identity/combined Step；其余实际发生
            # 的 14 类 Step 均须成功落盘。
            self.assertEqual(15, succeeded_steps)
            self.assertEqual(("idle", "skipped", 1), callback)
            self.assertEqual(("cleaned",), resource)

    def test_failed_callback_handoff_recovers_exact_terminal_snapshot_once(self) -> None:
        """同步恢复只重发结果快照，并复用同一个 Task Control Guard。"""

        def failed_transport(request):  # type: ignore[no-untyped-def]
            lease = request.lease
            return AnalysisCallbackDelivery(
                lease.execution,
                lease.lease_token,
                lease.lease_version,
                AnalysisCallbackDeliveryOutcome.DEFINITELY_NOT_SENT,
                "connect_timeout",
            )

        def delivered_transport(request):  # type: ignore[no-untyped-def]
            lease = request.lease
            return AnalysisCallbackDelivery(
                lease.execution,
                lease.lease_token,
                lease.lease_version,
                AnalysisCallbackDeliveryOutcome.DELIVERED,
            )

        with workspace_tempdir() as temporary_root:
            root = Path(temporary_root)
            database_path, task_id, execution, script, workflow, runtime, _progress = self._runtime(
                root,
                callback_url="https://callback.invalid/analysis",
                callback_transport=failed_transport,
            )
            self._expect_happy_workflow(script, execution)
            with patch(
                "app.modules.analysis.adapters.v2_callback.save_callback_history_payload"
            ):
                result = runtime.run(task_id)
            self.assertIs(TaskExecutionRuntimeOutcome.WORKFLOW_RETURNED, result.outcome)
            self.assertIs(RunAnalysisOutcome.SUCCEEDED, workflow.last_result.outcome)

            bootstrap = bootstrap_analysis_task_control_database(
                root / "old.sqlite3",
                database_path,
            )
            manager = SQLiteTransactionManager(
                SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100)
            )
            root_uows = build_sqlite_task_control_uow_factories(manager)
            results = SQLiteAnalysisResultSnapshotStore(manager)
            source = SQLiteAnalysisV2CallbackRecoverySource(
                task_reader=SQLiteTaskControlReadAdapter(manager),
                results=results,
            )
            candidate = source.load_recoverable(execution.file_name)
            self.assertIsNotNone(candidate)
            assert candidate is not None
            persisted = results.get(task_id)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(persisted.payload, candidate.payload)
            self.assertEqual(1, candidate.callback_attempts)

            callbacks = TaskControlAnalysisCallbackAdapter(
                root_uows.callback_delivery,
                clock=FakeClock(_T0),
                callback_timeout=1.0,
                lease_seconds=30.0,
                token_factory=lambda: "analysis-v2-callback-recovery-lease",
                transport=delivered_transport,
            )
            recovery = RecoverAnalysisCallbackSynchronously(
                source=source,
                callbacks=callbacks,
                callback_url="https://callback.invalid/analysis",
            )
            with patch(
                "app.modules.analysis.adapters.v2_callback.save_callback_history_payload"
            ):
                self.assertTrue(
                    recovery.execute(
                        execution.file_name,
                        request_trace_id="trace-check-task-v2",
                        expected_task_id=task_id,
                    )
                )
            self.assertIsNone(source.load_recoverable(execution.file_name))
            with sqlite3.connect(database_path) as connection:
                callback = connection.execute(
                    "SELECT guard.state, guard.last_outcome, latest.callback_attempts "
                    "FROM callback_delivery_guards AS guard "
                    "JOIN llm_tasks AS latest "
                    "ON latest.business_type = guard.business_type "
                    "AND latest.business_key = guard.business_key "
                    "WHERE guard.business_type = 'file' AND guard.business_key = ?",
                    (execution.file_name,),
                ).fetchone()
            self.assertEqual(("idle", "success", 2), callback)

    def test_rag_open_outcome_unknown_is_audited_and_quarantined_without_terminal(self) -> None:
        """未知远端写必须先审计，再隔离 Step、Task 与资源；不得自动 Callback。"""

        with workspace_tempdir() as temporary_root:
            root = Path(temporary_root)
            database_path, task_id, execution, script, workflow, runtime, progress = self._runtime(root)
            key = task_id.value
            prepared_artifact = ArtifactRef(
                task_id=task_id,
                artifact_id="a" * 64,
                step_key="b" * 64,
                kind=ArtifactKind.PREPARED,
                representation=DocumentRepresentation.MARKDOWN,
                metadata=ArtifactMetadata("text/markdown; charset=utf-8", 20, "c" * 64),
            )
            rag_artifact = ArtifactRef(
                task_id=task_id,
                artifact_id="d" * 64,
                step_key="e" * 64,
                kind=ArtifactKind.RAG_PROJECTION,
                representation=DocumentRepresentation.MARKDOWN,
                metadata=ArtifactMetadata("text/markdown; charset=utf-8", 20, "f" * 64),
            )
            prepared = PreparedAnalysisDocument(
                execution=execution,
                source_path="C:/analysis/source.txt",
                processing_path="C:/analysis/prepared.md",
                upload_path="C:/analysis/prepared.md",
                original_text="装备型号资料\n摘要正文",
                internal_prepared_basename="prepared.md",
                prepared_artifact=prepared_artifact,
                rag_upload_artifact=rag_artifact,
                rag_projection_profile_id=_execution_profile().rag_projection_profile_id,
                source_sha256="9" * 64,
            )
            unknown_event = AnalysisRagLifecycleEvent(
                1,
                "context_create",
                1,
                AnalysisRagLifecycleOutcome.OUTCOME_UNKNOWN,
                error_code="context_create_outcome_unknown",
            )
            open_error = AnalysisRagSessionOpenError(
                "模拟 Context 创建结果未知",
                execution=execution,
                stage=AnalysisRagSessionOpenStage.CONTEXT_CREATE,
                lifecycle_events=(unknown_event,),
                outcome_unknown=True,
            )
            script.expect_for(key, "workspace.create", AnalysisTaskWorkspace(execution, "C:/analysis/task-v2"))
            script.expect_for(
                key,
                "file.acquire",
                AcquiredAnalysisSource(execution, "C:/analysis/source.txt", "source.txt", "9" * 64),
            )
            script.expect_for(key, "file.prepare_document", prepared)
            recall = AnalysisRecallAuditReceipt(execution, f"analysis-recall:{key}", "recall:v2", 0)
            script.expect_for(key, "audit.reserve_recall", recall)
            script.expect_for(key, "rag.factory.create", None)
            script.expect_for(key, "rag.open_session", open_error)
            script.expect_for(
                key,
                "audit.finalize_recall",
                AnalysisRecallAuditReceipt(execution, recall.idempotency_key, recall.audit_id, 1, True),
            )
            script.expect_for(
                key,
                "audit.persist_interaction",
                AnalysisInteractionAuditReceipt(execution, f"analysis-rag:{key}", "interaction:v2"),
            )

            result = runtime.run(task_id)

            self.assertIs(TaskExecutionRuntimeOutcome.WORKFLOW_RETURNED, result.outcome)
            self.assertIs(RunAnalysisOutcome.RECOVERY_REQUIRED, workflow.last_result.outcome)
            script.assert_exhausted()
            self.assertEqual([0.35], [item.progress for item in progress.publications])
            with sqlite3.connect(database_path) as connection:
                task = connection.execute(
                    "SELECT execution_state, public_status FROM llm_task_executions "
                    "WHERE execution_id = ?",
                    (key,),
                ).fetchone()
                steps = dict(
                    connection.execute(
                        "SELECT step_key, state FROM task_steps WHERE task_id = ?",
                        (key,),
                    ).fetchall()
                )
                resource = connection.execute(
                    "SELECT state FROM analysis_resource_records WHERE execution_id = ?",
                    (key,),
                ).fetchone()
                result_count = connection.execute(
                    "SELECT COUNT(*) FROM analysis_result_snapshots WHERE task_id = ?",
                    (key,),
                ).fetchone()[0]
                callback_attempts = connection.execute(
                    "SELECT callback_attempts FROM llm_tasks "
                    "WHERE business_type = 'file' AND business_key = ?",
                    (execution.file_name,),
                ).fetchone()
            self.assertEqual(("recovery_required", "1"), task)
            self.assertEqual("outcome_unknown", steps["rag.session.open"])
            self.assertEqual("succeeded", steps["recall.finalize"])
            self.assertEqual("succeeded", steps["interaction_audit.commit"])
            self.assertNotIn("terminal.commit", steps)
            self.assertEqual(("quarantined",), resource)
            self.assertEqual(0, result_count)
            self.assertEqual((0,), callback_attempts)

    def test_resource_recovery_retries_only_idempotent_audit_not_remote_close(self) -> None:
        """close 已确认而审计追加失败时，维护扫描只补审计，不重放远端关闭。"""

        with workspace_tempdir() as temporary_root:
            root = Path(temporary_root)
            database_path, task_id, execution, script, workflow, runtime, _progress = self._runtime(root)
            self._expect_happy_workflow(
                script,
                execution,
                close_audit_result=RuntimeError("模拟生命周期审计暂时失败"),
            )
            # 第一项由正常 Worker 消费并进入 audit_pending；第二项只能由维护恢复消费。
            script.expect_for(task_id.value, "audit.append_lifecycle_events", None)

            result = runtime.run(task_id)

            self.assertIs(TaskExecutionRuntimeOutcome.WORKFLOW_RETURNED, result.outcome)
            self.assertIs(RunAnalysisOutcome.SUCCEEDED, workflow.last_result.outcome)
            with sqlite3.connect(database_path) as connection:
                before = connection.execute(
                    "SELECT state FROM analysis_resource_records WHERE execution_id = ?",
                    (task_id.value,),
                ).fetchone()
            self.assertEqual(("audit_pending",), before)

            bootstrap = bootstrap_analysis_task_control_database(
                root / "old.sqlite3",
                database_path,
            )
            manager = SQLiteTransactionManager(
                SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100)
            )
            sweep = RecoverAnalysisResources(
                store=SQLiteAnalysisV2ResourceStoreAdapter(manager),
                audit=StrictAnalysisPortFake(script),
                # 越过 Worker 写入的 close 保护期；恢复仍只允许补幂等审计。
                clock=lambda: datetime(2030, 1, 1, tzinfo=timezone.utc),
            ).run_once(limit=10)

            self.assertEqual(1, sweep.scanned_count)
            self.assertEqual(1, sweep.cleaned_count)
            self.assertEqual(0, sweep.quarantined_count)
            script.assert_exhausted()
            close_calls = [item for item in script.calls if item[0] == "rag.close_session"]
            self.assertEqual(1, len(close_calls))
            with sqlite3.connect(database_path) as connection:
                after = connection.execute(
                    "SELECT state FROM analysis_resource_records WHERE execution_id = ?",
                    (task_id.value,),
                ).fetchone()
            self.assertEqual(("cleaned",), after)

    def test_profile_mismatch_stops_before_workspace_or_external_port(self) -> None:
        with workspace_tempdir() as temporary_root:
            _db, task_id, _execution, script, _workflow, runtime, progress = self._runtime(
                Path(temporary_root),
                mismatched_profile=True,
            )
            result = runtime.run(task_id)
            self.assertIs(TaskExecutionRuntimeOutcome.WORKFLOW_ERROR, result.outcome)
            self.assertEqual([], script.calls)
            self.assertEqual([], progress.publications)


if __name__ == "__main__":
    unittest.main()
