"""MHTML 旧函数签名的兼容 Facade；唯一实现位于 DocumentProcessing。"""

from app.modules.document_processing.adapters.path_compat import (
    extract_retrieval_text_from_mhtml,
    extract_text_from_mhtml,
    is_mhtml_file,
    normalize_file_for_llm,
    normalize_file_for_retrieval,
    normalize_mhtml_file,
    normalize_mhtml_file_for_retrieval,
)


MHTML2PDF_AVAILABLE = True


__all__ = [
    "MHTML2PDF_AVAILABLE",
    "extract_retrieval_text_from_mhtml",
    "extract_text_from_mhtml",
    "is_mhtml_file",
    "normalize_file_for_llm",
    "normalize_file_for_retrieval",
    "normalize_mhtml_file",
    "normalize_mhtml_file_for_retrieval",
]
