from __future__ import annotations

import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from app.integrations.anythingllm.models import (
    AnythingLLMDocument,
    AnythingLLMSource,
    AnythingLLMThread,
    AnythingLLMWorkspace,
)
from app.integrations.anythingllm.errors import (
    AnythingLLMHTTPError,
    AnythingLLMTimeoutError,
)
from app.modules.report.adapters.anythingllm_rag import (
    AnythingLLMReportClientFactory,
    AnythingLLMReportRagAdapter,
    ReportAnythingLLMClients,
)
from app.modules.report.adapters.local_artifacts import LocalReportArtifactAdapter
from app.modules.report.ports import (
    CleanupReportRag,
    ReportArtifactCategory,
    ReportRagExecutionError,
    ReportRagRequest,
)
from app.modules.tasks.domain import TaskId
from app.services.core.config import AnythingLLMConfig
from tests import workspace_tempdir


class _Backend:
    def __init__(self) -> None:
        self.upload_markers: list[str] = []
        self.upload_names: list[str] = []
        self.ask_document_ids: tuple[str, ...] = ()
        self.deleted_threads: list[tuple[str, str]] = []
        self.deleted_workspaces: list[str] = []
        self.deleted_documents: list[str] = []
        self.upload_fail_at: int | None = None
        self.ask_error: Exception | None = None
        self.answer_text: str | None = "<section>报告</section>"
        self.source_mode = "matched"
        self.delete_document_failure: str | None = None
        self.delete_thread_error: Exception | None = None
        self.workspace_create_error: Exception | None = None
        self.workspaces: list[AnythingLLMWorkspace] = []
        self.upload_error_after_success_at: int | None = None


class _Documents:
    def __init__(self, backend: _Backend) -> None:
        self.backend = backend

    def upload_document(self, file_path: str, *, user_id=None, metadata=None):
        index = len(self.backend.upload_markers) + 1
        if self.backend.upload_fail_at == index:
            raise RuntimeError("upload failed")
        marker = metadata["docSource"]
        self.backend.upload_markers.append(marker)
        self.backend.upload_names.append(Path(file_path).name)
        document = AnythingLLMDocument(
            id=f"doc-{index}",
            location=f"custom-documents/source-{index}-00000000-0000-0000-0000-00000000000{index}.json",
            title=Path(file_path).name,
            document_ref=f"document:doc-{index}",
        )
        if self.backend.upload_error_after_success_at == index:
            # 模拟供应商已保存文档，但 HTTP 响应在客户端读取前超时。
            raise AnythingLLMTimeoutError("upload response timeout")
        return document

    def delete_document(self, location: str, *, user_id=None) -> None:
        self.backend.deleted_documents.append(location)
        if self.backend.delete_document_failure == location:
            raise RuntimeError("delete failed")


class _Workspaces:
    def __init__(self, backend: _Backend) -> None:
        self.backend = backend

    def create_workspace(self, name: str, *, settings=None, user_id=None):
        if self.backend.workspace_create_error is not None:
            raise self.backend.workspace_create_error
        return AnythingLLMWorkspace(id="workspace-1", slug="workspace-1", name=name)

    def list_workspaces(self, *, user_id=None):
        return list(self.backend.workspaces)

    def update_embeddings(self, workspace_slug: str, *, adds=None, user_id=None):
        return AnythingLLMWorkspace(
            id="workspace-1",
            slug=workspace_slug,
            name=workspace_slug,
        )

    def delete_workspace(self, workspace_slug: str, *, user_id=None) -> None:
        self.backend.deleted_workspaces.append(workspace_slug)


class _Threads:
    def __init__(self, backend: _Backend) -> None:
        self.backend = backend

    def create_thread(self, workspace_slug: str, name: str, *, user_id=None):
        return AnythingLLMThread(id="thread-1", slug="thread-1")

    def ask(
        self,
        workspace_slug: str,
        thread_slug: str,
        prompt: str,
        *,
        mode: str,
        user_id=None,
        document_ids=None,
    ):
        if self.backend.ask_error is not None:
            raise self.backend.ask_error
        self.backend.ask_document_ids = tuple(document_ids or ())
        matched = AnythingLLMSource(
            document_ref="legacy-name",
            text="matched",
            source_marker=self.backend.upload_markers[0],
        )
        sources: list[AnythingLLMSource] = [matched]
        if self.backend.source_mode == "mixed":
            sources.extend(
                (
                    AnythingLLMSource(document_ref="legacy", text="missing"),
                    AnythingLLMSource(
                        document_ref="legacy",
                        text="mismatched",
                        source_marker="docsense_ref:ffffffffffffffffffffffffffffffff",
                    ),
                )
            )
        return SimpleNamespace(text=self.backend.answer_text, sources=tuple(sources))

    def delete_thread(self, workspace_slug: str, thread_slug: str, *, user_id=None):
        self.backend.deleted_threads.append((workspace_slug, thread_slug))
        if self.backend.delete_thread_error is not None:
            raise self.backend.delete_thread_error


