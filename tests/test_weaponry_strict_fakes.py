"""阶段 1D-3A：严格 Fake、故障矩阵、资源所有权和 50 线程隔离测试。"""

from __future__ import annotations

import hashlib
import unittest
from concurrent.futures import ThreadPoolExecutor

from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.weaponry.domain import (
    AUXILIARY_GUIDANCE_NONE,
    AUXILIARY_GUIDANCE_TERMS_RULES_V1,
    EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1,
    AuxiliaryGuidance,
    AuxiliaryGuidancePolicySnapshot,
    EvidenceCandidate,
    EvidenceSelectionPolicy,
    RetrievalField,
    SelectedEvidence,
    WeaponryCallbackPayload,
    WeaponryDocumentScope,
    WeaponryDocumentSnapshot,
    WeaponryFieldResult,
    WeaponryFieldSpecification,
    build_input_extraction_prompt,
    build_retrieval_query,
)
from app.modules.weaponry.ports import (
    AcquireWeaponryCallback,
    AcquireWeaponryCleanupLease,
    AuxiliaryGuidanceOutcome,
    AuxiliaryGuidanceRequest,
    AuxiliaryGuidanceResult,
    CompleteWeaponryInteraction,
    CompleteWeaponryResourceCleanup,
    DeliverWeaponryCallback,
    EvidenceExtractionRequest,
    ExtractionAnswer,
    ExtractionSourceTrace,
    ExtractionValidationOutcome,
    OpenTargetEvidenceScope,
    PrepareWeaponryResourceCleanup,
    RegisterWeaponryResource,
    ReleaseWeaponryCleanupLease,
    ReleaseUnknownWeaponryCallback,
    ReserveWeaponryInteraction,
    SearchTargetEvidence,
    TargetEvidenceSearchResult,
    WeaponryAuditOutcome,
    WeaponryAuditReserveOutcome,
    WeaponryCallIdentity,
    WeaponryCallbackAcquireOutcome,
    WeaponryCallbackAcquireReason,
    WeaponryCallbackDeliveryOutcome,
    WeaponryCallbackDeliveryResult,
    WeaponryCleanupLeaseAcquireOutcome,
    WeaponryExternalOperationError,
    WeaponryExternalOutcome,
    WeaponryOperation,
    WeaponryPortStateError,
    WeaponryResourceCleanupOutcome,
    WeaponryResourceKind,
    WeaponryResourceOwnership,
    WeaponryResourceRecord,
    WeaponryResourceRecordState,
    WeaponrySourceBoundaryError,
    WeaponryTrackedResource,
    WeaponryTrackedResourceState,
    WeaponryTranslationOutcome,
    WeaponryTranslationRequest,
    WeaponryTranslationResult,
)
from tests.fakes.weaponry import (
    FakeAuxiliaryGuidancePort,
    FakeEvidenceExtractionPort,
    FakeTargetEvidenceRetrievalPort,
    FakeWeaponryCallbackPort,
    FakeWeaponryDispatcherPort,
    FakeWeaponryResourceStorePort,
    FakeWeaponryInteractionAuditPort,
    FakeWeaponryTranslationPort,
    WeaponryInvocationRecorder,
)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _field() -> WeaponryFieldSpecification:
    return WeaponryFieldSpecification.from_mapping(
        {
            "fieldName": "舰级名称",
            "fieldType": "INPUT",
            "fieldDescription": "提取正式舰级名称",
        }
    )


def _retrieval_query():
    field = _field()
    return build_retrieval_query(
        RetrievalField(
            field_name=field.field_name,
            field_description=field.field_description,
            field_type=field.field_type,
        )
    )


def _document(sequence_no: int, document_key: str) -> WeaponryDocumentSnapshot:
    return WeaponryDocumentSnapshot(
        sequence_no=sequence_no,
        document_key=document_key,
        file_name=f"{document_key}.pdf",
        original_name=f"{document_key}.pdf",
        ingested_file_name=f"{document_key}.md",
        source_architecture_id=7,
        external_document_ref=f"external:{document_key}",
        anything_document_id=f"provider:{document_key}",
    )


def _scope(*documents: WeaponryDocumentSnapshot) -> WeaponryDocumentScope:
    return WeaponryDocumentScope(
        mode="category",
        requested_file_names=(),
        documents=tuple(documents),
    )


def _policy() -> EvidenceSelectionPolicy:
    return EvidenceSelectionPolicy(
        profile_id="test-only-stage1d3a-profile",
        provider_fingerprint="test-provider-v1",
        embedding_fingerprint="test-embedding-v1",
        document_processing_fingerprint="test-processing-v1",
    )


def _candidate(document_key: str, candidate_id: str) -> EvidenceCandidate:
    return EvidenceCandidate(
        candidate_id=candidate_id,
        document_key=document_key,
        text=f"{document_key} 的舰级证据正文",
        provider_rank=1,
        provider_score=0.95,
        provider_score_present=True,
        score_profile_id="test-only-stage1d3a-profile",
    )


