from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import zipfile

from app.modules.report.adapters.legacy_files import LegacyReportFileAdapter
from app.modules.report.adapters.local_artifacts import LocalReportArtifactAdapter
from app.modules.report.domain import (
    ReportArtifactError,
    ReportTemplateError,
)
from app.modules.report.ports import (
    ReportArtifactCategory,
    ReportArtifactRef,
    ReportSourceDownload,
    ReportTemplateDownload,
)
from app.modules.tasks.domain import TaskId
from app.services.utils.word_extractor import extract_text_from_word
from tests import workspace_tempdir
from tests.document_processing_fixtures import build_test_document_preparer


class ReportIoAdapterTests(unittest.TestCase):
    @staticmethod
    def _write_minimal_docx(path: Path, body_xml: str) -> None:
        """生成仅含 document.xml 的受控 DOCX，避免依赖 Office 主进程。"""

        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "[Content_Types].xml",
                """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""",
            )
            archive.writestr(
                "word/document.xml",
                f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>{body_xml}</w:body>
</w:document>""",
            )

    def test_word_template_extractor_reads_paragraphs_and_table_cells(self) -> None:
        """Report 当前 Adapter 依赖的 Word 文本边界必须保留段落与表格单元格。"""

        with workspace_tempdir() as tmp:
            docx_path = Path(tmp) / "template.docx"
            self._write_minimal_docx(
                docx_path,
                """
  <w:p><w:r><w:t>报告标题</w:t></w:r></w:p>
  <w:tbl>
    <w:tr>
      <w:tc><w:p><w:r><w:t>章节</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>要求</w:t></w:r></w:p></w:tc>
    </w:tr>
  </w:tbl>
""",
            )

            text = extract_text_from_word(str(docx_path))

        self.assertIn("报告标题", text)
        self.assertIn("章节", text)
        self.assertIn("要求", text)

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

            adapter = LegacyReportFileAdapter(
                artifacts,
                document_preparer=build_test_document_preparer(
                    root / "document-processing"
                ),
                downloader=downloader,
                word_extractor=lambda _: "模板正文",
            )
            task_id = TaskId("execution-download")
            scope = artifacts.begin(task_id)
            self.assertEqual([], downloads)

            source = adapter.download_source(
                ReportSourceDownload(scope, "http://files.local/a.txt?token=secret", 1)
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
            self.assertEqual(
                "download:http://files.local/a.txt?token=secret",
                artifacts.resolve_path(normalized).read_text("utf-8"),
            )
            self.assertEqual(
                artifacts.resolve_path(normalized).read_bytes(),
                artifacts.resolve_path(prepared[0]).read_bytes(),
            )
            self.assertEqual("模板正文", adapter.extract_template_text(template))

    def test_legacy_doc_template_is_rejected_without_download_or_conversion(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            artifacts = LocalReportArtifactAdapter(root / "artifacts")
            download_calls: list[str] = []

            def downloader(
                url: str,
                file_name: str,
                temp_root: str,
                timeout: float,
                max_bytes: int,
            ) -> str:
                download_calls.append(url)
                raise AssertionError("legacy .doc 模板不应进入下载")

            adapter = LegacyReportFileAdapter(
                artifacts,
                document_preparer=build_test_document_preparer(
                    root / "document-processing"
                ),
                downloader=downloader,
                word_extractor=lambda _: "模板",
            )
            scope = artifacts.begin(TaskId("execution-doc-template"))

            with self.assertRaises(ReportTemplateError):
                adapter.download_template(
                    ReportTemplateDownload(
                        scope,
                        "http://files.local/template.DOC?signature=private",
                    )
                )

            self.assertEqual([], download_calls)


if __name__ == "__main__":
    unittest.main()
