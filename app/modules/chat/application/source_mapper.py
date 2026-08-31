"""把供应商无关来源终态严格映射为知识谱系公开 Chunk。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.modules.chat.ports.conversations import ChatSourceEvidence


class ChatSourceMappingError(ValueError):
    """来源无法按冻结范围唯一归属时抛出。

    这是安全边界异常：调用方必须令整轮失败，禁止丢弃坏来源后返回部分 Chunk，也禁止
    回退到文件名、标题、URL 或正文模糊匹配。
    """


_DOCUMENT_METADATA_OPENING = "<document_metadata>"
_DOCUMENT_METADATA_OPENING_NAME = "<document_metadata"
_DOCUMENT_METADATA_CLOSING = "</document_metadata>"


def _metadata_opening_end(value: str) -> int | None:
    """返回前置开始标签结束位置，并对疑似格式漂移失败关闭。"""

    cursor = 1 if value.startswith("\ufeff") else 0
    while cursor < len(value) and value[cursor] in " \t\r\n":
        cursor += 1
    name_end = cursor + len(_DOCUMENT_METADATA_OPENING_NAME)
    if (
        value[cursor:name_end].lower()
        != _DOCUMENT_METADATA_OPENING_NAME
    ):
        return None
    opening_end = cursor + len(_DOCUMENT_METADATA_OPENING)
    if value[cursor:opening_end].lower() != _DOCUMENT_METADATA_OPENING:
        raise ChatSourceMappingError("来源 Metadata 开始标签格式非法")
    return opening_end


def _metadata_closing_end(value: str, *, start: int) -> int | None:
    """用固定 ASCII 标签扫描闭合位置，避免 Application 层引入正则依赖。"""

    last_start = len(value) - len(_DOCUMENT_METADATA_CLOSING)
    for cursor in range(start, last_start + 1):
        closing_end = cursor + len(_DOCUMENT_METADATA_CLOSING)
        if value[cursor:closing_end].lower() == _DOCUMENT_METADATA_CLOSING:
            return closing_end
    return None


def sanitize_weaponry_source_content(value: str) -> str:
    """删除知识谱系来源开头完整的 AnythingLLM Metadata 包装。

    本函数是无 I/O、无共享状态的纯业务规则，只处理正文有效开头的供应商包装。没有
    Metadata 的正文会按原值返回；删除包装后，正文自身的换行、Unicode 码点、缩进和尾部
    空白都不会被规范化。若检测到未闭合、带未知属性或连续两层的前置包装，则失败关闭，
    避免把无法安全解释的供应商内部信息公开给前端。
    """

    if not isinstance(value, str):
        raise TypeError("来源正文必须是 str")

    opening_end = _metadata_opening_end(value)
    if opening_end is None:
        return value

    closing_end = _metadata_closing_end(value, start=opening_end)
    if closing_end is None:
        raise ChatSourceMappingError("来源 Metadata 缺少闭合标签")

    # 只删除闭合标签之后由空白字符组成的分隔行。若第一条正文行带缩进，循环会在发现
    # 非换行字符时停在该行起点，从而完整保留正文缩进。
    body = value[closing_end:]
    body_start = 0
    while body_start < len(body):
        line_cursor = body_start
        while line_cursor < len(body) and body[line_cursor] in " \t":
            line_cursor += 1
        if body.startswith("\r\n", line_cursor):
            body_start = line_cursor + 2
            continue
        if line_cursor < len(body) and body[line_cursor] in "\r\n":
            body_start = line_cursor + 1
            continue
        break

    sanitized = body[body_start:]
    if _metadata_opening_end(sanitized) is not None:
        raise ChatSourceMappingError("来源包含连续前置 Metadata 包装")
    return sanitized


@dataclass(frozen=True)
class ChatSourceDocument:
    """SourceMapper 所需的最小冻结文档身份。"""

    file_name: str
    original_file_name: str
    structured_source_key: str

    def __post_init__(self) -> None:
        for name in ("file_name", "original_file_name", "structured_source_key"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(f"{name} must be str")
            if not value or value.strip() != value:
                raise ValueError(f"{name} must be non-empty normalized text")


@dataclass(frozen=True)
class MappedChatSource:
    """可直接投影到 SSE 与历史的已清洗来源值。"""

    content: str
    file_name: str
    original_file_name: str


class ChatSourceMapper:
    """按结构化键一对一映射来源，并清除 Weaponry 公开正文的供应商包装。"""

    @staticmethod
    def map_sources(
        sources: Sequence[ChatSourceEvidence],
        documents: Sequence[ChatSourceDocument],
    ) -> tuple[MappedChatSource, ...]:
        source_items = tuple(sources)
        if not source_items:
            return ()

        documents_by_key: dict[str, ChatSourceDocument] = {}
        for document in documents:
            if not isinstance(document, ChatSourceDocument):
                raise TypeError("documents must contain ChatSourceDocument")
            if document.structured_source_key in documents_by_key:
                raise ChatSourceMappingError("冻结范围包含重复结构化来源键")
            documents_by_key[document.structured_source_key] = document

        mapped: list[MappedChatSource] = []
        for index, source in enumerate(source_items):
            if not isinstance(source, ChatSourceEvidence):
                raise TypeError("sources must contain ChatSourceEvidence")
            # 先删除已批准的前置供应商包装，再用 strip 仅判定清洗后的业务正文是否全空白；
            # 绝不把 strip 后的值写入结果，确保实际正文的缩进、换行和 Unicode 保持原样。
            sanitized_content = sanitize_weaponry_source_content(source.content)
            if not sanitized_content.strip():
                raise ChatSourceMappingError(f"来源正文为空: position={index}")
            source_key = source.structured_source_key
            if not source_key or source_key.strip() != source_key:
                raise ChatSourceMappingError(f"来源结构化键无效: position={index}")
            document = documents_by_key.get(source_key)
            if document is None:
                raise ChatSourceMappingError(f"来源不属于冻结范围: position={index}")
            mapped.append(
                MappedChatSource(
                    content=sanitized_content,
                    file_name=document.file_name,
                    original_file_name=document.original_file_name,
                )
            )
        return tuple(mapped)


__all__ = [
    "ChatSourceDocument",
    "ChatSourceMapper",
    "ChatSourceMappingError",
    "MappedChatSource",
    "sanitize_weaponry_source_content",
]