def _selected(document_key: str, candidate_id: str) -> SelectedEvidence:
    candidate = _candidate(document_key, candidate_id)
    return SelectedEvidence(
        candidate_id=candidate.candidate_id,
        document_key=candidate.document_key,
        text=candidate.text,
        provider_rank=candidate.provider_rank,
        provider_score=0.95,
        score_profile_id=candidate.score_profile_id,
        score_mode="score",
        original_index=0,
    )


def _reserve(
    audit: FakeWeaponryInteractionAuditPort,
    call: WeaponryCallIdentity,
    *,
    architecture_id: int = 7,
    allowed_document_keys: tuple[str, ...] = (),
):
    result = audit.reserve(
        ReserveWeaponryInteraction(
            business_ref=TaskBusinessRef("weaponry", str(architecture_id)),
            call=call,
            input_digest=_digest(call.call_id),
            input_chars=len(call.call_id),
            allowed_document_keys=allowed_document_keys,
        )
    )
    if result.outcome is not WeaponryAuditReserveOutcome.RESERVED:
        raise AssertionError(f"测试首次预留审计记录失败: outcome={result.outcome.value}")
    return result.reservation


def _resource_record(task_id: TaskId, architecture_id: int = 7) -> WeaponryResourceRecord:
    return WeaponryResourceRecord(
        task_id=task_id,
        business_ref=TaskBusinessRef("weaponry", str(architecture_id)),
    )


def _resource(
    *,
    resource_id: str,
    kind: WeaponryResourceKind,
    ownership: WeaponryResourceOwnership,
    call_id: str = "",
    document_key: str = "",
) -> WeaponryTrackedResource:
    return WeaponryTrackedResource(
        resource_id=resource_id,
        kind=kind,
        external_ref=f"opaque:{resource_id}",
        ownership=ownership,
        idempotency_key=f"idempotency:{resource_id}",
        call_id=call_id,
        document_key=document_key,
    )


def _callback_payload(architecture_id: int) -> WeaponryCallbackPayload:
    return WeaponryCallbackPayload(
        architecture_id=architecture_id,
        status="2",
        message="解析成功",
        fields=(WeaponryFieldResult(specification=_field()),),
    )


