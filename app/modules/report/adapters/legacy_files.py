"""把现有下载、规范化、OCR 准备和 Word 提取能力适配为报告文件端口。"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import unquote, urlsplit

from app.modules.document_processing import (
    LegacyOfficePreparer,
    is_legacy_office_path,
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
from app.services.utils.file_downloader import download_to_temp_file
from app.services.utils.mhtml_normalizer import normalize_file_for_llm
from app.services.utils.rag_pipeline import prepare_upload_files
from app.services.utils.word_extractor import extract_text_from_word

from .local_artifacts import LocalReportArtifactAdapter


logger = logging.getLogger(__name__)

_SAFE_SUFFIX_PATTERN = re.compile(r"^\.[A-Za-z0-9]{1,12}$")

Downloader = Callable[[str, str, str, float, int], str]
Normalizer = Callable[[str], str]
UploadPreparer = Callable[[str], Sequence[str]]
WordExtractor = Callable[[str], str]


class LegacyReportFileAdapter:
    """复用遗留文件能力，同时补齐任务隔离、原子发布和稳定错误分类。

    legacy 工具仍以真实路径工作；路径只在本适配器内部出现。每个工具的输出都会重新复制
    到当前 task 的明确 Artifact 类别中，避免 normalizer/OCR 返回任意宿主路径后被直接交
    给 RAG。下载仍保持当前 60 秒超时和受控离线 URL 口径，本阶段不新增 URL host 策略。
    """

    def __init__(
        self,
        artifacts: LocalReportArtifactAdapter,
        *,
        download_timeout: float = 60.0,
        max_download_bytes: int = 512 * 1024 * 1024,
        downloader: Downloader = download_to_temp_file,
        normalizer: Normalizer = normalize_file_for_llm,
        upload_preparer: UploadPreparer = prepare_upload_files,
        word_extractor: WordExtractor = extract_text_from_word,
        legacy_office_preparer: LegacyOfficePreparer | None = None,
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
            ("normalizer", normalizer),
            ("upload_preparer", upload_preparer),
            ("word_extractor", word_extractor),
        ):
            if not callable(dependency):
                raise TypeError(f"{name} 必须可调用")
        if legacy_office_preparer is not None and not callable(
            getattr(legacy_office_preparer, "prepare", None)
        ):
            raise TypeError("legacy_office_preparer 必须实现 prepare")
        self._artifacts = artifacts
        self._download_timeout = float(download_timeout)
        self._max_download_bytes = max_download_bytes
        self._downloader = downloader
        self._normalizer = normalizer
        self._upload_preparer = upload_preparer
        self._word_extractor = word_extractor
        self._legacy_office_preparer = legacy_office_preparer

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
        if is_legacy_office_path(source_path):
            return self._convert_legacy_source(source, source_path, scope)
        try:
            normalized_value = self._normalizer(str(source_path))
            if not isinstance(normalized_value, str) or not normalized_value.strip():
                raise ValueError("规范化工具未返回有效路径")
            normalized_path = Path(normalized_value)
            if not normalized_path.is_file():
                raise FileNotFoundError("规范化结果文件不存在")
            suffix = self._safe_suffix(normalized_path.suffix)
            artifact = self._artifacts.publish_file(
                scope,
                category=ReportArtifactCategory.NORMALIZED_SOURCE,
                source_path=normalized_path,
                file_name=f"{source.sequence_no:04d}-normalized{suffix}",
                sequence_no=source.sequence_no,
            )
        except Exception as exc:
            logger.warning(
                "报告源文件规范化失败: task_id=%s sequence_no=%s error_type=%s",
                source.task_id,
                source.sequence_no,
                type(exc).__name__,
                exc_info=True,
            )
            raise ReportSourceNormalizationError("报告源文件规范化失败") from exc
        logger.info(
            "报告源文件规范化完成: task_id=%s sequence_no=%s bytes=%d",
            source.task_id,
            source.sequence_no,
            artifact.size_bytes or 0,
        )
        return artifact

    def _convert_legacy_source(
        self,
        source: ReportArtifactRef,
        source_path: Path,
        scope,
    ) -> ReportArtifactRef:
        """把 legacy Office 源转为任务内 OOXML Artifact；失败禁止 raw fallback。"""

        preparer = self._legacy_office_preparer
        if preparer is None:
            logger.error(
                "报告 legacy Office 转换能力未配置: task_id=%s sequence_no=%s",
                source.task_id,
                source.sequence_no,
            )
            raise ReportInputError("报告源文件本地转换失败")

        try:
            job_id = f"report-{source.task_id.value}-{source.sequence_no}"
            with preparer.prepare(source_path, job_id=job_id) as result:
                prepared_path = Path(result.prepared_path)
                if not result.converted or not prepared_path.is_file():
                    raise ValueError("legacy Office 转换未返回有效 OOXML 文件")
                suffix = self._safe_suffix(result.target_suffix)
                if not suffix:
                    raise ValueError("legacy Office 转换未返回有效目标扩展名")
                artifact = self._artifacts.publish_file(
                    scope,
                    category=ReportArtifactCategory.NORMALIZED_SOURCE,
                    source_path=prepared_path,
                    file_name=(
                        f"{source.sequence_no:04d}-normalized{suffix}"
                    ),
                    sequence_no=source.sequence_no,
                )
        except Exception as exc:
            if isinstance(exc, ReportInputError):
                raise
            logger.warning(
                "报告 legacy Office 本地转换失败: "
                "task_id=%s sequence_no=%s error_type=%s",
                source.task_id,
                source.sequence_no,
                type(exc).__name__,
            )
            # 底层异常可能包含 LibreOffice stdout、profile 或宿主绝对路径。核心转换层已
            # 负责截断和脱敏诊断；报告应用层会用 logger.exception 记录这里的异常，因此
            # 必须切断异常链，避免敏感细节被二次展开。
            raise ReportInputError("报告源文件本地转换失败") from None

        logger.info(
            "报告 legacy Office 本地转换完成: "
            "task_id=%s sequence_no=%s target_suffix=%s bytes=%d",
            source.task_id,
            source.sequence_no,
            suffix,
            artifact.size_bytes or 0,
        )
        return artifact

    def prepare_upload_files(
        self,
        source: ReportArtifactRef,
    ) -> tuple[ReportArtifactRef, ...]:
        """执行现有 OCR 准备逻辑，并把全部结果按确定顺序发布为 RAG_INPUT。"""

        self._require_source_artifact(
            source,
            allowed=(
                ReportArtifactCategory.SOURCE,
                ReportArtifactCategory.NORMALIZED_SOURCE,
            ),
        )
        source_path = self._artifacts.resolve_path(source)
        scope = self._scope_for(source)
        try:
            prepared_values = self._upload_preparer(str(source_path))
            if isinstance(prepared_values, (str, bytes, bytearray)):
                raise TypeError("上传准备工具必须返回路径序列")
            prepared_paths = tuple(Path(value) for value in prepared_values)
            if not prepared_paths:
                raise ValueError("上传准备工具未返回文件")
            if any(not path.is_file() for path in prepared_paths):
                raise FileNotFoundError("上传准备结果包含不存在的文件")

            artifacts: list[ReportArtifactRef] = []
            for item_index, path in enumerate(prepared_paths, start=1):
                suffix = self._safe_suffix(path.suffix)
                artifacts.append(
                    self._artifacts.publish_file(
                        scope,
                        category=ReportArtifactCategory.RAG_INPUT,
                        source_path=path,
                        file_name=(
                            f"{source.sequence_no:04d}-{item_index:03d}{suffix}"
                        ),
                        sequence_no=source.sequence_no,
                    )
                )
        except Exception as exc:
            logger.exception(
                "报告 RAG 上传文件准备失败: task_id=%s sequence_no=%s error_type=%s",
                source.task_id,
                source.sequence_no,
                type(exc).__name__,
            )
            raise ReportInputError("报告文件无法准备为 RAG 输入") from exc
        logger.info(
            "报告 RAG 上传文件准备完成: task_id=%s sequence_no=%s output_count=%d",
            source.task_id,
            source.sequence_no,
            len(artifacts),
        )
        return tuple(artifacts)

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
