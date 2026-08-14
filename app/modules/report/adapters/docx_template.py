"""Report DOCX 模板正文提取 Adapter。"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree


_WORD_XML_TEXT_PARTS = ("word/document.xml",)
_WORD_XML_EXTRA_PREFIXES = ("word/header", "word/footer")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[1] if "}" in tag else tag


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        name = _local_name(node.tag)
        if name == "t" and node.text:
            parts.append(node.text)
        elif name == "tab":
            parts.append("\t")
        elif name in {"br", "cr"}:
            parts.append("\n")
    text = "".join(parts)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    return text.strip()


def _extract_xml_text(xml_bytes: bytes) -> list[str]:
    root = ElementTree.fromstring(xml_bytes)
    lines: list[str] = []
    for paragraph in root.iter():
        if _local_name(paragraph.tag) == "p":
            text = _paragraph_text(paragraph)
            if text:
                lines.append(text)
    return lines


def extract_docx_template_text(file_path: str) -> str:
    """提取 DOCX 正文、页眉和页脚文本，保持旧提取器的顺序与错误类型。"""

    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Word模板不存在: {file_path}")
    if not zipfile.is_zipfile(path):
        raise ValueError(f"不支持的Word模板格式，仅支持docx: {file_path}")

    lines: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        xml_names = [name for name in _WORD_XML_TEXT_PARTS if name in names]
        xml_names.extend(
            sorted(
                name
                for name in names
                if name.endswith(".xml")
                and any(name.startswith(prefix) for prefix in _WORD_XML_EXTRA_PREFIXES)
            )
        )
        if not xml_names:
            raise ValueError(f"Word模板缺少正文XML: {file_path}")
        for xml_name in xml_names:
            try:
                lines.extend(_extract_xml_text(archive.read(xml_name)))
            except ElementTree.ParseError as exc:
                raise ValueError(f"Word模板XML解析失败: {xml_name}") from exc
    return "\n".join(lines).strip()


__all__ = ["extract_docx_template_text"]
