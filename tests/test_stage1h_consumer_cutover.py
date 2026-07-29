"""阶段 1H-6 业务消费者单路径切换门禁。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.modules.analysis.adapters import (
    ArtifactAnalysisTranslationAdapter,
    LegacyAnalysisFilePreparationAdapter,
)
from app.modules.analysis.ports import (
    AnalysisExecutionRef,
    AnalysisFilePreparationRequest,
    AnalysisTranslationKind,
    AnalysisTranslationOutcome,
    AnalysisTranslationRequest,
)
from app.modules.document_processing import (
    DocumentProcessingResult,
    DocumentRepresentation,
    LegacyOfficeConfig,
    LibreOfficeLegacyOfficePreparer,
    ProcessingOutcome,
)
from app.modules.document_processing.adapters import (
    FIFOCapacityAdapter,
    LocalArtifactStoreAdapter,
    LocalDocumentPreparationAdapter,
    LocalDocumentPreparationError,
    LocalDocumentPreparationRequest,
    ScannedPDFEngine,
    SQLiteProcessingRecordAdapter,
)
from app.modules.report.adapters import (
    LegacyReportFileAdapter,
    LocalReportArtifactAdapter,
)
from app.modules.report.ports import ReportArtifactCategory
from app.modules.tasks.domain import TaskId
from app.modules.translation.adapters import (
    SafeHTMLTranslationRendererAdapter,
)
from app.modules.translation.application import TranslatePreparedDocument
from app.modules.translation.domain import TranslationMode


class _RecordingTranslationEngine:
    """不访问网络的 TranslationEngine，并记录调用范围。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, TranslationMode]] = []

    @property
    def engine_id(self) -> str:
        return "stage1h-cutover-fake"

    @property
    def engine_fingerprint(self) -> str:
        return "stage1h-cutover-fake-v1"

    def translate(
        self,
        text: str,
        *,
        target_language: str,
        mode: TranslationMode,
    ) -> str:
        self.calls.append((text, target_language, mode))
        return f"译文:{text}"


class _ResultApplication:
    """返回预设 Processing 结果，用于验证编排是否越过 unknown 或重复降级。"""

    def __init__(
        self,
        outcome: ProcessingOutcome,
        *,
        error_code: str,
    ) -> None:
        self._outcome = outcome
        self._error_code = error_code
        self.calls = 0

    def execute(self, request) -> DocumentProcessingResult:
        self.calls += 1
        return DocumentProcessingResult(
            outcome=self._outcome,
            step_key=request.step_key,
            error_code=self._error_code,
        )


def _execution(task_id: str, *, file_name: str = "sample.txt") -> AnalysisExecutionRef:
    return AnalysisExecutionRef(
        task_id=TaskId(task_id),
        file_name=file_name,
        batch_id="a" * 32,
        batch_sequence=1,
    )


