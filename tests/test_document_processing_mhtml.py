"""阶段 1H-3 MHTML 浏览器、降级与未知结果门禁。"""

from __future__ import annotations

import hashlib
import subprocess
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from app.modules.document_processing.adapters import (
    BytesArtifactContent,
    LocalArtifactStoreAdapter,
    SQLiteProcessingRecordAdapter,
)
from app.modules.document_processing.adapters.mhtml import (
    MHTMLBrowserPDFProcessorAdapter,
    MHTMLTextProcessorAdapter,
    MHTMLToPDFConverter,
    create_mhtml_browser_profile,
    create_mhtml_text_profile,
)
from app.modules.document_processing.application import (
    PrepareDocument,
    PrepareMHTMLDocument,
    PrepareMHTMLRequest,
)
from app.modules.document_processing.domain import (
    ArtifactKind,
    DocumentProcessingRequest,
    DocumentRepresentation,
    ProcessingOutcome,
)
from app.modules.document_processing.ports import ArtifactPublication
from app.modules.tasks.domain import TaskId
from tests import workspace_tempdir


_ASSET = (
    Path(__file__).resolve().parent
    / "assets"
    / "document_processing"
    / "simple.mhtml"
)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _requests(
    store: LocalArtifactStoreAdapter,
    index: int = 0,
) -> PrepareMHTMLRequest:
    task_id = TaskId(f"stage1h-mhtml-{index:02d}")
    source = store.publish(
        ArtifactPublication(
            task_id=task_id,
            step_key=_digest(f"mhtml-source-{index}"),
            kind=ArtifactKind.SOURCE,
            representation=DocumentRepresentation.ORIGINAL,
            media_type="multipart/related",
        ),
        BytesArtifactContent(_ASSET.read_bytes()),
    )
    return PrepareMHTMLRequest(
        browser_request=DocumentProcessingRequest(
            task_id=task_id,
            step_id="mhtml-browser-pdf",
            source_artifact=source,
            profile=create_mhtml_browser_profile(
                browser_fingerprint="fake-browser-v1"
            ),
            trace_id="trace-browser",
        ),
        fallback_request=DocumentProcessingRequest(
            task_id=task_id,
            step_id="mhtml-text-markdown",
            source_artifact=source,
            profile=create_mhtml_text_profile(),
            trace_id="trace-fallback",
        ),
    )


