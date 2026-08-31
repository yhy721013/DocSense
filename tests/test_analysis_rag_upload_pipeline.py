"""文件分析 RAG Artifact、上传描述符与副作用门禁的离线验收。"""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from app.modules.analysis.adapters import (
    LegacyAnalysisFilePreparationAdapter,
    LocalAnalysisTaskWorkspaceAdapter,
)
from app.modules.analysis.application.model_workflow import _AnalysisModelWorkflow
from app.modules.analysis.application.recover_resources import (
    AnalysisResourceLifecycle,
    AnalysisResourceLifecycleError,
)
from app.modules.analysis.application.workflow_models import _RagWorkflowState
from app.modules.analysis.domain.task_inputs import (
    AnalysisDocumentProcessingPolicySnapshot,
)
from app.modules.analysis.ports import (
    AnalysisExecutionRef,
    AnalysisFilePreparationRequest,
    AnalysisRagOperation,
    AnalysisRagPort,
    AnalysisRagRequest,
    AnalysisRagSessionRef,
    AnalysisRagUploadDescriptor,
    AnalysisResourceRecord,
    AnalysisResourceScanBatch,
    AnalysisResourceState,
)
from app.modules.document_processing.adapters import (
    BytesArtifactContent,
    LocalArtifactStoreAdapter,
    MarkdownRagProjectionProcessorAdapter,
    SQLiteProcessingRecordAdapter,
    build_markdown_rag_projection_profile,
)
from app.modules.document_processing.adapters.local_pipeline import (
    LocalPreparedArtifact,
)
from app.modules.document_processing.application import (
    PrepareDocument,
    ProjectDocumentForRag,
)
from app.modules.document_processing.domain import (
    ArtifactKind,
    DocumentRepresentation,
)
from app.modules.document_processing.ports import ArtifactPublication
from app.modules.tasks.domain import TaskId
from tests import workspace_tempdir


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _PreparedArtifactStub:
    """只实现文件分析 Adapter 需要的共享准备窄边界。"""

    def __init__(
        self,
        *,
        artifact_store: LocalArtifactStoreAdapter,
        prepared: LocalPreparedArtifact,
    ) -> None:
        self.artifact_store = artifact_store
        self._prepared = prepared

    def prepare(self, _request):  # type: ignore[no-untyped-def]
        return self._prepared


