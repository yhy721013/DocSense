from __future__ import annotations

import hashlib
import unittest

from app.modules.report.adapters.interaction_audit import (
    SQLiteReportInteractionAuditAdapter,
)
from app.modules.report.domain import ReportAuditError
from app.modules.report.ports import (
    AppendReportLifecycleEvents,
    PersistReportRagTrace,
    ReportAuditReceipt,
    ReportRagAttempt,
    ReportRagAuditOutcome,
    ReportRagLifecycleEvent,
    ReportRagSource,
    ReportRagTrace,
)
from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir


PROMPT = "根据全部文件生成报告"


def _trace(*, trace_id: str = "trace-report-audit") -> ReportRagTrace:
    source = ReportRagSource(
        document_ref="document:doc-1",
        text="证据",
        source_id="chunk-1",
        score=0.9,
    )
    return ReportRagTrace(
        trace_id=trace_id,
        context_name="report-132-execution",
        context_ref="workspace-1",
        conversation_ref="thread-1",
        attempts=(
            ReportRagAttempt(
                sequence_no=1,
                operation="report_generation",
                attempt_no=1,
                prompt_kind="report_generation",
                prompt_digest=hashlib.sha256(PROMPT.encode("utf-8")).hexdigest(),
                raw_response="<p>报告</p>",
                sources=(source,),
                source_count=1,
                verified_source_count=1,
                call_id="report-call-001",
            ),
        ),
        lifecycle_events=(
            ReportRagLifecycleEvent(
                sequence_no=1,
                operation="context_create",
                attempt_no=1,
                success=True,
                external_ref="workspace-1",
            ),
            ReportRagLifecycleEvent(
                sequence_no=2,
                operation="conversation_create",
                attempt_no=1,
                success=True,
                external_ref="thread-1",
            ),
        ),
        final_call_id="report-call-001",
        summary="complete report trace",
    )