class Stage1HConsumerCutoverTests(unittest.TestCase):
    """证明 Report/Analysis/Translation 共享一份 prepared Artifact。"""

    def _document_preparer(
        self,
        root: Path,
    ) -> LocalDocumentPreparationAdapter:
        return LocalDocumentPreparationAdapter(
            artifact_store=LocalArtifactStoreAdapter(root / "artifacts"),
            records=SQLiteProcessingRecordAdapter(root / "processing.sqlite3"),
            resource=FIFOCapacityAdapter(1),
            legacy_office_preparer=LibreOfficeLegacyOfficePreparer(
                LegacyOfficeConfig.disabled(jobs_root=root / "office-jobs")
            ),
            materialization_root=root / "materializations",
            legacy_policy_fingerprint="stage1h-cutover-policy-v1",
            ocr_languages="chi_sim+eng",
            ocr_dpi=300,
        )

    def test_local_pipeline_reuses_same_text_artifact(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.txt"
            source.write_text("Hello prepared Artifact", encoding="utf-8")
            preparer = self._document_preparer(root)
            request = LocalDocumentPreparationRequest(
                task_id=TaskId("stage1h-cutover-local"),
                source_path=source,
                logical_step="input",
                trace_id="stage1h-cutover-local-trace",
            )

            first = preparer.prepare(request)
            second = preparer.prepare(request)

            self.assertEqual(
                first.prepared_artifact,
                second.prepared_artifact,
            )
            self.assertEqual(
                b"Hello prepared Artifact",
                first.prepared_path.read_bytes(),
            )
            self.assertEqual(first.prepared_artifact, first.rag_artifact)

    def test_local_pipeline_publishes_single_read_source_snapshot(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.txt"
            source.write_text("accepted-bytes", encoding="utf-8")
            preparer = self._document_preparer(root)
            real_publish = preparer.artifact_store.publish
            publish_calls = 0

            def publish_after_host_mutation(publication, content):
                nonlocal publish_calls
                publish_calls += 1
                if publish_calls == 1:
                    source.write_text("mutated-host-bytes", encoding="utf-8")
                return real_publish(publication, content)

            with patch.object(
                preparer.artifact_store,
                "publish",
                side_effect=publish_after_host_mutation,
            ):
                result = preparer.prepare(
                    LocalDocumentPreparationRequest(
                        task_id=TaskId("stage1h-source-snapshot"),
                        source_path=source,
                        logical_step="input",
                        trace_id="stage1h-source-snapshot-trace",
                    )
                )

            with preparer.artifact_store.open_reader(
                result.source_artifact
            ) as reader:
                self.assertEqual(b"accepted-bytes", reader.read())
            self.assertEqual(b"accepted-bytes", result.prepared_path.read_bytes())
            snapshot_root = root / "materializations" / "source-snapshots"
            self.assertEqual([], list(snapshot_root.iterdir()))

    def test_scanned_pdf_confirmed_failures_fallback_to_source_for_rag_only(
        self,
    ) -> None:
        """Analysis 保留 MinerU -> OCR -> 原 PDF，且 PDF 不冒充文本 Artifact。"""

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "scan.pdf"
            source.write_bytes(b"%PDF-1.7\nstage1h-r-fallback")
            preparer = self._document_preparer(root / "document")
            mineru = _ResultApplication(
                ProcessingOutcome.FAILED,
                error_code="mineru_confirmed_failed",
            )
            ocr = _ResultApplication(
                ProcessingOutcome.FAILED,
                error_code="ocr_confirmed_failed",
            )
            preparer._mineru = mineru  # type: ignore[attr-defined]
            preparer._ocr = ocr  # type: ignore[attr-defined]

            with patch(
                "app.modules.document_processing.adapters.local_pipeline."
                "is_scanned_pdf",
                return_value=True,
            ):
                result = preparer.prepare(
                    LocalDocumentPreparationRequest(
                        task_id=TaskId("stage1h-r-analysis-pdf-fallback"),
                        source_path=source,
                        logical_step="input",
                        trace_id="stage1h-r-analysis-pdf-fallback",
                    )
                )

            self.assertEqual(1, mineru.calls)
            self.assertEqual(1, ocr.calls)
            self.assertIsNone(result.prepared_artifact)
            self.assertEqual(result.source_artifact, result.rag_artifact)
            self.assertIs(
                DocumentRepresentation.PDF,
                result.rag_artifact.representation,
            )
            self.assertEqual(source.read_bytes(), result.prepared_path.read_bytes())

    def test_scanned_pdf_unknown_never_starts_second_processor(self) -> None:
        """外部结果未知时必须先对账，不能以 OCR 降级制造第二次真实副作用。"""

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "scan.pdf"
            source.write_bytes(b"%PDF-1.7\nstage1h-r-unknown")
            preparer = self._document_preparer(root / "document")
            mineru = _ResultApplication(
                ProcessingOutcome.OUTCOME_UNKNOWN,
                error_code="mineru_submission_outcome_unknown",
            )
            ocr = _ResultApplication(
                ProcessingOutcome.FAILED,
                error_code="must_not_run",
            )
            preparer._mineru = mineru  # type: ignore[attr-defined]
            preparer._ocr = ocr  # type: ignore[attr-defined]

            with (
                patch(
                    "app.modules.document_processing.adapters.local_pipeline."
                    "is_scanned_pdf",
                    return_value=True,
                ),
                self.assertRaises(LocalDocumentPreparationError) as raised,
            ):
                preparer.prepare(
                    LocalDocumentPreparationRequest(
                        task_id=TaskId("stage1h-r-analysis-pdf-unknown"),
                        source_path=source,
                        logical_step="input",
                        trace_id="stage1h-r-analysis-pdf-unknown",
                    )
                )

            self.assertIs(
                ProcessingOutcome.OUTCOME_UNKNOWN,
                raised.exception.outcome,
            )
            self.assertEqual(1, mineru.calls)
            self.assertEqual(0, ocr.calls)

    def test_report_builtin_ocr_failure_falls_back_to_source_pdf(self) -> None:
        """Report 的既有顺序是内置 OCR -> 原 PDF，不额外提交 MinerU。"""

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "scan.pdf"
            source.write_bytes(b"%PDF-1.7\nstage1h-r-report-fallback")
            preparer = self._document_preparer(root / "document")
            mineru = _ResultApplication(
                ProcessingOutcome.FAILED,
                error_code="must_not_run",
            )
            ocr = _ResultApplication(
                ProcessingOutcome.FAILED,
                error_code="ocr_confirmed_failed",
            )
            preparer._mineru = mineru  # type: ignore[attr-defined]
            preparer._ocr = ocr  # type: ignore[attr-defined]

            with patch(
                "app.modules.document_processing.adapters.local_pipeline."
                "is_scanned_pdf",
                return_value=True,
            ):
                result = preparer.prepare(
                    LocalDocumentPreparationRequest(
                        task_id=TaskId("stage1h-r-report-pdf-fallback"),
                        source_path=source,
                        logical_step="input",
                        trace_id="stage1h-r-report-pdf-fallback",
                        scanned_pdf_engine=ScannedPDFEngine.BUILTIN_OCR,
                    )
                )

            self.assertEqual(0, mineru.calls)
            self.assertEqual(1, ocr.calls)
            self.assertIsNone(result.prepared_artifact)
            self.assertEqual(result.source_artifact, result.rag_artifact)

    def test_report_runs_document_processing_once_then_only_maps_rag_input(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_file = root / "report-source.txt"
            source_file.write_text("Report input", encoding="utf-8")
            document_preparer = self._document_preparer(root / "document")
            report_store = LocalReportArtifactAdapter(root / "report")
            scope = report_store.begin(TaskId("stage1h-cutover-report"))
            source = report_store.publish_file(
                scope,
                category=ReportArtifactCategory.SOURCE,
                source_path=source_file,
                file_name="0001.txt",
                sequence_no=1,
            )
            calls = 0
            original_prepare = document_preparer.prepare

            def recording_prepare(request):
                nonlocal calls
                calls += 1
                return original_prepare(request)

            document_preparer.prepare = recording_prepare  # type: ignore[method-assign]
            adapter = LegacyReportFileAdapter(
                report_store,
                document_preparer=document_preparer,
                upload_preparer=lambda _: self.fail(
                    "新 Report 路径不得再次调用旧 OCR 上传准备器"
                ),
            )

            normalized = adapter.normalize_source(source)
            rag_inputs = adapter.prepare_upload_files(normalized)

            self.assertEqual(1, calls)
            self.assertEqual(1, len(rag_inputs))
            self.assertEqual(
                report_store.resolve_path(normalized).read_bytes(),
                report_store.resolve_path(rag_inputs[0]).read_bytes(),
            )

    def test_analysis_maps_one_prepared_artifact_to_all_consumers(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            document_preparer = self._document_preparer(root / "document")

            def downloader(_url, file_name, destination, _timeout, _max_bytes):
                path = Path(destination) / file_name
                path.write_text("Analysis prepared input", encoding="utf-8")
                return str(path)

            adapter = LegacyAnalysisFilePreparationAdapter(
                downloader=downloader,
                document_preparer=document_preparer,
                normalizer=lambda _: self.fail(
                    "新 Analysis 路径不得调用旧 normalizer"
                ),
                upload_preparer=lambda *_: self.fail(
                    "新 Analysis 路径不得调用旧 OCR 上传准备器"
                ),
                text_reader=lambda _: self.fail(
                    "正文必须从同一 Artifact 读取"
                ),
            )
            execution = _execution("stage1h-cutover-analysis")
            prepared = adapter.prepare(
                AnalysisFilePreparationRequest(
                    execution=execution,
                    source_url="https://example.invalid/sample.txt",
                    task_root=str(root / "analysis-task"),
                )
            )

            self.assertIsNotNone(prepared.prepared_artifact)
            self.assertEqual(prepared.processing_path, prepared.upload_path)
            self.assertEqual(
                "Analysis prepared input",
                prepared.original_text,
            )
            self.assertEqual(
                Path(prepared.upload_path).read_text(encoding="utf-8"),
                prepared.original_text,
            )

    def test_analysis_translation_uses_artifact_and_engine_boundaries(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "source.txt"
            source.write_text("English paragraph", encoding="utf-8")
            preparer = self._document_preparer(root / "document")
            artifact = preparer.prepare(
                LocalDocumentPreparationRequest(
                    task_id=TaskId("stage1h-cutover-translation"),
                    source_path=source,
                    logical_step="input",
                    trace_id="stage1h-cutover-translation-trace",
                )
            ).prepared_artifact
            engine = _RecordingTranslationEngine()
            renderer = SafeHTMLTranslationRendererAdapter()
            application = TranslatePreparedDocument(
                reader=preparer.artifact_store,
                engine=engine,
                renderer=renderer,
            )
            adapter = ArtifactAnalysisTranslationAdapter(
                document_translation=application,
                engine=engine,
                renderer=renderer,
                mode_resolver=lambda: TranslationMode.MACHINE,
            )
            execution = _execution(
                "stage1h-cutover-translation",
                file_name="source.txt",
            )

            document = adapter.translate(
                AnalysisTranslationRequest(
                    execution=execution,
                    kind=AnalysisTranslationKind.DOCUMENT,
                    prepared_artifact=artifact,
                )
            )
            summary = adapter.translate(
                AnalysisTranslationRequest(
                    execution=execution,
                    kind=AnalysisTranslationKind.SUMMARY,
                    text="<summary>",
                )
            )

            self.assertIs(
                AnalysisTranslationOutcome.SUCCEEDED,
                document.outcome,
            )
            self.assertIn("译文:English paragraph", document.document_translation_one)
            self.assertIs(
                AnalysisTranslationOutcome.SUCCEEDED,
                summary.outcome,
            )
            self.assertEqual(
                '<div class="translated-text">译文:&lt;summary&gt;</div>',
                summary.document_translation_one,
            )
            self.assertEqual(2, len(engine.calls))


if __name__ == "__main__":
    unittest.main()
