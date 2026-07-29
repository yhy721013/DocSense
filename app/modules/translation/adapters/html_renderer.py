"""安全、确定性且保留 Markdown 结构的单语/双语 HTML Renderer。"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Sequence
from urllib.parse import urlsplit

import markdown
from bs4 import BeautifulSoup, NavigableString

from app.modules.translation.domain import (
    RenderedTranslation,
    TranslationRequest,
    TranslationUnit,
    split_translation_units,
)


HTML_RENDERER_ID = "safe-structured-translation-html"
HTML_RENDERER_FINGERPRINT = "docsense-translation-html-v2"
_PLACEHOLDER_PREFIX = "DOCSENSE_TRANSLATION_UNIT_"
_RAW_HTML = re.compile(
    r"<!--.*?-->|</?[A-Za-z][^>]*>",
    flags=re.DOTALL,
)
_SKIPPED_TEXT_PARENTS = frozenset({"code", "pre", "script", "style"})
_HEADER = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Translated Document</title>
<style>
body { margin: 0; padding: 20px; background: #f5f5f5; font-family: sans-serif; line-height: 1.6; }
.document-container { max-width: 900px; margin: 0 auto; background: white; padding: 40px; }
.paragraph { margin: 0 0 18px; white-space: pre-wrap; overflow-wrap: anywhere; }
.original-text { color: #333; }
.translated-text { color: #0066cc; }
.translation-pair > .original-text,
.translation-pair > .translated-text { display: block; }
.translation-pair > .translated-text { border-top: 1px dashed #e0e0e0; margin-top: 8px; padding-top: 8px; }
img { max-width: 100%; height: auto; }
pre { overflow-x: auto; padding: 12px; background: #f3f3f3; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #ddd; padding: 8px; }
</style>
</head>
<body>
<div class="document-container">
"""
_FOOTER = "</div>\n</body>\n</html>\n"


@dataclass(frozen=True, slots=True)
class _StructuredTemplate:
    """一次纯函数解析的 HTML 模板和有序文本节点。"""

    body_html: str
    source_units: tuple[str, ...]