class StrictRetrievalAndExtractionFakeTests(unittest.TestCase):
    def test_retrieval_requires_audit_and_resource_registration_then_closes_idempotently(self) -> None:
        recorder = WeaponryInvocationRecorder()
        retrieval = FakeTargetEvidenceRetrievalPort(recorder)
        audit = FakeWeaponryInteractionAuditPort(recorder)
        resources = FakeWeaponryResourceStorePort(recorder)
        task_id = TaskId("task-retrieval-order")
        document = _document(1, "doc-a")
        policy = _policy()
        scope = retrieval.open_scope(
            OpenTargetEvidenceScope(task_id, _scope(document), policy)
        )
        call = WeaponryCallIdentity(
            task_id=task_id,
            field_sequence=1,
            document_sequence=None,
            operation=WeaponryOperation.TARGET_RETRIEVAL,
        )
        command = SearchTargetEvidence(
            scope=scope,
            call=call,
            query=_retrieval_query(),
            allowed_document_keys=("doc-a",),
            candidate_top_n=8,
        )
        retrieval.search_results[call.attempt_key] = TargetEvidenceSearchResult(
            scope_ref=scope.scope_ref,
            call=call,
            candidates=(_candidate("doc-a", "candidate-a"),),
            score_mode="score",
            provider_fingerprint=policy.provider_fingerprint,
            embedding_fingerprint=policy.embedding_fingerprint,
        )

        with self.assertRaisesRegex(WeaponryPortStateError, "预留审计"):
            retrieval.search_target(command)
        _reserve(audit, call, allowed_document_keys=("doc-a",))
        with self.assertRaisesRegex(WeaponryPortStateError, "登记资源"):
            retrieval.search_target(command)

        record = resources.create(_resource_record(task_id))
        record = resources.register(
            RegisterWeaponryResource(
                task_id=task_id,
                resource=_resource(
                    resource_id="retrieval-scope-a",
                    kind=WeaponryResourceKind.RETRIEVAL_SCOPE,
                    ownership=WeaponryResourceOwnership.OWNED,
                ),
                expected_version=record.version,
            )
        )
        result = retrieval.search_target(command)
        self.assertEqual("candidate-a", result.candidates[0].candidate_id)

        retrieval.close_error_codes[scope.scope_ref] = "cleanup_interrupted"
        interrupted = retrieval.close_scope(scope)
        self.assertFalse(interrupted.success)
        retrieval.close_error_codes.pop(scope.scope_ref)
        self.assertTrue(retrieval.close_scope(scope).success)
        self.assertTrue(retrieval.close_scope(scope).already_applied)

    def test_retrieval_rejects_candidate_from_other_document(self) -> None:
        recorder = WeaponryInvocationRecorder()
        retrieval = FakeTargetEvidenceRetrievalPort(
            recorder,
            enforce_call_order=False,
        )
        task_id = TaskId("task-source-boundary")
        policy = _policy()
        scope = retrieval.open_scope(
            OpenTargetEvidenceScope(task_id, _scope(_document(1, "doc-a")), policy)
        )
        call = WeaponryCallIdentity(
            task_id=task_id,
            field_sequence=1,
            document_sequence=None,
            operation=WeaponryOperation.TARGET_RETRIEVAL,
        )
        command = SearchTargetEvidence(
            scope=scope,
            call=call,
            query=_retrieval_query(),
            allowed_document_keys=("doc-a",),
            candidate_top_n=8,
        )
        retrieval.search_results[call.attempt_key] = TargetEvidenceSearchResult(
            scope_ref=scope.scope_ref,
            call=call,
            candidates=(_candidate("doc-b", "wrong-source"),),
            score_mode="score",
            provider_fingerprint=policy.provider_fingerprint,
            embedding_fingerprint=policy.embedding_fingerprint,
        )

        with self.assertRaisesRegex(WeaponrySourceBoundaryError, "允许文档"):
            retrieval.search_target(command)

    def test_two_documents_may_have_same_answer_but_never_share_evidence_or_session(self) -> None:
        recorder = WeaponryInvocationRecorder()
        audit = FakeWeaponryInteractionAuditPort(recorder)
        extraction = FakeEvidenceExtractionPort(recorder)
        field = _field()
        task_id = TaskId("task-two-documents")
        requests: list[EvidenceExtractionRequest] = []

        for sequence, document_key in ((1, "doc-a"), (2, "doc-b")):
            document = _document(sequence, document_key)
            evidence = (_selected(document_key, f"ev-{document_key}"),)
            call = WeaponryCallIdentity(
                task_id=task_id,
                field_sequence=1,
                document_sequence=sequence,
                operation=WeaponryOperation.EVIDENCE_EXTRACTION,
            )
            request = EvidenceExtractionRequest(
                call=call,
                document=document,
                field=field,
                evidence=evidence,
                prompt=build_input_extraction_prompt(field, evidence),
                guidance=(),
                context_strategy=EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1,
                model_fingerprint="test-model-v1",
            )
            _reserve(audit, call, allowed_document_keys=(document_key,))
            extraction.results[call.attempt_key] = ExtractionAnswer(
                call=call,
                text="两份文件可以合法得到相同答案",
                raw_response_digest=_digest(f"raw-{document_key}"),
                raw_response_chars=10,
                evidence_ids=(f"ev-{document_key}",),
                sources=(
                    ExtractionSourceTrace(
                        source_ref=f"source-{document_key}",
                        document_key=document_key,
                        evidence_id=f"ev-{document_key}",
                        source_marker_digest=_digest(f"marker-{document_key}"),
                    ),
                ),
                validation_outcome=ExtractionValidationOutcome.MATCHED,
            )
            requests.append(request)

        answers = tuple(extraction.extract(request) for request in requests)
        self.assertEqual(answers[0].text, answers[1].text)
        self.assertNotEqual(answers[0].evidence_ids, answers[1].evidence_ids)
        self.assertEqual(2, len(set(extraction.session_refs)))

    def test_extraction_rejects_cross_document_source_and_supports_failure_classification(self) -> None:
        recorder = WeaponryInvocationRecorder()
        audit = FakeWeaponryInteractionAuditPort(recorder)
        extraction = FakeEvidenceExtractionPort(recorder)
        task_id = TaskId("task-extraction-errors")
        document = _document(1, "doc-b")
        evidence = (_selected("doc-b", "ev-b"),)
        field = _field()

        call = WeaponryCallIdentity(
            task_id=task_id,
            field_sequence=1,
            document_sequence=1,
            operation=WeaponryOperation.EVIDENCE_EXTRACTION,
        )
        request = EvidenceExtractionRequest(
            call=call,
            document=document,
            field=field,
            evidence=evidence,
            prompt=build_input_extraction_prompt(field, evidence),
            guidance=(),
            context_strategy=EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1,
            model_fingerprint="test-model-v1",
        )
        _reserve(audit, call, allowed_document_keys=("doc-b",))
        extraction.results[call.attempt_key] = ExtractionAnswer(
            call=call,
            text="错误来源回答",
            raw_response_digest=_digest("raw"),
            raw_response_chars=3,
            evidence_ids=("ev-b",),
            sources=(
                ExtractionSourceTrace(
                    source_ref="wrong-source",
                    document_key="doc-a",
                    evidence_id="ev-b",
                    source_marker_digest=_digest("wrong-marker"),
                ),
            ),
            validation_outcome=ExtractionValidationOutcome.MATCHED,
        )
        with self.assertRaises(WeaponrySourceBoundaryError):
            extraction.extract(request)

        for attempt_no, outcome, code in (
            (2, WeaponryExternalOutcome.DEFINITELY_FAILED, "session_missing"),
            (3, WeaponryExternalOutcome.OUTCOME_UNKNOWN, "session_create_unknown"),
        ):
            retry_call = WeaponryCallIdentity(
                task_id=task_id,
                field_sequence=1,
                document_sequence=1,
                operation=WeaponryOperation.EVIDENCE_EXTRACTION,
                attempt_no=attempt_no,
            )
            retry_request = EvidenceExtractionRequest(
                call=retry_call,
                document=document,
                field=field,
                evidence=evidence,
                prompt=build_input_extraction_prompt(field, evidence),
                guidance=(),
                context_strategy=EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1,
                model_fingerprint="test-model-v1",
            )
            _reserve(audit, retry_call, allowed_document_keys=("doc-b",))
            extraction.errors[retry_call.attempt_key] = WeaponryExternalOperationError(
                code,
                "模拟来源级会话创建失败",
                outcome=outcome,
            )
            with self.assertRaises(WeaponryExternalOperationError) as captured:
                extraction.extract(retry_request)
            self.assertIs(outcome, captured.exception.outcome)


