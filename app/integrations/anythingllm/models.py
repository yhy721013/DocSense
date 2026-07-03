"""AnythingLLM 适配层内部使用的稳定数据模型与字段归一化规则。

AnythingLLM 不同接口和版本可能使用 ``id``/``docId``、``location``/``docpath``、
``slug``/``threadSlug`` 等不同字段名。本模块是这些供应商字段别名的唯一收敛点：原子
客户端把原始 JSON 转换为不可变 DTO 后，上层代码只读取统一属性，不得再次解析别名。

``document_ref`` 用于在上传结果和模型来源之间比较文档身份。归一化过程会统一 Unicode、
大小写、路径分隔符和 URL 编码，但不会使用模糊子串匹配，避免名称相近的文档被误判为
同一来源。
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import unquote, urlsplit

from app.integrations.anythingllm.errors import AnythingLLMProtocolError


def _protocol_error(message: str) -> AnythingLLMProtocolError:
    """构造不携带原始响应正文的适配层协议异常。"""
    return AnythingLLMProtocolError(message)


def require_mapping(value: Any, *, context: str) -> Mapping[str, Any]:
    """要求上游字段为对象，并在类型不符时抛出稳定协议异常。

    参数:
        value: 待校验的 JSON 解码结果。
        context: 用于异常消息的中文字段或接口描述。

    返回:
        保持原对象只读语义的 ``Mapping`` 视图。
    """
    if not isinstance(value, Mapping):
        raise _protocol_error(f"AnythingLLM {context} 必须是 JSON 对象")
    return value


def require_sequence(value: Any, *, context: str) -> Sequence[Any]:
    """要求上游字段为数组，同时排除字符串等伪序列。"""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise _protocol_error(f"AnythingLLM {context} 必须是 JSON 数组")
    return value


def first_text(payload: Mapping[str, Any], *keys: str) -> str:
    """按声明顺序返回第一个非空文本字段，并统一去除首尾空白。"""
    for key in keys:
        value = payload.get(key)
        if value is not None:
            normalized = str(value).strip()
            if normalized:
                return normalized
    return ""


def normalize_document_path(value: str) -> str:
    """把 AnythingLLM 文档位置规范化为可用于 API 请求的相对路径。

    该函数统一 Windows 与 POSIX 分隔符，并在输入包含 ``custom-documents`` 目录时丢弃
    其之前的宿主路径。它不猜测缺失位置，也不根据文件名构造 AnythingLLM 内部路径。
    """
    normalized = str(value or "").strip().replace("\\", "/")
    if not normalized:
        return ""
    lowered = normalized.casefold()
    marker = "custom-documents/"
    marker_index = lowered.find(marker)
    if marker_index >= 0:
        normalized = normalized[marker_index:]
    return normalized.lstrip("/")


def normalize_document_ref(
    value: str,
    *,
    document_id: str | None = None,
) -> str:
    """生成用于严格比较文档身份的规范化引用。

    AnythingLLM 上传后的 ``location`` 通常形如
    ``custom-documents/原文件名-文档ID.json``，模型来源则可能只返回原文件名、hotdir
    ``file://`` URL 或 ``sourceDocument``。因此稳定身份使用 location 中推导出的逻辑
    文件名，而不是供应商存储目录或宿主机绝对路径。

    归一化依次执行：百分号解码、Windows/POSIX 分隔符统一、URL 路径提取、Unicode
    NFKC、上传后缀 ``-文档ID.json`` 精确移除、首尾空白清理和大小写折叠。最终返回
    ``name:<规范文件名>``。这里使用完整文件名精确相等，不使用模糊子串匹配。

    返回空串表示没有足够信息生成身份，调用方必须按协议失败或来源缺失处理，不能自行
    猜测。``document_id`` 只用于移除与上传响应 ID 完全一致的后缀，不执行宽松 UUID
    猜测。
    """
    raw_value = str(value or "").strip()
    if not raw_value:
        return ""

    decoded_value = unquote(raw_value).replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", decoded_value):
        path_value = decoded_value
    else:
        parsed = urlsplit(decoded_value)
        if parsed.scheme.casefold() == "file":
            # Windows 常见的 file://C:/path 会把盘符解析为 netloc，必须与 path 重新合并。
            path_value = f"{parsed.netloc}{parsed.path}"
        elif parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
            path_value = parsed.path
        else:
            path_value = decoded_value

    normalized = unicodedata.normalize("NFKC", path_value)
    normalized = normalize_document_path(normalized).rstrip("/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    file_name = normalized.rsplit("/", 1)[-1].strip()
    if not file_name:
        return ""

    normalized_id = str(document_id or "").strip()
    if normalized_id:
        generated_suffix = f"-{normalized_id}.json"
        if file_name.casefold().endswith(generated_suffix.casefold()):
            file_name = file_name[: -len(generated_suffix)].rstrip()
        elif file_name.casefold() == f"{normalized_id}.json".casefold():
            file_name = normalized_id
    if not file_name:
        return ""
    return f"name:{file_name.casefold()}"


@dataclass(frozen=True)
class AnythingLLMDocument:
    """AnythingLLM 文档的统一表示。

    ``id`` 和 ``location`` 对上传结果均为必填字段；缺失时不能构造路径或继续嵌入。
    ``document_ref`` 基于真实 ``location`` 生成，供后续来源校验使用。
    """

    id: str
    location: str
    title: str
    document_ref: str

    @classmethod
    def from_payload(cls, value: Any) -> "AnythingLLMDocument":
        """从上传结果或工作区文档记录解析统一 DTO。"""
        payload = require_mapping(value, context="文档记录")
        document_id = first_text(payload, "id", "docId", "documentId")
        location = first_text(payload, "location", "docpath", "docPath")
        if not document_id:
            raise _protocol_error("AnythingLLM 文档记录缺少 id/docId")
        if not location:
            raise _protocol_error("AnythingLLM 文档记录缺少 location/docpath")

        normalized_location = normalize_document_path(location)
        explicit_title = first_text(payload, "title", "name", "filename", "fileName")
        title = explicit_title or normalized_location.rsplit("/", 1)[-1]
        document_ref = normalize_document_ref(
            normalized_location,
            document_id=document_id,
        )
        if document_ref == f"name:{document_id.casefold()}" and explicit_title:
            # 某些部署只返回 <id>.json，location 无法提供逻辑文件名，此时使用响应标题。
            document_ref = normalize_document_ref(explicit_title)
        if not document_ref:
            raise _protocol_error("AnythingLLM 文档位置无法生成稳定 document_ref")
        return cls(
            id=document_id,
            location=normalized_location,
            title=title,
            document_ref=document_ref,
        )


@dataclass(frozen=True)
class AnythingLLMWorkspace:
    """AnythingLLM 工作区的统一标识与显示名称。"""

    id: str
    slug: str
    name: str

    @classmethod
    def from_payload(cls, value: Any) -> "AnythingLLMWorkspace":
        """兼容工作区 ``slug``/``id`` 别名并构造统一 DTO。"""
        payload = require_mapping(value, context="工作区记录")
        slug = first_text(payload, "slug", "workspaceSlug", "id")
        workspace_id = first_text(payload, "id", "workspaceId", "slug")
        if not slug:
            raise _protocol_error("AnythingLLM 工作区记录缺少 slug/id")
        if not workspace_id:
            workspace_id = slug
        return cls(
            id=workspace_id,
            slug=slug,
            name=first_text(payload, "name", "workspaceName") or slug,
        )


@dataclass(frozen=True)
class AnythingLLMThread:
    """AnythingLLM 线程的统一标识。"""

    id: str
    slug: str

    @classmethod
    def from_payload(cls, value: Any) -> "AnythingLLMThread":
        """兼容线程 slug 的历史字段名并构造统一 DTO。"""
        payload = require_mapping(value, context="线程记录")
        slug = first_text(payload, "slug", "threadSlug", "thread_slug", "id")
        thread_id = first_text(payload, "id", "threadId", "slug")
        if not slug:
            raise _protocol_error("AnythingLLM 线程记录缺少 slug/id")
        return cls(id=thread_id or slug, slug=slug)


@dataclass(frozen=True)
class AnythingLLMSource:
    """AnythingLLM 模型回答所引用来源的统一表示。

    ``id``、``title``、``url``、``score``、``distance`` 和 ``metadata`` 均允许缺失，因为
    不同部署版本返回的来源字段并不一致。``document_ref`` 与 ``text`` 始终为字符串；
    无法识别身份时前者为空串，由 Gateway 决定本次回答不满足来源契约。

    ``metadata`` 与 ``distance`` 用于兼容向量检索接口；业务 Port 转换时只选择业务需要的
    稳定字段，不应把整个供应商 metadata 继续向业务层传播。
    """

    document_ref: str
    text: str
    id: Optional[str] = None
    title: Optional[str] = None
    url: Optional[str] = None
    score: Optional[float] = None
    distance: Optional[float] = None
    metadata: Optional[Mapping[str, Any]] = None

    @classmethod
    def from_payload(cls, value: Any) -> "AnythingLLMSource":
        """综合路径、元数据、URL 与 ID 字段解析一个来源。"""
        payload = require_mapping(value, context="来源记录")
        metadata_value = payload.get("metadata")
        metadata = metadata_value if isinstance(metadata_value, Mapping) else {}

        source_id = first_text(payload, "id", "docId", "documentId") or None
        title = (
            first_text(payload, "title", "name")
            or first_text(metadata, "title", "file_name", "fileName")
            or None
        )
        source_url = (
            first_text(payload, "url", "link")
            or first_text(metadata, "url", "link")
            or None
        )
        identity_candidates = (
            first_text(payload, "sourceDocument", "docpath", "docPath", "location"),
            first_text(metadata, "sourceDocument", "docpath", "location"),
            title or "",
            source_url or "",
            source_id or "",
        )
        document_ref = ""
        for identity in identity_candidates:
            document_ref = normalize_document_ref(identity, document_id=source_id)
            if document_ref:
                break
        text = first_text(payload, "text", "pageContent", "content", "chunk")

        raw_score = payload.get("score", metadata.get("score"))
        score: Optional[float]
        try:
            score = float(raw_score) if raw_score is not None else None
        except (TypeError, ValueError):
            score = None

        raw_distance = payload.get("distance", metadata.get("distance"))
        try:
            distance = float(raw_distance) if raw_distance is not None else None
        except (TypeError, ValueError):
            distance = None

        return cls(
            document_ref=document_ref,
            text=text,
            id=source_id,
            title=title,
            url=source_url,
            score=score,
            distance=distance,
            # 冻结 DTO 不能继续持有上游可变字典，浅拷贝后使用只读映射阻止意外改写。
            metadata=MappingProxyType(dict(metadata)) if metadata else None,
        )


@dataclass(frozen=True)
class AnythingLLMAnswer:
    """线程问答完成后的规范化文本、原始文本和来源集合。"""

    text: str
    raw_text: str
    sources: tuple[AnythingLLMSource, ...]


def json_text(value: Any) -> str:
    """把上游非字符串回答稳定序列化为紧凑 JSON 文本。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