class SafeHTMLTranslationRendererAdapter:
    """保留 Markdown 结构，只让 TranslationEngine 接触可翻译文本节点。

    Renderer 每次都从 ``source_text`` 重新构造模板，不缓存 BeautifulSoup 或节点对象，
    因而可被多个任务并发复用。原始 HTML 标签先转义，生成后的 URL 属性再执行协议
    白名单，避免通过 Markdown 原文注入脚本。
    """

    @property
    def renderer_id(self) -> str:
        return HTML_RENDERER_ID

    @property
    def renderer_fingerprint(self) -> str:
        return HTML_RENDERER_FINGERPRINT

    def extract_units(
        self,
        *,
        request: TranslationRequest,
        source_text: str,
    ) -> Sequence[str]:
        return self._build_template(request, source_text).source_units

    def render(
        self,
        *,
        request: TranslationRequest,
        source_text: str,
        units: Sequence[TranslationUnit],
    ) -> RenderedTranslation:
        template = self._build_template(request, source_text)
        normalized_units = tuple(units)
        if not normalized_units:
            raise ValueError("Renderer 至少需要一个 TranslationUnit")
        if tuple(item.source_text for item in normalized_units) != template.source_units:
            raise ValueError("TranslationUnit 与源文档结构不一致")

        bilingual_body = template.body_html
        monolingual_body = template.body_html
        for unit in normalized_units:
            placeholder = self._placeholder(unit.ordinal)
            source = html.escape(unit.source_text, quote=True)
            translated = html.escape(unit.translated_text, quote=True)
            if unit.translated and unit.translated_text != unit.source_text:
                bilingual = (
                    '<span class="translation-pair">'
                    f'<span class="original-text">{source}</span>'
                    f'<span class="translated-text">{translated}</span>'
                    "</span>"
                )
                monolingual = (
                    f'<span class="translated-text">{translated}</span>'
                )
            else:
                failure_attribute = (
                    ' data-translation-failed="true"' if unit.failed else ""
                )
                bilingual = (
                    f'<span class="original-text"{failure_attribute}>'
                    f"{source}</span>"
                )
                monolingual = bilingual
            if placeholder not in bilingual_body or placeholder not in monolingual_body:
                raise ValueError("Renderer 模板缺少 TranslationUnit 占位符")
            bilingual_body = bilingual_body.replace(placeholder, bilingual, 1)
            monolingual_body = monolingual_body.replace(
                placeholder,
                monolingual,
                1,
            )

        return RenderedTranslation(
            bilingual_html=f"{_HEADER}{bilingual_body}{_FOOTER}",
            monolingual_html=f"{_HEADER}{monolingual_body}{_FOOTER}",
        )

    def _build_template(
        self,
        request: TranslationRequest,
        source_text: str,
    ) -> _StructuredTemplate:
        if not isinstance(request, TranslationRequest):
            raise TypeError("request 必须是 TranslationRequest")
        if not isinstance(source_text, str):
            raise TypeError("source_text 必须是 str")
        representation = request.prepared_artifact.representation
        if representation.value == "text":
            units = split_translation_units(source_text)
            body = "".join(
                '<div class="paragraph">'
                f"{self._placeholder(index)}</div>\n"
                for index in range(1, len(units) + 1)
            )
            return _StructuredTemplate(body, units)
        if representation.value != "markdown":
            raise ValueError("Renderer 只接受 Markdown/Text Artifact")

        escaped_source = _RAW_HTML.sub(
            lambda match: html.escape(match.group(0), quote=True),
            source_text,
        )
        body = markdown.markdown(
            escaped_source,
            extensions=("extra", "codehilite", "nl2br", "sane_lists"),
            output_format="html5",
        )
        soup = BeautifulSoup(body, "html.parser")
        self._sanitize_generated_html(soup)
        source_units: list[str] = []
        for node in tuple(soup.find_all(string=True)):
            if not isinstance(node, NavigableString):
                continue
            parent_name = node.parent.name.casefold() if node.parent and node.parent.name else ""
            raw = str(node)
            source = raw.strip()
            if not source or parent_name in _SKIPPED_TEXT_PARENTS:
                continue
            source_units.append(source)
            prefix_length = len(raw) - len(raw.lstrip())
            suffix_length = len(raw) - len(raw.rstrip())
            prefix = raw[:prefix_length]
            suffix = raw[len(raw) - suffix_length:] if suffix_length else ""
            node.replace_with(
                NavigableString(
                    f"{prefix}{self._placeholder(len(source_units))}{suffix}"
                )
            )
        return _StructuredTemplate(str(soup), tuple(source_units))

    @staticmethod
    def _sanitize_generated_html(soup: BeautifulSoup) -> None:
        """限制 Markdown 生成 URL 的协议，并移除潜在事件属性。"""

        for tag in soup.find_all(True):
            for attribute in tuple(tag.attrs):
                if attribute.casefold().startswith("on"):
                    del tag.attrs[attribute]
            for attribute in ("href", "src"):
                value = tag.attrs.get(attribute)
                if not isinstance(value, str):
                    continue
                if not SafeHTMLTranslationRendererAdapter._safe_url(
                    value,
                    image=attribute == "src",
                ):
                    del tag.attrs[attribute]

    @staticmethod
    def _safe_url(value: str, *, image: bool) -> bool:
        normalized = value.strip()
        if not normalized:
            return False
        if normalized.startswith(("#", "/", "./", "../")):
            return True
        lowered = normalized.casefold()
        if image and lowered.startswith("data:image/"):
            return ";base64," in lowered
        scheme = urlsplit(normalized).scheme.casefold()
        return scheme in {"http", "https", "mailto"}

    @staticmethod
    def _placeholder(ordinal: int) -> str:
        return f"{_PLACEHOLDER_PREFIX}{ordinal:08d}"


__all__ = [
    "HTML_RENDERER_FINGERPRINT",
    "HTML_RENDERER_ID",
    "SafeHTMLTranslationRendererAdapter",
]
