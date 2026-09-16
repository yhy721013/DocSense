"""阶段 1H-1 PrepareDocument 编排与故障语义门禁。"""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from app.modules.document_processing.adapters import LocalArtifactStoreAdapter
from app.modules.document_processing.application import PrepareDocument
from app.modules.document_processing.domain import (
    ArtifactKind,
    ArtifactMetadata,
    ArtifactRef,
    DocumentProcessingError,
    DocumentProcessingRequest,
    DocumentRepresentation,
    LineageEvent,
    ProcessingOutcome,
    ProcessingProfile,
    derive_artifact_id,
)
from app.modules.document_processing.ports import (
    ArtifactPublication,
    ProcessingAcquireDecision,
    ProcessingAcquireResult,
    ProcessingRecordSnapshot,
    ProcessingRecordState,
    ProcessorOutput,
)
from app.modules.tasks.domain import TaskId
from tests import workspace_tempdir
from tests.fakes.document_processing import (
    BytesArtifactContentFake,
    StrictArtifactStoreFake,
    StrictDocumentProcessorFake,
    StrictProcessingRecordFake,
)


def _digest(value: str | bytes) -> str:
    payload = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _request() -> DocumentProcessingRequest:
    task_id = TaskId("stage1h-application")
    source = ArtifactRef(
        task_id=task_id,
        artifact_id=_digest("source"),
        step_key=_digest("source-step"),
        kind=ArtifactKind.SOURCE,
        representation=DocumentRepresentation.ORIGINAL,
        metadata=ArtifactMetadata(
            media_type="text/plain",
            size_bytes=5,
            sha256=_digest("input"),
        ),
    )
    return DocumentProcessingRequest(
        task_id=task_id,
        step_id="prepare",
        source_artifact=source,
        profile=ProcessingProfile.create(
            processor_id="plain-text",
            processor_fingerprint="plain-text-v1",
            target_representation=DocumentRepresentation.TEXT,
        ),
        trace_id="trace-stage1h",
    )


def _artifact(request: DocumentProcessingRequest, payload: bytes) -> ArtifactRef:
    return ArtifactRef(
        task_id=request.task_id,
        artifact_id=derive_artifact_id(
            step_key=request.step_key,
            kind=ArtifactKind.PREPARED,
            representation=DocumentRepresentation.TEXT,
        ),
        step_key=request.step_key,
        kind=ArtifactKind.PREPARED,
        representation=DocumentRepresentation.TEXT,
        metadata=ArtifactMetadata(
            media_type="text/plain",
            size_bytes=len(payload),
            sha256=_digest(payload),
        ),
    )


def _acquired(request: DocumentProcessingRequest) -> ProcessingAcquireResult:
    return ProcessingAcquireResult(
        ProcessingAcquireDecision.ACQUIRED,
        ProcessingRecordSnapshot(
            step_key=request.step_key,
            state=ProcessingRecordState.RUNNING,
            claim_token="claim-stage1h",
        ),
    )


