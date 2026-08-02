"""阶段 1F-2R：Analysis Ports、CAS 契约与严格 Fake 门禁。"""

from __future__ import annotations

import hashlib
import threading
import unittest

from app.modules.analysis.domain.task_inputs import (
    AnalysisPolicySnapshot,
    AnalysisSubmissionSnapshot,
    AnalysisTaskInputV1,
    FrozenJsonObject,
)
from app.modules.analysis.ports import (
    AnalysisAuditOutcome,
    AnalysisAuditPort,
    AppendAnalysisLifecycleEvents,
    AnalysisBatchAdmission,
    AnalysisBatchAdmissionOutcome,
    AnalysisBatchCommand,
    AnalysisBatchCommandPort,
    AnalysisCallbackAcquireOutcome,
    AnalysisCallbackAcquireResult,
    AnalysisCallbackDelivery,
    AnalysisCallbackDeliveryOutcome,
    AnalysisCallbackDeliveryRequest,
    AnalysisCallbackGuardLease,
    AnalysisCallbackGuardSweepResult,
    AnalysisCallbackPort,
    AnalysisCallbackRequest,
    AnalysisCallbackWaitOutcome,
    AnalysisCallbackWaitResult,
    AnalysisDispatcherPort,
    AnalysisExecutionRef,
    AnalysisFilePreparationRequest,
    AnalysisInteractionAttempt,
    AnalysisInteractionAuditReceipt,
    AnalysisInteractionAuditRecord,
    AnalysisKnowledgePort,
    AnalysisKnowledgeDocumentMetadata,
    AnalysisKnowledgeWriteOutcome,
    AnalysisKnowledgeWriteRequest,
    AnalysisKnowledgeWriteResult,
    AnalysisRagCloseOutcome,
    AnalysisRagCloseRequest,
    AnalysisRagCloseResult,
    AnalysisRagExecutionError,
    AnalysisRagLifecycleEvent,
    AnalysisRagLifecycleOutcome,
    AnalysisRagOperation,
    AnalysisRagPort,
    AnalysisRagPortFactory,
    AnalysisRagRequest,
    AnalysisRagResult,
    AnalysisRagSessionOpenRequest,
    AnalysisRagSessionOpenResult,
    AnalysisRagSessionRef,
    AnalysisRagSource,
    AnalysisRecallAuditReceipt,
    AnalysisRecallAuditRecord,
    AnalysisResourceCommand,
    AnalysisResourcePort,
    AnalysisResourceRecord,
    AnalysisResourceScanBatch,
    AnalysisResourceState,
    AnalysisTaskClaim,
    AnalysisTaskClaimOutcome,
    AnalysisTaskWorkspacePort,
    AnalysisTranslationOutcome,
    AnalysisTranslationPort,
    AnalysisTranslationRequest,
    AnalysisTranslationResult,
    FilePreparationPort,
    FinalizeAnalysisRecallAudit,
    LoadAnalysisInteraction,
    PreparedAnalysisDocument,
    WaitForAnalysisCallbackRelease,
)
from app.modules.tasks.domain import TaskId
from tests.fakes.analysis import StrictAnalysisFakeScript, StrictAnalysisPortFake


def _fixture(index: int = 1) -> tuple[
    AnalysisExecutionRef,
    AnalysisBatchCommand,
    AnalysisTaskInputV1,
]:
    raw_params = {
        "fileName": f"ports-demo-{index}.txt",
        "filePath": f"https://example.invalid/ports-demo-{index}.txt",
    }
    submission = AnalysisSubmissionSnapshot.from_request_params(
        raw_params,
        policy_snapshot=AnalysisPolicySnapshot.default(),
    )
    task_id = TaskId(f"analysis-ports-task-{index}")
    execution = AnalysisExecutionRef(
        task_id=task_id,
        file_name=submission.file_name,
        batch_id=f"{index:032x}",
        batch_sequence=1,
    )
    task_input = AnalysisTaskInputV1.from_submission(
        submission,
        task_id=task_id.value,
        batch_id=execution.batch_id,
        batch_sequence=1,
        accepted_at="2026-07-26T10:00:00+08:00",
        trace_id=f"analysis-ports-trace-{index}",
    )
    command = AnalysisBatchCommand(
        request_projection=FrozenJsonObject.from_mapping(
            {"businessType": "file", "params": [raw_params]},
        ),
        submissions=(submission,),
        trace_id=f"analysis-ports-trace-{index}",
    )
    return execution, command, task_input


