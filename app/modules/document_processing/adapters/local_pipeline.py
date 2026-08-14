"""本地单实例部署使用的统一文档准备流水线。

本适配器是“宿主路径”与共享 Artifact 内核之间唯一的桥。业务模块可以把已经下载到
任务目录的文件交给这里，但格式判断、Processor 选择、谱系记录和最终路径解析都不再
散落在 Report、Analysis 或 Translation 中。

当前实现仍是阶段 1 的本地文件 + SQLite 组合，不宣称具备多实例全局限流能力。未来
替换对象存储或可靠队列时，应保持 :class:`LocalPreparedArtifact` 中的 ArtifactRef
语义不变，并把这里的路径字段继续限制在基础设施 Adapter 之间。
"""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from uuid import uuid4

from app.modules.document_processing.adapters.builtin_ocr import (
    BuiltinOCRDocumentProcessorAdapter,
    build_builtin_ocr_profile,
    is_scanned_pdf,
)
from app.modules.document_processing.adapters.capacity import (
    ResourceLimitedDocumentProcessorAdapter,
)
from app.modules.document_processing.adapters.content import FileArtifactContent
from app.modules.document_processing.adapters.libreoffice import (
    LibreOfficeDocumentProcessorAdapter,
    create_legacy_office_profile,
)
from app.modules.document_processing.adapters.libreoffice.profile import (
    target_suffix_for,
)
from app.modules.document_processing.adapters.local_artifacts import (
    LocalArtifactStoreAdapter,
)
from app.modules.document_processing.adapters.mhtml import (
    MHTMLBrowserConversionError,
    MHTMLBrowserPDFProcessorAdapter,
    MHTMLTextProcessorAdapter,
    MHTMLToPDFConverter,
    create_mhtml_browser_profile,
    create_mhtml_text_profile,
)
from app.modules.document_processing.adapters.mineru import (
    MinerUDocumentProcessorAdapter,
    MinerUOperationObserver,
    build_mineru_profile,
    mineru_endpoint_fingerprint,
)
from app.modules.document_processing.adapters.passthrough import (
    ValidatedPassthroughDocumentProcessorAdapter,
    build_passthrough_profile,
)
from app.modules.document_processing.application import (
    PrepareDocument,
    PrepareMHTMLDocument,
    PrepareMHTMLRequest,
)
from app.modules.document_processing.domain import (
    ArtifactKind,
    ArtifactRef,
    DocumentProcessingRequest,
    DocumentRepresentation,
    ProcessingOutcome,
    is_mhtml_content,
)
from app.modules.document_processing.ports import (
    ArtifactCatalogPort,
    ArtifactPublication,
    LegacyOfficePreparer,
    ProcessingRecordPort,
    ResourcePort,
)
from app.modules.tasks.domain import TaskId


logger = logging.getLogger(__name__)

_LEGACY_SUFFIXES = frozenset({".doc", ".ppt", ".xls"})
_MINERU_SUFFIXES = frozenset(
    {".pdf", ".docx", ".pptx", ".xlsx", ".png", ".jpg", ".jpeg"}
)
_MHTML_SUFFIXES = frozenset({".mhtml", ".mht"})
_TEXT_SUFFIXES = {
    ".md": (DocumentRepresentation.MARKDOWN, "text/markdown"),
    ".txt": (DocumentRepresentation.TEXT, "text/plain"),
}


class _NoopMinerUOperationObserver:
    """仅供离线组合显式未提供持久观察器时使用。"""

    def record_submission_intent(
        self,
        *,
        operation_key: str,
        provider: str,
    ) -> None:
        del operation_key, provider

    def record_provider_identity(
        self,
        *,
        operation_key: str,
        provider_operation_id: str,
    ) -> None:
        del operation_key, provider_operation_id

    def record_terminal(
        self,
        *,
        operation_key: str,
        state: str,
    ) -> None:
        del operation_key, state


