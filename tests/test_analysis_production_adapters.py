"""阶段 1F-3：Analysis 生产 I/O Adapter 的离线契约测试。

这些用例仅使用临时目录、内存 Fake 和临时 SQLite，不会连接 AnythingLLM、模型、OCR 或
真实知识库。重点验证 Adapter 的边界映射、任务隔离和结果未知时的 fail-closed 语义。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from typing import Iterator
import unittest

from app.modules.analysis.adapters import (
    LegacyAnalysisAuditAdapter,
    LegacyAnalysisFilePreparationAdapter,
    LegacyAnalysisKnowledgeAdapter,
    LegacyAnalysisRagAdapterFactory,
    LocalAnalysisTaskWorkspaceAdapter,
)
from app.modules.analysis.adapters.legacy_files import AnalysisFilePreparationError
from app.modules.analysis.domain.task_inputs import (
    AnalysisDocumentProcessingPolicySnapshot,
    FrozenJsonObject,
)
from app.modules.document_processing import (
    ArtifactKind,
    ArtifactMetadata,
    ArtifactRef,
    DocumentRepresentation,
    LegacyOfficeConversionError,
)
from app.modules.analysis.ports import (
    AnalysisAuditOutcome,
    AnalysisExecutionRef,
    AnalysisFilePreparationRequest,
    AnalysisInteractionAttempt,
    AnalysisInteractionAuditRecord,
    AnalysisKnowledgeDocumentMetadata,
    AnalysisKnowledgeWriteOutcome,
    AnalysisKnowledgeWriteRequest,
    AnalysisRagCloseOutcome,
    AnalysisRagCloseRequest,
    AnalysisRagExecutionError,
    AnalysisRagLifecycleEvent,
    AnalysisRagLifecycleOutcome,
    AnalysisRagOperation,
    AnalysisRagRequest,
    AnalysisRagSessionOpenError,
    AnalysisRagSessionOpenRequest,
    AnalysisRagUploadDescriptor,
    AnalysisRecallAuditRecord,
    AppendAnalysisLifecycleEvents,
    FinalizeAnalysisRecallAudit,
    LoadAnalysisInteraction,
)
from app.modules.tasks.domain import TaskId
from app.ports.knowledge_index import (
    CollectionRef,
    IndexedDocument,
    KnowledgeIndexDocumentReleasedError,
    KnowledgeIndexRetentionRequiredError,
)
from app.ports.rag import (
    CleanupResult,
    PreparedDocumentRef,
    RagAttempt,
    RagExecutionTrace,
    RagLifecycleEvent,
    RagResult,
    RagSource,
)
from app.services.core.config import OCRConfig
from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir


def _execution(task_id: str = "analysis-adapter-task-1") -> AnalysisExecutionRef:
    """构造一个只在离线测试内使用的稳定 execution 身份。"""

    return AnalysisExecutionRef(
        task_id=TaskId(task_id),
        file_name="adapter-demo.txt",
        batch_id="a" * 32,
        batch_sequence=1,
    )


def _bound_session(execution: AnalysisExecutionRef):  # type: ignore[no-untyped-def]
    """创建可安全转交知识库和审计的已绑定 RAG SessionRef。"""

    from app.modules.analysis.ports import AnalysisRagSessionRef

    return AnalysisRagSessionRef(
        execution=execution,
        session_ref="context:adapter::conversation:adapter",
        context_ref="context:adapter",
        conversation_ref="conversation:adapter",
    ).with_bound_document(
        document_ref="document:adapter",
        document_location="location:adapter",
        content_sha256="c" * 64,
        ingested_file_name="adapter-demo.txt",
    )


def _ocr_config() -> OCRConfig:
    """返回最小 OCR 配置；真实 OCR 不会在测试中被调用。"""

    return OCRConfig(
        enabled=False,
        languages="eng",
        dpi=150,
        sample_pages=1,
        text_threshold=1,
        cache_dir="shared-cache",
        analysis_scanned_pdf_engine="none",
        mineru_cache_dir="shared-mineru-cache",
        mineru_lang="en",
        mineru_api_url=None,
        tessdata_prefix=None,
    )


class _KnowledgePortFake:
    """只实现知识 Adapter 实际调用的两项能力，并按 mode 返回三态事实。"""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.calls: list[str] = []
        self.last_document = None
        self.last_metadata = None

    def ensure_collection(self, spec):  # type: ignore[no-untyped-def]
        self.calls.append("ensure_collection")
        return CollectionRef(
            ref=f"collection:{spec.architecture_id}",
            name=spec.name,
            architecture_id=spec.architecture_id,
        )

    def store_prepared_document(self, collection, document, metadata, *, operation_context, idempotency_key):  # type: ignore[no-untyped-def]
        self.calls.append("store_prepared_document")
        self.last_document = document
        self.last_metadata = metadata
        if self.mode == "released":
            raise KnowledgeIndexDocumentReleasedError("document released")
        if self.mode == "retained":
            raise KnowledgeIndexRetentionRequiredError("ownership unknown")
        if self.mode == "malformed_success":
            # 模拟远端已经提交，但供应商 SDK 返回了缺少永久引用的不完整成功对象。
            return SimpleNamespace(created=True, reused=False)
        return IndexedDocument(
            collection_ref=collection.ref,
            document_ref=document.document_ref,
            external_location="knowledge:adapter",
            idempotency_key=idempotency_key,
            created=True,
            reused=False,
        )


class _KnowledgeFactoryFake:
    """为一次 Adapter 调用发放一个内存知识库租约。"""

    def __init__(self, port: _KnowledgePortFake) -> None:
        self.port = port
        self.entered = 0
        self.exited = 0

    @contextmanager
    def create(self) -> Iterator[_KnowledgePortFake]:
        self.entered += 1
        try:
            yield self.port
        finally:
            self.exited += 1


class _NativeRagSessionFake:
    """模拟遗留 Gateway 会话，显式追加不可变 trace，避免真实 HTTP I/O。"""

    def __init__(self) -> None:
        self.trace = RagExecutionTrace(
            context_name="llm-file-analysis-adapter-task-1",
            context_ref="context:adapter",
            conversation_ref="conversation:adapter",
            attempts=(),
            failure_stage=None,
            error_message=None,
            lifecycle_events=(
                RagLifecycleEvent(1, "context_create", 1, True, "context:adapter", None, None),
                RagLifecycleEvent(2, "conversation_create", 1, True, "conversation:adapter", None, None),
            ),
            trace_id="adapter-trace",
        )
        self.closed_with: bool | None = None
        self.document_upload = None

    def analyse(self, file_path: str, prompt: str, *, prompt_kind, require_sources: bool, max_attempts: int, document_upload=None):  # type: ignore[no-untyped-def]
        self.document_upload = document_upload
        prepared = PreparedDocumentRef(
            document_ref="document:adapter",
            external_location="location:adapter",
            content_sha256="c" * 64,
            ingested_file_name="adapter-demo.txt",
        )
        attempt = RagAttempt(
            operation="analyse",
            attempt=1,
            prompt_kind=prompt_kind,
            raw_response='{"architectureId":103}',
            sources=(),
            failure_stage=None,
            error_message=None,
            prompt_digest=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        )
        self.trace = replace(
            self.trace,
            attempts=(attempt,),
            lifecycle_events=(
                *self.trace.lifecycle_events,
                RagLifecycleEvent(3, "document_upload", 1, True, "location:adapter", None, None),
                RagLifecycleEvent(4, "document_bind", 1, True, "document:adapter", None, None),
            ),
        )
        return RagResult(
            text=attempt.raw_response or "{}",
            sources=(),
            prepared_document=prepared,
            trace=self.trace,
        )

    def start_fresh_conversation(self, *, conversation_name: str, failure_is_fatal: bool = True) -> bool:
        raise AssertionError("本测试的首次抽取不应创建第二 Conversation")

    def ask(self, prompt: str, *, prompt_kind, require_sources: bool, max_attempts: int):  # type: ignore[no-untyped-def]
        raise AssertionError("本测试只覆盖首次 analyse 路径")

    def ask_optional(self, prompt: str, *, prompt_kind, require_sources: bool, max_attempts: int):  # type: ignore[no-untyped-def]
        raise AssertionError("本测试不覆盖身份重选路径")

    def close(self, *, retain_document: bool) -> CleanupResult:
        self.closed_with = retain_document
        self.trace = replace(
            self.trace,
            lifecycle_events=(
                *self.trace.lifecycle_events,
                RagLifecycleEvent(5, "context_delete", 1, True, "context:adapter", None, None),
            ),
        )
        return CleanupResult(success=True, already_closed=False)


class _DocumentRagGatewayFake:
    """返回唯一的原生会话，验证 Adapter 不通过进程全局字典查找任务。"""

    def __init__(self, session: _NativeRagSessionFake) -> None:
        self.session = session
        self.open_names: tuple[str, str] | None = None

    def open_isolated_session(self, *, context_name: str, conversation_name: str) -> _NativeRagSessionFake:
        self.open_names = (context_name, conversation_name)
        return self.session


class _DocumentRagFactoryFake:
    """记录租约 enter/exit，确保任务级 Adapter 释放自己的 Transport。"""

    def __init__(self, gateway: _DocumentRagGatewayFake) -> None:
        self.gateway = gateway
        self.entered = 0
        self.exited = 0

    @contextmanager
    def create(self) -> Iterator[_DocumentRagGatewayFake]:
        self.entered += 1
        try:
            yield self.gateway
        finally:
            self.exited += 1


class AnalysisProductionAdaptersTests(unittest.TestCase):
    """验证真实 Adapter 的任务目录、三态外部结果和 SQLite 审计映射。"""

    def test_file_adapter_keeps_download_normalize_and_ocr_outputs_inside_task_directory(self) -> None:
        execution = _execution()
        observed: dict[str, str] = {}

        with TemporaryDirectory() as temporary_root:
            workspace = LocalAnalysisTaskWorkspaceAdapter(temporary_root).create(execution)
            task_root = Path(workspace.root_path)

            def downloader(url: str, file_name: str, download_dir: str, timeout: float, maximum: int) -> str:
                target = Path(download_dir) / file_name
                target.write_text("原始文件", encoding="utf-8")
                return str(target)

            def upload_preparer(source_path: str, config: OCRConfig) -> str:
                source = Path(source_path)
                scoped_root = source.parents[1]
                observed["cache_dir"] = config.cache_dir
                observed["mineru_cache_dir"] = config.mineru_cache_dir
                target = scoped_root / "upload" / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                return str(target)

            adapter = LegacyAnalysisFilePreparationAdapter(
                ocr_config_loader=_ocr_config,
                downloader=downloader,
                normalizer=lambda source_path: source_path,
                upload_preparer=upload_preparer,
                text_reader=lambda path: "隔离后的正文",
            )
            prepared = adapter.prepare(
                AnalysisFilePreparationRequest(
                    execution=execution,
                    source_url="https://example.invalid/source.txt",
                    task_root=workspace.root_path,
                )
            )

            for value in (prepared.source_path, prepared.upload_path, observed["cache_dir"], observed["mineru_cache_dir"]):
                Path(value).resolve().relative_to(task_root.resolve())
            self.assertEqual("隔离后的正文", prepared.original_text)
            self.assertTrue(Path(prepared.source_path).is_file())
            self.assertTrue(Path(prepared.upload_path).is_file())

    def test_file_adapter_converts_three_legacy_formats_before_rag_text_and_translation(self) -> None:
        """DOC/PPT/XLS 都发布任务内 OOXML；转换 Job 清理后返回路径仍然有效。"""

        target_suffixes = {"doc": ".docx", "ppt": ".pptx", "xls": ".xlsx"}
        for source_suffix, target_suffix in target_suffixes.items():
            with self.subTest(source_suffix=source_suffix), TemporaryDirectory() as temporary_root:
                execution = _execution(f"analysis-legacy-{source_suffix}")
                if source_suffix == "xls":
                    execution = replace(execution, file_name="customer-hash.xls")
                workspace = LocalAnalysisTaskWorkspaceAdapter(temporary_root).create(execution)
                observed: dict[str, object] = {"closed": False}

                def downloader(_url, file_name, download_dir, _timeout, _maximum):  # type: ignore[no-untyped-def]
                    target = Path(download_dir) / file_name
                    target.write_bytes(b"legacy-binary")
                    return str(target)

                class Result:
                    converted = True
                    target_suffix = target_suffixes[source_suffix]

                    def __init__(self, prepared_path: Path) -> None:
                        self.prepared_path = prepared_path

                    def __enter__(self):  # type: ignore[no-untyped-def]
                        return self

                    def __exit__(self, *_args):  # type: ignore[no-untyped-def]
                        self.prepared_path.unlink()
                        observed["closed"] = True

                class Preparer:
                    def preflight(self) -> str:
                        return "26.2.1.0"

                    def prepare(self, source_path, *, job_id):  # type: ignore[no-untyped-def]
                        observed["raw"] = str(source_path)
                        observed["job_id"] = job_id
                        prepared_path = Path(temporary_root) / (
                            f"prepared-{'a' * 32}{target_suffixes[source_suffix]}"
                        )
                        prepared_path.write_text("converted-body", encoding="utf-8")
                        observed["temporary"] = str(prepared_path)
                        return Result(prepared_path)

                def upload_preparer(source_path: str, _config: OCRConfig) -> str:
                    observed["upload_input"] = source_path
                    return source_path

                def text_reader(source_path: str) -> str:
                    observed["text_input"] = source_path
                    return Path(source_path).read_text(encoding="utf-8")

                source_url = (
                    "https://example.invalid/download?id=opaque-token"
                    if source_suffix == "xls"
                    else f"https://example.invalid/source.{source_suffix}"
                )
                adapter = LegacyAnalysisFilePreparationAdapter(
                    ocr_config_loader=_ocr_config,
                    downloader=downloader,
                    normalizer=lambda path: path,
                    upload_preparer=upload_preparer,
                    text_reader=text_reader,
                    legacy_office_preparer=Preparer(),
                )
                prepared = adapter.prepare(
                    AnalysisFilePreparationRequest(
                        execution=execution,
                        source_url=source_url,
                        task_root=workspace.root_path,
                        document_processing_policy=(
                            AnalysisDocumentProcessingPolicySnapshot.for_source(
                                source_url,
                                business_file_name=execution.file_name,
                                allowed_version_series="26.2",
                            )
                        ),
                    )
                )

                task_root = Path(workspace.root_path).resolve()
                Path(prepared.source_path).resolve().relative_to(task_root)
                Path(prepared.processing_path).resolve().relative_to(task_root)
                Path(prepared.upload_path).resolve().relative_to(task_root)
                self.assertEqual(target_suffix, Path(prepared.processing_path).suffix)
                self.assertEqual(prepared.processing_path, observed["upload_input"])
                self.assertEqual(prepared.upload_path, observed["text_input"])
                self.assertEqual("converted-body", prepared.original_text)
                self.assertTrue(observed["closed"])
                self.assertFalse(Path(str(observed["temporary"])).exists())
                self.assertTrue(Path(prepared.processing_path).is_file())
                self.assertRegex(
                    prepared.internal_prepared_basename,
                    r"^prepared-[0-9a-f]{32}\.(docx|pptx|xlsx)$",
                )

    def test_file_adapter_legacy_conversion_failure_never_uses_raw_fallback(self) -> None:
        """转换能力、版本或输出失败时，不得调用 MHTML/OCR 把原格式送进 RAG。"""

        execution = _execution("analysis-legacy-failure")
        calls: list[str] = []
        with TemporaryDirectory() as temporary_root:
            workspace = LocalAnalysisTaskWorkspaceAdapter(temporary_root).create(execution)

            def downloader(_url, file_name, download_dir, _timeout, _maximum):  # type: ignore[no-untyped-def]
                target = Path(download_dir) / file_name
                target.write_bytes(b"secret-legacy-binary")
                return str(target)

            class FailingPreparer:
                def preflight(self) -> str:
                    return "26.2.9"

                def prepare(self, _source_path, *, job_id):  # type: ignore[no-untyped-def]
                    raise LegacyOfficeConversionError(
                        "test_conversion_failure",
                        diagnostic="C:/private/profile/secret",
                    )

            source_url = "https://example.invalid/private.xls"
            adapter = LegacyAnalysisFilePreparationAdapter(
                ocr_config_loader=_ocr_config,
                downloader=downloader,
                normalizer=lambda path: calls.append("normalize") or path,
                upload_preparer=lambda path, _config: calls.append("upload") or path,
                text_reader=lambda path: calls.append("text") or "",
                legacy_office_preparer=FailingPreparer(),
            )
            with self.assertLogs(
                "app.modules.analysis.adapters.legacy_files",
                level="ERROR",
            ) as captured:
                with self.assertRaisesRegex(
                    AnalysisFilePreparationError,
                    "Legacy Office 文件本地转换失败",
                ):
                    adapter.prepare(
                        AnalysisFilePreparationRequest(
                            execution=execution,
                            source_url=source_url,
                            task_root=workspace.root_path,
                            document_processing_policy=(
                                AnalysisDocumentProcessingPolicySnapshot.for_source(
                                    source_url
                                )
                            ),
                        )
                    )

        self.assertEqual([], calls)
        joined_logs = "\n".join(captured.output)
        self.assertIn("error_code=test_conversion_failure", joined_logs)
        self.assertNotIn("private/profile", joined_logs)
        self.assertNotIn("secret-legacy-binary", joined_logs)

    def test_file_adapter_rejects_runtime_version_drift_from_accepted_snapshot(self) -> None:
        """重启后运行时版本系列漂移时保留失败现场，不用新环境猜测执行承诺。"""

        execution = replace(
            _execution("analysis-legacy-version-drift"),
            file_name="versioned.xls",
        )
        prepare_called = False
        with TemporaryDirectory() as temporary_root:
            workspace = LocalAnalysisTaskWorkspaceAdapter(temporary_root).create(execution)

            def downloader(_url, file_name, download_dir, _timeout, _maximum):  # type: ignore[no-untyped-def]
                target = Path(download_dir) / file_name
                target.write_bytes(b"legacy")
                return str(target)

            class DriftedPreparer:
                def preflight(self) -> str:
                    return "27.1.0"

                def prepare(self, _source_path, *, job_id):  # type: ignore[no-untyped-def]
                    nonlocal prepare_called
                    prepare_called = True
                    raise AssertionError("版本门禁失败后不得启动转换进程")

            source_url = "https://example.invalid/download?id=versioned"
            adapter = LegacyAnalysisFilePreparationAdapter(
                ocr_config_loader=_ocr_config,
                downloader=downloader,
                normalizer=lambda path: path,
                upload_preparer=lambda path, _config: path,
                text_reader=lambda _path: "",
                legacy_office_preparer=DriftedPreparer(),
            )
            with self.assertRaises(AnalysisFilePreparationError):
                adapter.prepare(
                    AnalysisFilePreparationRequest(
                        execution=execution,
                        source_url=source_url,
                        task_root=workspace.root_path,
                        document_processing_policy=(
                            AnalysisDocumentProcessingPolicySnapshot.for_source(
                                source_url,
                                business_file_name=execution.file_name,
                                allowed_version_series="26.2",
                            )
                        ),
                    )
                )

        self.assertFalse(prepare_called)

    def test_file_adapter_rejects_downloader_path_outside_current_task_directory(self) -> None:
        execution = _execution("analysis-adapter-task-escape")
        with TemporaryDirectory() as temporary_root:
            workspace = LocalAnalysisTaskWorkspaceAdapter(temporary_root).create(execution)
            escaped = Path(temporary_root) / "escaped.txt"
            escaped.write_text("not task owned", encoding="utf-8")
            adapter = LegacyAnalysisFilePreparationAdapter(
                ocr_config_loader=_ocr_config,
                downloader=lambda *_: str(escaped),
                normalizer=lambda source_path: source_path,
                upload_preparer=lambda source_path, config: source_path,
                text_reader=lambda path: "",
            )

            with self.assertRaises(AnalysisFilePreparationError):
                adapter.prepare(
                    AnalysisFilePreparationRequest(
                        execution=execution,
                        source_url="https://example.invalid/escape.txt",
                        task_root=workspace.root_path,
                    )
                )

    def test_knowledge_adapter_preserves_committed_not_applied_and_unknown_outcomes(self) -> None:
        execution = _execution()
        request = AnalysisKnowledgeWriteRequest(
            execution=execution,
            architecture_id=103,
            idempotency_key="document:v1:adapter",
            document=_bound_session(execution),
            metadata=AnalysisKnowledgeDocumentMetadata(
                file_name=execution.file_name,
                original_file_name=execution.file_name,
                attributes=FrozenJsonObject.from_mapping({"country": "中国"}),
            ),
        )
        expected = {
            "committed": AnalysisKnowledgeWriteOutcome.COMMITTED,
            "released": AnalysisKnowledgeWriteOutcome.NOT_APPLIED,
            "retained": AnalysisKnowledgeWriteOutcome.OUTCOME_UNKNOWN,
        }

        for mode, outcome in expected.items():
            with self.subTest(mode=mode):
                port = _KnowledgePortFake(mode)
                factory = _KnowledgeFactoryFake(port)
                result = LegacyAnalysisKnowledgeAdapter(factory).persist(request)

                self.assertEqual(outcome, result.outcome)
                self.assertEqual(1, factory.entered)
                self.assertEqual(1, factory.exited)
                self.assertEqual(["ensure_collection", "store_prepared_document"], port.calls)
                if outcome is AnalysisKnowledgeWriteOutcome.COMMITTED:
                    self.assertEqual("knowledge:adapter", result.external_ref)
                else:
                    self.assertTrue(result.detail_code)

    def test_knowledge_handoff_keeps_original_and_ingested_names_separate(self) -> None:
        """永久转交保留业务原名，同时沿用会话确认的真实上传名与文档身份。"""

        execution = _execution("analysis-knowledge-name-semantics")
        session = _bound_session(execution)
        request = AnalysisKnowledgeWriteRequest(
            execution=execution,
            architecture_id=103,
            idempotency_key="document:v1:name-semantics",
            document=session,
            metadata=AnalysisKnowledgeDocumentMetadata(
                file_name=execution.file_name,
                original_file_name=" 原始资料.pdf",
                attributes=FrozenJsonObject.from_mapping({}),
            ),
        )
        port = _KnowledgePortFake("committed")

        result = LegacyAnalysisKnowledgeAdapter(
            _KnowledgeFactoryFake(port)
        ).persist(request)

        self.assertEqual(AnalysisKnowledgeWriteOutcome.COMMITTED, result.outcome)
        self.assertEqual(" 原始资料.pdf", port.last_metadata.original_name)
        self.assertEqual(session.ingested_file_name, port.last_metadata.ingested_file_name)
        self.assertEqual(session.document_ref, port.last_document.document_ref)
        self.assertEqual(
            session.document_location,
            port.last_document.external_location,
        )

    def test_knowledge_adapter_treats_malformed_post_commit_result_as_unknown(self) -> None:
        """外部写入返回后再发现结果不完整时，必须保留现场而不是抛普通异常。"""

        execution = _execution("analysis-knowledge-malformed-success")
        request = AnalysisKnowledgeWriteRequest(
            execution=execution,
            architecture_id=103,
            idempotency_key="document:v1:malformed-success",
            document=_bound_session(execution),
            metadata=AnalysisKnowledgeDocumentMetadata(
                file_name=execution.file_name,
                original_file_name=execution.file_name,
                attributes=FrozenJsonObject.from_mapping({}),
            ),
        )

        result = LegacyAnalysisKnowledgeAdapter(
            _KnowledgeFactoryFake(_KnowledgePortFake("malformed_success"))
        ).persist(request)

        self.assertEqual(AnalysisKnowledgeWriteOutcome.OUTCOME_UNKNOWN, result.outcome)
        self.assertEqual("knowledge_success_result_invalid", result.detail_code)

    def test_rag_adapter_binds_first_document_and_releases_own_transport(self) -> None:
        execution = _execution()
        native_session = _NativeRagSessionFake()
        gateway = _DocumentRagGatewayFake(native_session)
        factory = _DocumentRagFactoryFake(gateway)

        with LegacyAnalysisRagAdapterFactory(factory).create(execution) as adapter:
            opened = adapter.open_session(
                AnalysisRagSessionOpenRequest(
                    execution=execution,
                    upload_path="C:/runtime/adapter-demo.txt",
                )
            )
            self.assertFalse(opened.session.document_bound)
            result = adapter.execute(
                AnalysisRagRequest(
                    execution=execution,
                    session=opened.session,
                    operation=AnalysisRagOperation.EXTRACTION,
                    prompt="请抽取字段",
                    attempt_number=1,
                )
            )
            self.assertTrue(result.session.document_bound)
            self.assertEqual((3, 4), tuple(item.sequence_no for item in result.lifecycle_events))
            closed = adapter.close_session(
                AnalysisRagCloseRequest(
                    execution=execution,
                    session=result.session,
                    retain_document=True,
                )
            )
            self.assertEqual(AnalysisRagCloseOutcome.CONFIRMED, closed.outcome)
            self.assertEqual((5,), tuple(item.sequence_no for item in closed.lifecycle_events))

        self.assertTrue(native_session.closed_with)
        self.assertEqual(1, factory.entered)
        self.assertEqual(1, factory.exited)
        self.assertEqual(
            (f"llm-file-{execution.task_id.value}", "analysis-adapter-demo"),
            gateway.open_names,
        )

    def test_rag_adapter_maps_provider_neutral_upload_descriptor(self) -> None:
        """Analysis Adapter 只映射通用 DTO，不直接构造供应商 metadata。"""

        execution = _execution("analysis-rag-upload-options")
        native_session = _NativeRagSessionFake()
        factory = _DocumentRagFactoryFake(_DocumentRagGatewayFake(native_session))
        artifact = ArtifactRef(
            task_id=execution.task_id,
            artifact_id="a" * 64,
            step_key="b" * 64,
            kind=ArtifactKind.RAG_PROJECTION,
            representation=DocumentRepresentation.MARKDOWN,
            metadata=ArtifactMetadata(
                media_type="text/markdown; charset=utf-8",
                size_bytes=16,
                sha256="c" * 64,
            ),
        )
        descriptor = AnalysisRagUploadDescriptor(
            artifact=artifact,
            representation=DocumentRepresentation.MARKDOWN,
            media_type=artifact.metadata.media_type,
            transport_file_name="Nimitz (CVN 68) class.md",
            display_title="Nimitz (CVN 68) class.pdf",
            projection_profile_id="d" * 64,
        )

        with LegacyAnalysisRagAdapterFactory(factory).create(execution) as adapter:
            opened = adapter.open_session(
                AnalysisRagSessionOpenRequest(
                    execution=execution,
                    upload_path="C:/runtime/prepared.md",
                    upload_descriptor=descriptor,
                )
            )
            adapter.execute(
                AnalysisRagRequest(
                    execution=execution,
                    session=opened.session,
                    operation=AnalysisRagOperation.EXTRACTION,
                    prompt="请抽取字段",
                    attempt_number=1,
                )
            )

        self.assertIsNotNone(native_session.document_upload)
        self.assertEqual(
            "Nimitz (CVN 68) class.md",
            native_session.document_upload.transport_file_name,
        )
        self.assertEqual(
            "Nimitz (CVN 68) class.pdf",
            native_session.document_upload.display_title,
        )

    def test_rag_adapter_does_not_reuse_previous_response_when_stage_switch_fails(self) -> None:
        """阶段 Conversation 在查询前失败时，当前失败 attempt 不得串用上一轮响应。"""

        execution = _execution()
        native_session = _NativeRagSessionFake()
        factory = _DocumentRagFactoryFake(_DocumentRagGatewayFake(native_session))
        with LegacyAnalysisRagAdapterFactory(factory).create(execution) as adapter:
            opened = adapter.open_session(
                AnalysisRagSessionOpenRequest(execution, "C:/runtime/adapter-demo.txt")
            )
            classified = adapter.execute(
                AnalysisRagRequest(
                    execution,
                    opened.session,
                    AnalysisRagOperation.CLASSIFICATION,
                    "分类",
                    1,
                )
            )
            native_session.start_fresh_conversation = lambda **_kwargs: False  # type: ignore[method-assign]

            with self.assertRaises(AnalysisRagExecutionError) as raised:
                adapter.execute(
                    AnalysisRagRequest(
                        execution,
                        classified.session,
                        AnalysisRagOperation.IDENTITY_RESELECT,
                        "身份重选",
                        1,
                    )
                )

        self.assertIsNone(raised.exception.raw_response)
        self.assertEqual((), raised.exception.sources)

    def test_rag_adapter_uses_latest_successful_stage_conversation_ref(self) -> None:
        """共享 trace 保持主引用时，Adapter 仍应把结果关联到实际活动 Conversation。"""

        execution = _execution()
        native_session = _NativeRagSessionFake()
        factory = _DocumentRagFactoryFake(_DocumentRagGatewayFake(native_session))
        with LegacyAnalysisRagAdapterFactory(factory).create(execution) as adapter:
            opened = adapter.open_session(
                AnalysisRagSessionOpenRequest(execution, "C:/runtime/adapter-demo.txt")
            )
            classified = adapter.execute(
                AnalysisRagRequest(
                    execution,
                    opened.session,
                    AnalysisRagOperation.CLASSIFICATION,
                    "分类",
                    1,
                )
            )

            def switch_conversation(**_kwargs):  # type: ignore[no-untyped-def]
                native_session.trace = replace(
                    native_session.trace,
                    lifecycle_events=(
                        *native_session.trace.lifecycle_events,
                        RagLifecycleEvent(
                            5,
                            "conversation_create",
                            2,
                            True,
                            "conversation:fresh",
                            None,
                            None,
                        ),
                    ),
                )
                return True

            prepared = PreparedDocumentRef(
                document_ref="document:adapter",
                external_location="location:adapter",
                content_sha256="c" * 64,
                ingested_file_name="adapter-demo.txt",
            )
            native_session.start_fresh_conversation = switch_conversation  # type: ignore[method-assign]
            native_session.ask_optional = lambda *_args, **_kwargs: RagResult(  # type: ignore[method-assign]
                text="{}",
                sources=(),
                prepared_document=prepared,
                trace=native_session.trace,
            )
            reselected = adapter.execute(
                AnalysisRagRequest(
                    execution,
                    classified.session,
                    AnalysisRagOperation.IDENTITY_RESELECT,
                    "身份重选",
                    1,
                )
            )

        self.assertEqual("conversation:fresh", reselected.session.conversation_ref)
        self.assertEqual(
            "context:adapter::conversation:fresh",
            reselected.session.session_ref,
        )

    def test_rag_adapter_wraps_unexpected_error_with_unknown_lifecycle(self) -> None:
        """未分类异常必须稳定转换为 unknown，而不是被 DTO 一致性 ValueError 掩盖。"""

        execution = _execution()
        native_session = _NativeRagSessionFake()
        factory = _DocumentRagFactoryFake(_DocumentRagGatewayFake(native_session))
        with LegacyAnalysisRagAdapterFactory(factory).create(execution) as adapter:
            opened = adapter.open_session(
                AnalysisRagSessionOpenRequest(execution, "C:/runtime/adapter-demo.txt")
            )
            classified = adapter.execute(
                AnalysisRagRequest(
                    execution,
                    opened.session,
                    AnalysisRagOperation.CLASSIFICATION,
                    "分类",
                    1,
                )
            )
            native_session.start_fresh_conversation = lambda **_kwargs: True  # type: ignore[method-assign]
            native_session.ask_optional = lambda *_args, **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
                RuntimeError("unexpected")
            )

            with self.assertRaises(AnalysisRagExecutionError) as raised:
                adapter.execute(
                    AnalysisRagRequest(
                        execution,
                        classified.session,
                        AnalysisRagOperation.IDENTITY_RESELECT,
                        "身份重选",
                        1,
                    )
                )

        self.assertTrue(raised.exception.outcome_unknown)
        self.assertTrue(
            any(
                event.outcome is AnalysisRagLifecycleOutcome.OUTCOME_UNKNOWN
                for event in raised.exception.lifecycle_events
            )
        )
        self.assertIsNone(raised.exception.raw_response)

    def test_rag_adapter_treats_malformed_success_mapping_as_unknown(self) -> None:
        """远端查询成功后若来源映射损坏，必须保留已绑定文档并返回 unknown。"""

        execution = _execution("analysis-rag-malformed-success")
        native_session = _NativeRagSessionFake()
        original_analyse = native_session.analyse

        def malformed_analyse(*args, **kwargs):  # type: ignore[no-untyped-def]
            result = original_analyse(*args, **kwargs)
            return replace(
                result,
                sources=(
                    RagSource(
                        document_ref="document:adapter",
                        text="来源正文",
                    ),
                ),
            )

        native_session.analyse = malformed_analyse  # type: ignore[method-assign]
        factory = _DocumentRagFactoryFake(_DocumentRagGatewayFake(native_session))
        with LegacyAnalysisRagAdapterFactory(factory).create(execution) as adapter:
            adapter._source_from_native = (  # type: ignore[method-assign]
                lambda _source: (_ for _ in ()).throw(
                    ValueError("malformed source mapping")
                )
            )
            opened = adapter.open_session(
                AnalysisRagSessionOpenRequest(
                    execution,
                    "C:/runtime/adapter-demo.txt",
                )
            )
            with self.assertRaises(AnalysisRagExecutionError) as raised:
                adapter.execute(
                    AnalysisRagRequest(
                        execution,
                        opened.session,
                        AnalysisRagOperation.CLASSIFICATION,
                        "分类",
                        1,
                    )
                )

        self.assertTrue(raised.exception.outcome_unknown)
        self.assertEqual(
            "analysis_rag_success_result_invalid",
            raised.exception.error_code,
        )
        self.assertTrue(
            any(
                event.outcome is AnalysisRagLifecycleOutcome.OUTCOME_UNKNOWN
                for event in raised.exception.lifecycle_events
            )
        )
        self.assertTrue(
            {"document_upload", "document_bind"}.issubset(
                {
                    event.operation
                    for event in raised.exception.lifecycle_events
                }
            )
        )

    def test_rag_adapter_open_result_missing_identity_is_unknown(self) -> None:
        """打开调用返回但缺少会话引用时，不能断言外部 Context 未创建。"""

        execution = _execution("analysis-rag-open-identity-missing")
        native_session = _NativeRagSessionFake()
        native_session.trace = replace(
            native_session.trace,
            context_ref="",
            conversation_ref="",
        )
        factory = _DocumentRagFactoryFake(_DocumentRagGatewayFake(native_session))

        with LegacyAnalysisRagAdapterFactory(factory).create(execution) as adapter:
            with self.assertRaises(AnalysisRagSessionOpenError) as raised:
                adapter.open_session(
                    AnalysisRagSessionOpenRequest(
                        execution,
                        "C:/runtime/adapter-demo.txt",
                    )
                )

        self.assertTrue(raised.exception.outcome_unknown)
        self.assertTrue(
            any(
                event.outcome is AnalysisRagLifecycleOutcome.OUTCOME_UNKNOWN
                for event in raised.exception.lifecycle_events
            )
        )

    def test_rag_adapter_repeated_close_preserves_first_unknown_result(self) -> None:
        """close 超时后重复调用只能返回同一 unknown 事实，不能升级为已知未执行。"""

        execution = _execution()
        native_session = _NativeRagSessionFake()
        factory = _DocumentRagFactoryFake(_DocumentRagGatewayFake(native_session))
        with LegacyAnalysisRagAdapterFactory(factory).create(execution) as adapter:
            opened = adapter.open_session(
                AnalysisRagSessionOpenRequest(execution, "C:/runtime/adapter-demo.txt")
            )
            analysed = adapter.execute(
                AnalysisRagRequest(
                    execution,
                    opened.session,
                    AnalysisRagOperation.EXTRACTION,
                    "抽取",
                    1,
                )
            )
            native_session.close = lambda **_kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
                TimeoutError("close timeout")
            )
            request = AnalysisRagCloseRequest(
                execution,
                analysed.session,
                retain_document=False,
            )
            first = adapter.close_session(request)
            second = adapter.close_session(request)

        self.assertEqual(AnalysisRagCloseOutcome.OUTCOME_UNKNOWN, first.outcome)
        self.assertEqual(first, second)

    def test_audit_adapter_writes_recall_interaction_and_close_evidence_to_temporary_sqlite(self) -> None:
        """验证新 DTO 能完整落到现有 SQLite 审计事务，而不是只做结构类型检查。"""

        # SQLite 的 WAL/shm 文件在 Windows 上可能在解释器回收前仍被短暂占用；复用项目
        # 统一临时目录上下文，避免清理瞬态干扰审计事务本身的离线断言。
        with workspace_tempdir() as temporary_root:
            service = LLMTaskService(db_path=str(Path(temporary_root) / "tasks.sqlite3"))
            task = service.create_file_task("adapter-audit.txt", {"businessType": "file"})
            execution = AnalysisExecutionRef(
                task_id=TaskId(task["execution_id"]),
                file_name="adapter-audit.txt",
                batch_id="b" * 32,
                batch_sequence=1,
            )
            adapter = LegacyAnalysisAuditAdapter(service)
            recall_payload = FrozenJsonObject.from_mapping(
                {
                    "tree_fingerprint": "a" * 64,
                    "query_digest": "b" * 64,
                    "base_top64": [103],
                    "final_candidates": [
                        {"id": 103, "pathName": "装备/型号", "nodeType": "leaf"}
                    ],
                    "channel_rankings": {
                        "exact": [103],
                        "lexical": [103],
                        "tree": [103],
                        "rule": [],
                    },
                    "rrf_scores": {"103": 0.1},
                    "protected_reasons": {"103": ["test"]},
                    "prompt_chars": 10,
                    "recall_elapsed_ms": 1,
                },
                name="adapter_recall",
            )
            recall = adapter.reserve_recall(
                AnalysisRecallAuditRecord(
                    execution=execution,
                    idempotency_key=f"analysis-recall:{execution.task_id.value}",
                    payload=recall_payload,
                )
            )
            finalized = adapter.finalize_recall(
                FinalizeAnalysisRecallAudit(
                    receipt=recall,
                    expected_version=recall.version,
                    outcome=AnalysisAuditOutcome.SUCCEEDED,
                    payload=FrozenJsonObject.from_mapping(
                        {
                            "returned_architecture_id": 103,
                            "returned_rank": 1,
                            "total_elapsed_ms": 1,
                            "failure_stage": None,
                            "error_message": "",
                        },
                        name="adapter_recall_finalized",
                    ),
                )
            )
            self.assertTrue(finalized.finalized)

            prompt = "请抽取文件字段"
            session = _bound_session(execution)
            interaction = adapter.persist_interaction(
                AnalysisInteractionAuditRecord(
                    execution=execution,
                    idempotency_key=f"analysis-rag:{execution.task_id.value}",
                    session=session,
                    context_name=f"llm-file-{execution.task_id.value}",
                    trace_id="adapter-audit-trace",
                    prompt=prompt,
                    attempts=(
                        AnalysisInteractionAttempt(
                            operation=AnalysisRagOperation.EXTRACTION,
                            attempt_number=1,
                            prompt_digest=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                            raw_response='{"architectureId":103}',
                        ),
                    ),
                    lifecycle_events=(
                        AnalysisRagLifecycleEvent(1, "context_create", 1, AnalysisRagLifecycleOutcome.SUCCEEDED, session.context_ref),
                        AnalysisRagLifecycleEvent(2, "conversation_create", 1, AnalysisRagLifecycleOutcome.SUCCEEDED, session.conversation_ref),
                        AnalysisRagLifecycleEvent(3, "document_upload", 1, AnalysisRagLifecycleOutcome.SUCCEEDED, session.document_location),
                        AnalysisRagLifecycleEvent(4, "document_bind", 1, AnalysisRagLifecycleOutcome.SUCCEEDED, session.document_ref),
                    ),
                    outcome=AnalysisAuditOutcome.SUCCEEDED,
                    document_upload=FrozenJsonObject.from_mapping(
                        {
                            "representation": "markdown",
                            "media_type": "text/markdown; charset=utf-8",
                            "transport_file_name": "原始资料.md",
                            "display_title": "原始资料.pdf",
                            "artifact_sha256": "c" * 64,
                            "artifact_id": "a" * 64,
                            "projection_profile_id": "d" * 64,
                        },
                        name="analysis_upload_audit",
                    ),
                )
            )
            self.assertEqual(
                interaction,
                adapter.load_interaction(
                    LoadAnalysisInteraction(
                        execution=execution,
                        idempotency_key=interaction.idempotency_key,
                    )
                ),
            )
            adapter.append_lifecycle_events(
                AppendAnalysisLifecycleEvents(
                    receipt=interaction,
                    events=(
                        AnalysisRagLifecycleEvent(
                            5,
                            "context_delete",
                            1,
                            AnalysisRagLifecycleOutcome.SUCCEEDED,
                            session.context_ref,
                        ),
                    ),
                )
            )
            rows = service.get_llm_interactions("file", execution.file_name)
            self.assertEqual(1, len(rows))
            self.assertEqual(execution.task_id.value, rows[0]["execution_id"])
            self.assertEqual(
                "原始资料.md",
                rows[0]["document_upload"]["transport_file_name"],
            )
            self.assertEqual(
                "原始资料.pdf",
                rows[0]["document_upload"]["display_title"],
            )

    def test_audit_adapter_persists_context_only_open_failure(self) -> None:
        """Conversation 创建和 Context 回滚均失败时，仍须保存 Context 恢复引用。"""

        with workspace_tempdir() as temporary_root:
            service = LLMTaskService(db_path=str(Path(temporary_root) / "tasks.sqlite3"))
            task = service.create_file_task(
                "partial-open.txt",
                {"businessType": "file"},
            )
            execution = AnalysisExecutionRef(
                task_id=TaskId(task["execution_id"]),
                file_name="partial-open.txt",
                batch_id="d" * 32,
                batch_sequence=1,
            )
            adapter = LegacyAnalysisAuditAdapter(service)
            receipt = adapter.persist_interaction(
                AnalysisInteractionAuditRecord(
                    execution=execution,
                    idempotency_key=f"analysis-rag:{execution.task_id.value}",
                    session=None,
                    context_name=f"llm-file-{execution.task_id.value}",
                    trace_id="partial-open-trace",
                    prompt="文件分析会话打开失败",
                    attempts=(),
                    lifecycle_events=(
                        AnalysisRagLifecycleEvent(
                            1,
                            "context_create",
                            1,
                            AnalysisRagLifecycleOutcome.SUCCEEDED,
                            "context:partial-open",
                        ),
                        AnalysisRagLifecycleEvent(
                            2,
                            "conversation_create",
                            1,
                            AnalysisRagLifecycleOutcome.FAILED,
                            error_code="conversation_create_failed",
                        ),
                        AnalysisRagLifecycleEvent(
                            3,
                            "context_rollback",
                            1,
                            AnalysisRagLifecycleOutcome.FAILED,
                            "context:partial-open",
                            "context_rollback_failed",
                        ),
                    ),
                    outcome=AnalysisAuditOutcome.FAILED,
                    error_code="rag_open_conversation_create",
                )
            )

            loaded = adapter.load_interaction(
                LoadAnalysisInteraction(
                    execution=execution,
                    idempotency_key=receipt.idempotency_key,
                )
            )
            row = service.get_llm_interaction_by_execution(
                "file",
                execution.file_name,
                execution.task_id.value,
                receipt.idempotency_key,
            )

        self.assertEqual(receipt, loaded)
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual("context:partial-open", row["workspace_slug"])
        self.assertEqual("", row["thread_slug"])
        self.assertEqual("failed", row["workspace_cleanup_status"])


if __name__ == "__main__":
    unittest.main()