class AnalysisPortsTests(unittest.TestCase):
    """覆盖显式 RAG 生命周期、两阶段审计、资源 CAS 与 Callback Guard。"""

    def test_strict_fake_implements_all_ports_and_complete_lifecycle(self) -> None:
        execution, command, task_input = _fixture()
        script = StrictAnalysisFakeScript()
        fake = StrictAnalysisPortFake(script)
        for protocol in (
            AnalysisBatchCommandPort,
            FilePreparationPort,
            AnalysisRagPort,
            AnalysisKnowledgePort,
            AnalysisAuditPort,
            AnalysisTranslationPort,
            AnalysisResourcePort,
            AnalysisCallbackPort,
            AnalysisDispatcherPort,
        ):
            self.assertIsInstance(fake, protocol)

        prepared = PreparedAnalysisDocument(
            execution=execution,
            source_path="C:/analysis/ports-demo.txt",
            upload_path="uploads/ports-demo.txt",
            original_text="原始正文",
        )
        pending_session = AnalysisRagSessionRef(
            execution=execution,
            session_ref="session:ports-demo",
            context_ref="context:ports-demo",
            conversation_ref="conversation:ports-demo",
        )
        session = pending_session.with_bound_document(
            document_ref="document:ports-demo",
            document_location="location:ports-demo",
            content_sha256="a" * 64,
            ingested_file_name="ports-demo.txt",
            structured_source_key="docsense_ref:" + "a" * 32,
        )
        open_request = AnalysisRagSessionOpenRequest(
            execution=execution,
            upload_path=prepared.upload_path,
        )
        open_events = tuple(
            AnalysisRagLifecycleEvent(
                sequence_no=sequence_no,
                operation=operation,
                attempt_number=1,
                outcome=AnalysisRagLifecycleOutcome.SUCCEEDED,
                external_ref=external_ref,
            )
            for sequence_no, operation, external_ref in (
                (1, "context_create", "context:ports-demo"),
                (2, "conversation_create", pending_session.conversation_ref),
            )
        )
        open_result = AnalysisRagSessionOpenResult(
            session=pending_session,
            lifecycle_events=open_events,
        )
        rag_request = AnalysisRagRequest(
            execution=execution,
            session=pending_session,
            operation=AnalysisRagOperation.CLASSIFICATION,
            prompt="分类提示词",
            attempt_number=1,
        )
        rag_result = AnalysisRagResult(
            execution=execution,
            session=session,
            operation=rag_request.operation,
            attempt_number=rag_request.attempt_number,
            answer="分类结果",
        )
        close_request = AnalysisRagCloseRequest(
            execution=execution,
            session=session,
            retain_document=True,
        )
        close_result = AnalysisRagCloseResult(
            execution=execution,
            session=session,
            outcome=AnalysisRagCloseOutcome.CONFIRMED,
            lifecycle_events=(
                AnalysisRagLifecycleEvent(
                    sequence_no=5,
                    operation="conversation_close",
                    attempt_number=1,
                    outcome=AnalysisRagLifecycleOutcome.SUCCEEDED,
                    external_ref=session.session_ref,
                ),
            ),
        )
        recall_record = AnalysisRecallAuditRecord(
            execution=execution,
            idempotency_key="analysis-ports-task-1:recall",
            payload=FrozenJsonObject.from_mapping({"candidateCount": 1}),
        )
        recall_receipt = AnalysisRecallAuditReceipt(
            execution=execution,
            idempotency_key=recall_record.idempotency_key,
            audit_id="recall-audit:1",
            version=0,
        )
        finalize_command = FinalizeAnalysisRecallAudit(
            receipt=recall_receipt,
            expected_version=0,
            outcome=AnalysisAuditOutcome.SUCCEEDED,
            payload=FrozenJsonObject.from_mapping({"architectureId": 103}),
        )
        finalized_receipt = AnalysisRecallAuditReceipt(
            execution=execution,
            idempotency_key=recall_record.idempotency_key,
            audit_id=recall_receipt.audit_id,
            version=1,
            finalized=True,
        )
        interaction_record = AnalysisInteractionAuditRecord(
            execution=execution,
            idempotency_key="analysis-ports-task-1:interaction",
            session=session,
            context_name="llm-file-analysis-ports-task-1",
            trace_id=task_input.trace_id,
            prompt=rag_request.prompt,
            attempts=(
                AnalysisInteractionAttempt(
                    operation=rag_request.operation,
                    attempt_number=1,
                    prompt_digest=hashlib.sha256(
                        rag_request.prompt.encode("utf-8")
                    ).hexdigest(),
                    raw_response=rag_result.answer,
                ),
            ),
            lifecycle_events=open_events,
            outcome=AnalysisAuditOutcome.SUCCEEDED,
        )
        interaction_receipt = AnalysisInteractionAuditReceipt(
            execution=execution,
            idempotency_key=interaction_record.idempotency_key,
            audit_id="interaction-audit:1",
        )
        load_interaction = LoadAnalysisInteraction(
            execution=execution,
            idempotency_key=interaction_record.idempotency_key,
        )
        append_lifecycle = AppendAnalysisLifecycleEvents(
            receipt=interaction_receipt,
            events=close_result.lifecycle_events,
        )
        knowledge_request = AnalysisKnowledgeWriteRequest(
            execution=execution,
            architecture_id=103,
            idempotency_key="analysis-ports-task-1:knowledge",
            document=session,
            metadata=AnalysisKnowledgeDocumentMetadata(
                file_name=execution.file_name,
                original_file_name=execution.file_name,
                attributes=FrozenJsonObject.from_mapping({"country": "美国"}),
            ),
        )
        translation_request = AnalysisTranslationRequest(
            execution=execution,
            source_path="C:/analysis/ports-demo-1.pdf",
        )
        resource_payload = FrozenJsonObject.from_mapping(
            {"documentRef": session.document_ref},
        )
        resource_create = AnalysisResourceCommand(
            execution=execution,
            expected_state=None,
            expected_version=None,
            target_state=AnalysisResourceState.TRACKING,
            record_payload=resource_payload,
        )
        resource_record = AnalysisResourceRecord(
            execution=execution,
            state=AnalysisResourceState.TRACKING,
            version=0,
            record_payload=resource_payload,
        )
        resource_advance = AnalysisResourceCommand(
            execution=execution,
            expected_state=AnalysisResourceState.TRACKING,
            expected_version=0,
            target_state=AnalysisResourceState.AUDIT_PENDING,
            record_payload=resource_payload,
        )
        advanced_record = AnalysisResourceRecord(
            execution=execution,
            state=AnalysisResourceState.AUDIT_PENDING,
            version=1,
            record_payload=resource_payload,
        )
        deferred_record = AnalysisResourceRecord(
            execution=execution,
            state=AnalysisResourceState.AUDIT_PENDING,
            version=2,
            record_payload=resource_payload,
            recovery_deferral_count=1,
            next_recovery_at="2026-07-26T10:05:00+08:00",
            last_recovery_reason="audit_pending",
        )
        callback_payload = FrozenJsonObject.from_mapping({"businessType": "file"})
        callback_request = AnalysisCallbackRequest(
            execution=execution,
            callback_url="https://callback.invalid/analysis",
            payload=callback_payload,
        )
        lease = AnalysisCallbackGuardLease(
            execution=execution,
            lease_token="callback-lease-token",
            lease_version=1,
            expires_at="2026-07-26T10:01:00+08:00",
        )
        acquire_result = AnalysisCallbackAcquireResult(
            execution=execution,
            outcome=AnalysisCallbackAcquireOutcome.ACQUIRED,
            lease=lease,
        )
        wait_request = WaitForAnalysisCallbackRelease(
            execution=execution,
            timeout_seconds=1.0,
            poll_seconds=0.1,
        )
        wait_result = AnalysisCallbackWaitResult(
            execution=execution,
            outcome=AnalysisCallbackWaitOutcome.RELEASED,
        )
        delivery_request = AnalysisCallbackDeliveryRequest(
            lease=lease,
            callback_url=callback_request.callback_url,
            payload=callback_payload,
        )
        delivery = AnalysisCallbackDelivery(
            execution=execution,
            lease_token=lease.lease_token,
            lease_version=lease.lease_version,
            outcome=AnalysisCallbackDeliveryOutcome.DELIVERED,
        )

        expectations = (
            ("batch.create", AnalysisBatchAdmission(
                AnalysisBatchAdmissionOutcome.ACCEPTED,
                executions=(execution,),
            )),
            ("batch.load_input", task_input),
            ("batch.claim", AnalysisTaskClaim(
                AnalysisTaskClaimOutcome.CLAIMED,
                execution,
            )),
            ("file.prepare", prepared),
            ("rag.open_session", open_result),
            ("rag.execute", rag_result),
            ("audit.reserve_recall", recall_receipt),
            ("audit.finalize_recall", finalized_receipt),
            ("audit.persist_interaction", interaction_receipt),
            ("audit.load_interaction", interaction_receipt),
            ("audit.append_lifecycle_events", None),
            ("knowledge.persist", AnalysisKnowledgeWriteResult(
                execution=execution,
                idempotency_key=knowledge_request.idempotency_key,
                outcome=AnalysisKnowledgeWriteOutcome.COMMITTED,
                external_ref="knowledge:103",
            )),
            ("translation.translate", AnalysisTranslationResult(
                execution=execution,
                outcome=AnalysisTranslationOutcome.SUCCEEDED,
                document_translation_one="翻译文本",
                document_translation_two="双语翻译文本",
            )),
            ("rag.close_session", close_result),
            ("resource.create", resource_record),
            ("resource.advance", advanced_record),
            ("resource.get", advanced_record),
            (
                "resource.list_recoverable",
                AnalysisResourceScanBatch((advanced_record,)),
            ),
            ("resource.defer_recovery", deferred_record),
            ("callback.acquire", acquire_result),
            ("callback.wait", wait_result),
            ("callback.deliver", delivery),
            ("callback.complete", True),
            ("callback.freeze_expired", AnalysisCallbackGuardSweepResult(1, 1)),
            ("dispatcher.wake_up", None),
            ("dispatcher.start", None),
            ("dispatcher.stop", True),
            ("dispatcher.close", None),
        )
        for operation, result in expectations:
            script.expect(operation, result)

        self.assertEqual(1, len(fake.create_batch_if_allowed(command).executions))
        self.assertEqual(task_input, fake.load_input(execution.task_id))
        self.assertEqual(
            AnalysisTaskClaimOutcome.CLAIMED,
            fake.claim_if_accepted(execution.task_id).outcome,
        )
        self.assertEqual(
            prepared,
            fake.prepare(AnalysisFilePreparationRequest(
                execution=execution,
                source_url="https://example.invalid/ports-demo.txt",
                task_root="C:/analysis/analysis-ports-task-1",
            )),
        )
        self.assertEqual(open_result, fake.open_session(open_request))
        self.assertEqual(rag_result, fake.execute(rag_request))
        self.assertEqual(recall_receipt, fake.reserve_recall(recall_record))
        self.assertEqual(finalized_receipt, fake.finalize_recall(finalize_command))
        self.assertEqual(interaction_receipt, fake.persist_interaction(interaction_record))
        self.assertEqual(
            interaction_receipt,
            fake.load_interaction(load_interaction),
        )
        fake.append_lifecycle_events(append_lifecycle)
        self.assertEqual("knowledge:103", fake.persist(knowledge_request).external_ref)
        self.assertEqual(
            "翻译文本",
            fake.translate(translation_request).document_translation_one,
        )
        self.assertEqual(close_result, fake.close_session(close_request))
        self.assertEqual(0, fake.create(resource_create).version)
        self.assertEqual(1, fake.advance(resource_advance).version)
        self.assertEqual(advanced_record, fake.get(execution))
        self.assertEqual(
            AnalysisResourceScanBatch((advanced_record,)),
            fake.list_recoverable(limit=5),
        )
        self.assertEqual(
            deferred_record,
            fake.defer_recovery(
                execution,
                expected_version=1,
                retry_at=deferred_record.next_recovery_at or "",
                reason=deferred_record.last_recovery_reason,
            ),
        )
        self.assertEqual(acquire_result, fake.acquire(callback_request))
        self.assertEqual(wait_result, fake.wait_until_released(wait_request))
        self.assertEqual(delivery, fake.deliver(delivery_request))
        self.assertTrue(fake.complete(lease, delivery, callback_payload))
        self.assertEqual(1, fake.freeze_expired(limit=10).frozen_count)
        fake.wake_up()
        fake.start()
        self.assertTrue(fake.stop(timeout_seconds=2.0))
        fake.close()
        script.assert_exhausted()

    def test_wrong_execution_and_operation_results_fail_loudly(self) -> None:
        execution, _, _ = _fixture(1)
        other_execution, _, _ = _fixture(2)
        session = AnalysisRagSessionRef(
            execution,
            "session:1",
            "context:1",
            "conversation:1",
            "document:1",
            "location:1",
            "a" * 64,
            "demo.txt",
            structured_source_key="docsense_ref:" + "a" * 32,
        )
        request = AnalysisRagRequest(
            execution,
            session,
            AnalysisRagOperation.CLASSIFICATION,
            "prompt",
            1,
        )
        wrong_session = AnalysisRagSessionRef(
            other_execution,
            "session:2",
            "context:2",
            "conversation:2",
            "document:2",
            "location:2",
            "b" * 64,
            "other.txt",
            structured_source_key="docsense_ref:" + "b" * 32,
        )
        wrong_result = AnalysisRagResult(
            execution=other_execution,
            session=wrong_session,
            operation=AnalysisRagOperation.EXTRACTION,
            attempt_number=1,
            answer="answer",
        )
        script = StrictAnalysisFakeScript()
        script.expect("rag.execute", wrong_result)
        with self.assertRaisesRegex(AssertionError, "execution 不一致"):
            StrictAnalysisPortFake(script).execute(request)

    def test_value_objects_reject_invalid_capacity_and_success_placeholders(self) -> None:
        execution, _, _ = _fixture(1)
        with self.assertRaisesRegex(ValueError, "1..32"):
            AnalysisExecutionRef(
                task_id=execution.task_id,
                file_name=execution.file_name,
                batch_id=execution.batch_id,
                batch_sequence=33,
            )
        with self.assertRaisesRegex(ValueError, "两种非空展示结果"):
            AnalysisTranslationResult(
                execution=execution,
                outcome=AnalysisTranslationOutcome.SUCCEEDED,
            )
        payload = FrozenJsonObject.from_mapping({"schema_version": 1})
        with self.assertRaisesRegex(ValueError, "非法资源状态迁移"):
            AnalysisResourceCommand(
                execution=execution,
                expected_state=AnalysisResourceState.CLEANED,
                expected_version=3,
                target_state=AnalysisResourceState.TRACKING,
                record_payload=payload,
            )
        with self.assertRaisesRegex(ValueError, "expected_callback_attempts"):
            AnalysisCallbackRequest(
                execution=execution,
                callback_url="https://callback.invalid/analysis",
                payload=payload,
                allow_failed_retry=True,
            )

    def test_rag_execution_error_keeps_unknown_external_outcome_evidence(self) -> None:
        execution, _, _ = _fixture(1)
        session = AnalysisRagSessionRef(
            execution,
            "session:1",
            "context:1",
            "conversation:1",
            "document:1",
            "location:1",
            "a" * 64,
            "demo.txt",
            structured_source_key="docsense_ref:" + "a" * 32,
        )
        request = AnalysisRagRequest(
            execution,
            session,
            AnalysisRagOperation.EXTRACTION,
            "抽取提示词",
            2,
        )
        unknown_event = AnalysisRagLifecycleEvent(
            sequence_no=5,
            operation="conversation_create",
            attempt_number=2,
            outcome=AnalysisRagLifecycleOutcome.OUTCOME_UNKNOWN,
            error_code="conversation_create_timeout",
        )
        error = AnalysisRagExecutionError(
            "供应商结果未知",
            request=request,
            error_code="rag_outcome_unknown",
            lifecycle_events=(unknown_event,),
            outcome_unknown=True,
        )

        self.assertIs(request, error.request)
        self.assertTrue(error.outcome_unknown)
        self.assertEqual((unknown_event,), error.lifecycle_events)

    def test_rag_sources_reject_non_finite_scores_and_cross_document_failures(self) -> None:
        """成功与失败证据采用同一文档归属，并在进入严格 JSON 审计前拒绝 NaN。"""

        with self.assertRaisesRegex(ValueError, "有限数字"):
            AnalysisRagSource(
                document_ref="document:1",
                text="证据",
                score=float("nan"),
            )

        execution, _, _ = _fixture(1)
        session = AnalysisRagSessionRef(
            execution,
            "session:1",
            "context:1",
            "conversation:1",
            "document:1",
            "location:1",
            "a" * 64,
            "demo.txt",
            structured_source_key="docsense_ref:" + "a" * 32,
        )
        request = AnalysisRagRequest(
            execution,
            session,
            AnalysisRagOperation.EXTRACTION,
            "抽取提示词",
            1,
        )
        with self.assertRaisesRegex(ValueError, "request.session.document_ref"):
            AnalysisRagExecutionError(
                "失败",
                request=request,
                error_code="rag_failed",
                sources=(
                    AnalysisRagSource(
                        document_ref="document:other",
                        text="错误文档证据",
                    ),
                ),
            )

    def test_same_execution_results_still_require_call_level_correlation(self) -> None:
        execution, _, _ = _fixture(1)
        session = AnalysisRagSessionRef(
            execution,
            "session:1",
            "context:1",
            "conversation:1",
            "document:1",
            "location:1",
            "a" * 64,
            "demo.txt",
            structured_source_key="docsense_ref:" + "a" * 32,
        )
        rag_request = AnalysisRagRequest(
            execution,
            session,
            AnalysisRagOperation.CLASSIFICATION,
            "prompt",
            1,
        )
        rag_script = StrictAnalysisFakeScript()
        rag_script.expect(
            "rag.execute",
            AnalysisRagResult(
                execution=execution,
                session=session,
                operation=rag_request.operation,
                attempt_number=2,
                answer="late answer",
            ),
        )
        with self.assertRaisesRegex(AssertionError, "attempt"):
            StrictAnalysisPortFake(rag_script).execute(rag_request)

        translation_request = AnalysisTranslationRequest(
            execution=execution,
            source_path="C:/analysis/strict-fake.pdf",
        )
        wrong_execution, _, _ = _fixture(2)
        translation_script = StrictAnalysisFakeScript()
        translation_script.expect(
            "translation.translate",
            AnalysisTranslationResult(
                execution=wrong_execution,
                outcome=AnalysisTranslationOutcome.SUCCEEDED,
                document_translation_one="单语",
                document_translation_two="双语",
            ),
        )
        with self.assertRaisesRegex(AssertionError, "execution 不一致"):
            StrictAnalysisPortFake(translation_script).translate(translation_request)

    def test_per_execution_scripts_allow_concurrent_interleaving(self) -> None:
        script = StrictAnalysisFakeScript()
        fake = StrictAnalysisPortFake(script)
        requests: list[AnalysisTranslationRequest] = []
        expected: dict[str, str] = {}
        for index in range(1, 51):
            execution, _, _ = _fixture(index)
            request = AnalysisTranslationRequest(
                execution=execution,
                source_path=f"C:/analysis/concurrent-{index}.pdf",
            )
            result = AnalysisTranslationResult(
                execution=execution,
                outcome=AnalysisTranslationOutcome.SUCCEEDED,
                document_translation_one=f"译文-{index}",
                document_translation_two=f"双语译文-{index}",
            )
            script.expect_for(
                str(execution.task_id),
                "translation.translate",
                result,
                argument=request,
            )
            requests.append(request)
            expected[str(execution.task_id)] = result.document_translation_one

        observed: dict[str, str] = {}
        lock = threading.Lock()

        def run(request: AnalysisTranslationRequest) -> None:
            result = fake.translate(request)
            with lock:
                observed[str(request.execution.task_id)] = result.document_translation_one

        threads = [threading.Thread(target=run, args=(request,)) for request in requests]
        for thread in reversed(threads):
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

        self.assertEqual(expected, observed)
        script.assert_exhausted()

    def test_unconfigured_and_out_of_order_calls_fail_loudly(self) -> None:
        fake = StrictAnalysisPortFake()
        with self.assertRaisesRegex(AssertionError, "未配置调用"):
            fake.wake_up()

        script = StrictAnalysisFakeScript()
        script.expect("dispatcher.start")
        fake = StrictAnalysisPortFake(script)
        with self.assertRaisesRegex(AssertionError, "调用顺序不匹配"):
            fake.wake_up()
        with self.assertRaisesRegex(AssertionError, "仍有未消费期望"):
            script.assert_exhausted()


if __name__ == "__main__":
    unittest.main()