class ScannedPDFEngine(str, Enum):
    """扫描 PDF 的冻结执行选择。"""

    MINERU = "mineru"
    BUILTIN_OCR = "ocr"


@dataclass(frozen=True, slots=True)
class LocalDocumentPreparationRequest:
    """一次本地文件到 prepared Artifact 的受控请求。"""

    task_id: TaskId
    source_path: Path
    logical_step: str
    trace_id: str
    scanned_pdf_engine: ScannedPDFEngine = ScannedPDFEngine.MINERU

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        source = Path(self.source_path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError("待准备的本地源文件不存在")
        object.__setattr__(self, "source_path", source)
        for field_name in ("logical_step", "trace_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} 必须是非空 str")
            object.__setattr__(self, field_name, value.strip())
        if not isinstance(self.scanned_pdf_engine, ScannedPDFEngine):
            raise TypeError("scanned_pdf_engine 必须是 ScannedPDFEngine")


@dataclass(frozen=True, slots=True)
class LocalPreparedArtifact:
    """共享 Store 中的源、RAG 输入与可选文本 Artifact。

    正常路径下 ``rag_artifact`` 与 ``prepared_artifact`` 指向同一份 Markdown/Text。
    只有扫描 PDF 的两级文本提取都已明确失败时，才允许 ``rag_artifact`` 回退为
    已校验的 SOURCE PDF，同时把 ``prepared_artifact`` 置空。这样 RAG 仍可尝试直接
    解析原 PDF，但正文读取和全文翻译能够明确识别“文本能力不可用”，不会把二进制
    PDF 伪装成 prepared 文本。
    """

    source_artifact: ArtifactRef
    rag_artifact: ArtifactRef
    prepared_artifact: ArtifactRef | None
    prepared_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.source_artifact, ArtifactRef):
            raise TypeError("source_artifact 必须是 ArtifactRef")
        if not isinstance(self.rag_artifact, ArtifactRef):
            raise TypeError("rag_artifact 必须是 ArtifactRef")
        if self.source_artifact.task_id != self.rag_artifact.task_id:
            raise ValueError("source/RAG Artifact 必须属于同一 task")
        if self.rag_artifact.representation not in {
            DocumentRepresentation.MARKDOWN,
            DocumentRepresentation.TEXT,
            DocumentRepresentation.PDF,
        }:
            raise ValueError("RAG Artifact 必须是 Markdown/Text/PDF")
        if self.prepared_artifact is not None:
            if not isinstance(self.prepared_artifact, ArtifactRef):
                raise TypeError("prepared_artifact 必须是 ArtifactRef 或 None")
            if self.source_artifact.task_id != self.prepared_artifact.task_id:
                raise ValueError("source/prepared Artifact 必须属于同一 task")
            if self.prepared_artifact.representation not in {
                DocumentRepresentation.MARKDOWN,
                DocumentRepresentation.TEXT,
            }:
                raise ValueError("最终 prepared Artifact 必须是 Markdown/Text")
            if self.rag_artifact != self.prepared_artifact:
                raise ValueError("存在文本产物时，RAG 必须复用同一 prepared Artifact")
        elif (
            self.rag_artifact != self.source_artifact
            or self.source_artifact.representation is not DocumentRepresentation.PDF
        ):
            raise ValueError("无文本产物时只允许 SOURCE PDF 作为 RAG 降级输入")
        path = Path(self.prepared_path).resolve()
        if not path.is_file():
            raise FileNotFoundError("RAG Artifact 本地文件不存在")
        object.__setattr__(self, "prepared_path", path)


class LocalDocumentPreparationError(RuntimeError):
    """统一文档准备未形成可消费 Artifact。"""

    def __init__(
        self,
        error_code: str,
        *,
        outcome: ProcessingOutcome = ProcessingOutcome.FAILED,
    ) -> None:
        self.error_code = str(error_code).strip() or "document_preparation_failed"
        self.outcome = outcome
        super().__init__(self.error_code)


