"""文件分析任务目录与遗留文件处理能力的基础设施适配器。

本模块只封装既有下载、MHTML 规范化、OCR/MinerU 与正文读取工具。它不参与领域分类、
任务终态或回调；每次调用都必须接收由 ``AnalysisTaskWorkspacePort`` 创建的任务目录，
避免遗留工具把临时文件写入共享下载目录后被其他 execution 误用。
"""

from __future__ import annotations

from dataclasses import replace
import logging
import os
from pathlib import Path
import re
import shutil
from typing import Callable
from urllib.parse import unquote, urlsplit

import fitz

from app.modules.document_processing import (
    LegacyOfficeConversionError,
    LegacyOfficePreparer,
    is_legacy_office_path,
)
from app.modules.document_processing.adapters import (
    LocalDocumentPreparationAdapter,
    LocalDocumentPreparationRequest,
    ScannedPDFEngine,
)
from app.modules.document_processing.application import ProjectDocumentForRag
from app.modules.document_processing.domain import (
    DocumentRepresentation,
    ProcessingOutcome,
)
from app.modules.analysis.ports.files import (
    AnalysisFilePreparationRequest,
    AnalysisTaskWorkspace,
    AnalysisTaskWorkspacePort,
    PreparedAnalysisDocument,
)
from app.services.core.config import OCRConfig, load_ocr_config
from app.services.utils.file_downloader import (
    DEFAULT_MAX_DOWNLOAD_BYTES,
    download_to_temp_file,
)
from app.modules.document_processing.adapters.path_compat import (
    extract_text_from_mhtml,
    is_mhtml_file,
    normalize_file_for_llm,
)
from app.modules.document_processing.adapters.builtin_ocr import (
    prepare_analysis_file_for_upload,
)
from app.services.utils.word_extractor import extract_text_from_word


logger = logging.getLogger(__name__)

_SAFE_SUFFIX_PATTERN = re.compile(r"^\.[A-Za-z0-9]{1,12}$")

Downloader = Callable[[str, str, str, float, int], str]
Normalizer = Callable[[str], str]
UploadPreparer = Callable[[str, OCRConfig], str]
TextReader = Callable[[str], str]


class AnalysisFilePreparationError(RuntimeError):
    """文件下载、转换或正文读取无法形成任务内产物。"""


class LocalAnalysisTaskWorkspaceAdapter(AnalysisTaskWorkspacePort):
    """在固定根目录下创建 ``<task_id>`` 专属目录。

    该 Adapter 不提供删除能力。任务目录的清理与保留需要等待 1F-6 的资源事实落库后再
    统一处理；现在即使某一步失败，也宁可保留受控现场，不能递归删除无法证明归属的路径。
    """

    def __init__(self, root_directory: str) -> None:
        if not isinstance(root_directory, str) or not root_directory.strip():
            raise ValueError("root_directory 必须是非空 str")
        # Windows 在目录从“不存在”变为“已创建”的并发窗口中，Path.resolve 可能对
        # 同一绝对路径交替返回 ``C:\...`` 与 ``\\?\C:\...``。两者指向同一位置，
        # 但 Path.relative_to 会把它们当成不同根。构造时和每次创建任务目录时都先
        # 统一等价的 Win32 前缀，避免合法任务被误判为路径逃逸。
        self._root_directory = self._canonical_resolved(Path(root_directory))

    def create(self, execution) -> AnalysisTaskWorkspace:  # type: ignore[no-untyped-def]
        """创建当前 execution 的唯一目录，并阻止 TaskId 逃逸根目录。"""

        task_id = str(getattr(execution, "task_id", "") or "").strip()
        if not task_id:
            raise TypeError("execution 必须携带有效 task_id")
        if (
            Path(task_id).name != task_id
            or task_id in {".", ".."}
            or any(character in task_id for character in ("/", "\\"))
        ):
            raise AnalysisFilePreparationError("task_id 不能用于任务目录")

        task_root = self._canonical_resolved(self._root_directory / task_id)
        try:
            task_root.relative_to(self._root_directory)
        except ValueError as exc:
            raise AnalysisFilePreparationError("任务目录越出受控根目录") from exc
        task_root.mkdir(parents=True, exist_ok=True)
        logger.info(
            "已确认文件分析任务目录: task_id=%s root=%s",
            task_id,
            task_root,
        )
        return AnalysisTaskWorkspace(execution=execution, root_path=str(task_root))

    @staticmethod
    def _canonical_resolved(path: Path) -> Path:
        r"""解析真实路径，并统一 Windows 扩展路径的等价表示。

        这里只移除 Win32 API 的等价 ``\\?\`` 前缀，不做字符串层面的路径包含
        判断。符号链接和 ``..`` 仍先由 Path.resolve 解析，随后继续使用
        ``relative_to`` 校验真实包含关系，不能借规范化放宽目录逃逸门禁。
        """

        resolved = path.resolve()
        if os.name != "nt":
            return resolved
        text = str(resolved)
        if text.startswith("\\\\?\\UNC\\"):
            return Path("\\\\" + text[8:])
        if text.startswith("\\\\?\\"):
            return Path(text[4:])
        return resolved