class ReportInteractionAuditAdapterTests(unittest.TestCase):
    def test_unknown_create_outcome_is_never_misreported_as_deleted(self) -> None:
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task = service.create_report_task(132, {"params": []})
            task_id = TaskId(task["execution_id"])
            adapter = SQLiteReportInteractionAuditAdapter(service)
            trace = ReportRagTrace(
                trace_id="trace-outcome-unknown",
                context_name="report-132-execution",
                context_ref=None,
                conversation_ref=None,
                attempts=(),
                lifecycle_events=(
                    ReportRagLifecycleEvent(
                        sequence_no=1,
                        operation="context_create",
                        attempt_no=1,
                        success=False,
                        failure_stage="context_create_outcome_unknown",
                        error_message="workspace response timeout",
                    ),
                ),
                failure_stage="context_create_outcome_unknown",
                error_message="workspace response timeout",
                summary="unknown side effect",
            )

            adapter.persist_trace(
                PersistReportRagTrace(
                    task_id=task_id,
                    business_ref=TaskBusinessRef("report", "132"),
                    idempotency_key=f"report-rag:{task_id.value}",
                    prompt=PROMPT,
                    trace=trace,
                    outcome=ReportRagAuditOutcome.FAILED,
                    error_code="report_rag_error",
                )
            )

            interaction = service.get_llm_interactions("report", "132")[0]
            self.assertEqual("failed", interaction["workspace_cleanup_status"])
            self.assertEqual(
                "workspace response timeout",
                interaction["workspace_cleanup_error"],
            )

    def test_persist_trace_saves_trace_and_call_identity_without_loss(self) -> None:
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task = service.create_report_task(132, {"params": []})
            task_id = TaskId(task["execution_id"])
            adapter = SQLiteReportInteractionAuditAdapter(service)
            command = PersistReportRagTrace(
                task_id=task_id,
                business_ref=TaskBusinessRef("report", "132"),
                idempotency_key=f"report-rag:{task_id.value}",
                prompt=PROMPT,
                trace=_trace(),
                outcome=ReportRagAuditOutcome.SUCCEEDED,
            )

            receipt = adapter.persist_trace(command)
            replayed = adapter.persist_trace(command)

            self.assertEqual(receipt, replayed)
            interactions = service.get_llm_interactions("report", "132")
            self.assertEqual(1, len(interactions))
            self.assertEqual(3, interactions[0]["audit_schema_version"])
            self.assertEqual("trace-report-audit", interactions[0]["trace_id"])
            attempts = service.get_llm_interaction_attempts(int(receipt.audit_id))
            self.assertEqual("report-call-001", attempts[0]["call_id"])
            self.assertEqual("report_generation", attempts[0]["prompt_kind"])

    def test_cleanup_events_append_contiguously_and_update_cleanup_state(self) -> None:
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task = service.create_report_task(132, {"params": []})
            task_id = TaskId(task["execution_id"])
            adapter = SQLiteReportInteractionAuditAdapter(service)
            receipt = adapter.persist_trace(
                PersistReportRagTrace(
                    task_id=task_id,
                    business_ref=TaskBusinessRef("report", "132"),
                    idempotency_key=f"report-rag:{task_id.value}",
                    prompt=PROMPT,
                    trace=_trace(),
                    outcome=ReportRagAuditOutcome.SUCCEEDED,
                )
            )
            command = AppendReportLifecycleEvents(
                receipt=receipt,
                events=(
                    ReportRagLifecycleEvent(
                        sequence_no=3,
                        operation="context_delete",
                        attempt_no=1,
                        success=True,
                        external_ref="workspace-1",
                    ),
                    ReportRagLifecycleEvent(
                        sequence_no=4,
                        operation="global_document_delete",
                        attempt_no=1,
                        success=True,
                        external_ref="custom-documents/doc-1.json",
                    ),
                ),
            )

            adapter.append_lifecycle_events(command)
            adapter.append_lifecycle_events(command)

            events = service.get_llm_interaction_lifecycle_events(int(receipt.audit_id))
            self.assertEqual([1, 2, 3, 4], [event["sequence_no"] for event in events])
            interaction = service.get_llm_interactions("report", "132")[0]
            self.assertEqual("deleted", interaction["workspace_cleanup_status"])

    def test_cleanup_receipt_must_match_execution_and_idempotency_identity(self) -> None:
        """仅凭可猜测的自增 audit_id 不得向其他 execution 的审计记录追加事件。"""

        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task = service.create_report_task(132, {"params": []})
            task_id = TaskId(task["execution_id"])
            adapter = SQLiteReportInteractionAuditAdapter(service)
            receipt = adapter.persist_trace(
                PersistReportRagTrace(
                    task_id=task_id,
                    business_ref=TaskBusinessRef("report", "132"),
                    idempotency_key=f"report-rag:{task_id.value}",
                    prompt=PROMPT,
                    trace=_trace(),
                    outcome=ReportRagAuditOutcome.SUCCEEDED,
                )
            )
            event = ReportRagLifecycleEvent(
                sequence_no=3,
                operation="context_delete",
                attempt_no=1,
                success=True,
                external_ref="workspace-1",
            )
            forged_receipts = (
                ReportAuditReceipt(
                    TaskId("another-execution"),
                    receipt.idempotency_key,
                    receipt.audit_id,
                ),
                ReportAuditReceipt(
                    receipt.task_id,
                    "report-rag:forged-key",
                    receipt.audit_id,
                ),
            )

            for forged in forged_receipts:
                with self.subTest(forged=forged), self.assertRaises(ReportAuditError):
                    adapter.append_lifecycle_events(
                        AppendReportLifecycleEvents(forged, (event,))
                    )

            events = service.get_llm_interaction_lifecycle_events(
                int(receipt.audit_id)
            )

        self.assertEqual([1, 2], [item["sequence_no"] for item in events])

    def test_old_execution_cannot_audit_after_business_key_has_new_owner(self) -> None:
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            old_task = service.create_report_task(132, {"params": ["old"]})
            old_task_id = TaskId(old_task["execution_id"])
            service.create_report_task(132, {"params": ["new"]})
            adapter = SQLiteReportInteractionAuditAdapter(service)

            with self.assertRaises(ReportAuditError):
                adapter.persist_trace(
                    PersistReportRagTrace(
                        task_id=old_task_id,
                        business_ref=TaskBusinessRef("report", "132"),
                        idempotency_key=f"report-rag:{old_task_id.value}",
                        prompt=PROMPT,
                        trace=_trace(),
                        outcome=ReportRagAuditOutcome.SUCCEEDED,
                    )
                )

            self.assertEqual([], service.get_llm_interactions("report", "132"))

    def test_failed_cleanup_can_append_later_attempt_and_converge_to_deleted(self) -> None:
        """失败历史不可覆盖，但允许用连续新序号记录恢复成功。"""

        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task = service.create_report_task(132, {"params": []})
            task_id = TaskId(task["execution_id"])
            adapter = SQLiteReportInteractionAuditAdapter(service)
            receipt = adapter.persist_trace(
                PersistReportRagTrace(
                    task_id=task_id,
                    business_ref=TaskBusinessRef("report", "132"),
                    idempotency_key=f"report-rag:{task_id.value}",
                    prompt=PROMPT,
                    trace=_trace(),
                    outcome=ReportRagAuditOutcome.SUCCEEDED,
                )
            )
            first = AppendReportLifecycleEvents(
                receipt,
                (
                    ReportRagLifecycleEvent(
                        sequence_no=3,
                        operation="context_delete",
                        attempt_no=1,
                        success=False,
                        external_ref="workspace-1",
                        failure_stage="cleanup_context",
                        error_message="temporarily unavailable",
                    ),
                ),
            )
            recovered = AppendReportLifecycleEvents(
                receipt,
                (
                    ReportRagLifecycleEvent(
                        sequence_no=4,
                        operation="context_delete",
                        attempt_no=2,
                        success=True,
                        external_ref="workspace-1",
                    ),
                ),
            )

            adapter.append_lifecycle_events(first)
            adapter.append_lifecycle_events(recovered)
            # 模拟“审计已提交、资源 Store 更新前崩溃”后的幂等重放。
            adapter.append_lifecycle_events(recovered)

            interaction = service.get_llm_interactions("report", "132")[0]
            events = service.get_llm_interaction_lifecycle_events(
                int(receipt.audit_id)
            )
            self.assertEqual("deleted", interaction["workspace_cleanup_status"])
            self.assertEqual([1, 2, 3, 4], [item["sequence_no"] for item in events])

    def test_conversation_delete_failure_keeps_cleanup_failed(self) -> None:
        """对话线程是独立资源，不能因为未出现 Workspace/文档失败而漏报。"""

        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task = service.create_report_task(132, {"params": []})
            task_id = TaskId(task["execution_id"])
            adapter = SQLiteReportInteractionAuditAdapter(service)
            receipt = adapter.persist_trace(
                PersistReportRagTrace(
                    task_id=task_id,
                    business_ref=TaskBusinessRef("report", "132"),
                    idempotency_key=f"report-rag:{task_id.value}",
                    prompt=PROMPT,
                    trace=_trace(),
                    outcome=ReportRagAuditOutcome.SUCCEEDED,
                )
            )

            adapter.append_lifecycle_events(
                AppendReportLifecycleEvents(
                    receipt,
                    (
                        ReportRagLifecycleEvent(
                            sequence_no=3,
                            operation="conversation_delete",
                            attempt_no=1,
                            success=False,
                            external_ref="thread-1",
                            failure_stage="cleanup_conversation",
                            error_message="temporarily unavailable",
                        ),
                    ),
                )
            )

            interaction = service.get_llm_interactions("report", "132")[0]
            self.assertEqual("failed", interaction["workspace_cleanup_status"])
            self.assertEqual(
                "temporarily unavailable",
                interaction["workspace_cleanup_error"],
            )


if __name__ == "__main__":
    unittest.main()
