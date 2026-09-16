"""阶段 1H-1 SQLite Processing Record 门禁。"""

from __future__ import annotations

import hashlib
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.modules.document_processing.adapters import (
    BytesArtifactContent,
    LocalArtifactStoreAdapter,
    SQLiteProcessingRecordAdapter,
)
from app.modules.document_processing.application import ReconcileProcessingRecord
from app.modules.document_processing.domain import (
    ArtifactKind,
    ArtifactMetadata,
    ArtifactRef,
    DocumentProcessingRequest,
    DocumentRepresentation,
    LineageEvent,
    ProcessingProfile,
)
from app.modules.document_processing.ports import (
    ArtifactPublication,
    ProcessingAcquireDecision,
    ProcessingRecordState,
)
from app.modules.tasks.domain import TaskId
from tests import workspace_tempdir


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request(index: int = 0) -> DocumentProcessingRequest:
    task_id = TaskId(f"stage1h-record-{index:02d}")
    source = ArtifactRef(
        task_id=task_id,
        artifact_id=_digest(f"source-{index}"),
        step_key=_digest(f"source-step-{index}"),
        kind=ArtifactKind.SOURCE,
        representation=DocumentRepresentation.ORIGINAL,
        metadata=ArtifactMetadata(
            media_type="text/plain",
            size_bytes=5,
            sha256=_digest(f"input-{index}"),
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
        trace_id=f"trace-{index}",
    )


class SQLiteProcessingRecordTests(unittest.TestCase):
    def test_schema_coexists_with_existing_database_without_touching_sentinel(
        self,
    ) -> None:
        with workspace_tempdir() as temporary:
            db_path = Path(temporary) / "llm_tasks.sqlite3"
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    "CREATE TABLE existing_sentinel (value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO existing_sentinel(value) VALUES ('kept')"
                )
            SQLiteProcessingRecordAdapter(db_path)

            with sqlite3.connect(db_path) as connection:
                value = connection.execute(
                    "SELECT value FROM existing_sentinel"
                ).fetchone()[0]
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertEqual("kept", value)
            self.assertTrue(
                {
                    "document_processing_steps",
                    "document_processing_artifact_catalog",
                    "document_processing_artifacts",
                    "document_processing_lineage",
                }.issubset(tables)
            )

    def test_artifact_catalog_registers_source_and_multiple_ordinals(self) -> None:
        with workspace_tempdir() as temporary:
            root = Path(temporary)
            db_path = root / "llm_tasks.sqlite3"
            records = SQLiteProcessingRecordAdapter(db_path)
            store = LocalArtifactStoreAdapter(root / "artifacts")
            task_id = TaskId("stage1h-catalog")
            step_key = _digest("compound-artifact-step")
            artifacts = tuple(
                store.publish(
                    ArtifactPublication(
                        task_id=task_id,
                        step_key=step_key,
                        kind=ArtifactKind.SOURCE,
                        representation=DocumentRepresentation.ORIGINAL,
                        media_type="application/octet-stream",
                        ordinal=ordinal,
                    ),
                    BytesArtifactContent(f"source-{ordinal}".encode()),
                )
                for ordinal in (1, 2)
            )
            for artifact in artifacts:
                records.register_artifact(artifact)
                records.register_artifact(artifact)

            with sqlite3.connect(db_path) as connection:
                rows = connection.execute(
                    """
                    SELECT artifact_id, ordinal
                    FROM document_processing_artifact_catalog
                    WHERE step_key = ?
                    ORDER BY ordinal
                    """,
                    (step_key,),
                ).fetchall()
            self.assertEqual(
                [(artifacts[0].artifact_id, 1), (artifacts[1].artifact_id, 2)],
                rows,
            )

    def test_fifty_claims_for_same_step_have_exactly_one_owner(self) -> None:
        with workspace_tempdir() as temporary:
            records = SQLiteProcessingRecordAdapter(
                Path(temporary) / "llm_tasks.sqlite3"
            )
            request = _request()
            with ThreadPoolExecutor(max_workers=50) as executor:
                results = tuple(
                    executor.map(lambda _: records.acquire(request), range(50))
                )
            decisions = [item.decision for item in results]
            self.assertEqual(
                1,
                decisions.count(ProcessingAcquireDecision.ACQUIRED),
            )
            self.assertEqual(
                49,
                decisions.count(ProcessingAcquireDecision.RUNNING),
            )

    def test_fifty_distinct_steps_have_no_lock_error(self) -> None:
        with workspace_tempdir() as temporary:
            records = SQLiteProcessingRecordAdapter(
                Path(temporary) / "llm_tasks.sqlite3"
            )
            requests = tuple(_request(index) for index in range(50))
            with ThreadPoolExecutor(max_workers=50) as executor:
                results = tuple(executor.map(records.acquire, requests))
            self.assertTrue(
                all(
                    item.decision is ProcessingAcquireDecision.ACQUIRED
                    for item in results
                )
            )

    def test_success_atomically_persists_artifact_and_lineage(self) -> None:
        with workspace_tempdir() as temporary:
            db_path = Path(temporary) / "llm_tasks.sqlite3"
            records = SQLiteProcessingRecordAdapter(db_path)
            store = LocalArtifactStoreAdapter(Path(temporary) / "artifacts")
            request = _request()
            acquired = records.acquire(request)
            artifact = store.publish(
                ArtifactPublication(
                    task_id=request.task_id,
                    step_key=request.step_key,
                    kind=ArtifactKind.PREPARED,
                    representation=DocumentRepresentation.TEXT,
                    media_type="text/plain",
                ),
                BytesArtifactContent(b"result"),
            )
            lineage = LineageEvent.create(request=request, child=artifact)
            records.complete(
                request,
                claim_token=acquired.snapshot.claim_token or "",
                artifact=artifact,
                lineage=lineage,
            )

            snapshot = records.get(request.step_key)
            self.assertIsNotNone(snapshot)
            assert snapshot is not None
            self.assertEqual(ProcessingRecordState.SUCCEEDED, snapshot.state)
            self.assertEqual(artifact, snapshot.artifact)
            self.assertEqual(lineage, snapshot.lineage)
            self.assertEqual(
                ProcessingAcquireDecision.SUCCEEDED,
                records.acquire(request).decision,
            )

    def test_lineage_failure_rolls_back_artifact_and_success_transition(self) -> None:
        with workspace_tempdir() as temporary:
            db_path = Path(temporary) / "llm_tasks.sqlite3"
            records = SQLiteProcessingRecordAdapter(db_path)
            store = LocalArtifactStoreAdapter(Path(temporary) / "artifacts")
            request = _request()
            acquired = records.acquire(request)
            artifact = store.publish(
                ArtifactPublication(
                    task_id=request.task_id,
                    step_key=request.step_key,
                    kind=ArtifactKind.PREPARED,
                    representation=DocumentRepresentation.TEXT,
                    media_type="text/plain",
                ),
                BytesArtifactContent(b"result"),
            )
            lineage = LineageEvent.create(request=request, child=artifact)
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    """
                    CREATE TRIGGER reject_document_processing_lineage
                    BEFORE INSERT ON document_processing_lineage
                    BEGIN
                        SELECT RAISE(ABORT, 'injected lineage failure');
                    END
                    """
                )

            with self.assertRaises(Exception):
                records.complete(
                    request,
                    claim_token=acquired.snapshot.claim_token or "",
                    artifact=artifact,
                    lineage=lineage,
                )

            with sqlite3.connect(db_path) as connection:
                artifact_count = connection.execute(
                    "SELECT COUNT(*) FROM document_processing_artifacts"
                ).fetchone()[0]
                state = connection.execute(
                    """
                    SELECT state
                    FROM document_processing_steps
                    WHERE step_key = ?
                    """,
                    (request.step_key,),
                ).fetchone()[0]
            self.assertEqual(0, artifact_count)
            self.assertEqual("running", state)

    def test_reconciliation_confirms_unknown_failure_without_blind_retry(
        self,
    ) -> None:
        with workspace_tempdir() as temporary:
            root = Path(temporary)
            records = SQLiteProcessingRecordAdapter(root / "llm_tasks.sqlite3")
            store = LocalArtifactStoreAdapter(root / "artifacts")
            request = _request(index=20)
            acquired = records.acquire(request)
            records.mark_outcome_unknown(
                request,
                claim_token=acquired.snapshot.claim_token,
                error_code="provider_result_unknown",
            )
            recovery = ReconcileProcessingRecord(
                artifact_store=store,
                recovery=records,
            )

            recovery.confirm_failed(
                request,
                confirmed_error_code="provider_confirmed_failed",
            )

            snapshot = records.get(request.step_key)
            assert snapshot is not None
            self.assertEqual(ProcessingRecordState.FAILED, snapshot.state)
            self.assertEqual("provider_confirmed_failed", snapshot.error_code)
            self.assertEqual(
                ProcessingAcquireDecision.FAILED,
                records.acquire(request).decision,
            )

    def test_reconciliation_recovers_verified_published_artifact(self) -> None:
        with workspace_tempdir() as temporary:
            root = Path(temporary)
            records = SQLiteProcessingRecordAdapter(root / "llm_tasks.sqlite3")
            store = LocalArtifactStoreAdapter(root / "artifacts")
            request = _request(index=21)
            acquired = records.acquire(request)
            records.mark_outcome_unknown(
                request,
                claim_token=acquired.snapshot.claim_token,
                error_code="record_commit_unknown",
            )
            artifact = store.publish(
                ArtifactPublication(
                    task_id=request.task_id,
                    step_key=request.step_key,
                    kind=ArtifactKind.PREPARED,
                    representation=DocumentRepresentation.TEXT,
                    media_type="text/plain",
                ),
                BytesArtifactContent(b"recovered"),
            )
            recovery = ReconcileProcessingRecord(
                artifact_store=store,
                recovery=records,
            )

            lineage = recovery.recover_succeeded(
                request,
                artifact=artifact,
            )

            snapshot = records.get(request.step_key)
            assert snapshot is not None
            self.assertEqual(ProcessingRecordState.SUCCEEDED, snapshot.state)
            self.assertEqual(artifact, snapshot.artifact)
            self.assertEqual(lineage, snapshot.lineage)

    def test_stale_running_is_quarantined_before_manual_resolution(self) -> None:
        with workspace_tempdir() as temporary:
            root = Path(temporary)
            db_path = root / "llm_tasks.sqlite3"
            records = SQLiteProcessingRecordAdapter(db_path)
            store = LocalArtifactStoreAdapter(root / "artifacts")
            request = _request(index=22)
            records.acquire(request)
            with sqlite3.connect(db_path) as connection:
                connection.execute(
                    """
                    UPDATE document_processing_steps
                    SET updated_at = 1
                    WHERE step_key = ?
                    """,
                    (request.step_key,),
                )
            recovery = ReconcileProcessingRecord(
                artifact_store=store,
                recovery=records,
                clock=lambda: 1000.0,
            )

            quarantined = recovery.quarantine_stale_running(
                older_than_seconds=100,
            )

            self.assertEqual((request.step_key,), quarantined)
            snapshot = records.get(request.step_key)
            assert snapshot is not None
            self.assertEqual(
                ProcessingRecordState.OUTCOME_UNKNOWN,
                snapshot.state,
            )
            self.assertEqual(
                "processing_stale_running_requires_reconciliation",
                snapshot.error_code,
            )


if __name__ == "__main__":
    unittest.main()