class StrictAuditResourceAndCallbackFakeTests(unittest.TestCase):
    def test_audit_pending_is_diagnosable_and_complete_is_idempotent(self) -> None:
        audit = FakeWeaponryInteractionAuditPort()
        task_id = TaskId("task-audit")
        call = WeaponryCallIdentity(
            task_id=task_id,
            field_sequence=1,
            document_sequence=None,
            operation=WeaponryOperation.TARGET_RETRIEVAL,
        )
        reservation = _reserve(audit, call, allowed_document_keys=("doc-a",))
        self.assertEqual((reservation,), audit.list_pending(task_id, limit=10))

        completion = CompleteWeaponryInteraction(
            reservation=reservation,
            outcome=WeaponryAuditOutcome.SUCCEEDED,
            output_digest=_digest("candidate-result"),
            candidate_count=1,
            selected_count=1,
        )
        first = audit.complete(completion)
        second = audit.complete(completion)
        self.assertEqual(first, second)
        self.assertEqual((), audit.list_pending(task_id, limit=10))

        foreign = WeaponryCallIdentity(
            task_id=TaskId("task-foreign"),
            field_sequence=1,
            document_sequence=None,
            operation=WeaponryOperation.TARGET_RETRIEVAL,
        )
        with self.assertRaisesRegex(WeaponryPortStateError, "pending"):
            audit.complete(
                CompleteWeaponryInteraction(
                    reservation=type(reservation)(
                        reservation_id="foreign-reservation",
                        business_ref=TaskBusinessRef("weaponry", "8"),
                        call=foreign,
                    ),
                    outcome=WeaponryAuditOutcome.FAILED,
                    error_code="provider_failed",
                )
            )

    def test_resource_store_enforces_cleanup_order_ownership_cas_and_idempotency(self) -> None:
        store = FakeWeaponryResourceStorePort()
        task_id = TaskId("task-resources")
        record = store.create(_resource_record(task_id))
        resources = (
            _resource(
                resource_id="temporary-doc",
                kind=WeaponryResourceKind.TEMPORARY_DOCUMENT,
                ownership=WeaponryResourceOwnership.OWNED,
            ),
            _resource(
                resource_id="shared-source-map",
                kind=WeaponryResourceKind.SOURCE_MAPPING,
                ownership=WeaponryResourceOwnership.SHARED,
                document_key="doc-a",
            ),
            _resource(
                resource_id="retrieval-scope",
                kind=WeaponryResourceKind.RETRIEVAL_SCOPE,
                ownership=WeaponryResourceOwnership.OWNED,
            ),
            _resource(
                resource_id="source-thread",
                kind=WeaponryResourceKind.SOURCE_CONVERSATION,
                ownership=WeaponryResourceOwnership.OWNED,
                call_id="weaponry:task-resources:f1:d1:evidence_extraction",
            ),
        )
        for resource in resources:
            record = store.register(
                RegisterWeaponryResource(task_id, resource, record.version)
            )

        # 同一资源重放是幂等成功；同一幂等键绑定不同事实则由 Fake 主动拒绝。
        self.assertEqual(
            record,
            store.register(
                RegisterWeaponryResource(task_id, resources[-1], 0)
            ),
        )
        cleanup = store.prepare_cleanup(
            PrepareWeaponryResourceCleanup(task_id, record.version)
        )
        acquired_cleanup = store.acquire_cleanup(
            AcquireWeaponryCleanupLease(task_id, cleanup.version)
        )
        self.assertIsNotNone(acquired_cleanup.lease)
        lease = acquired_cleanup.lease
        cleanup = store.get(task_id)
        self.assertIsNotNone(cleanup)
        self.assertEqual(
            ("source-thread", "retrieval-scope", "temporary-doc"),
            tuple(item.resource_id for item in cleanup.owned_cleanup_candidates),
        )
        with self.assertRaisesRegex(WeaponryPortStateError, "shared"):
            store.complete_cleanup(
                CompleteWeaponryResourceCleanup(
                    task_id=task_id,
                    lease=lease,  # type: ignore[arg-type]
                    resource_id="shared-source-map",
                    outcome=WeaponryResourceCleanupOutcome.SUCCEEDED,
                    expected_version=cleanup.version,
                )
            )

        current = cleanup
        first_success_command = None
        for resource_id in ("source-thread", "retrieval-scope", "temporary-doc"):
            command = CompleteWeaponryResourceCleanup(
                task_id=task_id,
                lease=lease,  # type: ignore[arg-type]
                resource_id=resource_id,
                outcome=WeaponryResourceCleanupOutcome.SUCCEEDED,
                expected_version=current.version,
            )
            if first_success_command is None:
                first_success_command = command
            current = store.complete_cleanup(command)
        self.assertIs(WeaponryResourceRecordState.CLEANED, current.state)
        shared = next(
            item for item in current.resources if item.resource_id == "shared-source-map"
        )
        self.assertIs(WeaponryTrackedResourceState.ACTIVE, shared.state)
        self.assertEqual(current, store.complete_cleanup(first_success_command))

    def test_cleanup_unknown_cannot_be_blindly_retried(self) -> None:
        store = FakeWeaponryResourceStorePort()
        task_id = TaskId("task-cleanup-unknown")
        record = store.create(_resource_record(task_id))
        record = store.register(
            RegisterWeaponryResource(
                task_id,
                _resource(
                    resource_id="retrieval-scope",
                    kind=WeaponryResourceKind.RETRIEVAL_SCOPE,
                    ownership=WeaponryResourceOwnership.OWNED,
                ),
                record.version,
            )
        )
        record = store.prepare_cleanup(
            PrepareWeaponryResourceCleanup(task_id, record.version)
        )
        acquired_cleanup = store.acquire_cleanup(
            AcquireWeaponryCleanupLease(task_id, record.version)
        )
        self.assertIsNotNone(acquired_cleanup.lease)
        lease = acquired_cleanup.lease
        record = store.get(task_id)
        self.assertIsNotNone(record)
        record = store.complete_cleanup(
            CompleteWeaponryResourceCleanup(
                task_id=task_id,
                lease=lease,  # type: ignore[arg-type]
                resource_id="retrieval-scope",
                outcome=WeaponryResourceCleanupOutcome.OUTCOME_UNKNOWN,
                expected_version=record.version,
                error_code="delete_timeout_unknown",
            )
        )
        with self.assertRaisesRegex(WeaponryPortStateError, "先对账"):
            store.complete_cleanup(
                CompleteWeaponryResourceCleanup(
                    task_id=task_id,
                    lease=lease,  # type: ignore[arg-type]
                    resource_id="retrieval-scope",
                    outcome=WeaponryResourceCleanupOutcome.SUCCEEDED,
                    expected_version=record.version,
                )
            )

    def test_cleanup_lease_is_exclusive_fenced_and_releasable(self) -> None:
        store = FakeWeaponryResourceStorePort()
        task_id = TaskId("task-cleanup-lease")
        record = store.create(_resource_record(task_id))
        record = store.register(
            RegisterWeaponryResource(
                task_id,
                _resource(
                    resource_id="owned-scope",
                    kind=WeaponryResourceKind.RETRIEVAL_SCOPE,
                    ownership=WeaponryResourceOwnership.OWNED,
                ),
                record.version,
            )
        )
        record = store.prepare_cleanup(
            PrepareWeaponryResourceCleanup(task_id, record.version)
        )
        first = store.acquire_cleanup(
            AcquireWeaponryCleanupLease(task_id, record.version)
        )
        self.assertIs(WeaponryCleanupLeaseAcquireOutcome.ACQUIRED, first.outcome)
        self.assertIsNotNone(first.lease)
        record = store.get(task_id)
        self.assertIsNotNone(record)
        busy = store.acquire_cleanup(
            AcquireWeaponryCleanupLease(task_id, record.version)
        )
        self.assertIs(WeaponryCleanupLeaseAcquireOutcome.BUSY, busy.outcome)

        record = store.complete_cleanup(
            CompleteWeaponryResourceCleanup(
                task_id=task_id,
                lease=first.lease,  # type: ignore[arg-type]
                resource_id="owned-scope",
                outcome=WeaponryResourceCleanupOutcome.FAILED,
                expected_version=record.version,
                error_code="temporary_delete_failure",
            )
        )
        released = store.release_cleanup(
            ReleaseWeaponryCleanupLease(
                lease=first.lease,  # type: ignore[arg-type]
                expected_version=record.version,
            )
        )
        self.assertTrue(released.success)
        record = store.get(task_id)
        self.assertIsNotNone(record)
        second = store.acquire_cleanup(
            AcquireWeaponryCleanupLease(task_id, record.version)
        )
        self.assertIsNotNone(second.lease)
        self.assertGreater(
            second.lease.fencing_token,  # type: ignore[union-attr]
            first.lease.fencing_token,  # type: ignore[union-attr]
        )

    def test_callback_fake_enforces_latest_explicit_retry_and_unknown_freeze(self) -> None:
        callback = FakeWeaponryCallbackPort()
        architecture_id = 101
        first_task = TaskId("callback-first")
        payload = _callback_payload(architecture_id)
        callback.set_latest(first_task, architecture_id)
        callback.delivery_results[first_task] = WeaponryCallbackDeliveryResult(
            WeaponryCallbackDeliveryOutcome.DEFINITELY_NOT_SENT
        )

        acquired = callback.acquire(
            AcquireWeaponryCallback(first_task, architecture_id)
        )
        self.assertIs(WeaponryCallbackAcquireOutcome.ACQUIRED, acquired.outcome)
        delivery = callback.deliver(
            DeliverWeaponryCallback(acquired.lease, payload)  # type: ignore[arg-type]
        )
        self.assertTrue(
            callback.complete(acquired.lease, delivery, payload)  # type: ignore[arg-type]
        )
        self.assertIs(
            WeaponryCallbackAcquireOutcome.ALREADY_COMPLETED,
            callback.acquire(
                AcquireWeaponryCallback(first_task, architecture_id)
            ).outcome,
        )
        recovered = callback.acquire(
            AcquireWeaponryCallback(
                first_task,
                architecture_id,
                WeaponryCallbackAcquireReason.EXPLICIT_CHECK_TASK_RECOVERY,
            )
        )
        self.assertIs(WeaponryCallbackAcquireOutcome.ACQUIRED, recovered.outcome)
        callback.delivery_results[first_task] = WeaponryCallbackDeliveryResult(
            WeaponryCallbackDeliveryOutcome.DELIVERY_OUTCOME_UNKNOWN
        )
        unknown_delivery = callback.deliver(
            DeliverWeaponryCallback(recovered.lease, payload)  # type: ignore[arg-type]
        )
        self.assertTrue(
            callback.complete(recovered.lease, unknown_delivery, payload)  # type: ignore[arg-type]
        )
        self.assertIs(
            WeaponryCallbackAcquireOutcome.OUTCOME_UNKNOWN,
            callback.acquire(
                AcquireWeaponryCallback(
                    first_task,
                    architecture_id,
                    WeaponryCallbackAcquireReason.EXPLICIT_CHECK_TASK_RECOVERY,
                )
            ).outcome,
        )
        released = callback.release_unknown(
            ReleaseUnknownWeaponryCallback(
                architecture_id=architecture_id,
                released_by="operator",
                reason="已隔离旧 Worker 并核对甲方未处理",
                worker_stopped_confirmed=True,
            )
        )
        self.assertEqual("released", released.outcome.value)

        stale_task = TaskId("callback-stale")
        self.assertIs(
            WeaponryCallbackAcquireOutcome.STALE,
            callback.acquire(
                AcquireWeaponryCallback(stale_task, architecture_id)
            ).outcome,
        )


