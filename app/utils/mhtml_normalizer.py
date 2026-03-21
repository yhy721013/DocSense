from __future__ import annotations

import re
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from pathlib import Path


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
    return Path(file_path).suffix.lower() in {".mhtml", ".mht"}


def extract_text_from_mhtml(file_path: str) -> str:
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


def normalize_mhtml_file(file_path: str) -> str:
    source = Path(file_path)
    normalized_path = source.with_name(f"{source.name}.normalized.md")
    normalized_path.write_text(extract_text_from_mhtml(file_path) + "\n", encoding="utf-8")
    return str(normalized_path)


def normalize_file_for_llm(file_path: str) -> str:
    if not is_mhtml_file(file_path):
        return file_path
    return normalize_mhtml_file(file_path)
