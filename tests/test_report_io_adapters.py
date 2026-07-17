from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.modules.report.adapters.legacy_files import LegacyReportFileAdapter
from app.modules.report.adapters.local_artifacts import LocalReportArtifactAdapter
from app.modules.report.domain import ReportArtifactError, ReportSourceNormalizationError
from app.modules.report.ports import (
    ReportArtifactCategory,
    ReportArtifactRef,
    ReportSourceDownload,
    ReportTemplateDownload,
)
from app.modules.tasks.domain import TaskId
from tests import workspace_tempdir


class ReportIoAdapterTests(unittest.TestCase):
    def test_begin_allocates_namespace_without_creating_task_directory(self) -> None:
        """资源 Store 登记成功前，分配 scope 不得制造无主 Artifact 目录。"""

        with workspace_tempdir() as tmp:
            adapter = LocalReportArtifactAdapter(Path(tmp) / "artifacts")
            scope = adapter.begin(TaskId("execution-no-side-effect"))

            self.assertFalse((adapter.root / scope.namespace).exists())

    def test_artifact_namespaces_are_isolated_under_fifty_concurrent_tasks(self) -> None:
        """同根目录的 50 个 execution 不得共享目录或覆盖最终报告。"""

        with workspace_tempdir() as tmp:
            adapter = LocalReportArtifactAdapter(Path(tmp) / "artifacts")
            task_ids = tuple(TaskId(f"task/{index}:unsafe") for index in range(50))

            def persist(task_id: TaskId) -> tuple[str, str, str]:
                scope = adapter.begin(task_id)
                report = adapter.persist_report_html(scope, f"<p>{task_id.value}</p>")
                return scope.namespace, report.artifact_id, adapter.resolve_path(report).read_text(
                    encoding="utf-8"
                )

            with ThreadPoolExecutor(max_workers=16) as executor:
                results = tuple(executor.map(persist, task_ids))

            self.assertEqual(50, len({item[0] for item in results}))
            self.assertEqual({"output/report.html"}, {item[1] for item in results})
            self.assertEqual(
                {f"<p>{task_id.value}</p>" for task_id in task_ids},
                {item[2] for item in results},
            )
            self.assertFalse(any("task/" in item[0] for item in results))

    def test_artifact_cleanup_removes_scratch_but_retains_final_report(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            adapter = LocalReportArtifactAdapter(root / "artifacts")
            task_id = TaskId("execution-001")
            scope = adapter.begin(task_id)
            seed = root / "seed.pdf"
            seed.write_bytes(b"pdf-content")
            scratch = adapter.publish_file(
                scope,
                category=ReportArtifactCategory.SOURCE,
                source_path=seed,
                file_name="0001.pdf",
                sequence_no=1,
            )
            report = adapter.persist_report_html(scope, "<p>done</p>")

            result = adapter.cleanup_unretained(scope, retain=(report,))

            self.assertIn(scratch.artifact_id, {item.artifact_id for item in result.cleaned})
            self.assertEqual((), result.pending)
            self.assertFalse((adapter.root / scope.namespace / scratch.artifact_id).exists())
            self.assertEqual("<p>done</p>", adapter.resolve_path(report).read_text("utf-8"))
            self.assertEqual(64, len(report.checksum))

    def test_artifact_cleanup_keeps_missing_retained_report_pending(self) -> None:
        """终态引用仍在但文件丢失时，不能把资源记录误判为已经清理完成。"""

        with workspace_tempdir() as tmp:
            adapter = LocalReportArtifactAdapter(Path(tmp) / "artifacts")
            scope = adapter.begin(TaskId("execution-missing-retained"))
            report = adapter.persist_report_html(scope, "<p>done</p>")
            adapter.resolve_path(report).unlink()

            result = adapter.cleanup_unretained(scope, retain=(report,))

            self.assertEqual((), result.cleaned)
            self.assertEqual((report,), result.pending)

    def test_artifact_cleanup_keeps_tampered_retained_report_pending(self) -> None:
        """最终报告内容变化后，即使路径仍存在，也必须通过摘要差异暴露损坏。"""

        with workspace_tempdir() as tmp:
            adapter = LocalReportArtifactAdapter(Path(tmp) / "artifacts")
            scope = adapter.begin(TaskId("execution-tampered-retained"))
            report = adapter.persist_report_html(scope, "<p>done</p>")
            adapter.resolve_path(report).write_text("<p>tampered</p>", encoding="utf-8")

            result = adapter.cleanup_unretained(scope, retain=(report,))

            self.assertEqual((), result.cleaned)
            self.assertEqual((report,), result.pending)

    def test_artifact_resolver_rejects_forged_cross_category_reference(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            adapter = LocalReportArtifactAdapter(root / "artifacts")
            task_id = TaskId("execution-002")
            scope = adapter.begin(task_id)
            seed = root / "seed.bin"
            seed.write_bytes(b"content")
            stored = adapter.publish_file(
                scope,
                category=ReportArtifactCategory.SOURCE,
                source_path=seed,
                file_name="0001.bin",
                sequence_no=1,
            )
            forged = ReportArtifactRef(
                task_id=task_id,
                artifact_id=stored.artifact_id,
                category=ReportArtifactCategory.RAG_INPUT,
                sequence_no=1,
            )

            with self.assertRaises(ReportArtifactError):
                adapter.resolve_path(forged)

    def test_legacy_file_adapter_downloads_at_call_time_and_republishes_every_stage(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            artifacts = LocalReportArtifactAdapter(root / "artifacts")
            downloads: list[tuple[str, float]] = []

            def downloader(
                url: str,
                file_name: str,
                temp_root: str,
                timeout: float,
                max_bytes: int,
            ) -> str:
                downloads.append((url, timeout))
                self.assertGreater(max_bytes, 0)
                target = Path(temp_root) / file_name
                target.write_bytes(f"download:{url}".encode("utf-8"))
                return str(target)

            def normalizer(file_path: str) -> str:
                output = Path(file_path).with_suffix(".normalized.md")
                output.write_text("normalized", encoding="utf-8")
                return str(output)

            def upload_preparer(file_path: str) -> list[str]:
                output = Path(file_path).with_suffix(".ocr.md")
                output.write_text("ocr", encoding="utf-8")
                return [str(output)]

            adapter = LegacyReportFileAdapter(
                artifacts,
                downloader=downloader,
                normalizer=normalizer,
                upload_preparer=upload_preparer,
                word_extractor=lambda _: "模板正文",
            )
            task_id = TaskId("execution-download")
            scope = artifacts.begin(task_id)
            self.assertEqual([], downloads)

            source = adapter.download_source(
                ReportSourceDownload(scope, "http://files.local/a.pdf?token=secret", 1)
            )
            normalized = adapter.normalize_source(source)
            prepared = adapter.prepare_upload_files(normalized)
            template = adapter.download_template(
                ReportTemplateDownload(scope, "http://files.local/template")
            )

            self.assertEqual(2, len(downloads))
            self.assertEqual(60.0, downloads[0][1])
            self.assertEqual(ReportArtifactCategory.SOURCE, source.category)
            self.assertEqual(ReportArtifactCategory.NORMALIZED_SOURCE, normalized.category)
            self.assertEqual(ReportArtifactCategory.RAG_INPUT, prepared[0].category)
            self.assertEqual(ReportArtifactCategory.TEMPLATE, template.category)
            self.assertEqual(1, source.sequence_no)
            self.assertEqual(1, normalized.sequence_no)
            self.assertEqual(1, prepared[0].sequence_no)
            self.assertNotIn("secret", source.artifact_id)
            self.assertEqual("normalized", artifacts.resolve_path(normalized).read_text("utf-8"))
            self.assertEqual("ocr", artifacts.resolve_path(prepared[0]).read_text("utf-8"))
            self.assertEqual("模板正文", adapter.extract_template_text(template))

    def test_normalizer_failure_is_mapped_to_compatible_fallback_error(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            artifacts = LocalReportArtifactAdapter(root / "artifacts")

            def downloader(
                url: str,
                file_name: str,
                temp_root: str,
                timeout: float,
                max_bytes: int,
            ) -> str:
                self.assertGreater(max_bytes, 0)
                target = Path(temp_root) / file_name
                target.write_bytes(b"source")
                return str(target)

            adapter = LegacyReportFileAdapter(
                artifacts,
                downloader=downloader,
                normalizer=lambda _: (_ for _ in ()).throw(RuntimeError("boom")),
                upload_preparer=lambda path: [path],
                word_extractor=lambda _: "模板",
            )
            scope = artifacts.begin(TaskId("execution-normalizer"))
            source = adapter.download_source(
                ReportSourceDownload(scope, "http://files.local/a.mhtml", 1)
            )

            with self.assertRaises(ReportSourceNormalizationError):
                adapter.normalize_source(source)


if __name__ == "__main__":
    unittest.main()
