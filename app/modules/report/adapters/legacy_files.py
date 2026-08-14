"""把下载、共享文档准备和 Word 提取能力适配为报告文件端口。"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit

from app.modules.document_processing import (
    DocumentRepresentation,
)
from app.modules.document_processing.adapters import (
    LocalDocumentPreparationAdapter,
    LocalDocumentPreparationRequest,
    ScannedPDFEngine,
)
from app.modules.report.domain.errors import (
    ReportArtifactError,
    ReportInputError,
    ReportSourceNormalizationError,
    ReportTemplateError,
)
from app.modules.report.ports import (
    ReportArtifactCategory,
    ReportArtifactRef,
    ReportSourceDownload,
    ReportTemplateDownload,
)
from app.infrastructure.http.source_download import download_source_to_temp_file

from .local_artifacts import LocalReportArtifactAdapter
from .docx_template import extract_docx_template_text


logger = logging.getLogger(__name__)

_SAFE_SUFFIX_PATTERN = re.compile(r"^\.[A-Za-z0-9]{1,12}$")

Downloader = Callable[[str, str, str, float, int], str]
WordExtractor = Callable[[str], str]


class LegacyReportFileAdapter:
    """复用既有下载与模板提取能力，并统一接入共享文档处理链。

    下载器仍以真实路径工作，但返回路径只能位于当前任务的私有 staging 目录。源文件随后
    交给共享 ``document_preparer``，处理结果再映射到当前 task 的明确 Artifact 类别，
    Report 不再维护独立的规范化、OCR 或 MinerU 分支。下载仍保持既有超时和大小限制，
    本适配器不新增 URL host 策略。
    """

    def __init__(
        self,
        artifacts: LocalReportArtifactAdapter,
        *,
        document_preparer: LocalDocumentPreparationAdapter,
        download_timeout: float = 60.0,
        max_download_bytes: int = 512 * 1024 * 1024,
        downloader: Downloader = download_source_to_temp_file,
        word_extractor: WordExtractor = extract_docx_template_text,
    ) -> None:
        if not isinstance(artifacts, LocalReportArtifactAdapter):
            raise TypeError("artifacts 必须是 LocalReportArtifactAdapter")
        if (
            isinstance(download_timeout, bool)
            or not isinstance(download_timeout, (int, float))
            or float(download_timeout) <= 0
        ):
            raise ValueError("download_timeout 必须是正数")
        if (
            isinstance(max_download_bytes, bool)
            or not isinstance(max_download_bytes, int)
            or max_download_bytes < 1
        ):
            raise ValueError("max_download_bytes 必须是正整数")
        for name, dependency in (
            ("downloader", downloader),
            ("word_extractor", word_extractor),
        ):
            if not callable(dependency):
                raise TypeError(f"{name} 必须可调用")
        if not callable(
            getattr(document_preparer, "prepare", None)
        ):
            raise TypeError("document_preparer 必须实现 prepare")
        self._artifacts = artifacts
        self._download_timeout = float(download_timeout)
        self._max_download_bytes = max_download_bytes
        self._downloader = downloader
        self._word_extractor = word_extractor
        self._document_preparer = document_preparer

    def download_source(self, command: ReportSourceDownload) -> ReportArtifactRef:
        """在 Worker 执行时下载一个源文件，并保留请求顺序。"""

        if not isinstance(command, ReportSourceDownload):
            raise TypeError("command 必须是 ReportSourceDownload")
        suffix = self._suffix_from_url(command.source_url)
        file_name = f"{command.sequence_no:04d}{suffix}"
        logger.info(
            "开始下载报告源文件: task_id=%s sequence_no=%d suffix=%s",
            command.scope.task_id,
            command.sequence_no,
            suffix or "none",
        )
        try:
            artifact = self._download_and_publish(
                url=command.source_url,
                scope=command.scope,
                category=ReportArtifactCategory.SOURCE,
                file_name=file_name,
                suffix=suffix,
                sequence_no=command.sequence_no,
            )
        except Exception as exc:
            if isinstance(exc, ReportInputError):
                raise
            logger.exception(
                "报告源文件下载失败: task_id=%s sequence_no=%d error_type=%s",
                command.scope.task_id,
                command.sequence_no,
                type(exc).__name__,
            )
            raise ReportInputError("报告源文件下载失败") from exc
        logger.info(
            "报告源文件下载完成: task_id=%s sequence_no=%d bytes=%d",
            command.scope.task_id,
            command.sequence_no,
            artifact.size_bytes or 0,
        )
        return artifact

    def normalize_source(self, source: ReportArtifactRef) -> ReportArtifactRef:
        """规范化 MHTML 等输入；非 MHTML 也复制到独立 normalized 类别。"""

        self._require_source_artifact(
            source,
            allowed=(ReportArtifactCategory.SOURCE,),
        )
        scope = self._scope_for(source)
        source_path = self._artifacts.resolve_path(source)
        return self._prepare_with_document_processing(
            source,
            source_path=source_path,
            scope=scope,
        )

    def _prepare_with_document_processing(
        self,
        source: ReportArtifactRef,
        *,
        source_path: Path,
        scope,
    ) -> ReportArtifactRef:
        """生产路径只调用一次共享流水线，并映射回 ReportArtifactRef。"""

        preparer = self._document_preparer
        try:
            prepared = preparer.prepare(
                LocalDocumentPreparationRequest(
                    task_id=source.task_id,
                    source_path=source_path,
                    logical_step=f"report-source-{source.sequence_no}",
                    trace_id=(
                        f"report:{source.task_id.value}:{source.sequence_no}"
                    ),
                    # 报告旧语义对扫描 PDF 使用内置 OCR；该选择现已冻结到共享
                    # Processor Profile，不再调用 services/utils 的路径式工具。
                    scanned_pdf_engine=ScannedPDFEngine.BUILTIN_OCR,
                )
            )
            artifact = self._artifacts.publish_document_artifact(
                scope,
                category=ReportArtifactCategory.NORMALIZED_SOURCE,
                artifact=prepared.rag_artifact,
                document_store=preparer.artifact_store,
                file_name=(
                    f"{source.sequence_no:04d}-normalized"
                    f"{self._document_artifact_suffix(prepared.rag_artifact)}"
                ),
                sequence_no=source.sequence_no,
            )
        except Exception as exc:
            logger.warning(
                "报告共享文档准备失败: task_id=%s sequence_no=%s "
                "error_type=%s",
                source.task_id,
                source.sequence_no,
                type(exc).__name__,
                exc_info=True,
            )
            raise ReportSourceNormalizationError("报告源文件规范化失败") from exc
        logger.info(
            "报告共享文档准备完成: task_id=%s sequence_no=%s bytes=%d",
            source.task_id,
            source.sequence_no,
            artifact.size_bytes or 0,
        )
        return artifact

    def prepare_upload_files(
        self,
        source: ReportArtifactRef,
    ) -> tuple[ReportArtifactRef, ...]:
        """把共享 DocumentProcessing 已准备的唯一结果映射为 RAG_INPUT。"""

        self._require_source_artifact(
            source,
            allowed=(
                ReportArtifactCategory.SOURCE,
                ReportArtifactCategory.NORMALIZED_SOURCE,
            ),
        )
        source_path = self._artifacts.resolve_path(source)
        scope = self._scope_for(source)
        # normalize_source 已形成最终 prepared Artifact。这里只映射到报告既有的
        # RAG_INPUT 生命周期类别，禁止再次运行 OCR/MinerU 形成第二条真实转换链。
        try:
            artifact = self._artifacts.publish_file(
                scope,
                category=ReportArtifactCategory.RAG_INPUT,
                source_path=source_path,
                file_name=(
                    f"{source.sequence_no:04d}-001"
                    f"{self._safe_suffix(source_path.suffix)}"
                ),
                sequence_no=source.sequence_no,
            )
        except Exception as exc:
            logger.exception(
                "报告 prepared Artifact 映射 RAG 输入失败: "
                "task_id=%s sequence_no=%s error_type=%s",
                source.task_id,
                source.sequence_no,
                type(exc).__name__,
            )
            raise ReportInputError("报告文件无法准备为 RAG 输入") from exc
        return (artifact,)

    @staticmethod
    def _document_artifact_suffix(artifact) -> str:
        """把共享 Artifact 表示映射为 Report 私有文件名，不暴露宿主路径。"""

        mapping = {
            DocumentRepresentation.MARKDOWN: ".md",
            DocumentRepresentation.TEXT: ".txt",
            DocumentRepresentation.PDF: ".pdf",
        }
        try:
            return mapping[artifact.representation]
        except (AttributeError, KeyError) as exc:
            raise ReportInputError("报告共享文档产物格式不受支持") from exc

    def download_template(
        self,
        command: ReportTemplateDownload,
    ) -> ReportArtifactRef:
        """下载报告 Word 模板，并以 template 类别原子发布。"""

        if not isinstance(command, ReportTemplateDownload):
            raise TypeError("command 必须是 ReportTemplateDownload")
        suffix = self._suffix_from_url(command.template_url, fallback=".docx")
        if suffix == ".doc":
            logger.warning(
                "报告 legacy Word 模板不在支持范围: task_id=%s",
                command.scope.task_id,
            )
            raise ReportTemplateError("报告模板仅支持 .docx 格式")
        try:
            artifact = self._download_and_publish(
                url=command.template_url,
                scope=command.scope,
                category=ReportArtifactCategory.TEMPLATE,
                file_name=f"template{suffix}",
                suffix=suffix,
                sequence_no=None,
            )
        except Exception as exc:
            if isinstance(exc, ReportTemplateError):
                raise
            logger.exception(
                "报告模板下载失败: task_id=%s error_type=%s",
                command.scope.task_id,
                type(exc).__name__,
            )
            raise ReportTemplateError("报告模板下载失败") from exc
        logger.info(
            "报告模板下载完成: task_id=%s bytes=%d",
            command.scope.task_id,
            artifact.size_bytes or 0,
        )
        return artifact

    def extract_template_text(self, template: ReportArtifactRef) -> str:
        """从已隔离的 Word Artifact 提取模板文字。"""

        if (
            not isinstance(template, ReportArtifactRef)
            or template.category is not ReportArtifactCategory.TEMPLATE
        ):
            raise ReportTemplateError("模板 Artifact 类别无效")
        try:
            template_path = self._artifacts.resolve_path(template)
            text = self._word_extractor(str(template_path))
            if not isinstance(text, str):
                raise TypeError("Word 提取器必须返回 str")
        except Exception as exc:
            logger.exception(
                "报告 Word 模板提取失败: task_id=%s error_type=%s",
                template.task_id,
                type(exc).__name__,
            )
            raise ReportTemplateError("报告 Word 模板提取失败") from exc
        logger.info(
            "报告 Word 模板提取完成: task_id=%s text_chars=%d",
            template.task_id,
            len(text),
        )
        return text

    def _download_and_publish(
        self,
        *,
        url: str,
        scope,
        category: ReportArtifactCategory,
        file_name: str,
        suffix: str,
        sequence_no: int | None,
    ) -> ReportArtifactRef:
        staging = self._artifacts.staging_path(
            scope,
            category=category,
            suffix=suffix,
        )
        try:
            downloaded_value = self._downloader(
                url,
                staging.name,
                str(staging.parent),
                self._download_timeout,
                self._max_download_bytes,
            )
            if not isinstance(downloaded_value, str) or not downloaded_value.strip():
                raise ValueError("下载器未返回有效路径")
            downloaded_path = Path(downloaded_value).resolve()
            # legacy 下载器接受 file_name/temp_root。仍需验证返回值没有越出当前任务目录，
            # 防止测试替身或未来实现无意把其他任务文件发布到本 execution。
            expected_root = staging.parent.resolve()
            try:
                downloaded_path.relative_to(expected_root)
            except ValueError as exc:
                raise ReportArtifactError("下载器返回路径越出任务目录") from exc
            artifact = self._artifacts.publish_file(
                scope,
                category=category,
                source_path=downloaded_path,
                file_name=file_name,
                sequence_no=sequence_no,
            )
            return artifact
        finally:
            self._artifacts.remove_private_file(scope, staging)

    def _scope_for(self, artifact: ReportArtifactRef):
        return self._artifacts.begin(artifact.task_id)

    @staticmethod
    def _require_source_artifact(
        artifact: ReportArtifactRef,
        *,
        allowed: tuple[ReportArtifactCategory, ...],
    ) -> None:
        if not isinstance(artifact, ReportArtifactRef):
            raise TypeError("source 必须是 ReportArtifactRef")
        if artifact.category not in allowed:
            raise ReportInputError("源 Artifact 类别无效")
        if artifact.sequence_no is None:
            raise ReportInputError("源 Artifact 缺少 sequence_no")

    @classmethod
    def _suffix_from_url(cls, url: str, fallback: str = "") -> str:
        try:
            suffix = Path(unquote(urlsplit(url).path)).suffix
        except (TypeError, ValueError):
            suffix = ""
        return cls._safe_suffix(suffix) or fallback

    @staticmethod
    def _safe_suffix(value: str) -> str:
        normalized = str(value or "")
        return normalized.lower() if _SAFE_SUFFIX_PATTERN.fullmatch(normalized) else ""


__all__ = ["LegacyReportFileAdapter"]
