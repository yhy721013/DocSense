"""文件分析任务目录与共享文档处理能力的基础设施适配器。

本模块只负责受控下载、调用共享 DocumentProcessing，并把 canonical/RAG Artifact 映射到
当前 Analysis 任务目录。它不参与领域分类、任务终态或回调；每次调用都必须接收由
``AnalysisTaskWorkspacePort`` 创建的任务目录，避免临时文件在不同 execution 间混用。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
import re
import shutil
import hashlib
from typing import Callable
from urllib.parse import unquote, urlsplit

from app.modules.document_processing import (
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
    AcquiredAnalysisSource,
    AnalysisDocumentPreparationRequest,
    AnalysisFilePreparationRequest,
    AnalysisSourceAcquisitionRequest,
    AnalysisSourceResolutionRequest,
    AnalysisTaskWorkspace,
    AnalysisTaskWorkspacePort,
    PreparedAnalysisDocument,
)
from app.services.utils.file_downloader import (
    DEFAULT_MAX_DOWNLOAD_BYTES,
    download_to_temp_file,
)
logger = logging.getLogger(__name__)

_SAFE_SUFFIX_PATTERN = re.compile(r"^\.[A-Za-z0-9]{1,12}$")

Downloader = Callable[[str, str, str, float, int], str]


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

    def resolve(self, execution) -> AnalysisTaskWorkspace:  # type: ignore[no-untyped-def]
        """只读复核既有任务目录，恢复时不得把缺失目录补造为成功事实。"""

        task_id = str(getattr(execution, "task_id", "") or "").strip()
        if not task_id or Path(task_id).name != task_id or any(
            character in task_id for character in ("/", "\\")
        ):
            raise AnalysisFilePreparationError("task_id 不能用于任务目录")
        task_root = self._canonical_resolved(self._root_directory / task_id)
        try:
            task_root.relative_to(self._root_directory)
        except ValueError as exc:
            raise AnalysisFilePreparationError("任务目录越出受控根目录") from exc
        if not task_root.is_dir():
            raise AnalysisFilePreparationError("续跑任务目录不存在")
        logger.info("已复核文件分析续跑任务目录: task_id=%s", task_id)
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
    """把共享文档处理结果安全映射到当前 Analysis 任务目录。

    OCR/MinerU 的格式判定、转换和降级统一由显式注入的 ``document_preparer`` 承担；
    RAG 专用 Markdown 投影由 ``rag_projector`` 承担。本 Adapter 不解析全局缓存目录，
    只在调用方提供的任务根下创建下载目录和最终映射文件。
    """

    def __init__(
        self,
        *,
        document_preparer: LocalDocumentPreparationAdapter,
        rag_projector: ProjectDocumentForRag,
        download_timeout_seconds: float = 60.0,
        max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
        downloader: Downloader = download_to_temp_file,
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
            ("downloader", downloader),
        ):
            if not callable(dependency):
                raise TypeError(f"{name} 必须可调用")
        if not callable(
            getattr(document_preparer, "prepare", None)
        ):
            raise TypeError("document_preparer 必须实现 prepare")
        if not isinstance(rag_projector, ProjectDocumentForRag):
            raise TypeError("rag_projector 必须是 ProjectDocumentForRag")
        if not isinstance(document_scanned_pdf_engine, ScannedPDFEngine):
            raise TypeError(
                "document_scanned_pdf_engine 必须是 ScannedPDFEngine"
            )
        self._download_timeout_seconds = float(download_timeout_seconds)
        self._max_download_bytes = max_download_bytes
        self._downloader = downloader
        self._document_preparer = document_preparer
        self._rag_projector = rag_projector
        self._document_scanned_pdf_engine = document_scanned_pdf_engine

    @property
    def source_transport_profile_id(self) -> str:
        """返回下载 Adapter 的稳定语义身份，不泄露 Source URL。"""

        return "http-source-atomic-download-v1"

    @property
    def max_download_bytes(self) -> int:
        """返回本实例实际执行的下载字节上限。"""

        return self._max_download_bytes

    @property
    def rag_projection_profile_id(self) -> str:
        """返回实际注入 Markdown RAG 投影器的 Canonical ProfileId。"""

        return self._rag_projector.profile_id

    def prepare(
        self,
        request: AnalysisFilePreparationRequest,
    ) -> PreparedAnalysisDocument:
        """保留 v1 内部兼容外观；v2 必须分别调用 acquire/prepare_document。"""

        if not isinstance(request, AnalysisFilePreparationRequest):
            raise TypeError("request 必须是 AnalysisFilePreparationRequest")
        source = self.acquire_source(
            AnalysisSourceAcquisitionRequest(
                execution=request.execution,
                source_url=request.source_url,
                task_root=request.task_root,
            )
        )
        policy = request.document_processing_policy
        if policy is None:  # pragma: no cover - DTO 已保证
            raise AnalysisFilePreparationError("文件处理策略缺失")
        return self.prepare_document(
            AnalysisDocumentPreparationRequest(
                execution=request.execution,
                task_root=request.task_root,
                source=source,
                document_processing_policy=policy,
            )
        )

    def acquire_source(
        self,
        request: AnalysisSourceAcquisitionRequest,
    ) -> AcquiredAnalysisSource:
        """只执行受控下载并形成摘要，不调用 DocumentProcessing。"""

        if not isinstance(request, AnalysisSourceAcquisitionRequest):
            raise TypeError("request 必须是 AnalysisSourceAcquisitionRequest")
        task_root = self._task_root(request.task_root)
        download_dir = task_root / "download"
        download_dir.mkdir(parents=True, exist_ok=True)

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
            digest = self._sha256_file(downloaded_path)
            logger.info(
                "文件分析 Source 已受控获取: task_id=%s basename=%s checksum=%s",
                request.execution.task_id,
                downloaded_path.name,
                digest[:12],
            )
            return AcquiredAnalysisSource(
                execution=request.execution,
                source_path=str(downloaded_path),
                source_basename=downloaded_path.name,
                source_sha256=digest,
            )
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

    def prepare_document(
        self,
        request: AnalysisDocumentPreparationRequest,
    ) -> PreparedAnalysisDocument:
        """只处理已取得 Source；先复核任务目录、basename 与内容摘要。"""

        if not isinstance(request, AnalysisDocumentPreparationRequest):
            raise TypeError("request 必须是 AnalysisDocumentPreparationRequest")
        task_root = self._task_root(request.task_root)
        download_dir = task_root / "download"
        normalized_dir = task_root / "normalized"
        normalized_dir.mkdir(parents=True, exist_ok=True)
        source_path = self._require_file_within(
            request.source.source_path,
            root=download_dir,
            label="已取得 Source",
        )
        if source_path.name != request.source.source_basename:
            raise AnalysisFilePreparationError("Source basename 与冻结引用不一致")
        actual_digest = self._sha256_file(source_path)
        if actual_digest != request.source.source_sha256:
            raise AnalysisFilePreparationError("Source 内容摘要与冻结引用不一致")
        return self._prepare_shared_artifact(
            request,
            downloaded_path=source_path,
            normalized_dir=normalized_dir,
        )

    def resolve_source(
        self,
        request: AnalysisSourceResolutionRequest,
    ) -> AcquiredAnalysisSource:
        """只读复核续跑快照引用的任务内 Source；绝不重新下载。"""

        if not isinstance(request, AnalysisSourceResolutionRequest):
            raise TypeError("request 必须是 AnalysisSourceResolutionRequest")
        task_root = self._task_root(request.task_root)
        download_dir = task_root / "download"
        source_path = self._require_file_within(
            download_dir / request.source_basename,
            root=download_dir,
            label="续跑 Source",
        )
        actual_digest = self._sha256_file(source_path)
        if actual_digest != request.source_sha256:
            raise AnalysisFilePreparationError("续跑 Source 内容摘要漂移")
        logger.info(
            "已复核 Analysis 续跑 Source: task_id=%s basename=%s checksum=%s",
            request.execution.task_id,
            request.source_basename,
            actual_digest[:12],
        )
        return AcquiredAnalysisSource(
            execution=request.execution,
            source_path=str(source_path),
            source_basename=request.source_basename,
            source_sha256=actual_digest,
        )

    def _prepare_shared_artifact(
        self,
        request: AnalysisDocumentPreparationRequest,
        *,
        downloaded_path: Path,
        normalized_dir: Path,
    ) -> PreparedAnalysisDocument:
        """分别形成 canonical 正文与最终 RAG 上传 Artifact。

        Markdown/Text 的 canonical Artifact 必须先经过 RAG-only 投影；PDF 两级文本提取
        明确失败时仍复用原 PDF。投影仅改变检索输入，不覆盖正文读取和全文翻译来源。
        """

        preparer = self._document_preparer
        policy = request.document_processing_policy
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
            source_sha256=self._sha256_file(downloaded_path),
        )

    @staticmethod
    def _sha256_file(path: Path) -> str:
        """流式计算已受控下载文件摘要，避免大文件一次性进入内存。"""

        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

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

    @staticmethod
    def _task_root(raw_path: str) -> Path:
        path = Path(raw_path).resolve()
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise AnalysisFilePreparationError("task_root 不是目录")
        return path

    @staticmethod
    def _require_file_within(value: object, *, root: Path, label: str) -> Path:
        # 外部 Downloader 返回值仍限定为 str；本 Adapter 在映射共享 Artifact 后也会把
        # 自己构造的 ``Path`` 交给统一校验。两种形式都必须先解析并做相对根目录校验，
        # 不能因为内部 Path 与外部 str 的表示差异绕过或误伤任务隔离。
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

__all__ = (
    "AnalysisFilePreparationError",
    "LegacyAnalysisFilePreparationAdapter",
    "LocalAnalysisTaskWorkspaceAdapter",
)