class _Factory:
    def __init__(self, backend: _Backend) -> None:
        self.backend = backend
        self.create_count = 0
        self.fail_create_on: set[int] = set()
        self.fail_close_after_body_error = False
        self.lease_object_ids: list[tuple[int, int, int]] = []

    @contextmanager
    def create(self):
        self.create_count += 1
        if self.create_count in self.fail_create_on:
            raise RuntimeError("transport unavailable")
        documents = _Documents(self.backend)
        workspaces = _Workspaces(self.backend)
        threads = _Threads(self.backend)
        self.lease_object_ids.append((id(documents), id(workspaces), id(threads)))
        clients = ReportAnythingLLMClients(documents, workspaces, threads)
        try:
            yield clients
        except Exception:
            if self.fail_close_after_body_error:
                clients.lease_state.close_error = RuntimeError("close failed")
            raise


def _request(artifacts: LocalReportArtifactAdapter, root: Path) -> ReportRagRequest:
    task_id = TaskId("report-execution-001")
    scope = artifacts.begin(task_id)
    refs = []
    for index in (1, 2):
        seed = root / f"seed-{index}.md"
        seed.write_text(f"document-{index}", encoding="utf-8")
        refs.append(
            artifacts.publish_file(
                scope,
                category=ReportArtifactCategory.RAG_INPUT,
                source_path=seed,
                file_name=f"{index:04d}-001.md",
                sequence_no=index,
            )
        )
    return ReportRagRequest(
        task_id=task_id,
        trace_id="trace-report-001",
        ordered_source_files=tuple(refs),
        prompt="生成报告",
        context_name="report-1-execution-001",
        conversation_name="report-1-thread",
    )


