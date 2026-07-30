"""兼容旧 OCR 预处理导入路径。

唯一实现已经迁入 DocumentProcessing。业务调用方将在 1H-6 切换到 Artifact 用例；
迁移期间保留原函数名和参数，避免任何前后端接口契约变化。
"""

from app.modules.document_processing.adapters.builtin_ocr import (
    build_mineru_cache_key,
    build_ocr_cache_key,
    is_scanned_pdf,
    mineru_pdf_to_markdown,
    ocr_pdf_to_markdown,
    prepare_analysis_file_for_upload,
    prepare_file_for_upload,
)

__all__ = [
    "build_mineru_cache_key",
    "build_ocr_cache_key",
    "is_scanned_pdf",
    "mineru_pdf_to_markdown",
    "ocr_pdf_to_markdown",
    "prepare_analysis_file_for_upload",
    "prepare_file_for_upload",
]
