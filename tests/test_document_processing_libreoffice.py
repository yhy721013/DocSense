"""阶段 1H-2 Legacy Office 通用 Processor 与 Artifact 谱系门禁。"""

from __future__ import annotations

import hashlib
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.modules.document_processing.adapters import (
    BytesArtifactContent,
    LocalArtifactStoreAdapter,
    SQLiteProcessingRecordAdapter,
)
from app.modules.document_processing.adapters.libreoffice import (
    LibreOfficeDocumentProcessorAdapter,
    create_legacy_office_profile,
)
from app.modules.document_processing.application import PrepareDocument
from app.modules.document_processing.domain import (
    ArtifactKind,
    DocumentProcessingRequest,
    DocumentRepresentation,
    LegacyOfficeConversionError,
    ProcessingOutcome,
)
from app.modules.document_processing.ports import ArtifactPublication
from app.modules.tasks.domain import TaskId
from tests import workspace_tempdir


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _FakePreparation:
    def __init__(
        self,
        *,
        prepared_path: Path,
        cleanup,
    ) -> None:
        self.original_path = prepared_path.with_suffix(".doc")
        self.prepared_path = prepared_path
        self.source_suffix = ".doc"
        self.target_suffix = ".docx"
        self.libreoffice_version = "26.2.5.2"
        self.converted = True
        self._cleanup = cleanup
        self._closed = False
        self._lock = threading.Lock()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._cleanup()


class _FakeLegacyOfficePreparer:
    """只模拟已验证内核的边界，不模拟 Application/Artifact Store。"""

    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root
        self.prepare_calls: list[Path] = []
        self.cleaned_count = 0
        self._lock = threading.Lock()
        self.error: LegacyOfficeConversionError | None = None
        self.version = "26.2.5.2"

    def preflight(self) -> str | None:
        return self.version

    def sweep_stale_jobs(self) -> int:
        return 0

    def prepare(self, source_path, *, job_id: str):
        source = Path(source_path)
        with self._lock:
            self.prepare_calls.append(source)
            index = len(self.prepare_calls)
        if self.error is not None:
            raise self.error
        destination = self.output_root / f"{index:04d}-{job_id[:12]}.docx"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"OOXML:" + source.read_bytes())

        def cleanup() -> None:
            destination.unlink(missing_ok=True)
            with self._lock:
                self.cleaned_count += 1

        return _FakePreparation(
            prepared_path=destination,
            cleanup=cleanup,
        )


def _source_and_request(
    store: LocalArtifactStoreAdapter,
    index: int,
) -> DocumentProcessingRequest:
    task_id = TaskId(f"stage1h-libreoffice-{index:02d}")
    source = store.publish(
        ArtifactPublication(
            task_id=task_id,
            step_key=_digest(f"source-step-{index}"),
            kind=ArtifactKind.SOURCE,
            representation=DocumentRepresentation.ORIGINAL,
            media_type="application/msword",
        ),
        BytesArtifactContent(f"legacy-{index}".encode()),
    )
    return DocumentProcessingRequest(
        task_id=task_id,
        step_id="legacy-office-normalize",
        source_artifact=source,
        profile=create_legacy_office_profile(
            source_suffix=".doc",
            libreoffice_version="26.2.5.2",
            policy_fingerprint="legacy-office-policy-v2",
        ),
        trace_id=f"trace-{index}",
    )


