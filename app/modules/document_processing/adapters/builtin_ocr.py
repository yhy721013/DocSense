"""扫描 PDF 检测、内置 OCR 与既有路径兼容编排的唯一实现。"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Callable

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
    "is_scanned_pdf",
]