class StrictOptionalProviderAndDispatcherFakeTests(unittest.TestCase):
    def test_none_guidance_is_zero_io_and_terms_provider_is_explicit(self) -> None:
        recorder = WeaponryInvocationRecorder()
        audit = FakeWeaponryInteractionAuditPort(recorder)
        guidance = FakeAuxiliaryGuidancePort(recorder)
        task_id = TaskId("task-guidance")
        none_call = WeaponryCallIdentity(
            task_id=task_id,
            field_sequence=1,
            document_sequence=None,
            operation=WeaponryOperation.AUXILIARY_GUIDANCE,
        )
        _reserve(audit, none_call)
        none_policy = AuxiliaryGuidancePolicySnapshot(
            policy_id=AUXILIARY_GUIDANCE_NONE,
            catalog_fingerprint="",
            top_n=0,
            max_context_chars=0,
        )
        none_result = guidance.load(
            AuxiliaryGuidanceRequest(none_call, _field(), none_policy)
        )
        self.assertIs(AuxiliaryGuidanceOutcome.EMPTY, none_result.outcome)
        self.assertEqual(0, guidance.provider_io_calls)

        terms_call = WeaponryCallIdentity(
            task_id=task_id,
            field_sequence=2,
            document_sequence=None,
            operation=WeaponryOperation.AUXILIARY_GUIDANCE,
        )
        _reserve(audit, terms_call)
        terms_policy = AuxiliaryGuidancePolicySnapshot(
            policy_id=AUXILIARY_GUIDANCE_TERMS_RULES_V1,
            catalog_fingerprint="test-catalog-v1",
            top_n=3,
            max_context_chars=1000,
        )
        guidance.results[terms_call.attempt_key] = AuxiliaryGuidanceResult(
            call=terms_call,
            guidance=(AuxiliaryGuidance("term-1", "舰级术语口径"),),
            outcome=AuxiliaryGuidanceOutcome.PROVIDED,
        )
        provided = guidance.load(
            AuxiliaryGuidanceRequest(terms_call, _field(), terms_policy)
        )
        self.assertEqual("term-1", provided.guidance[0].guidance_id)
        self.assertEqual(1, guidance.provider_io_calls)

    def test_translation_failure_returns_empty_and_state_never_crosses_task(self) -> None:
        recorder = WeaponryInvocationRecorder()
        audit = FakeWeaponryInteractionAuditPort(recorder)
        translation = FakeWeaponryTranslationPort(recorder)
        calls = tuple(
            WeaponryCallIdentity(
                task_id=TaskId(f"translation-task-{index}"),
                field_sequence=1,
                document_sequence=1,
                operation=WeaponryOperation.TRANSLATION,
                item_sequence=1,
            )
            for index in (1, 2)
        )
        for call in calls:
            _reserve(audit, call)
        translation.results[calls[0].attempt_key] = WeaponryTranslationResult(
            call=calls[0],
            text="",
            outcome=WeaponryTranslationOutcome.FAILED,
            error_code="translation_unavailable",
        )
        translation.results[calls[1].attempt_key] = WeaponryTranslationResult(
            call=calls[1],
            text="translated text",
            outcome=WeaponryTranslationOutcome.SUCCEEDED,
        )

        failed = translation.translate(
            WeaponryTranslationRequest(calls[0], "待翻译正文", "en")
        )
        succeeded = translation.translate(
            WeaponryTranslationRequest(calls[1], "另一任务正文", "en")
        )
        self.assertEqual("", failed.text)
        self.assertEqual("translated text", succeeded.text)
        self.assertNotEqual(failed.call.task_id, succeeded.call.task_id)

    def test_dispatcher_has_bounded_lifecycle_and_failure_injection(self) -> None:
        dispatcher = FakeWeaponryDispatcherPort()
        accepted = TaskId("dispatch-accepted")
        failed = TaskId("dispatch-failed")
        dispatcher.start()
        dispatcher.dispatch(accepted)
        dispatcher.dispatch_errors[failed] = WeaponryExternalOperationError(
            "dispatch_signal_failed",
            "模拟唤醒失败",
            outcome=WeaponryExternalOutcome.DEFINITELY_FAILED,
        )
        with self.assertRaises(WeaponryExternalOperationError):
            dispatcher.dispatch(failed)
        dispatcher.stop_result = False
        self.assertFalse(dispatcher.stop(timeout_seconds=0.1))
        self.assertTrue(dispatcher.started)
        dispatcher.close()
        with self.assertRaisesRegex(WeaponryPortStateError, "关闭"):
            dispatcher.dispatch(accepted)


