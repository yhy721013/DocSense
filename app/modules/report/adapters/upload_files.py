"""报告兼容路径使用的本地上传文件准备函数。

本模块只负责把一个已经下载并规范化的本地文件交给现行 DocumentProcessing OCR
Adapter，并返回确定顺序的本地结果路径。它不创建 AnythingLLM Workspace、Thread 或
网络连接，避免把旧 ``rag_pipeline`` 的跨资源编排重新搬入 Report Adapter。
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.modules.document_processing.adapters.builtin_ocr import (
    prepare_file_for_upload,
)
from app.services.core.config import OCRConfig, load_ocr_config


logger = logging.getLogger(__name__)
_REPORT_OCR_CONFIG = load_ocr_config()


def prepare_report_upload_files(
    file_path: str,
    config: OCRConfig = _REPORT_OCR_CONFIG,
) -> list[str]:
    """按旧 Report 语义准备一个上传文件列表。

    非 PDF 或可提取文本的 PDF 保持原文件；扫描 PDF 优先返回 OCR 生成的 Markdown；
    OCR Adapter 降级为原 PDF 时继续返回原路径。不存在的输入仍返回空列表，随后由
    ``LegacyReportFileAdapter`` 统一映射为稳定的 ``ReportInputError``。
    """

    source = Path(file_path)
    if not source.exists():
        logger.warning("报告上传准备输入不存在: input_exists=false")
        return []

    prepared = Path(prepare_file_for_upload(str(source), config))
    if not prepared.exists():
        logger.warning(
            "报告上传准备结果不存在，降级使用原文件: fallback_to_source=true"
        )
        return [str(source)]

    logger.debug(
        "报告上传文件准备完成: output_count=1 output_suffix=%s",
        prepared.suffix.lower() or "none",
    )
    return [str(prepared)]


__all__ = ["prepare_report_upload_files"]