class LegacyAnalysisFilePreparationAdapter:
    """将遗留文件预处理限制在当前任务目录内。

    OCR 配置在本 Adapter 内复制，并把 OCR/MinerU 缓存根改写为任务目录下的明确子目录。
    这保留了原有算法和降级规则，同时避免共享缓存路径成为并发任务之间的隐式通信通道。
    """

    def __init__(
        self,
        *,
        download_timeout_seconds: float = 60.0,
        max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
        ocr_config_loader: Callable[[], OCRConfig] = load_ocr_config,
        downloader: Downloader = download_to_temp_file,
        normalizer: Normalizer = normalize_file_for_llm,
        upload_preparer: UploadPreparer = prepare_analysis_file_for_upload,
        text_reader: TextReader | None = None,
        legacy_office_preparer: LegacyOfficePreparer | None = None,
        document_preparer: LocalDocumentPreparationAdapter | None = None,
        rag_projector: ProjectDocumentForRag | None = None,
        document_scanned_pdf_engine: ScannedPDFEngine = (
            ScannedPDFEngine.MINERU
        ),
    ) -> None:
        if (
            isinstance(download_timeout_seconds, bool)
            or not isinstance(download_timeout_seconds, (int, float))
            or float(download_timeout_seconds) <= 0
        ):
            raise ValueError("download_timeout_seconds 必须是正数")
        if (
            isinstance(max_download_bytes, bool)
            or not isinstance(max_download_bytes, int)
            or max_download_bytes < 1
        ):
            raise ValueError("max_download_bytes 必须是正整数")
        for name, dependency in (
            ("ocr_config_loader", ocr_config_loader),
            ("downloader", downloader),
            ("normalizer", normalizer),
            ("upload_preparer", upload_preparer),
        ):
            if not callable(dependency):
                raise TypeError(f"{name} 必须可调用")
        if text_reader is not None and not callable(text_reader):
            raise TypeError("text_reader 必须可调用或 None")
        if legacy_office_preparer is not None and any(
            not callable(getattr(legacy_office_preparer, method_name, None))
            for method_name in ("preflight", "prepare")
        ):
            raise TypeError("legacy_office_preparer 必须实现 preflight/prepare")
        if document_preparer is not None and not callable(
            getattr(document_preparer, "prepare", None)
        ):
            raise TypeError("document_preparer 必须实现 prepare")
        if rag_projector is not None and not isinstance(
            rag_projector,
            ProjectDocumentForRag,
        ):
            raise TypeError("rag_projector 必须是 ProjectDocumentForRag 或 None")
        if rag_projector is not None and document_preparer is None:
            raise ValueError("rag_projector 必须与 document_preparer 一起注入")
        if not isinstance(document_scanned_pdf_engine, ScannedPDFEngine):
            raise TypeError(
                "document_scanned_pdf_engine 必须是 ScannedPDFEngine"
            )
        self._download_timeout_seconds = float(download_timeout_seconds)
        self._max_download_bytes = max_download_bytes
        self._ocr_config_loader = ocr_config_loader
        self._downloader = downloader
        self._normalizer = normalizer
        self._upload_preparer = upload_preparer
        self._text_reader = text_reader or self._read_original_text
        self._legacy_office_preparer = legacy_office_preparer
        self._document_preparer = document_preparer
        self._rag_projector = rag_projector
        self._document_scanned_pdf_engine = document_scanned_pdf_engine

    def prepare(
        self,
        request: AnalysisFilePreparationRequest,
    ) -> PreparedAnalysisDocument:
        """下载、规范化、OCR 并只返回当前任务根目录内的文件引用。"""

        if not isinstance(request, AnalysisFilePreparationRequest):
            raise TypeError("request 必须是 AnalysisFilePreparationRequest")
        task_root = self._task_root(request.task_root)
        download_dir = task_root / "download"
        normalized_dir = task_root / "normalized"
        download_dir.mkdir(parents=True, exist_ok=True)
        normalized_dir.mkdir(parents=True, exist_ok=True)

        url_suffix = self._suffix_from_url(request.source_url)
        business_suffix = self._safe_suffix(
            Path(request.execution.file_name).suffix
        )
        suffix = (
            business_suffix
            if business_suffix in {".doc", ".ppt", ".xls"}
            else url_suffix
        )
        source_name = f"source{suffix}"
        logger.info(
            "开始准备文件分析任务输入: task_id=%s file_name=%s suffix=%s",
            request.execution.task_id,
            request.execution.file_name,
            suffix or "none",
        )
        try:
            downloaded_path = self._download(
                source_url=request.source_url,
                file_name=source_name,
                download_dir=download_dir,
            )
            if self._document_preparer is not None:
                return self._prepare_shared_artifact(
                    request,
                    downloaded_path=downloaded_path,
                    normalized_dir=normalized_dir,
                )
            processing_path, internal_prepared_basename = (
                self._prepare_processing_path(
                    downloaded_path,
                    normalized_dir=normalized_dir,
                    request=request,
                )
            )
            upload_path = self._prepare_upload_path(
                processing_path,
                task_root=task_root,
            )
            original_text = self._text_reader(str(upload_path))
            if not isinstance(original_text, str):
                raise TypeError("正文读取器必须返回 str")
        except AnalysisFilePreparationError:
            raise
        except Exception as exc:
            logger.exception(
                "文件分析任务文件准备失败: task_id=%s file_name=%s error_type=%s",
                request.execution.task_id,
                request.execution.file_name,
                type(exc).__name__,
            )
            raise AnalysisFilePreparationError("文件分析文件准备失败") from exc

        logger.info(
            "文件分析任务文件准备完成: task_id=%s file_name=%s text_chars=%d",
            request.execution.task_id,
            request.execution.file_name,
            len(original_text),
        )
        return PreparedAnalysisDocument(
            execution=request.execution,
            source_path=str(downloaded_path),
            processing_path=str(processing_path),
            upload_path=str(upload_path),
            original_text=original_text,
            internal_prepared_basename=internal_prepared_basename,
        )

    def _prepare_shared_artifact(
        self,
        request: AnalysisFilePreparationRequest,
        *,
        downloaded_path: Path,
        normalized_dir: Path,
    ) -> PreparedAnalysisDocument:
        """分别形成 canonical 正文与最终 RAG 上传 Artifact。

        Markdown/Text 的 canonical Artifact 必须先经过 RAG-only 投影；PDF 两级文本提取
        明确失败时仍复用原 PDF。投影仅改变检索输入，不覆盖正文读取和全文翻译来源。
        """

        preparer = self._document_preparer
        assert preparer is not None
        policy = request.document_processing_policy
        if policy is None:  # pragma: no cover - DTO 已保证
            raise AnalysisFilePreparationError("文件处理策略缺失")
        legacy_source = is_legacy_office_path(downloaded_path)
        if legacy_source != policy.legacy_office_required:
            logger.error(
                "文件分析输入类型与冻结策略不一致: task_id=%s "
                "legacy_source=%s legacy_required=%s policy_fingerprint=%s",
                request.execution.task_id,
                legacy_source,
                policy.legacy_office_required,
                policy.processing_policy_fingerprint,
            )
            raise AnalysisFilePreparationError("文件类型与冻结处理策略不一致")
        try:
            prepared = preparer.prepare(
                LocalDocumentPreparationRequest(
                    task_id=request.execution.task_id,
                    source_path=downloaded_path,
                    logical_step="analysis-input",
                    trace_id=(
                        f"analysis:{request.execution.task_id.value}:prepare"
                    ),
                    scanned_pdf_engine=self._document_scanned_pdf_engine,
                )
            )
            canonical_rag_artifact = prepared.rag_artifact
            if canonical_rag_artifact.representation in {
                DocumentRepresentation.MARKDOWN,
                DocumentRepresentation.TEXT,
            }:
                if self._rag_projector is None:
                    raise AnalysisFilePreparationError("RAG Markdown 投影能力未配置")
                projected = self._rag_projector.execute(
                    canonical_rag_artifact,
                    trace_id=(
                        f"analysis:{request.execution.task_id.value}:rag-projection"
                    ),
                )
                if (
                    projected.outcome is not ProcessingOutcome.SUCCEEDED
                    or projected.artifact is None
                ):
                    logger.error(
                        "文件分析 RAG 投影失败: task_id=%s outcome=%s "
                        "error_code=%s source_artifact_id=%s",
                        request.execution.task_id,
                        projected.outcome.value,
                        projected.error_code or "-",
                        canonical_rag_artifact.artifact_id[:12],
                    )
                    raise AnalysisFilePreparationError("文件分析 RAG 投影失败")
                rag_artifact = projected.artifact
                projection_profile_id = self._rag_projector.profile_id
            else:
                rag_artifact = canonical_rag_artifact
                projection_profile_id = ""
            suffix = {
                DocumentRepresentation.MARKDOWN: ".md",
                DocumentRepresentation.PDF: ".pdf",
            }.get(rag_artifact.representation)
            if suffix is None:  # pragma: no cover - LocalPreparedArtifact 已强制集合
                raise AnalysisFilePreparationError("RAG Artifact 表示不受支持")
            target = normalized_dir / f"prepared{suffix}"
            temporary = target.with_suffix(f"{target.suffix}.part")
            try:
                with preparer.artifact_store.open_reader(
                    rag_artifact
                ) as reader, temporary.open("xb") as writer:
                    shutil.copyfileobj(reader, writer, length=1024 * 1024)
                    writer.flush()
                    os.fsync(writer.fileno())
                os.replace(temporary, target)
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    logger.warning(
                        "清理 Analysis Artifact 映射临时文件失败: "
                        "task_id=%s",
                        request.execution.task_id,
                        exc_info=True,
                    )
            upload_path = self._require_file_within(
                target,
                root=normalized_dir,
                label="prepared Artifact 映射",
            )
            text_artifact = prepared.prepared_artifact
            if text_artifact is None:
                # 两级 OCR 均已明确失败时，原 PDF 只进入 RAG。正文与全文翻译保持
                # 可降级失败，不能尝试把二进制内容解码成文本。
                original_text = ""
            else:
                with preparer.artifact_store.open_reader(
                    text_artifact
                ) as reader:
                    original_text = reader.read().decode("utf-8")
        except AnalysisFilePreparationError:
            raise
        except Exception as exc:
            logger.exception(
                "文件分析共享文档准备失败: task_id=%s file_name=%s "
                "error_type=%s",
                request.execution.task_id,
                request.execution.file_name,
                type(exc).__name__,
            )
            raise AnalysisFilePreparationError("文件分析文件准备失败") from exc

        logger.info(
            "文件分析共享 prepared Artifact 已映射: task_id=%s "
            "rag_artifact_id=%s text_available=%s text_chars=%d",
            request.execution.task_id,
            rag_artifact.artifact_id[:12],
            prepared.prepared_artifact is not None,
            len(original_text),
        )
        return PreparedAnalysisDocument(
            execution=request.execution,
            source_path=str(downloaded_path),
            processing_path=str(upload_path),
            upload_path=str(upload_path),
            original_text=original_text,
            internal_prepared_basename=upload_path.name,
            prepared_artifact=prepared.prepared_artifact,
            rag_upload_artifact=rag_artifact,
            rag_projection_profile_id=projection_profile_id,
        )

    def _prepare_processing_path(
        self,
        source_path: Path,
        *,
        normalized_dir: Path,
        request: AnalysisFilePreparationRequest,
    ) -> tuple[Path, str]:
        """按受理快照选择转换或既有规范化，Legacy 失败时禁止 raw fallback。"""

        policy = request.document_processing_policy
        if policy is None:  # pragma: no cover - DTO 已保证非空，保留防御边界
            raise AnalysisFilePreparationError("文件处理策略缺失")
        legacy_source = is_legacy_office_path(source_path)
        if legacy_source != policy.legacy_office_required:
            logger.error(
                "文件分析输入类型与处理策略不一致: task_id=%s "
                "legacy_source=%s legacy_office_required=%s policy_fingerprint=%s",
                request.execution.task_id,
                legacy_source,
                policy.legacy_office_required,
                policy.processing_policy_fingerprint,
            )
            raise AnalysisFilePreparationError("文件类型与冻结处理策略不一致")
        if not legacy_source:
            normalized = self._normalize_into_task(
                source_path,
                normalized_dir=normalized_dir,
                task_id=str(request.execution.task_id),
            )
            return normalized, ""
        return self._convert_legacy_into_task(
            source_path,
            normalized_dir=normalized_dir,
            request=request,
        )

    def _convert_legacy_into_task(
        self,
        source_path: Path,
        *,
        normalized_dir: Path,
        request: AnalysisFilePreparationRequest,
    ) -> tuple[Path, str]:
        """转换 Legacy Office 并在清理临时 Job 前发布到 execution 专属目录。"""

        preparer = self._legacy_office_preparer
        policy = request.document_processing_policy
        assert policy is not None
        if preparer is None:
            logger.error(
                "文件分析 Legacy Office 转换能力未配置: task_id=%s policy_fingerprint=%s",
                request.execution.task_id,
                policy.processing_policy_fingerprint,
            )
            raise AnalysisFilePreparationError("Legacy Office 文件本地转换失败")
        try:
            runtime_version = preparer.preflight()
            expected_series = policy.legacy_office_allowed_version_series
            if not runtime_version or not (
                runtime_version == expected_series
                or runtime_version.startswith(f"{expected_series}.")
            ):
                raise LegacyOfficeConversionError("snapshot_version_mismatch")
            with preparer.prepare(
                source_path,
                job_id=str(request.execution.task_id),
            ) as result:
                prepared_path = Path(result.prepared_path)
                target_suffix = self._safe_suffix(result.target_suffix)
                if (
                    not result.converted
                    or target_suffix not in {".docx", ".pptx", ".xlsx"}
                    or not prepared_path.is_file()
                ):
                    raise LegacyOfficeConversionError("invalid_prepared_result")
                # 保留转换器生成的唯一 opaque basename，既避免任务间覆盖，也允许最终
                # Callback 对本次精确内部名称执行窄替换。复制完成后才能退出上下文清理。
                basename = prepared_path.name
                if Path(basename).name != basename or prepared_path.suffix.lower() != target_suffix:
                    raise LegacyOfficeConversionError("invalid_prepared_basename")
                target = normalized_dir / basename
                shutil.copy2(prepared_path, target)
            processing_path = self._require_file_within(
                target,
                root=normalized_dir,
                label="Legacy Office 转换产物",
            )
        except LegacyOfficeConversionError as exc:
            # 转换层 diagnostic 可能包含宿主路径或进程输出，本业务层只记录稳定错误码；
            # 切断异常链，避免外层通用异常日志再次展开敏感细节。
            logger.error(
                "文件分析 Legacy Office 预处理失败: task_id=%s error_code=%s "
                "policy_fingerprint=%s",
                request.execution.task_id,
                exc.code,
                policy.processing_policy_fingerprint,
            )
            raise AnalysisFilePreparationError(
                "Legacy Office 文件本地转换失败"
            ) from None
        except Exception as exc:
            logger.error(
                "文件分析 Legacy Office 预处理失败: task_id=%s error_type=%s "
                "policy_fingerprint=%s",
                request.execution.task_id,
                type(exc).__name__,
                policy.processing_policy_fingerprint,
            )
            raise AnalysisFilePreparationError(
                "Legacy Office 文件本地转换失败"
            ) from None
        logger.info(
            "文件分析 Legacy Office 预处理完成: task_id=%s target_suffix=%s "
            "policy_fingerprint=%s",
            request.execution.task_id,
            processing_path.suffix.lower(),
            policy.processing_policy_fingerprint,
        )
        return processing_path, processing_path.name

    def _download(
        self,
        *,
        source_url: str,
        file_name: str,
        download_dir: Path,
    ) -> Path:
        downloaded = self._downloader(
            source_url,
            file_name,
            str(download_dir),
            self._download_timeout_seconds,
            self._max_download_bytes,
        )
        path = self._require_file_within(
            downloaded,
            root=download_dir,
            label="下载器返回路径",
        )
        return path

    def _normalize_into_task(
        self,
        source_path: Path,
        *,
        normalized_dir: Path,
        task_id: str,
    ) -> Path:
        """保留原有 MHTML 降级语义，并将任何输出复制回任务目录。"""

        candidate = source_path
        try:
            normalized = self._normalizer(str(source_path))
            candidate = self._require_file_within(
                normalized,
                root=source_path.parent.parent,
                label="规范化器返回路径",
            )
        except Exception as exc:
            # 旧链路把 MHTML 规范化视为增强能力：失败后继续上传原文件。这里保留同一
            # 业务语义，但完整异常仅写日志，避免记录 URL、正文或潜在敏感文件名。
            logger.warning(
                "文件分析 MHTML 规范化失败，降级使用原文件: task_id=%s error_type=%s",
                task_id,
                type(exc).__name__,
                exc_info=True,
            )
            candidate = source_path
        suffix = self._safe_suffix(candidate.suffix)
        target = normalized_dir / f"rag-input{suffix}"
        if candidate.resolve() != target.resolve():
            shutil.copy2(candidate, target)
        return self._require_file_within(target, root=normalized_dir, label="规范化产物")

    def _prepare_upload_path(self, source_path: Path, *, task_root: Path) -> Path:
        """把 OCR/MinerU 的两个缓存目录固定到本任务根目录内。"""

        config = self._ocr_config_loader()
        if not isinstance(config, OCRConfig):
            raise TypeError("ocr_config_loader 必须返回 OCRConfig")
        task_config = replace(
            config,
            cache_dir=str(task_root / "ocr"),
            mineru_cache_dir=str(task_root / "mineru"),
        )
        prepared = self._upload_preparer(str(source_path), task_config)
        return self._require_file_within(
            prepared,
            root=task_root,
            label="OCR 准备器返回路径",
        )

    @staticmethod
    def _task_root(raw_path: str) -> Path:
        path = Path(raw_path).resolve()
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise AnalysisFilePreparationError("task_root 不是目录")
        return path

    @staticmethod
    def _require_file_within(value: object, *, root: Path, label: str) -> Path:
        # 外部 Downloader/Normalizer/UploadPreparer 的 Port 返回值仍限定为 str；但本 Adapter
        # 在复制规范化产物后会把自己刚构造的 ``Path`` 再交给该统一校验函数。两种形式都要
        # 先解析并做相对根目录校验，不能因为内部 Path 与外部 str 的表示差异绕过或误伤隔离。
        if not isinstance(value, (str, Path)) or not str(value).strip():
            raise AnalysisFilePreparationError(f"{label} 不是有效路径")
        path = Path(value).resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError as exc:
            raise AnalysisFilePreparationError(f"{label} 越出任务目录") from exc
        if not path.is_file():
            raise AnalysisFilePreparationError(f"{label} 不存在或不是普通文件")
        return path

    @classmethod
    def _suffix_from_url(cls, source_url: str) -> str:
        try:
            suffix = Path(unquote(urlsplit(source_url).path)).suffix
        except (TypeError, ValueError):
            suffix = ""
        return cls._safe_suffix(suffix)

    @staticmethod
    def _safe_suffix(value: str) -> str:
        normalized = str(value or "")
        return normalized.lower() if _SAFE_SUFFIX_PATTERN.fullmatch(normalized) else ""

    @staticmethod
    def _read_original_text(file_path: str) -> str:
        """保持旧 Analysis 的正文读取规则，不把读取失败伪装为空正文。"""

        path = Path(file_path)
        suffix = path.suffix.lower()
        if suffix in {".txt", ".md", ".json", ".csv"}:
            return path.read_text(encoding="utf-8", errors="ignore")
        if suffix == ".pdf":
            with fitz.open(path) as document:
                return "\n".join(page.get_text() for page in document)
        if suffix == ".docx":
            return extract_text_from_word(str(path))
        if is_mhtml_file(str(path)):
            return extract_text_from_mhtml(str(path))
        return ""


__all__ = (
    "AnalysisFilePreparationError",
    "LegacyAnalysisFilePreparationAdapter",
    "LocalAnalysisTaskWorkspaceAdapter",
)