class MHTMLDocumentProcessingTests(unittest.TestCase):
    def _application(self, temporary: str, runner):
        root = Path(temporary)
        store = LocalArtifactStoreAdapter(root / "artifacts")
        records = SQLiteProcessingRecordAdapter(root / "llm_tasks.sqlite3")
        converter = MHTMLToPDFConverter(
            browser_path=str(root / "fake-browser.exe"),
            runner=runner,
        )
        browser = PrepareDocument(
            processor=MHTMLBrowserPDFProcessorAdapter(
                source_store=store,
                converter=converter,
                scratch_root=root / "mhtml-jobs",
            ),
            artifact_store=store,
            records=records,
        )
        fallback = PrepareDocument(
            processor=MHTMLTextProcessorAdapter(source_store=store),
            artifact_store=store,
            records=records,
        )
        return (
            store,
            records,
            PrepareMHTMLDocument(browser=browser, fallback=fallback),
        )

    def test_pdf_success_keeps_required_no_sandbox_argument(self) -> None:
        commands: list[list[str]] = []

        def runner(command, **_kwargs):
            commands.append(command)
            output_arg = next(
                item for item in command if item.startswith("--print-to-pdf=")
            )
            Path(output_arg.split("=", 1)[1]).write_bytes(
                b"%PDF-1.7\nstage1h\n"
            )
            return subprocess.CompletedProcess(command, 0, "", "")

        with workspace_tempdir() as temporary:
            store, _, application = self._application(temporary, runner)
            result = application.execute(_requests(store))

            self.assertEqual(ProcessingOutcome.SUCCEEDED, result.outcome)
            assert result.artifact is not None
            self.assertEqual(
                DocumentRepresentation.PDF,
                result.artifact.representation,
            )
        self.assertIn("--no-sandbox", commands[0])

    def test_confirmed_browser_failure_automatically_falls_back_to_markdown(
        self,
    ) -> None:
        def runner(command, **_kwargs):
            return subprocess.CompletedProcess(command, 17, "", "failed")

        with workspace_tempdir() as temporary:
            store, records, application = self._application(temporary, runner)
            command = _requests(store)
            result = application.execute(command)

            self.assertEqual(ProcessingOutcome.SUCCEEDED, result.outcome)
            assert result.artifact is not None
            self.assertEqual(
                DocumentRepresentation.MARKDOWN,
                result.artifact.representation,
            )
            with store.open_reader(result.artifact) as reader:
                text = reader.read().decode("utf-8")
            self.assertIn("Hello Document Processing", text)
            primary = records.get(command.browser_request.step_key)
            fallback = records.get(command.fallback_request.step_key)
            self.assertEqual("failed", primary.state.value)  # type: ignore[union-attr]
            self.assertEqual("succeeded", fallback.state.value)  # type: ignore[union-attr]

    def test_timeout_remains_unknown_and_never_starts_fallback(self) -> None:
        def runner(command, **_kwargs):
            raise subprocess.TimeoutExpired(command, 60)

        with workspace_tempdir() as temporary:
            store, records, application = self._application(temporary, runner)
            command = _requests(store)
            result = application.execute(command)

            self.assertEqual(
                ProcessingOutcome.OUTCOME_UNKNOWN,
                result.outcome,
            )
            self.assertEqual(
                "mhtml_browser_timeout_outcome_unknown",
                result.error_code,
            )
            self.assertIsNone(records.get(command.fallback_request.step_key))
            scratch = Path(temporary) / "mhtml-jobs"
            self.assertEqual(1, len(list(scratch.iterdir())))

    def test_existing_scratch_is_never_claimed_or_deleted(self) -> None:
        cases = {
            "unmarked": None,
            "foreign": "FOREIGN_OWNER\n",
            "previous-docsense": "DOCSENSE_MHTML_JOB_V1\n",
        }
        for index, (case_name, marker_value) in enumerate(cases.items(), start=70):
            with self.subTest(case=case_name), workspace_tempdir() as temporary:
                runner_calls = 0

                def runner(command, **_kwargs):
                    nonlocal runner_calls
                    runner_calls += 1
                    return subprocess.CompletedProcess(command, 0, "", "")

                store, records, application = self._application(temporary, runner)
                command = _requests(store, index=index)
                job = (
                    Path(temporary)
                    / "mhtml-jobs"
                    / f"job-{command.browser_request.step_key}"
                )
                job.mkdir(parents=True)
                sentinel = job / "sentinel.bin"
                sentinel.write_bytes(b"must-survive")
                if marker_value is not None:
                    (job / ".docsense-mhtml-job").write_text(
                        marker_value,
                        encoding="ascii",
                    )

                result = application.execute(command)

                self.assertEqual(ProcessingOutcome.OUTCOME_UNKNOWN, result.outcome)
                self.assertEqual("mhtml_scratch_ownership_conflict", result.error_code)
                self.assertEqual(0, runner_calls)
                self.assertEqual(b"must-survive", sentinel.read_bytes())
                self.assertIsNone(records.get(command.fallback_request.step_key))

    def test_mhtml_signature_detection_does_not_read_entire_materialized_file(
        self,
    ) -> None:
        def runner(command, **_kwargs):
            output_arg = next(
                item for item in command if item.startswith("--print-to-pdf=")
            )
            Path(output_arg.split("=", 1)[1]).write_bytes(b"%PDF-1.7\nbounded\n")
            return subprocess.CompletedProcess(command, 0, "", "")

        with workspace_tempdir() as temporary:
            store, _, application = self._application(temporary, runner)
            command = _requests(store, index=80)
            with patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("禁止用 read_bytes 读取整个 MHTML"),
            ):
                result = application.execute(command)
            self.assertEqual(ProcessingOutcome.SUCCEEDED, result.outcome)

    def test_browser_profile_freezes_confirmed_policy(self) -> None:
        profile = create_mhtml_browser_profile(
            browser_fingerprint="browser-v1"
        )
        self.assertEqual(
            {
                "fallbackPolicy": "markdown_on_confirmed_failure_v1",
                "noSandbox": True,
                "unknownOutcomePolicy": "reconcile_then_fallback_v1",
            },
            profile.to_dict()["parameters"],
        )

    def test_fifty_confirmed_failures_fallback_without_task_collision(self) -> None:
        def runner(command, **_kwargs):
            return subprocess.CompletedProcess(command, 17, "", "failed")

        with workspace_tempdir() as temporary:
            store, records, application = self._application(temporary, runner)
            commands = tuple(_requests(store, index) for index in range(50))
            with ThreadPoolExecutor(max_workers=50) as executor:
                results = tuple(executor.map(application.execute, commands))

            self.assertTrue(
                all(item.outcome is ProcessingOutcome.SUCCEEDED for item in results)
            )
            self.assertEqual(
                50,
                len({item.artifact.artifact_id for item in results}),  # type: ignore[union-attr]
            )
            self.assertTrue(
                all(
                    records.get(command.fallback_request.step_key) is not None
                    for command in commands
                )
            )
            jobs_root = Path(temporary) / "mhtml-jobs"
            self.assertEqual([], list(jobs_root.iterdir()))


if __name__ == "__main__":
    unittest.main()
