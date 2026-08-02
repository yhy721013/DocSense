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
    """可直接投影到 SSE 与历史的无损来源值。"""

    content: str
    file_name: str
    original_file_name: str


class ChatSourceMapper:
    """仅按 ``structured_source_key`` 一对一映射来源，保持上游顺序和正文原值。"""

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
            # 正文必须无损保留；只用 strip 判定“全空白”，绝不把 strip 后的值写入结果。
            if not source.content.strip():
                raise ChatSourceMappingError(f"来源正文为空: position={index}")
            source_key = source.structured_source_key
            if not source_key or source_key.strip() != source_key:
                raise ChatSourceMappingError(f"来源结构化键无效: position={index}")
            document = documents_by_key.get(source_key)
            if document is None:
                raise ChatSourceMappingError(f"来源不属于冻结范围: position={index}")
            mapped.append(
                MappedChatSource(
                    content=source.content,
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
]