class _CountingRag(AnalysisRagPort):
    """只用于证明上传意图 CAS 失败发生在首次 execute 之前。"""

    def __init__(self) -> None:
        self.execute_count = 0

    def open_session(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError("本测试不调用 open_session")

    def execute(self, request):  # type: ignore[no-untyped-def]
        self.execute_count += 1
        raise AssertionError("CAS 失败后不得进入 RAG execute")

    def close_session(self, request):  # type: ignore[no-untyped-def]
        raise AssertionError("本测试不调用 close_session")


class _InMemoryResourceStore:
    """严格执行 state/version CAS 的最小资源仓储。"""

    def __init__(self) -> None:
        self.record: AnalysisResourceRecord | None = None

    def create(self, command):  # type: ignore[no-untyped-def]
        if self.record is not None:
            return self.record
        self.record = AnalysisResourceRecord(
            execution=command.execution,
            state=command.target_state,
            version=0,
            record_payload=command.record_payload,
        )
        return self.record

    def get(self, execution):  # type: ignore[no-untyped-def]
        return self.record if self.record and self.record.execution == execution else None

    def advance(self, command):  # type: ignore[no-untyped-def]
        current = self.record
        if (
            current is None
            or current.execution != command.execution
            or current.state is not command.expected_state
            or current.version != command.expected_version
        ):
            raise RuntimeError("resource CAS missed")
        self.record = AnalysisResourceRecord(
            execution=current.execution,
            state=command.target_state,
            version=current.version + 1,
            record_payload=command.record_payload,
        )
        return self.record

    def list_recoverable(self, *, limit: int) -> AnalysisResourceScanBatch:
        return AnalysisResourceScanBatch(records=())

    def defer_recovery(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("本测试不执行恢复延期")

    def quarantine_recovery_record(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("本测试不执行坏记录隔离")


class AnalysisRagUploadPipelineTests(unittest.TestCase):
    """验证 canonical/RAG 分离和远端上传前的 fail-closed 门禁。"""

    def test_upload_descriptor_preserves_frozen_business_names_exactly(self) -> None:
        """描述符只能校验冻结名称，不能在内部边界静默裁剪。"""

        with workspace_tempdir() as temporary:
            task_id = TaskId("analysis-rag-exact-names")
            store = LocalArtifactStoreAdapter(Path(temporary) / "artifacts")
            artifact = self._publish(
                store,
                task_id=task_id,
                payload=b"# projected\n",
                kind=ArtifactKind.RAG_PROJECTION,
                representation=DocumentRepresentation.MARKDOWN,
                media_type="text/markdown; charset=utf-8",
            )
            descriptor = AnalysisRagUploadDescriptor(
                artifact=artifact,
                representation=DocumentRepresentation.MARKDOWN,
                media_type=artifact.metadata.media_type,
                transport_file_name=" 原始资料.md",
                display_title=" 原始资料.pdf",
                projection_profile_id="a" * 64,
            )

        self.assertEqual(" 原始资料.md", descriptor.transport_file_name)
        self.assertEqual(" 原始资料.pdf", descriptor.display_title)

    @staticmethod
    def _publish(
        store: LocalArtifactStoreAdapter,
        *,
        task_id: TaskId,
        payload: bytes,
        kind: ArtifactKind,
        representation: DocumentRepresentation,
        media_type: str,
    ):
        return store.publish(
            ArtifactPublication(
                task_id=task_id,
                step_key=_digest(
                    f"{task_id.value}:{kind.value}:{representation.value}"
                ),
                kind=kind,
                representation=representation,
                media_type=media_type,
            ),
            BytesArtifactContent(payload),
        )

    def test_markdown_uses_projection_without_mutating_canonical_body(self) -> None:
        payload = (
            "# 舰艇资料\n\n"
            "正文 ![舰徽](data:image/png;base64,aGVsbG8=) 结束\n"
        ).encode("utf-8")
        with workspace_tempdir() as temporary:
            root = Path(temporary)
            task_id = TaskId("analysis-rag-projection")
            execution = AnalysisExecutionRef(
                task_id=task_id,
                file_name="business-hash.md",
                batch_id="1" * 32,
                batch_sequence=1,
            )
            store = LocalArtifactStoreAdapter(root / "artifacts")
            canonical = self._publish(
                store,
                task_id=task_id,
                payload=payload,
                kind=ArtifactKind.PREPARED,
                representation=DocumentRepresentation.MARKDOWN,
                media_type="text/markdown; charset=utf-8",
            )
            canonical_path = root / "canonical.md"
            canonical_path.write_bytes(payload)
            local_prepared = LocalPreparedArtifact(
                source_artifact=canonical,
                rag_artifact=canonical,
                prepared_artifact=canonical,
                prepared_path=canonical_path,
            )
            projector = ProjectDocumentForRag(
                prepare_document=PrepareDocument(
                    processor=MarkdownRagProjectionProcessorAdapter(
                        source_store=store,
                        materialization_root=root / "projection-scratch",
                    ),
                    artifact_store=store,
                    records=SQLiteProcessingRecordAdapter(
                        root / "processing.sqlite3"
                    ),
                ),
                profile=build_markdown_rag_projection_profile(),
            )
            workspace = LocalAnalysisTaskWorkspaceAdapter(
                str(root / "tasks")
            ).create(execution)

            def downloader(
                _url: str,
                file_name: str,
                directory: str,
                _timeout: float,
                _maximum: int,
            ) -> str:
                target = Path(directory) / file_name
                target.write_bytes(payload)
                return str(target)

            adapter = LegacyAnalysisFilePreparationAdapter(
                downloader=downloader,
                document_preparer=_PreparedArtifactStub(
                    artifact_store=store,
                    prepared=local_prepared,
                ),
                rag_projector=projector,
            )

            prepared = adapter.prepare(
                AnalysisFilePreparationRequest(
                    execution=execution,
                    source_url="https://example.invalid/source.md",
                    task_root=workspace.root_path,
                    document_processing_policy=(
                        AnalysisDocumentProcessingPolicySnapshot.for_source(
                            "https://example.invalid/source.md",
                            business_file_name=execution.file_name,
                        )
                    ),
                )
            )

            self.assertEqual(payload.decode("utf-8"), prepared.original_text)
            self.assertEqual(canonical, prepared.prepared_artifact)
            self.assertIsNotNone(prepared.rag_upload_artifact)
            assert prepared.rag_upload_artifact is not None
            self.assertNotEqual(canonical, prepared.rag_upload_artifact)
            self.assertEqual(
                DocumentRepresentation.MARKDOWN,
                prepared.rag_upload_artifact.representation,
            )
            projected = Path(prepared.upload_path).read_text(encoding="utf-8")
            self.assertNotIn("aGVsbG8=", projected)
            self.assertNotIn("内嵌图片已移除", projected)
            self.assertNotIn("payload_bytes=", projected)
            self.assertNotIn("sha256=", projected)
            self.assertNotIn("舰徽", projected)
            with store.open_reader(canonical) as reader:
                self.assertEqual(payload, reader.read())

    def test_upload_intent_checkpoint_failure_prevents_rag_execute(self) -> None:
        execution = AnalysisExecutionRef(
            task_id=TaskId("analysis-upload-cas-failure"),
            file_name="business-hash.md",
            batch_id="2" * 32,
            batch_sequence=1,
        )
        state = _RagWorkflowState(
            session=AnalysisRagSessionRef(
                execution=execution,
                session_ref="context::conversation",
                context_ref="context",
                conversation_ref="conversation",
            ),
            document_upload_intent_checkpoint=lambda: (_ for _ in ()).throw(
                RuntimeError("resource CAS failed")
            ),
        )
        rag = _CountingRag()

        with self.assertRaisesRegex(RuntimeError, "resource CAS failed"):
            _AnalysisModelWorkflow().execute_rag(
                execution=execution,
                state=state,
                rag=rag,
                operation=AnalysisRagOperation.COMBINED,
                prompt="仅用于离线门禁测试",
                max_model_calls=1,
            )

        self.assertEqual(0, rag.execute_count)
        self.assertTrue(state.preserve_scene)

    def test_pdf_text_failure_reuses_exact_pdf_without_markdown_projection(self) -> None:
        """两级文本提取明确失败时继续上传真实 PDF，且不得伪装成 .md。"""

        pdf_payload = b"%PDF-1.7\n% offline fixture\n"
        with workspace_tempdir() as temporary:
            root = Path(temporary)
            task_id = TaskId("analysis-rag-pdf-fallback")
            execution = AnalysisExecutionRef(
                task_id=task_id,
                file_name="business-hash.pdf",
                batch_id="4" * 32,
                batch_sequence=1,
            )
            store = LocalArtifactStoreAdapter(root / "artifacts")
            pdf_artifact = self._publish(
                store,
                task_id=task_id,
                payload=pdf_payload,
                kind=ArtifactKind.SOURCE,
                representation=DocumentRepresentation.PDF,
                media_type="application/pdf",
            )
            canonical_path = root / "source.pdf"
            canonical_path.write_bytes(pdf_payload)
            local_prepared = LocalPreparedArtifact(
                source_artifact=pdf_artifact,
                rag_artifact=pdf_artifact,
                prepared_artifact=None,
                prepared_path=canonical_path,
            )
            projector = ProjectDocumentForRag(
                prepare_document=PrepareDocument(
                    processor=MarkdownRagProjectionProcessorAdapter(
                        source_store=store,
                        materialization_root=root / "projection-scratch",
                    ),
                    artifact_store=store,
                    records=SQLiteProcessingRecordAdapter(
                        root / "processing.sqlite3"
                    ),
                ),
                profile=build_markdown_rag_projection_profile(),
            )
            workspace = LocalAnalysisTaskWorkspaceAdapter(
                str(root / "tasks")
            ).create(execution)

            def downloader(
                _url: str,
                file_name: str,
                directory: str,
                _timeout: float,
                _maximum: int,
            ) -> str:
                target = Path(directory) / file_name
                target.write_bytes(pdf_payload)
                return str(target)

            prepared = LegacyAnalysisFilePreparationAdapter(
                downloader=downloader,
                document_preparer=_PreparedArtifactStub(
                    artifact_store=store,
                    prepared=local_prepared,
                ),
                rag_projector=projector,
            ).prepare(
                AnalysisFilePreparationRequest(
                    execution=execution,
                    source_url="https://example.invalid/source.pdf",
                    task_root=workspace.root_path,
                )
            )

            self.assertEqual("", prepared.original_text)
            self.assertIsNone(prepared.prepared_artifact)
            self.assertEqual(pdf_artifact, prepared.rag_upload_artifact)
            self.assertEqual("", prepared.rag_projection_profile_id)
            self.assertEqual(".pdf", Path(prepared.upload_path).suffix)
            self.assertEqual(pdf_payload, Path(prepared.upload_path).read_bytes())

    def test_resource_upload_descriptor_tracks_three_crash_boundaries(self) -> None:
        """资源事实可区分未上传、已开始但未知、已确认三个中断点。"""

        with workspace_tempdir() as temporary:
            root = Path(temporary)
            task_id = TaskId("analysis-upload-checkpoints")
            execution = AnalysisExecutionRef(
                task_id=task_id,
                file_name="business-hash.md",
                batch_id="3" * 32,
                batch_sequence=1,
            )
            store = LocalArtifactStoreAdapter(root / "artifacts")
            artifact = self._publish(
                store,
                task_id=task_id,
                payload=b"# projected\n",
                kind=ArtifactKind.RAG_PROJECTION,
                representation=DocumentRepresentation.MARKDOWN,
                media_type="text/markdown; charset=utf-8",
            )
            descriptor = AnalysisRagUploadDescriptor(
                artifact=artifact,
                representation=DocumentRepresentation.MARKDOWN,
                media_type=artifact.metadata.media_type,
                transport_file_name="原始资料.md",
                display_title="原始资料.pdf",
                projection_profile_id="a" * 64,
            )
            resource_store = _InMemoryResourceStore()
            lifecycle = AnalysisResourceLifecycle(
                store=resource_store,
                execution=execution,
            )
            state = _RagWorkflowState()

            lifecycle.register(
                task_root=str(root / "task"),
                source_path=str(root / "task" / "source.pdf"),
                processing_path=str(root / "task" / "prepared.md"),
                upload_path=str(root / "task" / "prepared.md"),
                state=state,
                upload_descriptor=descriptor,
            )
            assert resource_store.record is not None
            registered = resource_store.record.record_payload.to_dict()
            self.assertEqual(3, registered["schema_version"])
            self.assertEqual("not_started", registered["upload"]["delivery_state"])
            self.assertEqual(
                artifact.artifact_id,
                registered["upload"]["artifact"]["artifact_id"],
            )

            lifecycle.prepare_document_upload()
            assert resource_store.record is not None
            started = resource_store.record.record_payload.to_dict()
            self.assertEqual(
                "started_unknown",
                started["upload"]["delivery_state"],
            )

            state.session = AnalysisRagSessionRef(
                execution=execution,
                session_ref="context::conversation",
                context_ref="context",
                conversation_ref="conversation",
                document_ref="document",
                document_location="location",
                content_sha256=artifact.metadata.sha256,
                ingested_file_name="原始资料.md",
                structured_source_key="docsense_ref:" + "a" * 32,
            )
            lifecycle.checkpoint_rag_state(state)
            assert resource_store.record is not None
            confirmed = resource_store.record.record_payload.to_dict()
            self.assertEqual("confirmed", confirmed["upload"]["delivery_state"])
            self.assertEqual(
                AnalysisResourceState.TRACKING,
                resource_store.record.state,
            )

    def test_uploaded_document_name_mismatch_is_quarantined(self) -> None:
        """Provider 返回的实际上传名漂移时必须隔离现场，禁止继续知识转交。"""

        with workspace_tempdir() as temporary:
            root = Path(temporary)
            task_id = TaskId("analysis-upload-name-mismatch")
            execution = AnalysisExecutionRef(
                task_id=task_id,
                file_name="business-hash.md",
                batch_id="4" * 32,
                batch_sequence=1,
            )
            store = LocalArtifactStoreAdapter(root / "artifacts")
            artifact = self._publish(
                store,
                task_id=task_id,
                payload=b"# projected\n",
                kind=ArtifactKind.RAG_PROJECTION,
                representation=DocumentRepresentation.MARKDOWN,
                media_type="text/markdown; charset=utf-8",
            )
            descriptor = AnalysisRagUploadDescriptor(
                artifact=artifact,
                representation=DocumentRepresentation.MARKDOWN,
                media_type=artifact.metadata.media_type,
                transport_file_name="原始资料.md",
                display_title="原始资料.pdf",
                projection_profile_id="b" * 64,
            )
            resource_store = _InMemoryResourceStore()
            lifecycle = AnalysisResourceLifecycle(
                store=resource_store,
                execution=execution,
            )
            state = _RagWorkflowState()
            lifecycle.register(
                task_root=str(root / "task"),
                source_path=str(root / "task" / "source.pdf"),
                processing_path=str(root / "task" / "prepared.md"),
                upload_path=str(root / "task" / "prepared.md"),
                state=state,
                upload_descriptor=descriptor,
            )
            lifecycle.prepare_document_upload()
            state.session = AnalysisRagSessionRef(
                execution=execution,
                session_ref="context::conversation",
                context_ref="context",
                conversation_ref="conversation",
                document_ref="document",
                document_location="location",
                content_sha256=artifact.metadata.sha256,
                ingested_file_name="provider-rewritten.md",
                structured_source_key="docsense_ref:" + "a" * 32,
            )

            with self.assertRaisesRegex(
                AnalysisResourceLifecycleError,
                "文档身份",
            ):
                lifecycle.checkpoint_rag_state(state)

            assert resource_store.record is not None
            payload = resource_store.record.record_payload.to_dict()
            self.assertTrue(state.preserve_scene)
            self.assertEqual(
                AnalysisResourceState.QUARANTINED,
                resource_store.record.state,
            )
            self.assertEqual(
                "uploaded_document_identity_mismatch",
                payload["diagnosis"]["reason"],
            )


if __name__ == "__main__":
    unittest.main()