class ReportRagAdapterTests(unittest.TestCase):
    def test_client_factory_preserves_body_error_and_exposes_close_error(self) -> None:
        class _CloseFailTransport:
            def close(self) -> None:
                raise RuntimeError("close failed")

        transport = _CloseFailTransport()
        factory = AnythingLLMReportClientFactory(
            AnythingLLMConfig(
                base_url="http://anythingllm.local",
                api_key="test-key",
                timeout=1.0,
                storage_root=None,
            ),
            transport_factory=lambda **_: transport,  # type: ignore[arg-type]
        )
        body_error = RuntimeError("body failed")
        leased_clients = None

        with self.assertRaises(RuntimeError) as raised:
            with factory.create() as clients:
                leased_clients = clients
                raise body_error

        self.assertIs(body_error, raised.exception)
        self.assertIsNotNone(leased_clients)
        self.assertIsInstance(
            leased_clients.lease_state.close_error,  # type: ignore[union-attr]
            RuntimeError,
        )

    def test_multidocument_success_preserves_order_trace_sources_and_cleanup(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            artifacts = LocalReportArtifactAdapter(root / "artifacts")
            request = _request(artifacts, root)
            backend = _Backend()
            factory = _Factory(backend)
            adapter = AnythingLLMReportRagAdapter(
                factory,
                artifact_path_resolver=artifacts.resolve_path,
            )

            response = adapter.generate(request)

            self.assertEqual("<section>报告</section>", response.raw_content)
            self.assertEqual(("doc-1", "doc-2"), backend.ask_document_ids)
            self.assertEqual(["0001-001.md", "0002-001.md"], backend.upload_names)
            self.assertEqual(request.trace_id, response.trace.trace_id)
            self.assertTrue(response.trace.final_call_id.startswith("report-call-"))
            self.assertEqual(response.trace.final_call_id, response.trace.attempts[0].call_id)
            self.assertEqual(1, response.trace.attempts[0].verified_source_count)
            self.assertEqual("document:doc-1", response.trace.attempts[0].sources[0].document_ref)
            self.assertIsNotNone(response.cleanup_ref)

            cleanup_events = adapter.cleanup(
                CleanupReportRag(response.cleanup_ref)  # type: ignore[arg-type]
            )

            self.assertEqual(2, factory.create_count)
            self.assertEqual(2, len(set(factory.lease_object_ids)))
            self.assertEqual([("workspace-1", "thread-1")], backend.deleted_threads)
            self.assertEqual(["workspace-1"], backend.deleted_workspaces)
            self.assertEqual(
                list(reversed([
                    "custom-documents/source-1-00000000-0000-0000-0000-000000000001.json",
                    "custom-documents/source-2-00000000-0000-0000-0000-000000000002.json",
                ])),
                backend.deleted_documents,
            )
            self.assertEqual(
                response.trace.lifecycle_events[-1].sequence_no + 1,
                cleanup_events[0].sequence_no,
            )
            self.assertTrue(all(event.success for event in cleanup_events))

    def test_partial_upload_failure_carries_trace_and_only_created_resources(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            artifacts = LocalReportArtifactAdapter(root / "artifacts")
            request = _request(artifacts, root)
            backend = _Backend()
            backend.upload_fail_at = 2
            adapter = AnythingLLMReportRagAdapter(
                _Factory(backend),
                artifact_path_resolver=artifacts.resolve_path,
            )

            with self.assertRaises(ReportRagExecutionError) as raised:
                adapter.generate(request)

            error = raised.exception
            self.assertEqual("document_upload", error.trace.failure_stage)
            self.assertEqual(request.trace_id, error.trace.trace_id)
            self.assertEqual(0, len(error.trace.attempts))
            self.assertEqual(1, len(backend.upload_markers))
            self.assertIsNotNone(error.cleanup_ref)
            operations = tuple(event.operation for event in error.trace.lifecycle_events)
            self.assertIn("document_upload", operations)
            self.assertEqual("transport_close", operations[-1])

    def test_ambiguous_context_create_is_reconciled_by_unique_task_name(self) -> None:
        """写响应丢失后只能查回唯一资源，不能盲目重放创建请求。"""

        with workspace_tempdir() as tmp:
            root = Path(tmp)
            artifacts = LocalReportArtifactAdapter(root / "artifacts")
            request = _request(artifacts, root)
            backend = _Backend()
            backend.workspace_create_error = AnythingLLMTimeoutError(
                "workspace response timeout"
            )
            backend.workspaces.append(
                AnythingLLMWorkspace(
                    id="workspace-recovered",
                    slug="workspace-recovered",
                    name=request.context_name,
                )
            )
            adapter = AnythingLLMReportRagAdapter(
                _Factory(backend),
                artifact_path_resolver=artifacts.resolve_path,
            )

            response = adapter.generate(request)

            self.assertEqual("workspace-recovered", response.trace.context_ref)
            reconcile = tuple(
                event
                for event in response.trace.lifecycle_events
                if event.operation == "context_reconcile"
            )
            self.assertEqual(1, len(reconcile))
            self.assertTrue(reconcile[0].success)

    def test_ambiguous_context_create_without_unique_match_requires_quarantine(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            artifacts = LocalReportArtifactAdapter(root / "artifacts")
            backend = _Backend()
            backend.workspace_create_error = AnythingLLMTimeoutError(
                "workspace response timeout"
            )
            adapter = AnythingLLMReportRagAdapter(
                _Factory(backend),
                artifact_path_resolver=artifacts.resolve_path,
            )

            with self.assertRaises(ReportRagExecutionError) as raised:
                adapter.generate(_request(artifacts, root))

            self.assertTrue(raised.exception.external_outcome_unknown)
            self.assertEqual(
                "context_create_outcome_unknown",
                raised.exception.trace.failure_stage,
            )
            self.assertIsNone(raised.exception.cleanup_ref)

    def test_ambiguous_document_upload_requires_quarantine(self) -> None:
        """上传已执行但缺少 location 时，已知 Workspace 也不足以证明全局文档已清理。"""

        with workspace_tempdir() as tmp:
            root = Path(tmp)
            artifacts = LocalReportArtifactAdapter(root / "artifacts")
            backend = _Backend()
            backend.upload_error_after_success_at = 1
            adapter = AnythingLLMReportRagAdapter(
                _Factory(backend),
                artifact_path_resolver=artifacts.resolve_path,
            )

            with self.assertRaises(ReportRagExecutionError) as raised:
                adapter.generate(_request(artifacts, root))

            self.assertTrue(raised.exception.external_outcome_unknown)
            self.assertEqual(
                "document_upload_outcome_unknown",
                raised.exception.trace.failure_stage,
            )
            self.assertIsNotNone(raised.exception.cleanup_ref)

    def test_query_failure_is_recorded_as_failed_attempt_with_call_id(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            artifacts = LocalReportArtifactAdapter(root / "artifacts")
            backend = _Backend()
            backend.ask_error = RuntimeError("provider failed with secret detail")
            adapter = AnythingLLMReportRagAdapter(
                _Factory(backend),
                artifact_path_resolver=artifacts.resolve_path,
            )

            with self.assertRaises(ReportRagExecutionError) as raised:
                adapter.generate(_request(artifacts, root))

            attempt = raised.exception.trace.attempts[0]
            self.assertEqual("model_query", attempt.failure_stage)
            self.assertTrue(attempt.call_id.startswith("report-call-"))
            self.assertNotIn("secret detail", attempt.error_message or "")

    def test_stage_and_transport_close_failures_are_both_preserved(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            artifacts = LocalReportArtifactAdapter(root / "artifacts")
            backend = _Backend()
            backend.ask_error = RuntimeError("query failed")
            factory = _Factory(backend)
            factory.fail_close_after_body_error = True
            adapter = AnythingLLMReportRagAdapter(
                factory,
                artifact_path_resolver=artifacts.resolve_path,
            )

            with self.assertRaises(ReportRagExecutionError) as raised:
                adapter.generate(_request(artifacts, root))

            self.assertEqual("model_query", raised.exception.trace.failure_stage)
            close_events = tuple(
                event
                for event in raised.exception.trace.lifecycle_events
                if event.operation == "transport_close"
            )
            self.assertEqual(1, len(close_events))
            self.assertFalse(close_events[0].success)
            self.assertEqual("transport_close", close_events[0].failure_stage)

    def test_source_verification_records_matched_missing_and_conflicting_sources(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            artifacts = LocalReportArtifactAdapter(root / "artifacts")
            backend = _Backend()
            backend.source_mode = "mixed"
            adapter = AnythingLLMReportRagAdapter(
                _Factory(backend),
                artifact_path_resolver=artifacts.resolve_path,
            )

            response = adapter.generate(_request(artifacts, root))
            attempt = response.trace.attempts[0]

            self.assertEqual(3, attempt.source_count)
            self.assertEqual(1, attempt.verified_source_count)
            self.assertEqual(1, attempt.missing_marker_count)
            self.assertEqual(1, attempt.mismatched_marker_count)
            self.assertEqual("conflict", attempt.source_marker_status)

    def test_cleanup_transport_failure_returns_auditable_failed_deletion_events(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            artifacts = LocalReportArtifactAdapter(root / "artifacts")
            backend = _Backend()
            factory = _Factory(backend)
            factory.fail_create_on.add(2)
            adapter = AnythingLLMReportRagAdapter(
                factory,
                artifact_path_resolver=artifacts.resolve_path,
            )
            response = adapter.generate(_request(artifacts, root))

            events = adapter.cleanup(
                CleanupReportRag(response.cleanup_ref)  # type: ignore[arg-type]
            )

            self.assertFalse(events[0].success)
            self.assertEqual("cleanup_transport_open", events[0].operation)
            deletion_events = tuple(
                event
                for event in events
                if event.operation
                in {
                    "conversation_delete",
                    "context_delete",
                    "global_document_delete",
                }
            )
            self.assertEqual(4, len(deletion_events))
            self.assertTrue(all(not event.success for event in deletion_events))

    def test_cleanup_remote_not_found_is_idempotent_success(self) -> None:
        """进程可能在远端删除成功后崩溃，恢复时 404 必须收敛为已删除。"""

        with workspace_tempdir() as tmp:
            root = Path(tmp)
            artifacts = LocalReportArtifactAdapter(root / "artifacts")
            backend = _Backend()
            adapter = AnythingLLMReportRagAdapter(
                _Factory(backend),
                artifact_path_resolver=artifacts.resolve_path,
            )
            response = adapter.generate(_request(artifacts, root))
            backend.delete_thread_error = AnythingLLMHTTPError(
                "thread not found",
                method="DELETE",
                url="http://anythingllm.local/thread",
                status_code=404,
            )

            events = adapter.cleanup(
                CleanupReportRag(response.cleanup_ref)  # type: ignore[arg-type]
            )

            conversation = next(
                event for event in events if event.operation == "conversation_delete"
            )
            self.assertTrue(conversation.success)
            self.assertIsNone(conversation.failure_stage)
            self.assertTrue(all(event.success for event in events))

    def test_none_model_content_remains_successful_compatible_empty_result(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            artifacts = LocalReportArtifactAdapter(root / "artifacts")
            backend = _Backend()
            backend.answer_text = None
            adapter = AnythingLLMReportRagAdapter(
                _Factory(backend),
                artifact_path_resolver=artifacts.resolve_path,
            )

            response = adapter.generate(_request(artifacts, root))

            self.assertIsNone(response.raw_content)
            self.assertEqual("", response.trace.attempts[0].raw_response)
            self.assertTrue(response.trace.succeeded)


if __name__ == "__main__":
    unittest.main()
