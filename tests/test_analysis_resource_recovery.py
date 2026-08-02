"""阶段 1F-6：Analysis 资源事实、CAS 与 fail-closed 恢复离线验收。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import threading
import unittest

from app.modules.analysis.adapters import (
    AnalysisResourceStoreConcurrencyError,
    InMemoryAnalysisResourceActivityAdapter,
    SQLiteAnalysisBatchCommandAdapter,
    SQLiteAnalysisResourceStoreAdapter,
)
from app.modules.analysis.application import (
    AnalysisResourceLifecycle,
    AnalysisResourceRecoveryOutcome,
    RecoverAnalysisResources,
)
from app.modules.analysis.application.workflow_models import _RagWorkflowState
from app.modules.analysis.domain.task_inputs import (
    AnalysisPolicySnapshot,
    AnalysisSubmissionSnapshot,
    FrozenJsonObject,
)
from app.modules.analysis.ports import (
    AnalysisInteractionAuditReceipt,
    AnalysisKnowledgeDocumentMetadata,
    AnalysisKnowledgeWriteOutcome,
    AnalysisKnowledgeWriteRequest,
    AnalysisKnowledgeWriteResult,
    AnalysisRagCloseOutcome,
    AnalysisRagCloseResult,
    AnalysisRagLifecycleEvent,
    AnalysisRagLifecycleOutcome,
    AnalysisRagSessionRef,
    AnalysisRecallAuditReceipt,
    AnalysisResourceCommand,
    AnalysisResourceState,
)
from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir


_AFTER_CLOSE_WORKER_DEADLINE = datetime(2100, 1, 1, tzinfo=timezone.utc)


def _command(prefix: str) -> object:
    """构造一条最小新 Analysis batch，避免测试写入旧 file 兼容任务。"""

    raw_params = {
        "fileName": f"{prefix}.txt",
        "filePath": f"https://example.invalid/{prefix}.txt",
    }
    projection = FrozenJsonObject.from_mapping(
        {"businessType": "file", "params": [raw_params]},
        name="analysis_resource_test_request",
    )
    submission = AnalysisSubmissionSnapshot.from_request_params(
        raw_params,
        policy_snapshot=AnalysisPolicySnapshot.default(),
    )
    from app.modules.analysis.ports import AnalysisBatchCommand

    return AnalysisBatchCommand(
        request_projection=projection,
        submissions=(submission,),
        trace_id=f"analysis-resource-trace-{prefix}",
    )


def _execution(service: LLMTaskService, prefix: str):  # type: ignore[no-untyped-def]
    adapter = SQLiteAnalysisBatchCommandAdapter(service)
    admission = adapter.create_batch_if_allowed(_command(prefix))
    return admission.executions[0]


def _state(execution):  # type: ignore[no-untyped-def]
    """构造已绑定文档、已提交交互审计的任务级状态，不触发任何 RAG I/O。"""

    state = _RagWorkflowState()
    state.session = AnalysisRagSessionRef(
        execution=execution,
        session_ref="context:resource::conversation:resource",
        context_ref="context:resource",
        conversation_ref="conversation:resource",
        document_ref="document:resource",
        document_location="location:resource",
        content_sha256="a" * 64,
        ingested_file_name="resource.txt",
        structured_source_key="docsense_ref:" + "a" * 32,
    )
    state.opened = True
    state.recall_receipt = AnalysisRecallAuditReceipt(
        execution=execution,
        idempotency_key=f"analysis-recall:{execution.task_id.value}",
        audit_id="recall:resource",
        version=1,
        finalized=True,
    )
    state.recall_finalized = True
    state.interaction_receipt = AnalysisInteractionAuditReceipt(
        execution=execution,
        idempotency_key=f"analysis-rag:{execution.task_id.value}",
        audit_id="interaction:resource",
    )
    return state


def _knowledge_request(execution, state):  # type: ignore[no-untyped-def]
    return AnalysisKnowledgeWriteRequest(
        execution=execution,
        architecture_id=103,
        idempotency_key=f"document:v1:{execution.task_id.value}",
        document=state.session,
        metadata=AnalysisKnowledgeDocumentMetadata(
            file_name=execution.file_name,
            original_file_name=execution.file_name,
            attributes=FrozenJsonObject.from_mapping({"country": "中国"}),
        ),
    )


def _confirmed_close(execution, state):  # type: ignore[no-untyped-def]
    return AnalysisRagCloseResult(
        execution=execution,
        session=state.session,
        outcome=AnalysisRagCloseOutcome.CONFIRMED,
        lifecycle_events=(
            AnalysisRagLifecycleEvent(
                sequence_no=5,
                operation="conversation_close",
                attempt_number=1,
                outcome=AnalysisRagLifecycleOutcome.SUCCEEDED,
                external_ref=state.session.conversation_ref,
            ),
        ),
    )


class _AuditAppendFake:
    """只允许恢复器追加/查回审计，其他方法一旦被调用即说明越界。"""

    def __init__(self, *, append_error: BaseException | None = None) -> None:
        self.append_error = append_error
        self.append_commands: list[object] = []
        self.load_queries: list[object] = []
        self.loaded_receipt: AnalysisInteractionAuditReceipt | None = None

    def reserve_recall(self, record):  # type: ignore[no-untyped-def]
        raise AssertionError("资源恢复不得重新预留召回审计")

    def finalize_recall(self, command):  # type: ignore[no-untyped-def]
        raise AssertionError("资源恢复不得重新终结召回审计")

    def persist_interaction(self, record):  # type: ignore[no-untyped-def]
        raise AssertionError("资源恢复不得重放交互审计")

    def load_interaction(self, query):  # type: ignore[no-untyped-def]
        self.load_queries.append(query)
        return self.loaded_receipt

    def append_lifecycle_events(self, command):  # type: ignore[no-untyped-def]
        self.append_commands.append(command)
        if self.append_error is not None:
            raise self.append_error
        return None


class AnalysisResourceRecoveryTests(unittest.TestCase):
    """验证资源记录只经 CAS 推进，恢复从不执行远端删除。"""

    def _store(self, service: LLMTaskService) -> SQLiteAnalysisResourceStoreAdapter:
        return SQLiteAnalysisResourceStoreAdapter(service)

    def _registered_lifecycle(self, service: LLMTaskService, prefix: str):  # type: ignore[no-untyped-def]
        execution = _execution(service, prefix)
        state = _state(execution)
        lifecycle = AnalysisResourceLifecycle(store=self._store(service), execution=execution)
        lifecycle.register(
            task_root=f"C:/analysis/{prefix}",
            source_path=f"C:/analysis/{prefix}/source.txt",
            upload_path=f"C:/analysis/{prefix}/upload.txt",
            state=state,
        )
        return execution, state, lifecycle

    @staticmethod
    def _commit_knowledge(lifecycle, execution, state):  # type: ignore[no-untyped-def]
        request = _knowledge_request(execution, state)
        lifecycle.record_knowledge_result(
            request,
            AnalysisKnowledgeWriteResult(
                execution=execution,
                idempotency_key=request.idempotency_key,
                outcome=AnalysisKnowledgeWriteOutcome.COMMITTED,
                external_ref="knowledge:resource",
            ),
        )

    def test_same_state_cas_persists_incremental_references_and_rejects_stale_writer(self) -> None:
        """Context 与 Document 分批出现时允许同状态更新，但旧版本不能覆盖新事实。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            execution = _execution(service, "resource-cas")
            store = self._store(service)
            initial = store.create(
                AnalysisResourceCommand(
                    execution=execution,
                    expected_state=None,
                    expected_version=None,
                    target_state=AnalysisResourceState.TRACKING,
                    record_payload=FrozenJsonObject.from_mapping({"schema_version": 1}),
                )
            )
            command = AnalysisResourceCommand(
                execution=execution,
                expected_state=AnalysisResourceState.TRACKING,
                expected_version=initial.version,
                target_state=AnalysisResourceState.TRACKING,
                record_payload=FrozenJsonObject.from_mapping(
                    {"schema_version": 1, "context_ref": "context:one"},
                ),
            )
            barrier = threading.Barrier(2)

            def advance_once():  # type: ignore[no-untyped-def]
                barrier.wait(timeout=5)
                try:
                    return store.advance(command)
                except AnalysisResourceStoreConcurrencyError:
                    return None

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = tuple(executor.map(lambda _: advance_once(), range(2)))

            successes = tuple(item for item in results if item is not None)
            self.assertEqual(1, len(successes))
            self.assertEqual(1, successes[0].version)
            self.assertEqual("context:one", successes[0].record_payload.to_dict()["context_ref"])
            latest = store.get(execution)
            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertEqual(1, latest.version)

    def test_confirmed_close_is_cleaned_only_after_idempotent_audit_append(self) -> None:
        """close 结果已知但审计尚未追加时是 cleanup_pending，恢复可安全补审计。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            execution, state, lifecycle = self._registered_lifecycle(service, "resource-clean")
            self._commit_knowledge(lifecycle, execution, state)
            lifecycle.prepare_close(retain_document=True)
            lifecycle.mark_close_running()
            lifecycle.record_close_result(_confirmed_close(execution, state))
            record = lifecycle.record
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(AnalysisResourceState.CLEANUP_PENDING, record.state)

            audit = _AuditAppendFake()
            result = RecoverAnalysisResources(
                store=self._store(service),
                audit=audit,
                clock=lambda: _AFTER_CLOSE_WORKER_DEADLINE,
            ).recover(record)
            self.assertEqual(AnalysisResourceRecoveryOutcome.CLEANED, result.outcome)
            self.assertEqual(1, len(audit.append_commands))
            cleaned = self._store(service).get(execution)
            self.assertIsNotNone(cleaned)
            assert cleaned is not None
            self.assertEqual(AnalysisResourceState.CLEANED, cleaned.state)

    def test_running_close_is_not_mutated_before_recovery_deadline(self) -> None:
        """活跃 Worker 的 Callback、close、审计窗口都只能观察，不能破坏 CAS。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            execution = _execution(service, "resource-running-protected")
            state = _state(execution)
            started_at = datetime(2026, 7, 30, 2, 0, tzinfo=timezone.utc)
            resource_activity = InMemoryAnalysisResourceActivityAdapter()
            lifecycle = AnalysisResourceLifecycle(
                store=self._store(service),
                execution=execution,
                clock=lambda: started_at,
                close_running_grace_seconds=60.0,
                resource_activity=resource_activity,
            )
            lifecycle.register(
                task_root="C:/analysis/resource-running-protected",
                source_path="C:/analysis/resource-running-protected/source.txt",
                upload_path="C:/analysis/resource-running-protected/upload.txt",
                state=state,
            )
            self._commit_knowledge(lifecycle, execution, state)
            tracking = lifecycle.record
            assert tracking is not None
            tracking_version = tracking.version
            recovery = RecoverAnalysisResources(
                store=self._store(service),
                audit=_AuditAppendFake(),
                clock=lambda: started_at + timedelta(hours=1),
                resource_activity=resource_activity,
            )
            callback_waiting = recovery.recover(tracking)
            self.assertEqual(
                AnalysisResourceRecoveryOutcome.PENDING,
                callback_waiting.outcome,
            )
            self.assertEqual("resource_owner_active", callback_waiting.reason)
            persisted = self._store(service).get(execution)
            assert persisted is not None
            self.assertEqual(tracking_version, persisted.version)

            lifecycle.prepare_close(retain_document=True)
            lifecycle.mark_close_running()
            running = lifecycle.record
            assert running is not None
            running_version = running.version

            audit = _AuditAppendFake()
            recovery = RecoverAnalysisResources(
                store=self._store(service),
                audit=audit,
                # 即使持久 deadline 已经过期，只要当前单实例 Worker 仍明确存活，
                # 维护线程也不得把一个未设 HTTP timeout 的长 close 抢走。
                clock=lambda: started_at + timedelta(hours=1),
                resource_activity=resource_activity,
            )
            protected = recovery.recover(running)

            self.assertEqual(
                AnalysisResourceRecoveryOutcome.PENDING,
                protected.outcome,
            )
            self.assertEqual("resource_owner_active", protected.reason)
            self.assertEqual([], audit.append_commands)
            persisted = self._store(service).get(execution)
            assert persisted is not None
            self.assertEqual(AnalysisResourceState.CLEANUP_PENDING, persisted.state)
            self.assertEqual(running_version, persisted.version)

            # 维护线程没有修改版本后，原 Worker 仍能按既有 CAS 顺利保存 close 结果和
            # 审计完成事实；close 已返回、审计仍在追加的短窗口同样必须保持只观察。
            lifecycle.record_close_result(_confirmed_close(execution, state))
            close_recorded = lifecycle.record
            assert close_recorded is not None
            close_recorded_version = close_recorded.version
            audit_running = recovery.recover(close_recorded)
            self.assertEqual(
                AnalysisResourceRecoveryOutcome.PENDING,
                audit_running.outcome,
            )
            self.assertEqual("resource_owner_active", audit_running.reason)
            persisted = self._store(service).get(execution)
            assert persisted is not None
            self.assertEqual(close_recorded_version, persisted.version)

            # 这正是线上 close_state_running 以及其后审计窗口竞态的回归门禁。
            lifecycle.mark_close_audited()
            lifecycle.finish_worker()
            cleaned = self._store(service).get(execution)
            assert cleaned is not None
            self.assertEqual(AnalysisResourceState.CLEANED, cleaned.state)

    def test_expired_running_close_is_quarantined_without_replaying_remote_close(self) -> None:
        """活跃保护期耗尽后只能记录外部结果未知并隔离，绝不能自动重放 close。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            execution = _execution(service, "resource-running-expired")
            state = _state(execution)
            started_at = datetime(2026, 7, 30, 2, 0, tzinfo=timezone.utc)
            lifecycle = AnalysisResourceLifecycle(
                store=self._store(service),
                execution=execution,
                clock=lambda: started_at,
                close_running_grace_seconds=60.0,
            )
            lifecycle.register(
                task_root="C:/analysis/resource-running-expired",
                source_path="C:/analysis/resource-running-expired/source.txt",
                upload_path="C:/analysis/resource-running-expired/upload.txt",
                state=state,
            )
            self._commit_knowledge(lifecycle, execution, state)
            lifecycle.prepare_close(retain_document=True)
            lifecycle.mark_close_running()
            running = lifecycle.record
            assert running is not None

            audit = _AuditAppendFake()
            expired = RecoverAnalysisResources(
                store=self._store(service),
                audit=audit,
                clock=lambda: started_at + timedelta(seconds=61),
            ).recover(running)

            self.assertEqual(
                AnalysisResourceRecoveryOutcome.QUARANTINED,
                expired.outcome,
            )
            self.assertEqual("close_running_deadline_expired", expired.reason)
            self.assertEqual([], audit.append_commands)
            quarantined = self._store(service).get(execution)
            assert quarantined is not None
            self.assertEqual(AnalysisResourceState.QUARANTINED, quarantined.state)
            payload = quarantined.record_payload.to_dict()
            self.assertEqual(
                "outcome_unknown",
                payload["cleanup"]["session_close"]["state"],
            )
            self.assertEqual("unknown", payload["ownership"]["document"])

    def test_terminal_resource_record_cannot_be_reopened_through_sqlite_service(self) -> None:
        """即使绕过 Port DTO，SQLite 写边界也不能复活 cleaned 终态。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            execution, state, lifecycle = self._registered_lifecycle(
                service,
                "resource-terminal",
            )
            self._commit_knowledge(lifecycle, execution, state)
            lifecycle.prepare_close(retain_document=True)
            lifecycle.mark_close_running()
            lifecycle.record_close_result(_confirmed_close(execution, state))
            record = lifecycle.record
            assert record is not None
            result = RecoverAnalysisResources(
                store=self._store(service),
                audit=_AuditAppendFake(),
                clock=lambda: _AFTER_CLOSE_WORKER_DEADLINE,
            ).recover(record)
            self.assertEqual(AnalysisResourceRecoveryOutcome.CLEANED, result.outcome)
            cleaned = self._store(service).get(execution)
            assert cleaned is not None

            with self.assertRaisesRegex(ValueError, "非法analysis资源状态迁移"):
                service.advance_analysis_resource_record(
                    execution_id=execution.task_id.value,
                    business_type="file",
                    business_key=execution.file_name,
                    expected_state="cleaned",
                    expected_version=cleaned.version,
                    target_state="tracking",
                    record_payload=cleaned.record_payload.to_dict(),
                    updated_at="2030-01-01T00:00:00+00:00",
                )

    def test_close_failure_with_known_result_preserves_receipt_for_audit_recovery(self) -> None:
        """首次结果落库失败后，fallback 仍保存已知 outcome 与 close events。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            execution, state, lifecycle = self._registered_lifecycle(
                service,
                "resource-known-result",
            )
            self._commit_knowledge(lifecycle, execution, state)
            lifecycle.prepare_close(retain_document=True)
            lifecycle.mark_close_running()

            lifecycle.record_close_failure(
                RuntimeError("result checkpoint temporarily unavailable"),
                _confirmed_close(execution, state),
            )

            record = lifecycle.record
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(AnalysisResourceState.AUDIT_PENDING, record.state)
            payload = record.record_payload.to_dict()
            self.assertEqual(
                "confirmed",
                payload["cleanup"]["session_close"]["state"],
            )
            self.assertEqual(1, len(payload["cleanup"]["close_events"]))
            self.assertEqual(
                "outcome_unknown",
                payload["cleanup"]["audit_append"]["state"],
            )

    def test_unknown_close_is_quarantined_and_recovery_never_replays_delete(self) -> None:
        """远端 close 结果未知后，只保留资源现场，不调用审计补偿或删除。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            execution, state, lifecycle = self._registered_lifecycle(service, "resource-unknown")
            self._commit_knowledge(lifecycle, execution, state)
            lifecycle.prepare_close(retain_document=True)
            lifecycle.mark_close_running()
            unknown = AnalysisRagCloseResult(
                execution=execution,
                session=state.session,
                outcome=AnalysisRagCloseOutcome.OUTCOME_UNKNOWN,
                detail_code="network_result_unknown",
                lifecycle_events=(
                    AnalysisRagLifecycleEvent(
                        sequence_no=5,
                        operation="conversation_close",
                        attempt_number=1,
                        outcome=AnalysisRagLifecycleOutcome.OUTCOME_UNKNOWN,
                        external_ref=state.session.conversation_ref,
                        error_code="network_result_unknown",
                    ),
                ),
            )
            lifecycle.record_close_result(unknown)
            record = lifecycle.record
            self.assertIsNotNone(record)
            assert record is not None
            self.assertEqual(AnalysisResourceState.QUARANTINED, record.state)

            audit = _AuditAppendFake()
            result = RecoverAnalysisResources(store=self._store(service), audit=audit).recover(record)
            self.assertEqual(AnalysisResourceRecoveryOutcome.QUARANTINED, result.outcome)
            self.assertEqual([], audit.append_commands)
            persisted = self._store(service).get(execution)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(AnalysisResourceState.QUARANTINED, persisted.state)
            self.assertEqual("unknown", persisted.record_payload.to_dict()["ownership"]["document"])

    def test_audit_append_failure_uses_bounded_backoff_then_quarantines(self) -> None:
        """可幂等的审计追加可退避一次；达到上限后只隔离，不再无限重试。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            execution, state, lifecycle = self._registered_lifecycle(service, "resource-backoff")
            self._commit_knowledge(lifecycle, execution, state)
            lifecycle.prepare_close(retain_document=True)
            lifecycle.mark_close_running()
            lifecycle.record_close_result(_confirmed_close(execution, state))
            record = lifecycle.record
            assert record is not None
            audit = _AuditAppendFake(append_error=RuntimeError("sqlite temporarily unavailable"))
            recovery = RecoverAnalysisResources(
                store=self._store(service),
                audit=audit,
                clock=lambda: _AFTER_CLOSE_WORKER_DEADLINE,
                max_deferrals=1,
                retry_base_seconds=1.0,
                retry_max_seconds=1.0,
            )
            first = recovery.recover(record)
            self.assertEqual(AnalysisResourceRecoveryOutcome.DEFERRED, first.outcome)
            deferred = self._store(service).get(execution)
            self.assertIsNotNone(deferred)
            assert deferred is not None
            self.assertEqual(1, deferred.recovery_deferral_count)
            self.assertEqual(AnalysisResourceState.CLEANUP_PENDING, deferred.state)

            second = recovery.recover(deferred)
            self.assertEqual(AnalysisResourceRecoveryOutcome.QUARANTINED, second.outcome)
            quarantined = self._store(service).get(execution)
            self.assertIsNotNone(quarantined)
            assert quarantined is not None
            self.assertEqual(AnalysisResourceState.QUARANTINED, quarantined.state)

    def test_structurally_poisoned_record_is_quarantined_after_bounded_deferral(self) -> None:
        """预算耗尽后隔离不应再次解析同一个缺字段 payload。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(str(Path(runtime_directory) / "tasks.sqlite3"))
            execution = _execution(service, "resource-structural-poison")
            store = self._store(service)
            tracking = store.create(
                AnalysisResourceCommand(
                    execution=execution,
                    expected_state=None,
                    expected_version=None,
                    target_state=AnalysisResourceState.TRACKING,
                    record_payload=FrozenJsonObject.from_mapping(
                        {"schema_version": 1}
                    ),
                )
            )
            poisoned = store.advance(
                AnalysisResourceCommand(
                    execution=execution,
                    expected_state=AnalysisResourceState.TRACKING,
                    expected_version=tracking.version,
                    target_state=AnalysisResourceState.CLEANUP_PENDING,
                    record_payload=tracking.record_payload,
                )
            )
            recovery = RecoverAnalysisResources(
                store=store,
                audit=_AuditAppendFake(),
                max_deferrals=1,
                retry_base_seconds=1.0,
                retry_max_seconds=1.0,
            )

            first = recovery.recover(poisoned)
            self.assertEqual(AnalysisResourceRecoveryOutcome.DEFERRED, first.outcome)
            deferred = store.get(execution)
            assert deferred is not None
            second = recovery.recover(deferred)

            self.assertEqual(
                AnalysisResourceRecoveryOutcome.QUARANTINED,
                second.outcome,
            )
            quarantined = store.get(execution)
            assert quarantined is not None
            self.assertEqual(AnalysisResourceState.QUARANTINED, quarantined.state)

    def test_invalid_json_record_is_quarantined_without_starving_following_scan(self) -> None:
        """无法反序列化的最老记录应保留原文隔离，不能让 list 整体失败。"""

        with workspace_tempdir() as runtime_directory:
            database_path = Path(runtime_directory) / "tasks.sqlite3"
            service = LLMTaskService(str(database_path))
            execution, _state_value, lifecycle = self._registered_lifecycle(
                service,
                "resource-json-poison",
            )
            lifecycle.prepare_close(retain_document=False)
            healthy_execution, healthy_state, healthy_lifecycle = (
                self._registered_lifecycle(service, "resource-after-poison")
            )
            self._commit_knowledge(
                healthy_lifecycle,
                healthy_execution,
                healthy_state,
            )
            healthy_lifecycle.prepare_close(retain_document=True)
            healthy_lifecycle.mark_close_running()
            healthy_lifecycle.record_close_result(
                _confirmed_close(healthy_execution, healthy_state)
            )
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    """
                    UPDATE analysis_resource_records
                    SET record_payload = '{'
                    WHERE execution_id = ?
                    """,
                    (execution.task_id.value,),
                )

            sweep = RecoverAnalysisResources(
                store=self._store(service),
                audit=_AuditAppendFake(),
                clock=lambda: _AFTER_CLOSE_WORKER_DEADLINE,
            ).run_once(limit=10)

            self.assertEqual(2, sweep.scanned_count)
            self.assertEqual(1, sweep.cleaned_count)
            self.assertEqual(1, sweep.quarantined_count)
            control = service.get_analysis_resource_control_record(
                execution.task_id.value
            )
            self.assertIsNotNone(control)
            assert control is not None
            self.assertEqual("quarantined", control["state"])
            with sqlite3.connect(database_path) as connection:
                raw_payload = connection.execute(
                    """
                    SELECT record_payload
                    FROM analysis_resource_records
                    WHERE execution_id = ?
                    """,
                    (execution.task_id.value,),
                ).fetchone()[0]
            self.assertEqual("{", raw_payload)
            healthy_record = self._store(service).get(healthy_execution)
            assert healthy_record is not None
            self.assertEqual(
                AnalysisResourceState.CLEANED,
                healthy_record.state,
            )


if __name__ == "__main__":  # pragma: no cover - 仅供本地定向调试。
    unittest.main()
