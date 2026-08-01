"""RAG-only Markdown 投影的离线功能、幂等、内存与并发门禁。"""

from __future__ import annotations

import hashlib
import tracemalloc
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.modules.document_processing.adapters import (
    BytesArtifactContent,
    FileArtifactContent,
    LocalArtifactStoreAdapter,
    MarkdownRagProjectionProcessorAdapter,
    SQLiteProcessingRecordAdapter,
    build_markdown_rag_projection_profile,
)
from app.modules.document_processing.application import (
    PrepareDocument,
    ProjectDocumentForRag,
)
from app.modules.document_processing.domain import (
    ArtifactKind,
    DocumentProcessingRequest,
    DocumentRepresentation,
    ProcessingOutcome,
    RagProjectionError,
)
from app.modules.document_processing.ports import ArtifactPublication
from app.modules.tasks.domain import TaskId
from tests import workspace_tempdir


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = (
    ROOT
    / "tests"
    / "assets"
    / "document_processing"
    / "rag_projection_base64.md"
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _build_runtime(temporary: str):
    root = Path(temporary)
    store = LocalArtifactStoreAdapter(root / "artifacts")
    processor = MarkdownRagProjectionProcessorAdapter(
        source_store=store,
        materialization_root=root / "scratch",
    )
    prepare = PrepareDocument(
        processor=processor,
        artifact_store=store,
        records=SQLiteProcessingRecordAdapter(root / "processing.sqlite3"),
    )
    project = ProjectDocumentForRag(
        prepare_document=prepare,
        profile=build_markdown_rag_projection_profile(),
    )
    return store, project


def _publish_source(
    store: LocalArtifactStoreAdapter,
    task_id: TaskId,
    payload: bytes,
):
    return store.publish(
        ArtifactPublication(
            task_id=task_id,
            step_key=_digest(f"{task_id.value}:prepared"),
            kind=ArtifactKind.PREPARED,
            representation=DocumentRepresentation.MARKDOWN,
            media_type="text/markdown; charset=utf-8",
        ),
        BytesArtifactContent(payload),
    )


class MarkdownRagProjectionTests(unittest.TestCase):
    """验证投影不污染 canonical Artifact，并保持确定性恢复事实。"""

    def test_removes_visible_data_uri_payload_and_preserves_markdown_structure(self) -> None:
        payload = FIXTURE.read_bytes()
        canonical_sha256 = hashlib.sha256(payload).hexdigest()
        with workspace_tempdir() as temporary:
            store, project = _build_runtime(temporary)
            source = _publish_source(
                store,
                TaskId("rag-projection-fixture"),
                payload,
            )

            result = project.execute(source, trace_id="rag-projection-fixture-trace")

            self.assertEqual(ProcessingOutcome.SUCCEEDED, result.outcome)
            self.assertIsNotNone(result.artifact)
            assert result.artifact is not None
            self.assertEqual(ArtifactKind.RAG_PROJECTION, result.artifact.kind)
            self.assertEqual(
                DocumentRepresentation.MARKDOWN,
                result.artifact.representation,
            )
            with store.open_reader(result.artifact) as reader:
                projected = reader.read().decode("utf-8")

            self.assertIn("# 舰艇资料", projected)
            self.assertIn("| 舰名 | 示例舰 |", projected)
            self.assertIn("正文前。", projected)
            self.assertIn("正文后。", projected)
            self.assertIn(
                "![外部图](https://example.invalid/image.png)",
                projected,
            )
            self.assertNotIn("aGVsbG8=", projected)
            self.assertNotIn("%%%not-valid-base64%%%", projected)
            self.assertIn("内嵌图片已移除：alt=舰徽", projected)
            self.assertIn(
                hashlib.sha256(b"hello").hexdigest(),
                projected,
            )
            # fenced 与四空格缩进代码不是可渲染图片，必须逐字保留。
            self.assertIn(
                "![示例](data:image/png;base64,Y29kZS1leGFtcGxl)",
                projected,
            )
            self.assertIn(
                "![示例](data:image/png;base64,aW5kZW50ZWQ=)",
                projected,
            )
            self.assertIn(
                "`![示例](data:image/png;base64,aW5saW5l)`",
                projected,
            )
            self.assertIn(
                r"\![示例](data:image/png;base64,ZXNjYXBlZA==)",
                projected,
            )

            self.assertEqual(canonical_sha256, source.metadata.sha256)
            self.assertTrue(store.verify(source))
            with store.open_reader(source) as reader:
                self.assertEqual(payload, reader.read())

    def test_same_source_and_profile_reuses_identical_projection(self) -> None:
        with workspace_tempdir() as temporary:
            store, project = _build_runtime(temporary)
            source = _publish_source(
                store,
                TaskId("rag-projection-idempotent"),
                b"# title\n\nplain text\n",
            )
            first = project.execute(source, trace_id="first-attempt")
            second = project.execute(source, trace_id="replayed-attempt")

            self.assertEqual(ProcessingOutcome.SUCCEEDED, first.outcome)
            self.assertEqual(ProcessingOutcome.SUCCEEDED, second.outcome)
            self.assertFalse(first.reused)
            self.assertTrue(second.reused)
            self.assertEqual(first.artifact, second.artifact)
            self.assertEqual(first.lineage, second.lineage)

    def test_invalid_utf8_fails_without_publishing_projection(self) -> None:
        with workspace_tempdir() as temporary:
            store, project = _build_runtime(temporary)
            source = _publish_source(
                store,
                TaskId("rag-projection-invalid-utf8"),
                b"# title\n\xff\n",
            )
            result = project.execute(source, trace_id="invalid-utf8")

            self.assertEqual(ProcessingOutcome.FAILED, result.outcome)
            self.assertEqual("rag_projection_utf8_invalid", result.error_code)
            self.assertIsNone(result.artifact)
            self.assertTrue(store.verify(source))

    def test_source_open_failure_does_not_leave_part_file(self) -> None:
        """源流尚未取得时不得提前占用目标句柄或遗留临时文件。"""

        with workspace_tempdir() as temporary:
            root = Path(temporary)
            scratch_root = root / "scratch"
            store = LocalArtifactStoreAdapter(root / "artifacts")
            processor = MarkdownRagProjectionProcessorAdapter(
                source_store=store,
                materialization_root=scratch_root,
            )
            task_id = TaskId("rag-projection-source-open-failure")
            source = _publish_source(store, task_id, b"# title\n")
            request = DocumentProcessingRequest(
                task_id=task_id,
                step_id="rag-projection",
                source_artifact=source,
                profile=build_markdown_rag_projection_profile(),
                trace_id="source-open-failure",
            )

            # 这里模拟 Artifact Store 在进入源读取上下文之前失败。Windows 下若目标文件
            # 已提前打开，异常清理会因句柄仍被占用而留下 .part 文件。
            with patch.object(
                store,
                "open_reader",
                side_effect=OSError("source unavailable"),
            ):
                with self.assertRaises(RagProjectionError) as raised:
                    processor.process(request)

            self.assertEqual(
                "rag_projection_unexpected_error",
                raised.exception.code,
            )
            self.assertEqual([], list(scratch_root.rglob("*.part")))
            self.assertEqual([], list(scratch_root.iterdir()))

    def test_same_task_outputs_use_full_namespace_and_distinct_short_names(
        self,
    ) -> None:
        """同任务并发候选必须隔离，且不能靠截断到 32 bit 的随机名碰运气。"""

        with workspace_tempdir() as temporary:
            root = Path(temporary)
            store = LocalArtifactStoreAdapter(root / "artifacts")
            processor = MarkdownRagProjectionProcessorAdapter(
                source_store=store,
                materialization_root=root / "scratch",
            )
            task_id = TaskId("rag-projection-same-task")
            source = _publish_source(store, task_id, b"# title\n")
            request = DocumentProcessingRequest(
                task_id=task_id,
                step_id="rag-projection",
                source_artifact=source,
                profile=build_markdown_rag_projection_profile(),
                trace_id="same-task-output",
            )

            first = processor.process(request)
            second = processor.process(request)
            try:
                with first.content.open_reader() as reader:
                    first_path = Path(reader.name)
                with second.content.open_reader() as reader:
                    second_path = Path(reader.name)

                self.assertNotEqual(first_path, second_path)
                self.assertEqual(64, len(first_path.parents[1].name))
                self.assertEqual(64, len(second_path.parents[1].name))
                self.assertLessEqual(len(first_path.name), 32)
                self.assertLessEqual(len(second_path.name), 32)
            finally:
                first.close()
                second.close()

    def test_ten_mib_data_uri_uses_bounded_memory_and_linear_streaming(self) -> None:
        with workspace_tempdir() as temporary:
            root = Path(temporary)
            input_path = root / "large.md"
            payload_chunk = b"A" * (64 * 1024)
            with input_path.open("wb") as writer:
                writer.write(b"# large\n\n![large](data:image/png;base64,")
                for _ in range(160):
                    writer.write(payload_chunk)
                writer.write(b")\n\nafter\n")

            store, project = _build_runtime(temporary)
            task_id = TaskId("rag-projection-large")
            source = store.publish(
                ArtifactPublication(
                    task_id=task_id,
                    step_key=_digest("rag-projection-large:prepared"),
                    kind=ArtifactKind.PREPARED,
                    representation=DocumentRepresentation.MARKDOWN,
                    media_type="text/markdown; charset=utf-8",
                ),
                FileArtifactContent(input_path),
            )

            tracemalloc.start()
            try:
                result = project.execute(source, trace_id="large-projection")
                _, peak_bytes = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()

            self.assertEqual(ProcessingOutcome.SUCCEEDED, result.outcome)
            self.assertLess(
                peak_bytes,
                8 * 1024 * 1024,
                "10 MiB 单行 Base64 不应按整行或整文档载入内存",
            )
            assert result.artifact is not None
            self.assertLess(result.artifact.metadata.size_bytes, 1024)
            with store.open_reader(result.artifact) as reader:
                projected = reader.read()
            self.assertNotIn(payload_chunk, projected)
            self.assertIn(b"after", projected)

    def test_fifty_tasks_with_same_content_do_not_share_artifact_identity(self) -> None:
        with workspace_tempdir() as temporary:
            store, project = _build_runtime(temporary)
            payload = b"![same](data:image/png;base64,aGVsbG8=)\n"
            sources = tuple(
                _publish_source(
                    store,
                    TaskId(f"rag-projection-concurrent-{index:02d}"),
                    payload,
                )
                for index in range(50)
            )

            def execute(index: int):
                return project.execute(
                    sources[index],
                    trace_id=f"rag-projection-concurrent-trace-{index:02d}",
                )

            with ThreadPoolExecutor(max_workers=50) as executor:
                results = tuple(executor.map(execute, range(50)))

            self.assertTrue(
                all(result.outcome is ProcessingOutcome.SUCCEEDED for result in results)
            )
            artifacts = tuple(result.artifact for result in results)
            self.assertTrue(all(artifact is not None for artifact in artifacts))
            self.assertEqual(
                50,
                len({artifact.artifact_id for artifact in artifacts if artifact}),
            )
            self.assertEqual(
                50,
                len({artifact.task_id for artifact in artifacts if artifact}),
            )
            self.assertTrue(
                all(store.verify(artifact) for artifact in artifacts if artifact)
            )


if __name__ == "__main__":
    unittest.main()
