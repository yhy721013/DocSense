"""阶段 1D-3A：武器谱供应商无关 Port DTO 与类型边界测试。"""

from __future__ import annotations

import hashlib
import unittest

from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.tasks.ports import ProgressPublisherPort, TaskCommandPort
from app.modules.weaponry.domain import (
    AUXILIARY_GUIDANCE_NONE,
    EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1,
    AuxiliaryGuidancePolicySnapshot,
    EvidenceCandidate,
    EvidenceSelectionPolicy,
    ExtractionPrompt,
    RetrievalField,
    SelectedEvidence,
    WeaponryDocumentScope,
    WeaponryDocumentSnapshot,
    WeaponryFieldSpecification,
    build_input_extraction_prompt,
    build_retrieval_query,
)
from app.modules.weaponry.ports import (
    AuxiliaryGuidancePort,
    AuxiliaryGuidanceRequest,
    CompleteWeaponryInteraction,
    EvidenceExtractionPort,
    EvidenceExtractionRequest,
    ExtractionAnswer,
    ExtractionSourceTrace,
    ExtractionValidationOutcome,
    OpenTargetEvidenceScope,
    ReserveWeaponryInteraction,
    SearchTargetEvidence,
    TargetEvidenceRetrievalPort,
    TargetEvidenceScope,
    WeaponryAuditOutcome,
    WeaponryAuditReservation,
    WeaponryCallIdentity,
    WeaponryCallbackPort,
    WeaponryCallbackRecoverySourcePort,
    WeaponryInteractionAuditPort,
    WeaponryOperation,
    WeaponryResourceStorePort,
    WeaponryTaskDispatcherLifecyclePort,
    WeaponryTaskDispatcherPort,
    WeaponryTranslationPort,
    WeaponryTranslationRequest,
)
from tests.fakes.weaponry import (
    FakeAuxiliaryGuidancePort,
    FakeEvidenceExtractionPort,
    FakeTargetEvidenceRetrievalPort,
    FakeWeaponryCallbackPort,
    FakeWeaponryDispatcherPort,
    FakeWeaponryInteractionAuditPort,
    FakeWeaponryProgressPublisherPort,
    FakeWeaponryResourceStorePort,
    FakeWeaponryTaskCommandPort,
    FakeWeaponryTranslationPort,
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


def _document(sequence_no: int = 1, document_key: str = "doc-a") -> WeaponryDocumentSnapshot:
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


def _selected(document_key: str = "doc-a", candidate_id: str = "ev-a") -> SelectedEvidence:
    return SelectedEvidence(
        candidate_id=candidate_id,
        document_key=document_key,
        text=f"{document_key} 的舰级证据正文",
        provider_rank=1,
        provider_score=0.95,
        score_profile_id="test-only-stage1d3a-profile",
        score_mode="score",
        original_index=0,
    )


class WeaponryCallIdentityTests(unittest.TestCase):
    def test_call_id_is_stable_across_attempts_and_source_calls_include_document(self) -> None:
        task_id = TaskId("task-1")
        first = WeaponryCallIdentity(
            task_id=task_id,
            field_sequence=2,
            document_sequence=3,
            operation=WeaponryOperation.EVIDENCE_EXTRACTION,
            attempt_no=1,
        )
        retry = WeaponryCallIdentity(
            task_id=task_id,
            field_sequence=2,
            document_sequence=3,
            operation=WeaponryOperation.EVIDENCE_EXTRACTION,
            attempt_no=2,
        )

        self.assertEqual(first.call_id, retry.call_id)
        self.assertEqual(
            "weaponry:task-1:f2:d3:evidence_extraction",
            first.call_id,
        )
        self.assertNotEqual(first.attempt_key, retry.attempt_key)

        translation_first = WeaponryCallIdentity(
            task_id=task_id,
            field_sequence=2,
            document_sequence=3,
            operation=WeaponryOperation.TRANSLATION,
            item_sequence=1,
        )
        translation_second = WeaponryCallIdentity(
            task_id=task_id,
            field_sequence=2,
            document_sequence=3,
            operation=WeaponryOperation.TRANSLATION,
            item_sequence=2,
        )
        self.assertEqual(
            "weaponry:task-1:f2:d3:translation:i1",
            translation_first.call_id,
        )
        self.assertNotEqual(translation_first.call_id, translation_second.call_id)

        with self.assertRaisesRegex(ValueError, "document_sequence"):
            WeaponryCallIdentity(
                task_id=task_id,
                field_sequence=1,
                document_sequence=None,
                operation=WeaponryOperation.TRANSLATION,
            )
        with self.assertRaisesRegex(ValueError, "必须是 None"):
            WeaponryCallIdentity(
                task_id=task_id,
                field_sequence=1,
                document_sequence=1,
                operation=WeaponryOperation.TARGET_RETRIEVAL,
            )
        with self.assertRaisesRegex(ValueError, "item_sequence"):
            WeaponryCallIdentity(
                task_id=task_id,
                field_sequence=1,
                document_sequence=1,
                operation=WeaponryOperation.TRANSLATION,
            )


class WeaponryPortDtoBoundaryTests(unittest.TestCase):
    def test_retrieval_accepts_only_retrieval_query_and_frozen_document_subset(self) -> None:
        task_id = TaskId("task-retrieval")
        document = _document()
        policy = _policy()
        open_command = OpenTargetEvidenceScope(task_id, _scope(document), policy)
        scope = TargetEvidenceScope(
            task_id=task_id,
            scope_ref="opaque-scope",
            allowed_document_keys=(document.document_key,),
            selection_profile_id=policy.profile_id,
            provider_fingerprint=policy.provider_fingerprint,
            embedding_fingerprint=policy.embedding_fingerprint,
        )
        call = WeaponryCallIdentity(
            task_id=task_id,
            field_sequence=1,
            document_sequence=None,
            operation=WeaponryOperation.TARGET_RETRIEVAL,
        )
        query = _retrieval_query()

        command = SearchTargetEvidence(
            scope=scope,
            call=call,
            query=query,
            allowed_document_keys=(document.document_key,),
            candidate_top_n=8,
        )
        self.assertEqual(query, command.query)
        self.assertEqual(policy, open_command.policy)

        prompt = ExtractionPrompt(
            text="只根据证据抽取",
            field_type="INPUT",
            document_key="doc-a",
            evidence_ids=("ev-a",),
            rows=("证据",),
        )
        with self.assertRaisesRegex(TypeError, "RetrievalQuery"):
            SearchTargetEvidence(
                scope=scope,
                call=call,
                query=prompt,  # type: ignore[arg-type]
                allowed_document_keys=("doc-a",),
                candidate_top_n=8,
            )
        with self.assertRaisesRegex(ValueError, "超出"):
            SearchTargetEvidence(
                scope=scope,
                call=call,
                query=query,
                allowed_document_keys=("doc-b",),
                candidate_top_n=8,
            )

    def test_extraction_request_freezes_evidence_rows_and_rejects_mismatch(self) -> None:
        task_id = TaskId("task-extraction")
        document = _document()
        field = _field()
        evidence = (_selected(),)
        call = WeaponryCallIdentity(
            task_id=task_id,
            field_sequence=1,
            document_sequence=1,
            operation=WeaponryOperation.EVIDENCE_EXTRACTION,
        )
        prompt = build_input_extraction_prompt(field, evidence)

        request = EvidenceExtractionRequest(
            call=call,
            document=document,
            field=field,
            evidence=evidence,
            prompt=prompt,
            guidance=(),
            context_strategy=EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1,
            model_fingerprint="test-model-v1",
        )
        self.assertEqual(prompt.rows, tuple(item.text for item in request.evidence))

        mismatched = ExtractionPrompt(
            text=prompt.text,
            field_type=prompt.field_type,
            document_key=prompt.document_key,
            evidence_ids=prompt.evidence_ids,
            rows=("被替换的正文",),
        )
        with self.assertRaisesRegex(ValueError, "不一致"):
            EvidenceExtractionRequest(
                call=call,
                document=document,
                field=field,
                evidence=evidence,
                prompt=mismatched,
                guidance=(),
                context_strategy=EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1,
                model_fingerprint="test-model-v1",
            )

    def test_extraction_answer_keeps_only_digest_and_verified_source_trace(self) -> None:
        call = WeaponryCallIdentity(
            task_id=TaskId("task-answer"),
            field_sequence=1,
            document_sequence=1,
            operation=WeaponryOperation.EVIDENCE_EXTRACTION,
        )
        answer = ExtractionAnswer(
            call=call,
            text="尼米兹级",
            raw_response_digest=_digest("模型原始回答"),
            raw_response_chars=6,
            evidence_ids=("ev-a",),
            sources=(
                ExtractionSourceTrace(
                    source_ref="opaque-source-1",
                    document_key="doc-a",
                    evidence_id="ev-a",
                    source_marker_digest=_digest("marker-a"),
                ),
            ),
            validation_outcome=ExtractionValidationOutcome.MATCHED,
        )
        self.assertEqual("尼米兹级", answer.text)
        self.assertFalse(hasattr(answer, "raw_response"))

    def test_auxiliary_and_translation_requests_require_correct_call_scope(self) -> None:
        task_id = TaskId("task-call-scope")
        retrieval_call = WeaponryCallIdentity(
            task_id=task_id,
            field_sequence=1,
            document_sequence=None,
            operation=WeaponryOperation.TARGET_RETRIEVAL,
        )
        policy = AuxiliaryGuidancePolicySnapshot(
            policy_id=AUXILIARY_GUIDANCE_NONE,
            catalog_fingerprint="",
            top_n=0,
            max_context_chars=0,
        )
        with self.assertRaisesRegex(ValueError, "auxiliary_guidance"):
            AuxiliaryGuidanceRequest(retrieval_call, _field(), policy)
        with self.assertRaisesRegex(ValueError, "translation"):
            WeaponryTranslationRequest(
                call=retrieval_call,
                text="舰级",
                target_language="zh-CN",
            )

    def test_audit_completion_checks_source_accounting_and_retrieval_counts(self) -> None:
        call = WeaponryCallIdentity(
            task_id=TaskId("task-audit-dto"),
            field_sequence=1,
            document_sequence=None,
            operation=WeaponryOperation.TARGET_RETRIEVAL,
        )
        command = ReserveWeaponryInteraction(
            business_ref=TaskBusinessRef("weaponry", "12"),
            call=call,
            input_digest=_digest("query"),
            input_chars=5,
            allowed_document_keys=("doc-a",),
        )
        reservation = WeaponryAuditReservation(
            reservation_id="reservation-1",
            business_ref=command.business_ref,
            call=call,
        )
        with self.assertRaisesRegex(ValueError, "selected_count"):
            CompleteWeaponryInteraction(
                reservation=reservation,
                outcome=WeaponryAuditOutcome.SUCCEEDED,
                output_digest=_digest("result"),
                candidate_count=1,
                selected_count=2,
            )
        with self.assertRaisesRegex(ValueError, "分类数量"):
            CompleteWeaponryInteraction(
                reservation=reservation,
                outcome=WeaponryAuditOutcome.SUCCEEDED,
                output_digest=_digest("result"),
                source_count=2,
                verified_source_count=1,
            )
        with self.assertRaisesRegex(ValueError, "完整覆盖"):
            CompleteWeaponryInteraction(
                reservation=reservation,
                outcome=WeaponryAuditOutcome.REJECTED,
                candidate_count=2,
                selected_count=0,
                rejection_reasons=("too-short",),
                error_code="all_rejected",
            )


class WeaponryPortProtocolConformanceTests(unittest.TestCase):
    def test_strict_fakes_implement_every_1d3a_protocol(self) -> None:
        retrieval = FakeTargetEvidenceRetrievalPort()
        extraction = FakeEvidenceExtractionPort()
        guidance = FakeAuxiliaryGuidancePort()
        translation = FakeWeaponryTranslationPort()
        audit = FakeWeaponryInteractionAuditPort()
        callback = FakeWeaponryCallbackPort()
        resource = FakeWeaponryResourceStorePort()
        dispatcher = FakeWeaponryDispatcherPort()
        tasks = FakeWeaponryTaskCommandPort()
        progress = FakeWeaponryProgressPublisherPort()

        self.assertIsInstance(retrieval, TargetEvidenceRetrievalPort)
        self.assertIsInstance(extraction, EvidenceExtractionPort)
        self.assertIsInstance(guidance, AuxiliaryGuidancePort)
        self.assertIsInstance(translation, WeaponryTranslationPort)
        self.assertIsInstance(audit, WeaponryInteractionAuditPort)
        self.assertIsInstance(callback, WeaponryCallbackPort)
        self.assertIsInstance(callback, WeaponryCallbackRecoverySourcePort)
        self.assertIsInstance(resource, WeaponryResourceStorePort)
        self.assertIsInstance(dispatcher, WeaponryTaskDispatcherPort)
        self.assertIsInstance(dispatcher, WeaponryTaskDispatcherLifecyclePort)
        self.assertIsInstance(tasks, TaskCommandPort)
        self.assertIsInstance(progress, ProgressPublisherPort)


if __name__ == "__main__":
    unittest.main()
