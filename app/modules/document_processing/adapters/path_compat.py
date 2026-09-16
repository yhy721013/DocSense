"""遗留路径式 MHTML API 的唯一实现。

这些函数只服务尚未删除的 Python 兼容签名；新业务必须使用 Artifact 流水线。实现放在
DocumentProcessing Adapter 层，避免 Analysis/Report 反向依赖 ``services/utils``。
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.modules.document_processing.domain import (
    extract_mhtml_text,
    is_mhtml_content,
)

from .mhtml import (
    MHTMLBrowserOutcomeUnknownError,
    convert_mhtml_to_pdf,
)


logger = logging.getLogger(__name__)


def is_mhtml_file(file_path: str) -> bool:
    path = Path(file_path)
    if not path.is_file():
        return False
    try:
        with path.open("rb") as reader:
            header = reader.read(1024)
    except OSError:
        return False
    detected = is_mhtml_content(file_name=path.name, header=header)
    if detected and path.suffix.casefold() not in {".mhtml", ".mht"}:
        logger.warning(
            "检测到扩展名与内容不一致的 MHTML: suffix=%s file_name=%s",
            path.suffix,
            path.name,
        )
    return detected


def extract_text_from_mhtml(file_path: str) -> str:
    return extract_mhtml_text(Path(file_path).read_bytes())


def extract_retrieval_text_from_mhtml(file_path: str) -> str:
    return extract_mhtml_text(Path(file_path).read_bytes(), retrieval=True)


def normalize_mhtml_file_for_retrieval(file_path: str) -> str:
    source = Path(file_path)
    destination = source.with_name(f"{source.name}.retrieval.md")
    destination.write_text(
        extract_retrieval_text_from_mhtml(file_path) + "\n",
        encoding="utf-8",
    )
    return str(destination)


def normalize_file_for_retrieval(file_path: str) -> str:
    return (
        normalize_mhtml_file_for_retrieval(file_path)
        if is_mhtml_file(file_path)
        else file_path
    )


def normalize_mhtml_file(
    file_path: str,
    use_pdf_conversion: bool = True,
) -> str:
    source = Path(file_path)
    if use_pdf_conversion:
        destination = source.with_name(f"{source.name}.normalized.pdf")
        try:
            return convert_mhtml_to_pdf(str(source), str(destination))
        except MHTMLBrowserOutcomeUnknownError:
            # 结果未知时禁止启动降级链，等待人工/恢复流程协调。
            raise
        except Exception as exc:
            logger.warning(
                "MHTML 浏览器转换已确认失败，执行 Markdown 降级: "
                "error_type=%s",
                type(exc).__name__,
            )
    destination = source.with_name(f"{source.name}.normalized.md")
    destination.write_text(
        extract_text_from_mhtml(file_path) + "\n",
        encoding="utf-8",
    )
    return str(destination)


def normalize_file_for_llm(
    file_path: str,
    use_pdf_conversion: bool = True,
) -> str:
    return (
        normalize_mhtml_file(
            file_path,
            use_pdf_conversion=use_pdf_conversion,
        )
        if is_mhtml_file(file_path)
        else file_path
    )


__all__ = [
    "extract_retrieval_text_from_mhtml",
    "extract_text_from_mhtml",
    "is_mhtml_file",
    "normalize_file_for_llm",
    "normalize_file_for_retrieval",
    "normalize_mhtml_file",
    "normalize_mhtml_file_for_retrieval",
]