class PrepareDocumentTests(unittest.TestCase):
    def test_success_publishes_then_atomically_completes_record(self) -> None:
        request = _request()
        payload = b"prepared"
        content = BytesArtifactContentFake(payload)
        output = ProcessorOutput(
            content=content,
            kind=ArtifactKind.PREPARED,
            representation=DocumentRepresentation.TEXT,
            media_type="text/plain",
            warnings=("normalized",),
        )
        publication = ArtifactPublication(
            task_id=request.task_id,
            step_key=request.step_key,
            kind=ArtifactKind.PREPARED,
            representation=DocumentRepresentation.TEXT,
            media_type="text/plain",
        )
        artifact = _artifact(request, payload)
        lineage = LineageEvent.create(request=request, child=artifact)
        processor = StrictDocumentProcessorFake()
        store = StrictArtifactStoreFake()
        records = StrictProcessingRecordFake()
        records.expect_acquire(request, result=_acquired(request))
        processor.expect_process(request, result=output)
        store.expect_publish(publication, content, result=artifact)
        records.expect_complete(
            request,
            "claim-stage1h",
            artifact,
            lineage,
        )

        result = PrepareDocument(
            processor=processor,
            artifact_store=store,
            records=records,
        ).execute(request)

        self.assertEqual(ProcessingOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(("normalized",), result.warnings)
        processor.assert_complete()
        store.assert_complete()
        records.assert_complete()

    def test_succeeded_record_reuses_verified_artifact_without_processor(self) -> None:
        request = _request()
        artifact = _artifact(request, b"prepared")
        lineage = LineageEvent.create(request=request, child=artifact)
        processor = StrictDocumentProcessorFake()
        store = StrictArtifactStoreFake()
        records = StrictProcessingRecordFake()
        records.expect_acquire(
            request,
            result=ProcessingAcquireResult(
                ProcessingAcquireDecision.SUCCEEDED,
                ProcessingRecordSnapshot(
                    step_key=request.step_key,
                    state=ProcessingRecordState.SUCCEEDED,
                    claim_token="old-claim",
                    artifact=artifact,
                    lineage=lineage,
                ),
            ),
        )
        store.expect_verify(artifact, result=True)

        result = PrepareDocument(
            processor=processor,
            artifact_store=store,
            records=records,
        ).execute(request)

        self.assertEqual(ProcessingOutcome.SUCCEEDED, result.outcome)
        self.assertTrue(result.reused)
        processor.assert_complete()
        store.assert_complete()
        records.assert_complete()

    def test_running_record_skips_without_duplicate_processor_call(self) -> None:
        request = _request()
        processor = StrictDocumentProcessorFake()
        store = StrictArtifactStoreFake()
        records = StrictProcessingRecordFake()
        records.expect_acquire(
            request,
            result=ProcessingAcquireResult(
                ProcessingAcquireDecision.RUNNING,
                ProcessingRecordSnapshot(
                    step_key=request.step_key,
                    state=ProcessingRecordState.RUNNING,
                    claim_token="other-owner",
                ),
            ),
        )

        result = PrepareDocument(
            processor=processor,
            artifact_store=store,
            records=records,
        ).execute(request)

        self.assertEqual(ProcessingOutcome.SKIPPED, result.outcome)
        self.assertEqual("processing_step_in_progress", result.error_code)
        processor.assert_complete()
        store.assert_complete()
        records.assert_complete()

    def test_processor_failure_is_persisted_without_publishing(self) -> None:
        request = _request()
        processor = StrictDocumentProcessorFake()
        store = StrictArtifactStoreFake()
        records = StrictProcessingRecordFake()
        records.expect_acquire(request, result=_acquired(request))
        processor.expect_process(
            request,
            error=DocumentProcessingError(
                "processor_rejected",
                "测试注入失败",
            ),
        )
        records.expect_fail(
            request,
            "claim-stage1h",
            "processor_rejected",
        )

        result = PrepareDocument(
            processor=processor,
            artifact_store=store,
            records=records,
        ).execute(request)

        self.assertEqual(ProcessingOutcome.FAILED, result.outcome)
        store.assert_complete()
        records.assert_complete()

    def test_record_failure_after_publish_keeps_artifact_and_returns_unknown(
        self,
    ) -> None:
        request = _request()
        payload = b"published-before-db-failure"
        content = BytesArtifactContentFake(payload)
        output = ProcessorOutput(
            content=content,
            kind=ArtifactKind.PREPARED,
            representation=DocumentRepresentation.TEXT,
            media_type="text/plain",
        )
        artifact = _artifact(request, payload)
        lineage = LineageEvent.create(request=request, child=artifact)
        processor = StrictDocumentProcessorFake()
        records = StrictProcessingRecordFake()
        records.expect_acquire(request, result=_acquired(request))
        processor.expect_process(request, result=output)
        records.expect_complete(
            request,
            "claim-stage1h",
            artifact,
            lineage,
            error=RuntimeError("injected commit failure"),
        )
        records.expect_unknown(
            request,
            "artifact_published_record_outcome_unknown",
            "claim-stage1h",
        )

        with workspace_tempdir() as temporary:
            store = LocalArtifactStoreAdapter(Path(temporary) / "artifacts")
            result = PrepareDocument(
                processor=processor,
                artifact_store=store,
                records=records,
            ).execute(request)
            self.assertTrue(store.verify(artifact))

        self.assertEqual(ProcessingOutcome.OUTCOME_UNKNOWN, result.outcome)
        processor.assert_complete()
        records.assert_complete()


if __name__ == "__main__":
    unittest.main()