class LocalDocumentPreparationAdapter:
    """把本地源文件编排为唯一 Markdown/Text Artifact。

    每个 Processor 都复用同一个 Artifact Store、Processing Record 和重型资源许可；
    因此 MHTML → PDF → Markdown、Legacy Office → OOXML → Markdown 等多步骤链仍只有
    一份可追踪谱系，而不是业务模块各自维护临时目录。
    """

    def __init__(
        self,
        *,
        artifact_store: LocalArtifactStoreAdapter,
        records: ProcessingRecordPort,
        resource: ResourcePort,
        legacy_office_preparer: LegacyOfficePreparer,
        materialization_root: str | Path,
        legacy_policy_fingerprint: str,
        ocr_languages: str,
        ocr_dpi: int,
        ocr_enabled: bool = True,
        ocr_sample_pages: int = 3,
        ocr_text_threshold: int = 50,
        mineru_lang: str = "ch",
        mineru_api_url: str | None = None,
        max_text_bytes: int = 512 * 1024 * 1024,
        mineru_operation_observer: MinerUOperationObserver | None = None,
    ) -> None:
        if not isinstance(artifact_store, LocalArtifactStoreAdapter):
            raise TypeError("artifact_store 必须是 LocalArtifactStoreAdapter")
        if not isinstance(records, ProcessingRecordPort):
            raise TypeError("records 必须实现 ProcessingRecordPort")
        if not isinstance(records, ArtifactCatalogPort):
            raise TypeError("records 必须实现 ArtifactCatalogPort")
        if not isinstance(resource, ResourcePort):
            raise TypeError("resource 必须实现 ResourcePort")
        if not isinstance(legacy_office_preparer, LegacyOfficePreparer):
            raise TypeError("legacy_office_preparer 必须实现 LegacyOfficePreparer")
        self._store = artifact_store
        self._records = records
        self._artifact_catalog = records
        self._resource = resource
        self._legacy_office_preparer = legacy_office_preparer
        self._root = Path(materialization_root).expanduser().resolve()
        self._legacy_policy_fingerprint = self._required(
            legacy_policy_fingerprint,
            "legacy_policy_fingerprint",
        )
        self._ocr_languages = self._required(ocr_languages, "ocr_languages")
        self._ocr_dpi = int(ocr_dpi)
        self._ocr_enabled = bool(ocr_enabled)
        self._ocr_sample_pages = int(ocr_sample_pages)
        self._ocr_text_threshold = int(ocr_text_threshold)
        self._mineru_lang = self._required(mineru_lang, "mineru_lang")
        self._mineru_api_url = (
            str(mineru_api_url).strip() if mineru_api_url else None
        )
        self._max_text_bytes = int(max_text_bytes)
        operation_observer = (
            mineru_operation_observer or _NoopMinerUOperationObserver()
        )

        self._passthrough = self._application(
            ValidatedPassthroughDocumentProcessorAdapter(
                source_store=self._store,
            )
        )
        self._mineru = self._application(
            ResourceLimitedDocumentProcessorAdapter(
                processor=MinerUDocumentProcessorAdapter(
                    source_store=self._store,
                    materialization_root=self._root / "mineru",
                    operation_observer=operation_observer,
                    api_url=self._mineru_api_url,
                ),
                resource=self._resource,
            )
        )
        self._ocr = self._application(
            ResourceLimitedDocumentProcessorAdapter(
                processor=BuiltinOCRDocumentProcessorAdapter(
                    source_store=self._store,
                    materialization_root=self._root / "ocr",
                ),
                resource=self._resource,
            )
        )
        self._legacy = self._application(
            LibreOfficeDocumentProcessorAdapter(
                preparer=self._legacy_office_preparer,
                source_store=self._store,
                materialization_root=self._root / "libreoffice",
            )
        )
        self._mhtml_text = self._application(
            MHTMLTextProcessorAdapter(source_store=self._store)
        )

    @property
    def execution_profile_id(self) -> str:
        """返回 Report/Analysis Input 可冻结的共享路由策略版本。"""

        return "local-document-preparation-router-v1"

    @property
    def execution_profile_fingerprint(self) -> str:
        """返回不含路径、URL 原文和凭据的确定性能力摘要。

        该摘要描述受理后可能采用的 Processor 路由及其安全参数；具体文件实际命中的
        ProcessingProfile 仍由 DocumentProcessing 记录保存，两者共同用于恢复核验。
        """

        payload = {
            "profileId": self.execution_profile_id,
            "legacyPolicyFingerprint": self._legacy_policy_fingerprint,
            "ocrLanguages": self._ocr_languages,
            "ocrDpi": self._ocr_dpi,
            "ocrEnabled": self._ocr_enabled,
            "ocrSamplePages": self._ocr_sample_pages,
            "ocrTextThreshold": self._ocr_text_threshold,
            "mineruLanguage": self._mineru_lang,
            "mineruEndpointFingerprint": mineru_endpoint_fingerprint(
                self._mineru_api_url
            ),
            "maxTextBytes": self._max_text_bytes,
        }
        material = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    @property
    def artifact_store(self) -> LocalArtifactStoreAdapter:
        """仅供同层业务 Adapter 读取已验证 Artifact。"""

        return self._store

    def prepare(
        self,
        request: LocalDocumentPreparationRequest,
    ) -> LocalPreparedArtifact:
        if not isinstance(request, LocalDocumentPreparationRequest):
            raise TypeError("request 必须是 LocalDocumentPreparationRequest")
        suffix = request.source_path.suffix.casefold()
        snapshot_path, source_digest, header = self._snapshot_source(
            request.source_path
        )
        try:
            detected_mhtml = is_mhtml_content(
                file_name=request.source_path.name,
                header=header,
            )
            if detected_mhtml and suffix not in _MHTML_SUFFIXES:
                logger.warning(
                    "统一文档准备检测到扩展名与内容不一致的 MHTML: "
                    "task_id=%s suffix=%s",
                    request.task_id,
                    suffix or "none",
                )
            source = self._publish_source(
                request,
                snapshot_path=snapshot_path,
                source_digest=source_digest,
                suffix=suffix,
                detected_mhtml=detected_mhtml,
            )
        finally:
            try:
                snapshot_path.unlink(missing_ok=True)
            except OSError:
                logger.warning(
                    "源文件单次读取快照清理失败: file_name=%s",
                    snapshot_path.name,
                    exc_info=True,
                )
        source_storage_path = self._store.resolve_path(source)
        logger.info(
            "开始统一文档准备: task_id=%s logical_step=%s suffix=%s "
            "source_artifact_id=%s",
            request.task_id,
            request.logical_step,
            suffix or "none",
            source.artifact_id[:12],
        )

        if detected_mhtml:
            result = self._prepare_mhtml(request, source)
            if (
                result.representation is DocumentRepresentation.PDF
            ):  # 浏览器主流程成功后再形成统一 Markdown。
                result = self._prepare_mineru(
                    request,
                    source=result,
                    source_suffix=".pdf",
                    use_ocr=False,
                    step_suffix="mhtml-pdf-to-markdown",
                )
        elif suffix in _TEXT_SUFFIXES:
            representation, media_type = _TEXT_SUFFIXES[suffix]
            result = self._execute(
                self._passthrough,
                request=request,
                source=source,
                step_id=f"{request.logical_step}:passthrough",
                profile=build_passthrough_profile(
                    source_suffix=suffix,
                    target_representation=representation,
                    media_type=media_type,
                    max_size_bytes=self._max_text_bytes,
                ),
            )
        elif suffix in _LEGACY_SUFFIXES:
            version = self._legacy_office_preparer.preflight()
            normalized = self._execute(
                self._legacy,
                request=request,
                source=source,
                step_id=f"{request.logical_step}:legacy-office",
                profile=create_legacy_office_profile(
                    source_suffix=suffix,
                    libreoffice_version=version,
                    policy_fingerprint=self._legacy_policy_fingerprint,
                ),
            )
            result = self._prepare_mineru(
                request,
                source=normalized,
                source_suffix=target_suffix_for(suffix),
                use_ocr=False,
                step_suffix="legacy-ooxml-to-markdown",
            )
        elif suffix == ".pdf":
            scanned = self._ocr_enabled and is_scanned_pdf(
                str(source_storage_path),
                sample_pages=self._ocr_sample_pages,
                text_threshold=self._ocr_text_threshold,
            )
            if (
                scanned
                and request.scanned_pdf_engine
                is ScannedPDFEngine.BUILTIN_OCR
            ):
                result = self._execute_or_source_pdf_fallback(
                    self._ocr,
                    request=request,
                    source=source,
                    step_id=f"{request.logical_step}:builtin-ocr",
                    profile=build_builtin_ocr_profile(
                        languages=self._ocr_languages,
                        dpi=self._ocr_dpi,
                    ),
                    failure_stage="builtin-ocr",
                )
            elif scanned:
                # Analysis 的既有业务顺序是 MinerU OCR -> 内置 OCR -> 原 PDF。
                # 只有明确失败才允许进入下一层；任何 outcome_unknown 都必须冻结
                # 当前步骤并等待对账，禁止以另一条真实处理链制造重复副作用。
                try:
                    result = self._prepare_mineru(
                        request,
                        source=source,
                        source_suffix=suffix,
                        use_ocr=True,
                        step_suffix="mineru",
                    )
                except LocalDocumentPreparationError as exc:
                    if exc.outcome is not ProcessingOutcome.FAILED:
                        raise
                    logger.warning(
                        "扫描 PDF 的 MinerU 明确失败，尝试内置 OCR: "
                        "task_id=%s error_code=%s",
                        request.task_id,
                        exc.error_code,
                    )
                    result = self._execute_or_source_pdf_fallback(
                        self._ocr,
                        request=request,
                        source=source,
                        step_id=f"{request.logical_step}:builtin-ocr-fallback",
                        profile=build_builtin_ocr_profile(
                            languages=self._ocr_languages,
                            dpi=self._ocr_dpi,
                        ),
                        failure_stage="builtin-ocr-fallback",
                    )
            else:
                result = self._prepare_mineru(
                    request,
                    source=source,
                    source_suffix=suffix,
                    use_ocr=False,
                    step_suffix="mineru",
                )
        elif suffix in _MINERU_SUFFIXES:
            result = self._prepare_mineru(
                request,
                source=source,
                source_suffix=suffix,
                use_ocr=False,
                step_suffix="mineru",
            )
        else:
            raise LocalDocumentPreparationError(
                "document_format_unsupported"
            )

        prepared_artifact = (
            result
            if result.representation in {
                DocumentRepresentation.MARKDOWN,
                DocumentRepresentation.TEXT,
            }
            else None
        )
        prepared_path = self._store.resolve_path(result)
        logger.info(
            "统一文档准备完成: task_id=%s logical_step=%s "
            "rag_artifact_id=%s representation=%s text_available=%s bytes=%d",
            request.task_id,
            request.logical_step,
            result.artifact_id[:12],
            result.representation.value,
            prepared_artifact is not None,
            result.metadata.size_bytes,
        )
        return LocalPreparedArtifact(
            source_artifact=source,
            rag_artifact=result,
            prepared_artifact=prepared_artifact,
            prepared_path=prepared_path,
        )

    def _execute_or_source_pdf_fallback(
        self,
        application: PrepareDocument,
        *,
        request: LocalDocumentPreparationRequest,
        source: ArtifactRef,
        step_id: str,
        profile,
        failure_stage: str,
    ) -> ArtifactRef:
        """执行文本提取；明确失败时仅把 SOURCE PDF 交给 RAG。

        ``outcome_unknown`` 可能意味着处理器仍在运行或外部提交已经被受理，因此不能
        降级。只有 Processing Record 已提交确定 ``failed`` 时才允许返回原 PDF。
        """

        try:
            return self._execute(
                application,
                request=request,
                source=source,
                step_id=step_id,
                profile=profile,
            )
        except LocalDocumentPreparationError as exc:
            if exc.outcome is not ProcessingOutcome.FAILED:
                raise
            logger.warning(
                "扫描 PDF 文本提取明确失败，原 PDF 仅作为 RAG 输入: "
                "task_id=%s stage=%s error_code=%s",
                request.task_id,
                failure_stage,
                exc.error_code,
            )
            return source

    def _prepare_mhtml(
        self,
        request: LocalDocumentPreparationRequest,
        source: ArtifactRef,
    ) -> ArtifactRef:
        fallback_request = self._processing_request(
            request,
            source=source,
            step_id=f"{request.logical_step}:mhtml-text-fallback",
            profile=create_mhtml_text_profile(),
        )
        try:
            converter = MHTMLToPDFConverter()
        except MHTMLBrowserConversionError as exc:
            # 浏览器在任何外部进程启动前即确认不可用，可以安全进入纯文本降级。
            logger.warning(
                "MHTML 浏览器不可用，执行确定性 Markdown 降级: "
                "task_id=%s error_code=%s",
                request.task_id,
                exc.code,
            )
            return self._require_success(
                self._mhtml_text.execute(fallback_request)
            )

        browser_path = Path(converter.chrome_path)
        stat = browser_path.stat()
        fingerprint = hashlib.sha256(
            (
                f"browser-file-v1\0{browser_path.name.casefold()}\0"
                f"{stat.st_size}\0{stat.st_mtime_ns}"
            ).encode("utf-8")
        ).hexdigest()
        browser = self._application(
            MHTMLBrowserPDFProcessorAdapter(
                source_store=self._store,
                converter=converter,
                scratch_root=self._root / "mhtml",
            )
        )
        orchestration = PrepareMHTMLDocument(
            browser=browser,
            fallback=self._mhtml_text,
        )
        result = orchestration.execute(
            PrepareMHTMLRequest(
                browser_request=self._processing_request(
                    request,
                    source=source,
                    step_id=f"{request.logical_step}:mhtml-browser",
                    profile=create_mhtml_browser_profile(
                        browser_fingerprint=fingerprint
                    ),
                ),
                fallback_request=fallback_request,
            )
        )
        return self._require_success(result)

    def _prepare_mineru(
        self,
        request: LocalDocumentPreparationRequest,
        *,
        source: ArtifactRef,
        source_suffix: str,
        use_ocr: bool,
        step_suffix: str,
    ) -> ArtifactRef:
        return self._execute(
            self._mineru,
            request=request,
            source=source,
            step_id=f"{request.logical_step}:{step_suffix}",
            profile=build_mineru_profile(
                source_suffix=source_suffix,
                use_ocr=use_ocr,
                lang=self._mineru_lang,
                api_mode="remote" if self._mineru_api_url else "local",
                endpoint_fingerprint=mineru_endpoint_fingerprint(
                    self._mineru_api_url
                ),
            ),
        )

    def _execute(
        self,
        application: PrepareDocument,
        *,
        request: LocalDocumentPreparationRequest,
        source: ArtifactRef,
        step_id: str,
        profile,
    ) -> ArtifactRef:
        return self._require_success(
            application.execute(
                self._processing_request(
                    request,
                    source=source,
                    step_id=step_id,
                    profile=profile,
                )
            )
        )

    def _processing_request(
        self,
        request: LocalDocumentPreparationRequest,
        *,
        source: ArtifactRef,
        step_id: str,
        profile,
    ) -> DocumentProcessingRequest:
        return DocumentProcessingRequest(
            task_id=request.task_id,
            step_id=step_id,
            source_artifact=source,
            profile=profile,
            trace_id=request.trace_id,
        )

    def _publish_source(
        self,
        request: LocalDocumentPreparationRequest,
        *,
        snapshot_path: Path,
        source_digest: str,
        suffix: str,
        detected_mhtml: bool,
    ) -> ArtifactRef:
        step_key = hashlib.sha256(
            (
                f"document-source-v1\0{request.task_id.value}\0"
                f"{request.logical_step}\0{suffix}\0{source_digest}"
            ).encode("utf-8")
        ).hexdigest()
        representation = (
            DocumentRepresentation.ORIGINAL
            if detected_mhtml
            else self._source_representation(suffix)
        )
        media_type = (
            "multipart/related"
            if detected_mhtml
            else _TEXT_SUFFIXES[suffix][1]
            if suffix in _TEXT_SUFFIXES
            else mimetypes.guess_type(request.source_path.name)[0]
            or "application/octet-stream"
        )
        artifact = self._store.publish(
            ArtifactPublication(
                task_id=request.task_id,
                step_key=step_key,
                kind=ArtifactKind.SOURCE,
                representation=representation,
                media_type=media_type,
            ),
            FileArtifactContent(snapshot_path),
        )
        try:
            self._artifact_catalog.register_artifact(artifact)
        except Exception as exc:
            logger.exception(
                "Source Artifact 已发布但所有权登记失败: task_id=%s "
                "artifact_id=%s",
                request.task_id,
                artifact.artifact_id[:12],
            )
            raise LocalDocumentPreparationError(
                "source_artifact_catalog_outcome_unknown",
                outcome=ProcessingOutcome.OUTCOME_UNKNOWN,
            ) from exc
        return artifact

    def _application(self, processor) -> PrepareDocument:
        return PrepareDocument(
            processor=processor,
            artifact_store=self._store,
            records=self._records,
        )

    @staticmethod
    def _require_success(result) -> ArtifactRef:
        if (
            result.outcome is ProcessingOutcome.SUCCEEDED
            and result.artifact is not None
        ):
            return result.artifact
        raise LocalDocumentPreparationError(
            result.error_code or "document_preparation_failed",
            outcome=result.outcome,
        )

    @staticmethod
    def _source_representation(suffix: str) -> DocumentRepresentation:
        if suffix in _TEXT_SUFFIXES:
            return _TEXT_SUFFIXES[suffix][0]
        if suffix == ".pdf":
            return DocumentRepresentation.PDF
        if suffix in {".docx", ".pptx", ".xlsx"}:
            return DocumentRepresentation.OOXML
        return DocumentRepresentation.ORIGINAL

    def _snapshot_source(self, path: Path) -> tuple[Path, str, bytes]:
        """单次读取宿主路径，生成与 header/hash/发布内容完全一致的受控快照。"""

        snapshot_root = (self._root / "source-snapshots").resolve()
        snapshot_root.mkdir(parents=True, exist_ok=True)
        snapshot_path = snapshot_root / f"{uuid4().hex}.snapshot"
        digest = hashlib.sha256()
        header = bytearray()
        try:
            with path.open("rb") as reader, snapshot_path.open("xb") as writer:
                while chunk := reader.read(1024 * 1024):
                    if len(header) < 1024:
                        header.extend(chunk[: 1024 - len(header)])
                    digest.update(chunk)
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
        except Exception:
            snapshot_path.unlink(missing_ok=True)
            raise
        return snapshot_path, digest.hexdigest(), bytes(header)

    @staticmethod
    def _required(value: object, name: str) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError(f"{name} 不能为空")
        return normalized


__all__ = [
    "LocalDocumentPreparationAdapter",
    "LocalDocumentPreparationError",
    "LocalDocumentPreparationRequest",
    "LocalPreparedArtifact",
    "ScannedPDFEngine",
]