class WeaponryFiftyTaskIsolationTests(unittest.TestCase):
    def test_fifty_tasks_have_unique_scope_session_call_and_resource_identity(self) -> None:
        recorder = WeaponryInvocationRecorder()
        retrieval = FakeTargetEvidenceRetrievalPort(recorder)
        extraction = FakeEvidenceExtractionPort(recorder)
        audit = FakeWeaponryInteractionAuditPort(recorder)
        resources = FakeWeaponryResourceStorePort(recorder)
        field = _field()
        policy = _policy()

        configured: list[
            tuple[
                TaskId,
                int,
                WeaponryDocumentSnapshot,
                WeaponryCallIdentity,
                WeaponryCallIdentity,
                SelectedEvidence,
            ]
        ] = []
        for index in range(1, 51):
            task_id = TaskId(f"parallel-task-{index:02d}")
            architecture_id = 1000 + index
            document = _document(1, f"doc-{index:02d}")
            retrieval_call = WeaponryCallIdentity(
                task_id=task_id,
                field_sequence=1,
                document_sequence=None,
                operation=WeaponryOperation.TARGET_RETRIEVAL,
            )
            extraction_call = WeaponryCallIdentity(
                task_id=task_id,
                field_sequence=1,
                document_sequence=1,
                operation=WeaponryOperation.EVIDENCE_EXTRACTION,
            )
            selected = _selected(document.document_key, f"ev-{index:02d}")
            retrieval.search_results[retrieval_call.attempt_key] = (
                TargetEvidenceSearchResult(
                    scope_ref=f"fake-retrieval-scope:{task_id.value}",
                    call=retrieval_call,
                    candidates=(
                        _candidate(document.document_key, selected.candidate_id),
                    ),
                    score_mode="score",
                    provider_fingerprint=policy.provider_fingerprint,
                    embedding_fingerprint=policy.embedding_fingerprint,
                )
            )
            extraction.results[extraction_call.attempt_key] = ExtractionAnswer(
                call=extraction_call,
                text=f"task-{index:02d}-answer",
                raw_response_digest=_digest(f"task-{index:02d}-raw"),
                raw_response_chars=10,
                evidence_ids=(selected.candidate_id,),
                sources=(
                    ExtractionSourceTrace(
                        source_ref=f"source-{index:02d}",
                        document_key=document.document_key,
                        evidence_id=selected.candidate_id,
                        source_marker_digest=_digest(f"marker-{index:02d}"),
                    ),
                ),
                validation_outcome=ExtractionValidationOutcome.MATCHED,
            )
            configured.append(
                (
                    task_id,
                    architecture_id,
                    document,
                    retrieval_call,
                    extraction_call,
                    selected,
                )
            )

        def run_one(item):
            (
                task_id,
                architecture_id,
                document,
                retrieval_call,
                extraction_call,
                selected,
            ) = item
            scope = retrieval.open_scope(
                OpenTargetEvidenceScope(task_id, _scope(document), policy)
            )
            record = resources.create(_resource_record(task_id, architecture_id))
            record = resources.register(
                RegisterWeaponryResource(
                    task_id,
                    _resource(
                        resource_id=f"retrieval:{task_id.value}",
                        kind=WeaponryResourceKind.RETRIEVAL_SCOPE,
                        ownership=WeaponryResourceOwnership.OWNED,
                    ),
                    record.version,
                )
            )
            retrieval_reservation = _reserve(
                audit,
                retrieval_call,
                architecture_id=architecture_id,
                allowed_document_keys=(document.document_key,),
            )
            search = retrieval.search_target(
                SearchTargetEvidence(
                    scope=scope,
                    call=retrieval_call,
                    query=_retrieval_query(),
                    allowed_document_keys=(document.document_key,),
                    candidate_top_n=8,
                )
            )
            audit.complete(
                CompleteWeaponryInteraction(
                    reservation=retrieval_reservation,
                    outcome=WeaponryAuditOutcome.SUCCEEDED,
                    output_digest=_digest(search.call.attempt_key),
                    candidate_count=len(search.candidates),
                    selected_count=1,
                )
            )

            extraction_reservation = _reserve(
                audit,
                extraction_call,
                architecture_id=architecture_id,
                allowed_document_keys=(document.document_key,),
            )
            request = EvidenceExtractionRequest(
                call=extraction_call,
                document=document,
                field=field,
                evidence=(selected,),
                prompt=build_input_extraction_prompt(field, (selected,)),
                guidance=(),
                context_strategy=EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1,
                model_fingerprint="test-model-v1",
            )
            answer = extraction.extract(request)
            record = resources.register(
                RegisterWeaponryResource(
                    task_id,
                    _resource(
                        resource_id=f"session:{task_id.value}",
                        kind=WeaponryResourceKind.SOURCE_CONVERSATION,
                        ownership=WeaponryResourceOwnership.OWNED,
                        call_id=extraction_call.call_id,
                        document_key=document.document_key,
                    ),
                    record.version,
                )
            )
            audit.complete(
                CompleteWeaponryInteraction(
                    reservation=extraction_reservation,
                    outcome=WeaponryAuditOutcome.SUCCEEDED,
                    output_digest=answer.raw_response_digest,
                    output_chars=len(answer.text),
                    source_count=len(answer.sources),
                    verified_source_count=len(answer.sources),
                )
            )
            retrieval.close_scope(scope)
            return (
                scope.scope_ref,
                retrieval_call.call_id,
                extraction_call.call_id,
                answer.evidence_ids,
                tuple(item.resource_id for item in record.resources),
            )

        with ThreadPoolExecutor(max_workers=50) as executor:
            results = tuple(executor.map(run_one, configured))

        self.assertEqual(50, len(results))
        self.assertEqual(50, len({item[0] for item in results}))
        self.assertEqual(100, len({call for item in results for call in item[1:3]}))
        self.assertEqual(50, len(set(extraction.session_refs)))
        self.assertEqual(50, len(resources.records))
        self.assertEqual((), retrieval.active_scope_refs)
        self.assertEqual(
            100,
            len({resource_id for item in results for resource_id in item[4]}),
        )
        for index, item in enumerate(results, start=1):
            self.assertEqual((f"ev-{index:02d}",), item[3])


if __name__ == "__main__":
    unittest.main()
