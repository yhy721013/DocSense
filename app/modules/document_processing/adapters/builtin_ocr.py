"""扫描 PDF 检测、内置 OCR 与既有路径兼容编排的唯一实现。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol, Union

import fitz

from app.modules.document_processing.adapters.content import FileArtifactContent
from app.modules.document_processing.domain import (
    ArtifactKind,
    DocumentProcessingError,
    DocumentProcessingRequest,
    DocumentRepresentation,
    ProcessingProfile,
)
from app.modules.document_processing.ports import (
    ArtifactStorePort,
    ProcessorOutput,
)
logger = logging.getLogger(__name__)
BUILTIN_OCR_PROCESSOR_ID = "builtin-ocr-to-markdown"
BUILTIN_OCR_PROCESSOR_FINGERPRINT = "docsense-builtin-ocr-adapter-v1"
_MATERIALIZATION_MARKER = ".docsense-builtin-ocr-materialization"


class OCRRuntimeConfig(Protocol):
    """旧路径兼容编排所需的配置形状，避免 Adapter 反向依赖 services。"""

    enabled: bool
    languages: str
    dpi: int
    sample_pages: int
    text_threshold: int
    cache_dir: str
    analysis_scanned_pdf_engine: str
    mineru_cache_dir: str
    mineru_lang: str
    mineru_api_url: str | None
    tessdata_prefix: str | None


def build_builtin_ocr_profile(
    *,
    languages: str,
    dpi: int,
) -> ProcessingProfile:
    """冻结内置 OCR 语言和 DPI；运行中不再重新读取环境默认值。"""

    normalized_languages = str(languages).strip()
    if not normalized_languages:
        raise ValueError("OCR languages 不能为空")
    if isinstance(dpi, bool) or not isinstance(dpi, int) or dpi < 50:
        raise ValueError("OCR dpi 必须是不小于 50 的整数")
    return ProcessingProfile.create(
        processor_id=BUILTIN_OCR_PROCESSOR_ID,
        processor_fingerprint=BUILTIN_OCR_PROCESSOR_FINGERPRINT,
        target_representation=DocumentRepresentation.MARKDOWN,
        parameters={
            "dpi": dpi,
            "languages": normalized_languages,
            "sourceSuffix": ".pdf",
        },
    )


def build_ocr_cache_key(path: Union[str, Path], size: int, mtime_ns: int) -> str:
    resolved_path = str(Path(path).resolve(strict=False)).replace("\\", "/")
    fingerprint = f"{resolved_path}|{size}|{mtime_ns}"
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def build_mineru_cache_key(
    path: Union[str, Path],
    size: int,
    mtime_ns: int,
    lang: str,
    api_url: str | None,
) -> str:
    resolved_path = str(Path(path).resolve(strict=False)).replace("\\", "/")
    fingerprint = f"mineru|{resolved_path}|{size}|{mtime_ns}|{lang}|{api_url or ''}"
    return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()


def prepare_file_for_upload(file_path: str, ocr_config: OCRRuntimeConfig) -> str:
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        return str(path)

    if not ocr_config.enabled:
        return str(path)

    if path.suffix.lower() != ".pdf":
        return str(path)

    if not is_scanned_pdf(
        str(path),
        sample_pages=ocr_config.sample_pages,
        text_threshold=ocr_config.text_threshold,
    ):
        return str(path)

    return _prepare_scanned_pdf_with_builtin_ocr(path, ocr_config)


def prepare_analysis_file_for_upload(
    file_path: str,
    ocr_config: OCRRuntimeConfig,
) -> str:
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        return str(path)

    if not ocr_config.enabled:
        return str(path)

    if path.suffix.lower() != ".pdf":
        return str(path)

    if not is_scanned_pdf(
        str(path),
        sample_pages=ocr_config.sample_pages,
        text_threshold=ocr_config.text_threshold,
    ):
        return str(path)

    if ocr_config.analysis_scanned_pdf_engine == "mineru":
        try:
            markdown_path = mineru_pdf_to_markdown(path, ocr_config)
            logger.info(
                "扫描 PDF 已由 MinerU 解析为 Markdown: input_file=%s output_file=%s",
                path.name,
                markdown_path.name,
            )
            return str(markdown_path)
        except Exception as exc:  # pylint: disable=broad-except
            logger.warning(
                "扫描 PDF 的 MinerU 解析失败，改用内置 OCR: input_file=%s error_type=%s",
                path.name,
                type(exc).__name__,
            )

    return _prepare_scanned_pdf_with_builtin_ocr(path, ocr_config)


def _prepare_scanned_pdf_with_builtin_ocr(
    path: Path,
    ocr_config: OCRRuntimeConfig,
) -> str:
    try:
        markdown_path = ocr_pdf_to_markdown(path, ocr_config)
        logger.info(
            "扫描 PDF 已由内置 OCR 解析为 Markdown: input_file=%s output_file=%s",
            path.name,
            markdown_path.name,
        )
        return str(markdown_path)
    except Exception as exc:  # pylint: disable=broad-except
        logger.warning(
            "扫描 PDF 的内置 OCR 解析失败，改为直接上传原文件: "
            "input_file=%s error_type=%s",
            path.name,
            type(exc).__name__,
        )
        return str(path)


def mineru_pdf_to_markdown(
    pdf_path: Path,
    ocr_config: OCRRuntimeConfig,
) -> Path:
    source_path = pdf_path.resolve(strict=True)
    source_stat = source_path.stat()

    cache_root = _resolve_cache_root(ocr_config.mineru_cache_dir)
    cache_key = build_mineru_cache_key(
        source_path,
        source_stat.st_size,
        source_stat.st_mtime_ns,
        ocr_config.mineru_lang,
        ocr_config.mineru_api_url,
    )
    markdown_path = _safe_cache_file(cache_root, f"{cache_key}.md")
    metadata_path = _safe_cache_file(cache_root, f"{cache_key}.meta.json")

    if markdown_path.exists() and markdown_path.stat().st_size > 0:
        return markdown_path

    from app.modules.document_processing.adapters.mineru import MinerUConverter

    generated_at = datetime.now(timezone.utc).isoformat()
    converter = MinerUConverter(output_dir=str(cache_root))
    result_path = Path(
        converter.convert_to_markdown(
            input_path=str(source_path),
            use_ocr=True,
            lang=ocr_config.mineru_lang,
            extract_images=True,
            formula_enable=True,
            table_enable=True,
            backend="pipeline",
            api_url=ocr_config.mineru_api_url,
            output_subdir=cache_key,
        )
    )

    if result_path.is_dir():
        md_files = sorted(result_path.rglob("*.md"))
        if not md_files:
            raise FileNotFoundError(f"MinerU 未生成 Markdown: {result_path}")
        result_path = md_files[0]

    markdown_body = result_path.read_text(encoding="utf-8", errors="ignore").strip()
    if not markdown_body:
        raise ValueError(f"MinerU 生成的 Markdown 为空: {result_path}")

    markdown_text = "\n".join(
        [
            "# MinerU Markdown",
            "",
            f"- Source File: `{source_path.name}`",
            f"- Generated At (UTC): {generated_at}",
            f"- MinerU Language: `{ocr_config.mineru_lang}`",
            "",
            markdown_body,
            "",
        ]
    )
    _atomic_write_text(markdown_path, markdown_text)

    metadata = {
        "cache_key": cache_key,
        "source_file": source_path.name,
        "source_path": str(source_path),
        "source_size": source_stat.st_size,
        "source_mtime_ns": source_stat.st_mtime_ns,
        "generated_at_utc": generated_at,
        "mineru_lang": ocr_config.mineru_lang,
        "mineru_api_url": ocr_config.mineru_api_url,
        "mineru_result_path": str(result_path),
        "markdown_path": str(markdown_path),
    }
    _atomic_write_text(metadata_path, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")

    return markdown_path


def ocr_pdf_to_markdown(
    pdf_path: Path,
    ocr_config: OCRRuntimeConfig,
) -> Path:
    source_path = pdf_path.resolve(strict=True)
    source_stat = source_path.stat()

    cache_root = _resolve_cache_root(ocr_config.cache_dir)
    cache_key = build_ocr_cache_key(source_path, source_stat.st_size, source_stat.st_mtime_ns)
    markdown_path = _safe_cache_file(cache_root, f"{cache_key}.md")
    metadata_path = _safe_cache_file(cache_root, f"{cache_key}.meta.json")

    if markdown_path.exists() and markdown_path.stat().st_size > 0:
        return markdown_path

    _configure_tessdata(ocr_config)

    generated_at = datetime.now(timezone.utc).isoformat()
    markdown_text, page_count = _render_ocr_markdown(
        source_path,
        languages=ocr_config.languages,
        dpi=ocr_config.dpi,
        generated_at=generated_at,
    )
    _atomic_write_text(markdown_path, markdown_text)

    metadata = {
        "cache_key": cache_key,
        "source_file": source_path.name,
        "source_path": str(source_path),
        "source_size": source_stat.st_size,
        "source_mtime_ns": source_stat.st_mtime_ns,
        "generated_at_utc": generated_at,
        "ocr_languages": ocr_config.languages,
        "ocr_dpi": ocr_config.dpi,
        "page_count": page_count,
        "markdown_path": str(markdown_path),
    }
    _atomic_write_text(metadata_path, json.dumps(metadata, ensure_ascii=False, indent=2) + "\n")

    return markdown_path


def _render_ocr_markdown(
    source_path: Path,
    *,
    languages: str,
    dpi: int,
    generated_at: str,
) -> tuple[str, int]:
    """执行内置 OCR 并返回 Markdown；不读取配置、不修改进程环境。"""

    markdown_lines: list[str] = []
    with fitz.open(str(source_path)) as document:
        page_count = len(document)
        markdown_lines.extend(["# OCR Markdown", ""])
        markdown_lines.append(f"- Source File: `{source_path.name}`")
        if generated_at:
            markdown_lines.append(f"- Generated At (UTC): {generated_at}")
        markdown_lines.extend(
            [
                f"- OCR Languages: `{languages}`",
                f"- OCR DPI: {dpi}",
                f"- Total Pages: {page_count}",
                "",
            ]
        )
        for page_index in range(page_count):
            page = document[page_index]
            textpage = page.get_textpage_ocr(language=languages, dpi=dpi)
            page_text = page.get_text("text", textpage=textpage).strip()
            markdown_lines.extend(
                [
                    f"## Page {page_index + 1}",
                    "",
                    page_text,
                    "",
                ]
            )
    return "\n".join(markdown_lines).rstrip() + "\n", page_count


def _configure_tessdata(ocr_config: OCRRuntimeConfig) -> None:
    if not ocr_config.tessdata_prefix:
        return
    os.environ["TESSDATA_PREFIX"] = ocr_config.tessdata_prefix


def _resolve_cache_root(cache_dir: str) -> Path:
    cache_root = Path(cache_dir).resolve(strict=False)
    cache_root.mkdir(parents=True, exist_ok=True)
    return cache_root


def _safe_cache_file(cache_root: Path, file_name: str) -> Path:
    candidate = (cache_root / file_name).resolve(strict=False)
    candidate.relative_to(cache_root)
    return candidate


def _atomic_write_text(target: Path, content: str) -> None:
    temp_path = target.with_suffix(target.suffix + ".tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(target)


def is_scanned_pdf(pdf_path: str, sample_pages: int = 3, text_threshold: int = 50) -> bool:
    # 通过抽样页文本长度判断是否为扫描件
    try:
        with fitz.open(pdf_path) as doc:
            total_pages = len(doc)
            pages_to_check = min(sample_pages, total_pages)
            if pages_to_check <= 0:
                return True

            total_text_length = 0
            for page_num in range(pages_to_check):
                page = doc[page_num]
                text = page.get_text().strip()
                total_text_length += len(text)

        avg_text_per_page = total_text_length / pages_to_check
        return avg_text_per_page < text_threshold
    except Exception:
        return True


class BuiltinOCRDocumentProcessorAdapter:
    """将单个 PDF Artifact 物化后执行内置 OCR。

    ``tessdata`` 环境必须由组合根在并发任务开始前配置完成；本 Adapter 不修改任何
    进程级环境变量。输出目录由 step_key 独占，避免 50 个 accepted 任务互相覆盖。
    """

    def __init__(
        self,
        *,
        source_store: ArtifactStorePort,
        materialization_root: str | Path,
        renderer: Callable[..., tuple[str, int]] = _render_ocr_markdown,
    ) -> None:
        if not isinstance(source_store, ArtifactStorePort):
            raise TypeError("source_store 必须实现 ArtifactStorePort")
        self._source_store = source_store
        self._root = self._canonical_resolved(
            Path(materialization_root).expanduser()
        )
        self._renderer = renderer

    def process(self, request: DocumentProcessingRequest) -> ProcessorOutput:
        parameters = self._validate_profile(request)
        scratch = self._canonical_resolved(self._root / request.step_key)
        self._require_contained(scratch)
        if scratch.exists():
            raise DocumentProcessingError(
                "builtin_ocr_materialization_conflict",
                "OCR 独占物化目录已存在，必须先完成恢复或清理",
                outcome_unknown=True,
            )
        scratch.mkdir(parents=True, exist_ok=False)
        (scratch / _MATERIALIZATION_MARKER).write_text(
            "DOCSENSE_BUILTIN_OCR_MATERIALIZATION_V1\n",
            encoding="ascii",
        )
        source_path = scratch / "source.pdf"
        markdown_path = scratch / "result.md"
        try:
            with self._source_store.open_reader(
                request.source_artifact
            ) as reader, source_path.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            markdown, page_count = self._renderer(
                source_path,
                languages=str(parameters["languages"]),
                dpi=int(parameters["dpi"]),
                # 时间戳不是内容语义，Artifact 输出必须对同一个 step 保持确定。
                generated_at="",
            )
            if page_count <= 0 or not markdown.strip():
                raise DocumentProcessingError(
                    "builtin_ocr_empty_result",
                    "内置 OCR 未生成非空 Markdown",
                )
            with markdown_path.open("x", encoding="utf-8", newline="\n") as writer:
                writer.write(markdown)
                writer.flush()
                os.fsync(writer.fileno())
            logger.info(
                "内置 OCR Processor 已生成候选: task_id=%s step_key=%s "
                "page_count=%d bytes=%d",
                request.task_id,
                request.step_key[:12],
                page_count,
                markdown_path.stat().st_size,
            )
            return ProcessorOutput.with_cleanup(
                content=FileArtifactContent(markdown_path),
                kind=ArtifactKind.PREPARED,
                representation=DocumentRepresentation.MARKDOWN,
                media_type="text/markdown",
                cleanup=lambda: self._cleanup(scratch),
            )
        except DocumentProcessingError:
            self._cleanup(scratch)
            raise
        except Exception as exc:
            self._cleanup(scratch)
            logger.exception(
                "内置 OCR Processor 执行失败: task_id=%s step_key=%s",
                request.task_id,
                request.step_key[:12],
            )
            raise DocumentProcessingError(
                "builtin_ocr_processor_failed",
                "内置 OCR Processor 执行失败",
            ) from exc

    @staticmethod
    def _validate_profile(
        request: DocumentProcessingRequest,
    ) -> dict[str, object]:
        profile = request.profile
        if (
            profile.processor_id != BUILTIN_OCR_PROCESSOR_ID
            or profile.target_representation
            is not DocumentRepresentation.MARKDOWN
        ):
            raise DocumentProcessingError(
                "builtin_ocr_profile_mismatch",
                "请求不是内置 OCR Markdown profile",
            )
        parameters = profile.to_dict()["parameters"]
        if (
            not isinstance(parameters, dict)
            or set(parameters) != {"dpi", "languages", "sourceSuffix"}
            or parameters["sourceSuffix"] != ".pdf"
            or not str(parameters["languages"]).strip()
            or isinstance(parameters["dpi"], bool)
            or not isinstance(parameters["dpi"], int)
            or parameters["dpi"] < 50
        ):
            raise DocumentProcessingError(
                "builtin_ocr_profile_invalid",
                "内置 OCR profile 参数不合法",
            )
        return parameters

    def _cleanup(self, scratch: Path) -> None:
        try:
            self._require_contained(scratch)
            marker = scratch / _MATERIALIZATION_MARKER
            if (
                scratch.parent != self._root
                or not marker.is_file()
                or marker.read_text(encoding="ascii")
                != "DOCSENSE_BUILTIN_OCR_MATERIALIZATION_V1\n"
            ):
                logger.warning(
                    "跳过不满足所有权条件的 OCR 目录清理: directory_name=%s",
                    scratch.name,
                )
                return
            shutil.rmtree(scratch)
        except OSError:
            logger.warning(
                "OCR 物化目录清理失败，将由巡检继续处理: directory_name=%s",
                scratch.name,
                exc_info=True,
            )

    def _require_contained(self, path: Path) -> None:
        try:
            path.relative_to(self._root)
        except ValueError as exc:
            raise DocumentProcessingError(
                "builtin_ocr_materialization_path_escape",
                "OCR 物化路径越出允许边界",
            ) from exc

    @staticmethod
    def _canonical_resolved(path: Path) -> Path:
        """统一 Windows 扩展长度路径与普通路径表示，避免等价路径误判逃逸。"""

        resolved = path.resolve()
        if os.name != "nt":
            return resolved
        text = str(resolved)
        if text.startswith("\\\\?\\UNC\\"):
            return Path("\\\\" + text[8:])
        if text.startswith("\\\\?\\"):
            return Path(text[4:])
        return resolved


__all__ = [
    "BUILTIN_OCR_PROCESSOR_FINGERPRINT",
    "BUILTIN_OCR_PROCESSOR_ID",
    "BuiltinOCRDocumentProcessorAdapter",
    "build_builtin_ocr_profile",
    "build_mineru_cache_key",
    "build_ocr_cache_key",
    "is_scanned_pdf",
    "mineru_pdf_to_markdown",
    "ocr_pdf_to_markdown",
    "prepare_analysis_file_for_upload",
    "prepare_file_for_upload",
]
