"""MHTML MIME 识别与正文提取纯规则。

输入只包含文件名和不可变字节，不读取路径、不启动浏览器，也不写文件。普通正文保留
页面语义；检索正文优先 main/article，并去除导航、页脚、引用与重复行。
"""

from __future__ import annotations

import re
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser


_BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "body", "div", "dt",
    "dd", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
    "h4", "h5", "h6", "header", "li", "main", "nav", "p", "section",
    "table", "td", "th", "title", "tr", "ul", "ol",
}
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
_GENERAL_SKIP = {"script", "style", "noscript"}
_RETRIEVAL_SKIP = _GENERAL_SKIP | {
    "aside", "canvas", "footer", "form", "nav", "svg", "template",
}
_NOISE_TOKENS = {
    "advertisement", "catlinks", "comments", "mw-editsection",
    "mw-references-wrap", "navbox", "navigation", "printfooter",
    "references", "reflist", "related-posts", "share-tools", "sidebar",
    "site-footer", "table-of-contents", "toc",
}


def is_mhtml_content(*, file_name: str, header: bytes) -> bool:
    """按既有扩展名优先、MIME 头补充的规则识别网页归档。"""

    suffix = "." + file_name.rsplit(".", 1)[-1].casefold() if "." in file_name else ""
    if suffix in {".mhtml", ".mht"}:
        return True
    decoded = header[:1024].decode("utf-8", errors="ignore").casefold()
    return (
        ("from:" in decoded and ("saved by blink" in decoded or "multipart/" in decoded))
        or ("content-type: multipart/" in decoded and "boundary=" in decoded)
    )


def _candidates(payload: bytes) -> tuple[str, str]:
    message = BytesParser(policy=policy.default).parsebytes(payload)
    html = ""
    plain = ""
    for part in message.walk():
        if part.is_multipart():
            continue
        body = part.get_payload(decode=True)
        if not body:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            text = body.decode(charset, errors="ignore").strip()
        except LookupError:
            text = body.decode("utf-8", errors="ignore").strip()
        if not text:
            continue
        content_type = part.get_content_type().casefold()
        if content_type == "text/html" and not html:
            html = text
        elif content_type == "text/plain" and not plain:
            plain = text
    return html, plain


class _TextExtractor(HTMLParser):
    def __init__(self, *, retrieval: bool) -> None:
        super().__init__(convert_charrefs=True)
        self._retrieval = retrieval
        self._all: list[str] = []
        self._primary: list[str] = []
        self._skip_depth = 0
        self._primary_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # type: ignore[override]
        normalized = tag.casefold()
        is_void = normalized in _VOID_TAGS
        if self._skip_depth:
            if not is_void:
                self._skip_depth += 1
            return
        if self._is_skipped(normalized, attrs):
            if not is_void:
                self._skip_depth = 1
            return
        primary = self._is_primary(normalized, attrs)
        if self._primary_depth and not is_void:
            self._primary_depth += 1
        elif primary and not is_void:
            self._primary_depth = 1
        if normalized in _BLOCK_TAGS or normalized in {"br", "hr"}:
            self._newline(self._all)
            if self._primary_depth:
                self._newline(self._primary)

    def handle_startendtag(self, tag: str, attrs) -> None:  # type: ignore[override]
        normalized = tag.casefold()
        if not self._skip_depth and not self._is_skipped(normalized, attrs):
            if normalized in _BLOCK_TAGS or normalized in {"br", "hr"}:
                self._newline(self._all)
                if self._primary_depth:
                    self._newline(self._primary)

    def handle_endtag(self, tag: str) -> None:  # type: ignore[override]
        normalized = tag.casefold()
        if normalized in _VOID_TAGS:
            return
        if self._skip_depth:
            self._skip_depth -= 1
            return
        if normalized in _BLOCK_TAGS or normalized in {"br", "hr"}:
            self._newline(self._all)
            if self._primary_depth:
                self._newline(self._primary)
        if self._primary_depth:
            self._primary_depth -= 1

    def handle_data(self, data: str) -> None:  # type: ignore[override]
        if self._skip_depth:
            return
        text = re.sub(r"\s+", " ", data).strip()
        if not text:
            return
        self._append(self._all, text)
        if self._primary_depth:
            self._append(self._primary, text)

    def text(self) -> str:
        all_text = self._compact(self._all, deduplicate=self._retrieval)
        primary = self._compact(self._primary, deduplicate=True)
        if self._retrieval and len(primary) >= 200:
            return primary
        return all_text

    def _is_skipped(self, tag: str, attrs) -> bool:
        if tag in (_RETRIEVAL_SKIP if self._retrieval else _GENERAL_SKIP):
            return True
        if not self._retrieval:
            return False
        identity = " ".join(
            str(value or "").casefold()
            for name, value in attrs
            if str(name or "").casefold() in {"class", "id", "role", "aria-label"}
        )
        return bool(set(re.findall(r"[a-z0-9_-]+", identity)) & _NOISE_TOKENS)

    @staticmethod
    def _is_primary(tag: str, attrs) -> bool:
        return tag in {"article", "main"} or any(
            str(name or "").casefold() == "role"
            and str(value or "").casefold() == "main"
            for name, value in attrs
        )

    @staticmethod
    def _append(chunks: list[str], text: str) -> None:
        if chunks and not chunks[-1].endswith(("\n", " ")):
            chunks.append(" ")
        chunks.append(text)

    @staticmethod
    def _newline(chunks: list[str]) -> None:
        if chunks and not chunks[-1].endswith("\n"):
            chunks.append("\n")

    @staticmethod
    def _compact(chunks: list[str], *, deduplicate: bool) -> str:
        lines = [
            re.sub(r"\s+", " ", line).strip()
            for line in "".join(chunks).splitlines()
        ]
        result: list[str] = []
        seen: set[str] = set()
        for line in lines:
            key = line.casefold()
            if not line or (deduplicate and key in seen):
                continue
            seen.add(key)
            result.append(line)
        return "\n".join(result).strip()


def extract_mhtml_text(payload: bytes, *, retrieval: bool = False) -> str:
    """提取普通或检索正文；空正文严格失败。"""

    if not isinstance(payload, bytes):
        raise TypeError("payload 必须是 bytes")
    html, plain = _candidates(payload)
    if html:
        parser = _TextExtractor(retrieval=retrieval)
        parser.feed(html)
        parser.close()
        extracted = parser.text()
        if extracted:
            return extracted
    if plain:
        compact = "\n".join(
            line
            for line in (
                re.sub(r"\s+", " ", item).strip()
                for item in plain.splitlines()
            )
            if line
        )
        if compact:
            return compact
    raise ValueError("mhtml中未找到可用正文")


__all__ = ["extract_mhtml_text", "is_mhtml_content"]