class LibreOfficeDocumentProcessorTests(unittest.TestCase):
    def _build(self, temporary: str):
        root = Path(temporary)
        store = LocalArtifactStoreAdapter(root / "artifacts")
        records = SQLiteProcessingRecordAdapter(root / "llm_tasks.sqlite3")
        legacy = _FakeLegacyOfficePreparer(root / "legacy-output")
        processor = LibreOfficeDocumentProcessorAdapter(
            preparer=legacy,
            source_store=store,
            materialization_root=root / "materialized",
        )
        application = PrepareDocument(
            processor=processor,
            artifact_store=store,
            records=records,
        )
        return store, records, legacy, processor, application

    def test_conversion_publishes_normalized_artifact_and_lineage(self) -> None:
        with workspace_tempdir() as temporary:
            store, records, legacy, _, application = self._build(temporary)
            request = _source_and_request(store, 0)

            result = application.execute(request)

            self.assertEqual(ProcessingOutcome.SUCCEEDED, result.outcome)
            assert result.artifact is not None
            assert result.lineage is not None
            self.assertEqual(ArtifactKind.NORMALIZED, result.artifact.kind)
            self.assertEqual(
                DocumentRepresentation.OOXML,
                result.artifact.representation,
            )
            self.assertEqual(
                request.source_artifact.artifact_id,
                result.lineage.parent_artifact_id,
            )
            self.assertEqual(
                result.artifact.artifact_id,
                result.lineage.child_artifact_id,
            )
            with store.open_reader(result.artifact) as reader:
                self.assertEqual(b"OOXML:legacy-0", reader.read())
            self.assertEqual(1, legacy.cleaned_count)
            self.assertEqual([], list((Path(temporary) / "materialized").iterdir()))
            self.assertEqual(
                result.artifact,
                records.get(request.step_key).artifact,  # type: ignore[union-attr]
            )

    def test_same_step_reuses_artifact_without_second_conversion(self) -> None:
        with workspace_tempdir() as temporary:
            store, _, legacy, _, application = self._build(temporary)
            request = _source_and_request(store, 0)
            first = application.execute(request)
            second = application.execute(request)

            self.assertEqual(ProcessingOutcome.SUCCEEDED, first.outcome)
            self.assertEqual(ProcessingOutcome.SUCCEEDED, second.outcome)
            self.assertTrue(second.reused)
            self.assertEqual(1, len(legacy.prepare_calls))

    def test_version_drift_fails_before_materialization_and_conversion(self) -> None:
        with workspace_tempdir() as temporary:
            store, _, legacy, _, application = self._build(temporary)
            request = _source_and_request(store, 0)
            legacy.version = "26.2.6.1"

            result = application.execute(request)

            self.assertEqual(ProcessingOutcome.FAILED, result.outcome)
            self.assertEqual("snapshot_version_mismatch", result.error_code)
            self.assertEqual([], legacy.prepare_calls)

    def test_conversion_failure_has_no_raw_fallback_or_derived_artifact(self) -> None:
        with workspace_tempdir() as temporary:
            store, records, legacy, _, application = self._build(temporary)
            request = _source_and_request(store, 0)
            legacy.error = LegacyOfficeConversionError("invalid_ole2_signature")

            result = application.execute(request)

            self.assertEqual(ProcessingOutcome.FAILED, result.outcome)
            self.assertEqual("invalid_ole2_signature", result.error_code)
            snapshot = records.get(request.step_key)
            self.assertIsNotNone(snapshot)
            self.assertEqual("failed", snapshot.state.value)  # type: ignore[union-attr]
            # Store 中只有输入 Artifact，失败路径没有复制 raw 文件冒充 normalized。
            stored_files = list(store.root.rglob("*.bin"))
            self.assertEqual(1, len(stored_files))

    def test_materialization_cleanup_failure_does_not_reverse_success_and_sweeps(
        self,
    ) -> None:
        with workspace_tempdir() as temporary:
            store, _, legacy, processor, application = self._build(temporary)
            request = _source_and_request(store, 0)
            with patch(
                "app.modules.document_processing.adapters.libreoffice."
                "processor.shutil.rmtree",
                side_effect=OSError("injected cleanup interruption"),
            ):
                result = application.execute(request)

            self.assertEqual(ProcessingOutcome.SUCCEEDED, result.outcome)
            materialized_root = Path(temporary) / "materialized"
            self.assertEqual(1, len(list(materialized_root.iterdir())))
            self.assertEqual(1, processor.sweep_stale_materializations())
            self.assertEqual([], list(materialized_root.iterdir()))
            self.assertEqual(1, legacy.cleaned_count)

    def test_fifty_tasks_keep_materialization_and_lineage_isolated(self) -> None:
        with workspace_tempdir() as temporary:
            store, records, legacy, _, application = self._build(temporary)
            requests = tuple(
                _source_and_request(store, index) for index in range(50)
            )
            with ThreadPoolExecutor(max_workers=50) as executor:
                results = tuple(executor.map(application.execute, requests))

            self.assertTrue(
                all(item.outcome is ProcessingOutcome.SUCCEEDED for item in results)
            )
            self.assertEqual(50, len({item.artifact.artifact_id for item in results}))
            self.assertEqual(50, len({str(path) for path in legacy.prepare_calls}))
            self.assertEqual(50, legacy.cleaned_count)
            self.assertEqual([], list((Path(temporary) / "materialized").iterdir()))
            self.assertTrue(
                all(records.get(request.step_key) is not None for request in requests)
            )


if __name__ == "__main__":
    unittest.main()
