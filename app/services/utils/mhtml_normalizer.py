from __future__ import annotations

import logging
import re
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# 【新增】导入 MHTML 转 PDF 转换器
try:
    from app.services.translator.mhtml2pdf import convert_mhtml_to_pdf
    MHTML2PDF_AVAILABLE = True
except ImportError:
    MHTML2PDF_AVAILABLE = False
    logger.warning("mhtml2pdf 模块未找到，将降级使用纯文本提取模式")


_BLOCK_BREAK_TAGS = {
    "address",
    "article",
    "aside",
    "blockquote",
    "body",
    "div",
    "dt",
    "dd",
    "figcaption",
    "figure",
    "footer",
    "form",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "header",
    "li",
    "main",
    "nav",
    "p",
    "section",
    "table",
    "td",
    "th",
    "title",
    "tr",
    "ul",
    "ol",
}
_INLINE_BREAK_TAGS = {"br", "hr"}
_SKIP_TAGS = {"script", "style", "noscript"}


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        normalized_tag = tag.lower()
        if normalized_tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if normalized_tag in _BLOCK_BREAK_TAGS or normalized_tag in _INLINE_BREAK_TAGS:
            self._append_newline()

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        normalized_tag = tag.lower()
        if normalized_tag in _SKIP_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if normalized_tag in _BLOCK_BREAK_TAGS or normalized_tag in _INLINE_BREAK_TAGS:
            self._append_newline()

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if self._skip_depth:
            return
        text = re.sub(r"\s+", " ", data).strip()
        if not text:
            return
        if self._chunks and not self._chunks[-1].endswith(("\n", " ")):
            self._chunks.append(" ")
        self._chunks.append(text)

    def get_text(self) -> str:
        joined = "".join(self._chunks)
        lines = [line.strip() for line in joined.splitlines()]
        compact = "\n".join(line for line in lines if line)
        return re.sub(r"\n{3,}", "\n\n", compact).strip()

    def _append_newline(self) -> None:
        if not self._chunks:
            return
        if self._chunks[-1].endswith("\n"):
            return
        self._chunks.append("\n")


def is_mhtml_file(file_path: str) -> bool:
    """
    检查文件是否是MHTML格式。
    
    【关键增强】不仅检查扩展名，还通过文件头内容验证真实格式。
    这样可以检测到被错误命名为 .pdf 的MHTML文件。
    
    :param file_path: 文件路径
    :return: True 如果是MHTML格式，否则 False
    """
    path = Path(file_path)
    if not path.exists() or not path.is_file():
        return False
    
    # 先检查扩展名（快速判断）
    if path.suffix.lower() in {".mhtml", ".mht"}:
        return True
    
    # 【关键修复】如果扩展名不是 .mhtml/.mht，但可能是被错误命名的MHTML文件
    # 通过读取文件头内容进行二次验证
    try:
        with open(file_path, 'rb') as f:
            # MHTML文件通常以 MIME 头部开始，包含 "From:" 和 multipart boundary
            header = f.read(1024)  # 读取前1KB足够判断
        
        header_str = header.decode('utf-8', errors='ignore').lower()
        
        # MHTML典型特征：
        # 1. 包含 "From: <Saved by Blink>" 或类似的邮件头
        # 2. 包含 multipart MIME boundary
        # 3. 可能包含 "Content-Type: multipart/related"
        if 'from:' in header_str and ('saved by blink' in header_str or 'multipart/' in header_str):
            logger.warning(
                "检测到文件内容实际为 MHTML，扩展名与内容不一致: "
                "suffix=%s file_name=%s",
                path.suffix,
                path.name,
            )
            return True
        
        # 额外的MIME边界检测
        if 'content-type: multipart/' in header_str and 'boundary=' in header_str:
            logger.warning("检测到 MIME multipart 内容，按 MHTML 文件处理: file_name=%s", path.name)
            return True
            
    except Exception:
        # 如果读取失败，保守起见不认为是MHTML
        pass
    
    return False


def extract_text_from_mhtml(file_path: str) -> str:
    """
    【保留】从 MHTML 中提取纯文本（降级方案）
    当 MHTML → PDF 转换失败时使用
    """
    message = BytesParser(policy=policy.default).parsebytes(Path(file_path).read_bytes())
    html_candidate = ""
    text_candidate = ""

    for part in message.walk():
        if part.is_multipart():
            continue

        content_type = part.get_content_type().lower()
        payload = part.get_payload(decode=True)
        if not payload:
            continue

        charset = part.get_content_charset() or "utf-8"
        try:
            text = payload.decode(charset, errors="ignore").strip()
        except LookupError:
            text = payload.decode("utf-8", errors="ignore").strip()

        if not text:
            continue

        if content_type == "text/html" and not html_candidate:
            html_candidate = text
        elif content_type == "text/plain" and not text_candidate:
            text_candidate = text

    if html_candidate:
        parser = _HTMLTextExtractor()
        parser.feed(html_candidate)
        parser.close()
        extracted = parser.get_text()
        if extracted:
            return extracted

    if text_candidate:
        return text_candidate

    raise ValueError("mhtml中未找到可用正文")


def normalize_mhtml_file(file_path: str, use_pdf_conversion: bool = True) -> str:
    """
    将 MHTML 文件标准化为可用于 LLM 处理的格式
    
    【新流程】优先使用 MHTML → PDF 转换，生成 .normalized.pdf
    【降级方案】如果 PDF 转换失败或禁用，则使用纯文本提取，生成 .normalized.md
    
    :param file_path: MHTML 文件路径
    :param use_pdf_conversion: 是否启用 PDF 转换（默认 True）
    :return: 标准化后的文件路径（.normalized.pdf 或 .normalized.md）
    """
    source = Path(file_path)
    
    # 【新流程】尝试使用 MHTML → PDF 转换
    if use_pdf_conversion and MHTML2PDF_AVAILABLE:
        try:
            logger.info("MHTML 标准化使用高质量流程：MHTML → PDF")
            
            # 生成 PDF 输出路径
            pdf_output_path = str(source.with_name(f"{source.name}.normalized.pdf"))
            
            # 调用 MHTML → PDF 转换器
            convert_mhtml_to_pdf(str(source), pdf_output_path)
            
            logger.info(
                "MHTML 标准化 PDF 已生成: output_file=%s",
                Path(pdf_output_path).name,
            )
            return pdf_output_path
            
        except Exception as e:
            logger.warning("MHTML 转 PDF 失败，准备切换降级流程: error_type=%s", type(e).__name__)
            logger.info("MHTML 标准化切换到纯文本提取模式")
    
    # 【降级方案】使用纯文本提取
    logger.info("MHTML 标准化使用降级流程：MHTML → 纯文本 MD")
    normalized_path = source.with_name(f"{source.name}.normalized.md")
    normalized_path.write_text(extract_text_from_mhtml(file_path) + "\n", encoding="utf-8")
    logger.info("MHTML 标准化 Markdown 已生成: output_file=%s", normalized_path.name)
    return str(normalized_path)


def normalize_file_for_llm(file_path: str, use_pdf_conversion: bool = True) -> str:
    """
    将文件标准化为适合 LLM 处理的格式
    
    :param file_path: 输入文件路径
    :param use_pdf_conversion: 对 MHTML 是否启用 PDF 转换（默认 True）
    :return: 标准化后的文件路径
    """
    if not is_mhtml_file(file_path):
        return file_path
    return normalize_mhtml_file(file_path, use_pdf_conversion=use_pdf_conversion)
