"""阶段 1H-4 MinerU、内置 OCR、直通格式与重型许可门禁。"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.modules.document_processing.adapters import (
    BytesArtifactContent,
    FIFOCapacityAdapter,
    LocalArtifactStoreAdapter,
    ResourceLimitedDocumentProcessorAdapter,
)
from app.modules.document_processing.adapters.builtin_ocr import (
    BuiltinOCRDocumentProcessorAdapter,
    build_builtin_ocr_profile,
)
from app.modules.document_processing.adapters.mineru import (
    MinerUConverter,
    MinerUDocumentProcessorAdapter,
    build_mineru_profile,
    mineru_endpoint_fingerprint,
)
from app.modules.document_processing.adapters.passthrough import (
    ValidatedPassthroughDocumentProcessorAdapter,
    build_passthrough_profile,
)
from app.modules.document_processing.adapters.sqlite_operations import (
    SQLiteMinerUOperationObserver,
)
from app.modules.document_processing.domain import (
    ArtifactKind,
    DocumentProcessingError,
    DocumentProcessingRequest,
    DocumentRepresentation,
)
from app.modules.document_processing.ports import ArtifactPublication
from app.modules.tasks.domain import TaskId
from tests import workspace_tempdir


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _request(
    store: LocalArtifactStoreAdapter,
    *,
    index: int,
    payload: bytes,
    media_type: str,
    profile,
) -> DocumentProcessingRequest:
    task_id = TaskId(f"stage1h-format-{index:02d}")
    source = store.publish(
        ArtifactPublication(
            task_id=task_id,
            step_key=_digest(f"format-source-{index}"),
            kind=ArtifactKind.SOURCE,
            representation=DocumentRepresentation.ORIGINAL,
            media_type=media_type,
        ),
        BytesArtifactContent(payload),
    )
    return DocumentProcessingRequest(
        task_id=task_id,
        step_id="prepare-format",
        source_artifact=source,
        profile=profile,
        trace_id=f"trace-format-{index:02d}",
    )


class _ObserverFake:
    def __init__(self) -> None:
        self.intents: list[tuple[str, str]] = []
        self.identities: list[tuple[str, str]] = []
        self.terminals: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def record_submission_intent(
        self,
        *,
        operation_key: str,
        provider: str,
    ) -> None:
        with self._lock:
            self.intents.append((operation_key, provider))

    def record_provider_identity(
        self,
        *,
        operation_key: str,
        provider_operation_id: str,
    ) -> None:
        with self._lock:
            self.identities.append((operation_key, provider_operation_id))

    def record_terminal(
        self,
        *,
        operation_key: str,
        state: str,
    ) -> None:
        with self._lock:
            self.terminals.append((operation_key, state))


class _FakeMinerUConverter:
    """每个实例只持有自己的输出目录，禁止测试误用共享 Converter。"""

    def __init__(self, output_dir: str, *, operation_observer) -> None:
        self.output_dir = Path(output_dir)
        self.observer = operation_observer

    def convert_to_markdown(
        self,
        *,
        input_path: str,
        output_subdir: str,
        operation_key: str,
        **_kwargs,
    ) -> str:
        self.observer.record_submission_intent(
            operation_key=operation_key,
            provider="mineru",
        )
        self.observer.record_provider_identity(
            operation_key=operation_key,
            provider_operation_id=f"provider-{operation_key[:12]}",
        )
        target = self.output_dir / output_subdir / "document.md"
        target.parent.mkdir(parents=True, exist_ok=False)
        target.write_text(
            f"# Parsed\n\n{Path(input_path).read_bytes().hex()}\n",
            encoding="utf-8",
        )
        return str(target)


class _UnknownMinerUConverter(_FakeMinerUConverter):
    def convert_to_markdown(self, **kwargs) -> str:
        operation_key = str(kwargs["operation_key"])
        self.observer.record_submission_intent(
            operation_key=operation_key,
            provider="mineru",
        )
        raise DocumentProcessingError(
            "mineru_submission_outcome_unknown",
            "提交响应丢失",
            outcome_unknown=True,
        )


class _EmptyMinerUConverter(_FakeMinerUConverter):
    def convert_to_markdown(self, **kwargs) -> str:
        target = self.output_dir / str(kwargs["output_subdir"])
        target.mkdir(parents=True, exist_ok=False)
        return str(target)


class _MultipleMinerUConverter(_FakeMinerUConverter):
    def convert_to_markdown(self, **kwargs) -> str:
        target = self.output_dir / str(kwargs["output_subdir"])
        target.mkdir(parents=True, exist_ok=False)
        (target / "a.md").write_text("a", encoding="utf-8")
        (target / "b.md").write_text("b", encoding="utf-8")
        return str(target)


class _ImageMinerUConverter(_FakeMinerUConverter):
    """模拟 MinerU 的 Markdown + 相对图片结果。"""

    def convert_to_markdown(self, **kwargs) -> str:
        target = self.output_dir / str(kwargs["output_subdir"])
        image = target / "images" / "diagram.png"
        image.parent.mkdir(parents=True, exist_ok=False)
        image.write_bytes(b"\x89PNG\r\n\x1a\nDOCSENSE")
        markdown = target / "document.md"
        markdown.write_text(
            "# Parsed\n\n![diagram](images/diagram.png)\n",
            encoding="utf-8",
        )
        return str(target)


class _FailingMinerUConverter(_FakeMinerUConverter):
    def convert_to_markdown(self, **_kwargs) -> str:
        raise ConnectionError("service unavailable")


class _FakeAsyncClient:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        del exc_type, exc_value, traceback


class DocumentFormatAdapterTests(unittest.TestCase):
    def test_mineru_profile_persists_only_redacted_endpoint_fingerprint(
        self,
    ) -> None:
        api_url = (
            "https://user:super-secret@mineru.example.test:8443/parse"
            "?token=another-secret"
        )
        fingerprint = mineru_endpoint_fingerprint(api_url)
        profile = build_mineru_profile(
            source_suffix=".pdf",
            use_ocr=False,
            lang="ch",
            api_mode="remote",
            endpoint_fingerprint=fingerprint,
        )
        serialized = json.dumps(profile.to_dict(), ensure_ascii=False)
        parameters = profile.to_dict()["parameters"]

        self.assertEqual("remote", parameters["apiMode"])
        self.assertEqual(fingerprint, parameters["endpointFingerprint"])
        for secret in (
            "super-secret",
            "another-secret",
            "mineru.example.test",
            api_url,
        ):
            self.assertNotIn(secret, serialized)

    def test_fifo_capacity_grants_waiters_in_arrival_order(self) -> None:
        with workspace_tempdir() as temporary:
            store = LocalArtifactStoreAdapter(Path(temporary) / "artifacts")
            request = _request(
                store,
                index=0,
                payload=b"text",
                media_type="text/plain",
                profile=build_passthrough_profile(
                    source_suffix=".txt",
                    target_representation=DocumentRepresentation.TEXT,
                    media_type="text/plain",
                    max_size_bytes=128,
                ),
            )
            resource = FIFOCapacityAdapter(1)
            release_owner = threading.Event()
            owner_entered = threading.Event()
            order: list[int] = []
            order_lock = threading.Lock()

            def owner() -> None:
                with resource.acquire(request):
                    owner_entered.set()
                    release_owner.wait(timeout=10)

            def waiter(index: int) -> None:
                with resource.acquire(request):
                    with order_lock:
                        order.append(index)

            with ThreadPoolExecutor(max_workers=6) as executor:
                owner_future = executor.submit(owner)
                self.assertTrue(owner_entered.wait(timeout=10))
                waiter_futures = []
                for index in range(5):
                    waiter_futures.append(executor.submit(waiter, index))
                    deadline = time.monotonic() + 10
                    while (
                        resource.waiting_count < index + 1
                        and time.monotonic() < deadline
                    ):
                        time.sleep(0.001)
                    self.assertEqual(index + 1, resource.waiting_count)
                release_owner.set()
                owner_future.result(timeout=10)
                for future in waiter_futures:
                    future.result(timeout=10)
            self.assertEqual(list(range(5)), order)

    def test_fifo_capacity_rejects_excess_process_waiters(self) -> None:
        with workspace_tempdir() as temporary:
            store = LocalArtifactStoreAdapter(Path(temporary) / "artifacts")
            request = _request(
                store,
                index=90,
                payload=b"text",
                media_type="text/plain",
                profile=build_passthrough_profile(
                    source_suffix=".txt",
                    target_representation=DocumentRepresentation.TEXT,
                    media_type="text/plain",
                    max_size_bytes=128,
                ),
            )
            resource = FIFOCapacityAdapter(1, max_waiters=1)
            owner_entered = threading.Event()
            release_owner = threading.Event()

            def owner() -> None:
                with resource.acquire(request):
                    owner_entered.set()
                    release_owner.wait(timeout=10)

            def waiter() -> None:
                with resource.acquire(request):
                    return

            with ThreadPoolExecutor(max_workers=2) as executor:
                owner_future = executor.submit(owner)
                self.assertTrue(owner_entered.wait(timeout=10))
                waiter_future = executor.submit(waiter)
                deadline = time.monotonic() + 10
                while resource.waiting_count != 1 and time.monotonic() < deadline:
                    time.sleep(0.001)
                self.assertEqual(1, resource.waiting_count)
                with self.assertRaises(DocumentProcessingError) as raised:
                    with resource.acquire(request):
                        pass
                self.assertEqual("document_resource_queue_full", raised.exception.code)
                release_owner.set()
                owner_future.result(timeout=10)
                waiter_future.result(timeout=10)
            self.assertEqual(0, resource.waiting_count)

    def test_text_passthrough_validates_utf8_and_never_mutates_source(self) -> None:
        with workspace_tempdir() as temporary:
            store = LocalArtifactStoreAdapter(Path(temporary) / "artifacts")
            profile = build_passthrough_profile(
                source_suffix=".txt",
                target_representation=DocumentRepresentation.TEXT,
                media_type="text/plain",
                max_size_bytes=1024,
            )
            request = _request(
                store,
                index=1,
                payload="可复用文本".encode("utf-8"),
                media_type="text/plain",
                profile=profile,
            )
            output = ValidatedPassthroughDocumentProcessorAdapter(
                source_store=store
            ).process(request)
            with output.content.open_reader() as reader:
                self.assertEqual("可复用文本".encode("utf-8"), reader.read())
            self.assertEqual(DocumentRepresentation.TEXT, output.representation)

    def test_text_passthrough_rejects_invalid_encoding_and_blank_content(
        self,
    ) -> None:
        with workspace_tempdir() as temporary:
            store = LocalArtifactStoreAdapter(Path(temporary) / "artifacts")
            profile = build_passthrough_profile(
                source_suffix=".md",
                target_representation=DocumentRepresentation.MARKDOWN,
                media_type="text/markdown",
                max_size_bytes=1024,
            )
            processor = ValidatedPassthroughDocumentProcessorAdapter(
                source_store=store
            )
            invalid = _request(
                store,
                index=2,
                payload=b"\xff",
                media_type="text/markdown",
                profile=profile,
            )
            blank = _request(
                store,
                index=3,
                payload=b" \r\n\t",
                media_type="text/markdown",
                profile=profile,
            )
            with self.assertRaisesRegex(
                DocumentProcessingError,
                "UTF-8",
            ):
                processor.process(invalid)
            with self.assertRaisesRegex(
                DocumentProcessingError,
                "空白",
            ):
                processor.process(blank)

    def test_mineru_fifty_tasks_use_distinct_scratch_and_provider_identity(
        self,
    ) -> None:
        with workspace_tempdir() as temporary:
            root = Path(temporary)
            store = LocalArtifactStoreAdapter(root / "artifacts")
            observer = _ObserverFake()
            processor = MinerUDocumentProcessorAdapter(
                source_store=store,
                materialization_root=root / "mineru",
                operation_observer=observer,
                converter_factory=_FakeMinerUConverter,
            )
            profile = build_mineru_profile(
                source_suffix=".pdf",
                use_ocr=True,
                lang="ch",
            )
            requests = [
                _request(
                    store,
                    index=index,
                    payload=f"%PDF-1.7\n{index}".encode(),
                    media_type="application/pdf",
                    profile=profile,
                )
                for index in range(50)
            ]
            with ThreadPoolExecutor(max_workers=50) as executor:
                outputs = tuple(executor.map(processor.process, requests))
            payloads: set[bytes] = set()
            for output in outputs:
                with output.content.open_reader() as reader:
                    payloads.add(reader.read())
            self.assertEqual(50, len(payloads))
            self.assertEqual(50, len({key for key, _ in observer.intents}))
            self.assertEqual(50, len({key for key, _ in observer.identities}))
            for output in outputs:
                output.close()
            self.assertEqual([], list((root / "mineru").iterdir()))

    def test_mineru_unknown_preserves_owned_scratch_for_reconciliation(
        self,
    ) -> None:
        with workspace_tempdir() as temporary:
            root = Path(temporary)
            store = LocalArtifactStoreAdapter(root / "artifacts")
            request = _request(
                store,
                index=4,
                payload=b"%PDF-1.7\nunknown",
                media_type="application/pdf",
                profile=build_mineru_profile(
                    source_suffix=".pdf",
                    use_ocr=True,
                    lang="ch",
                ),
            )
            processor = MinerUDocumentProcessorAdapter(
                source_store=store,
                materialization_root=root / "mineru",
                operation_observer=_ObserverFake(),
                converter_factory=_UnknownMinerUConverter,
            )
            with self.assertRaises(DocumentProcessingError) as raised:
                processor.process(request)
            self.assertTrue(raised.exception.outcome_unknown)
            self.assertTrue((root / "mineru" / request.step_key).is_dir())

    def test_mineru_deterministic_failures_clean_scratch(self) -> None:
        converter_cases = (
            (_FailingMinerUConverter, "mineru_processor_failed"),
            (_EmptyMinerUConverter, "mineru_markdown_result_ambiguous"),
            (_MultipleMinerUConverter, "mineru_markdown_result_ambiguous"),
        )
        for index, (converter_factory, expected_code) in enumerate(
            converter_cases,
            start=10,
        ):
            with self.subTest(converter=converter_factory.__name__):
                with workspace_tempdir() as temporary:
                    root = Path(temporary)
                    store = LocalArtifactStoreAdapter(root / "artifacts")
                    request = _request(
                        store,
                        index=index,
                        payload=b"%PDF-1.7\nfailed",
                        media_type="application/pdf",
                        profile=build_mineru_profile(
                            source_suffix=".pdf",
                            use_ocr=True,
                            lang="ch",
                        ),
                    )
                    processor = MinerUDocumentProcessorAdapter(
                        source_store=store,
                        materialization_root=root / "mineru",
                        operation_observer=_ObserverFake(),
                        converter_factory=converter_factory,
                    )
                    with self.assertRaises(
                        DocumentProcessingError
                    ) as raised:
                        processor.process(request)
                    self.assertEqual(expected_code, raised.exception.code)
                    self.assertFalse(
                        (root / "mineru" / request.step_key).exists()
                    )

    def test_mineru_images_remain_readable_after_scratch_cleanup(self) -> None:
        with workspace_tempdir() as temporary:
            root = Path(temporary)
            store = LocalArtifactStoreAdapter(root / "artifacts")
            request = _request(
                store,
                index=13,
                payload=b"%PDF-1.7\nwith-image",
                media_type="application/pdf",
                profile=build_mineru_profile(
                    source_suffix=".pdf",
                    use_ocr=False,
                    lang="ch",
                ),
            )
            processor = MinerUDocumentProcessorAdapter(
                source_store=store,
                materialization_root=root / "mineru",
                operation_observer=_ObserverFake(),
                converter_factory=_ImageMinerUConverter,
            )
            output = processor.process(request)
            published = store.publish(
                ArtifactPublication(
                    task_id=request.task_id,
                    step_key=_digest("published-markdown-with-image"),
                    kind=output.kind,
                    representation=output.representation,
                    media_type=output.media_type,
                ),
                output.content,
            )
            output.close()

            self.assertFalse((root / "mineru" / request.step_key).exists())
            with store.open_reader(published) as reader:
                markdown = reader.read().decode("utf-8")
            self.assertIn(
                "data:image/png;base64,iVBORw0KGgpET0NTRU5TRQ==",
                markdown,
            )
            self.assertNotIn("images/diagram.png", markdown)

    def test_real_mineru_converter_returns_full_result_directory_for_processor(
        self,
    ) -> None:
        with workspace_tempdir() as temporary:
            root = Path(temporary)
            output_root = root / "output"
            archive = root / "result.zip"
            archive.write_bytes(b"not-a-real-zip")
            converter = MinerUConverter(
                output_dir=str(output_root),
                operation_observer=_ObserverFake(),
            )
            submit = SimpleNamespace(task_id="provider-multiple", queued_ahead=None)

            def extract_multiple(_archive, destination) -> None:
                destination.mkdir(parents=True, exist_ok=True)
                (destination / "a.md").write_text("a", encoding="utf-8")
                (destination / "b.md").write_text("b", encoding="utf-8")

            with (
                patch(
                    "app.modules.document_processing.adapters.mineru.httpx.AsyncClient",
                    return_value=_FakeAsyncClient(),
                ),
                patch(
                    "app.modules.document_processing.adapters.mineru._api_client.fetch_server_health",
                    new=AsyncMock(
                        return_value=SimpleNamespace(
                            base_url="http://mineru.invalid"
                        )
                    ),
                ),
                patch(
                    "app.modules.document_processing.adapters.mineru._api_client.submit_parse_task",
                    new=AsyncMock(return_value=submit),
                ),
                patch(
                    "app.modules.document_processing.adapters.mineru._api_client.wait_for_task_result",
                    new=AsyncMock(return_value=None),
                ),
                patch(
                    "app.modules.document_processing.adapters.mineru._api_client.download_result_zip",
                    new=AsyncMock(return_value=archive),
                ),
                patch(
                    "app.modules.document_processing.adapters.mineru._api_client.safe_extract_zip",
                    side_effect=extract_multiple,
                ),
            ):
                result = converter._run_conversion(  # pylint: disable=protected-access
                    upload_assets=[],
                    form_data={},
                    api_url="http://mineru.invalid",
                    server_url=None,
                    output_dir=output_root,
                    operation_key=_digest("multiple-markdown"),
                    return_result_directory=True,
                )

            self.assertEqual(output_root.resolve(), Path(result).resolve())
            self.assertEqual(2, len(tuple(output_root.glob("*.md"))))

    def test_mineru_submission_timeout_is_unknown_but_health_failure_is_not(
        self,
    ) -> None:
        with workspace_tempdir() as temporary:
            root = Path(temporary)
            observer = _ObserverFake()
            converter = MinerUConverter(
                output_dir=str(root / "output"),
                operation_observer=observer,
            )
            health = SimpleNamespace(base_url="http://mineru.invalid")
            with (
                patch(
                    "app.modules.document_processing.adapters.mineru.httpx.AsyncClient",
                    return_value=_FakeAsyncClient(),
                ),
                patch(
                    "app.modules.document_processing.adapters.mineru._api_client.fetch_server_health",
                    new=AsyncMock(return_value=health),
                ),
                patch(
                    "app.modules.document_processing.adapters.mineru._api_client.submit_parse_task",
                    new=AsyncMock(side_effect=TimeoutError("response lost")),
                ),
            ):
                with self.assertRaises(
                    DocumentProcessingError
                ) as raised:
                    converter._run_conversion(  # pylint: disable=protected-access
                        upload_assets=[],
                        form_data={},
                        api_url="http://mineru.invalid",
                        server_url=None,
                        output_dir=root / "output",
                        operation_key=_digest("submit-timeout"),
                    )
            self.assertTrue(raised.exception.outcome_unknown)
            self.assertEqual(1, len(observer.intents))

            with (
                patch(
                    "app.modules.document_processing.adapters.mineru.httpx.AsyncClient",
                    return_value=_FakeAsyncClient(),
                ),
                patch(
                    "app.modules.document_processing.adapters.mineru._api_client.fetch_server_health",
                    new=AsyncMock(side_effect=ConnectionError("offline")),
                ),
            ):
                with self.assertRaises(ConnectionError):
                    converter._run_conversion(  # pylint: disable=protected-access
                        upload_assets=[],
                        form_data={},
                        api_url="http://mineru.invalid",
                        server_url=None,
                        output_dir=root / "output",
                        operation_key=_digest("health-failure"),
                    )

    def test_mineru_uses_safe_archive_extraction_and_deletes_download(
        self,
    ) -> None:
        with workspace_tempdir() as temporary:
            root = Path(temporary)
            archive = root / "result.zip"
            archive.write_bytes(b"not-a-real-zip")
            converter = MinerUConverter(
                output_dir=str(root / "output"),
                operation_observer=_ObserverFake(),
            )
            submit = SimpleNamespace(
                task_id="provider-1",
                queued_ahead=None,
            )
            with (
                patch(
                    "app.modules.document_processing.adapters.mineru.httpx.AsyncClient",
                    return_value=_FakeAsyncClient(),
                ),
                patch(
                    "app.modules.document_processing.adapters.mineru._api_client.fetch_server_health",
                    new=AsyncMock(
                        return_value=SimpleNamespace(
                            base_url="http://mineru.invalid"
                        )
                    ),
                ),
                patch(
                    "app.modules.document_processing.adapters.mineru._api_client.submit_parse_task",
                    new=AsyncMock(return_value=submit),
                ),
                patch(
                    "app.modules.document_processing.adapters.mineru._api_client.wait_for_task_result",
                    new=AsyncMock(return_value=None),
                ),
                patch(
                    "app.modules.document_processing.adapters.mineru._api_client.download_result_zip",
                    new=AsyncMock(return_value=archive),
                ),
                patch(
                    "app.modules.document_processing.adapters.mineru._api_client.safe_extract_zip",
                    side_effect=ValueError("archive path escape"),
                ) as safe_extract,
            ):
                with self.assertRaisesRegex(ValueError, "path escape"):
                    converter._run_conversion(  # pylint: disable=protected-access
                        upload_assets=[],
                        form_data={},
                        api_url="http://mineru.invalid",
                        server_url=None,
                        output_dir=root / "output",
                        operation_key=_digest("archive"),
                    )
            safe_extract.assert_called_once()
            self.assertFalse(archive.exists())

    def test_builtin_ocr_fifty_tasks_are_bounded_by_shared_capacity(
        self,
    ) -> None:
        with workspace_tempdir() as temporary:
            root = Path(temporary)
            store = LocalArtifactStoreAdapter(root / "artifacts")
            state_lock = threading.Lock()
            active = 0
            maximum = 0
            release = threading.Event()
            entered_two = threading.Event()

            def renderer(source_path, **_kwargs):
                nonlocal active, maximum
                with state_lock:
                    active += 1
                    maximum = max(maximum, active)
                    if active == 2:
                        entered_two.set()
                release.wait(timeout=10)
                try:
                    return f"# OCR\n\n{source_path.name}\n", 1
                finally:
                    with state_lock:
                        active -= 1

            processor = BuiltinOCRDocumentProcessorAdapter(
                source_store=store,
                materialization_root=root / "ocr",
                renderer=renderer,
            )
            limited = ResourceLimitedDocumentProcessorAdapter(
                processor=processor,
                resource=FIFOCapacityAdapter(2),
            )
            profile = build_builtin_ocr_profile(
                languages="chi_sim+eng",
                dpi=300,
            )
            requests = [
                _request(
                    store,
                    index=index + 100,
                    payload=f"%PDF-1.7\n{index}".encode(),
                    media_type="application/pdf",
                    profile=profile,
                )
                for index in range(50)
            ]
            with ThreadPoolExecutor(max_workers=50) as executor:
                futures = [
                    executor.submit(limited.process, request)
                    for request in requests
                ]
                self.assertTrue(entered_two.wait(timeout=10))
                release.set()
                outputs = [future.result(timeout=10) for future in futures]
            self.assertEqual(2, maximum)
            for output in outputs:
                output.close()
            self.assertEqual([], list((root / "ocr").iterdir()))

    def test_sqlite_observer_requires_intent_and_rejects_identity_drift(
        self,
    ) -> None:
        with workspace_tempdir() as temporary:
            observer = SQLiteMinerUOperationObserver(
                Path(temporary) / "llm_tasks.sqlite3"
            )
            operation_key = _digest("external-operation")
            with self.assertRaises(DocumentProcessingError):
                observer.record_provider_identity(
                    operation_key=operation_key,
                    provider_operation_id="provider-1",
                )
            observer.record_submission_intent(
                operation_key=operation_key,
                provider="mineru",
            )
            observer.record_provider_identity(
                operation_key=operation_key,
                provider_operation_id="provider-1",
            )
            snapshot = observer.get(operation_key)
            self.assertIsNotNone(snapshot)
            self.assertEqual("provider_identified", snapshot.state)  # type: ignore[union-attr]
            observer.record_terminal(
                operation_key=operation_key,
                state="failed",
            )
            terminal = observer.get(operation_key)
            self.assertEqual("failed", terminal.state)  # type: ignore[union-attr]
            with self.assertRaises(DocumentProcessingError):
                observer.record_terminal(
                    operation_key=operation_key,
                    state="succeeded",
                )
            with self.assertRaises(DocumentProcessingError):
                observer.record_provider_identity(
                    operation_key=operation_key,
                    provider_operation_id="provider-2",
                )


if __name__ == "__main__":
    unittest.main()
