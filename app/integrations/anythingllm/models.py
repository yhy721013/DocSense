"""AnythingLLM 适配层内部使用的稳定数据模型与字段归一化规则。

AnythingLLM 不同接口和版本可能使用 ``id``/``docId``、``location``/``docpath``、
``slug``/``threadSlug`` 等不同字段名。本模块是这些供应商字段别名的唯一收敛点：原子
客户端把原始 JSON 转换为不可变 DTO 后，上层代码只读取统一属性，不得再次解析别名。

上传文档的 ``document_ref`` 根据真实 location 与文档 ID 生成，作为 Gateway 向业务层
交付的稳定引用。模型来源中的文件名、title、URL 和分片 ID 不具备同等可信度；新 RAG
链路只使用 Session 写入并由结构化 ``docSource`` 返回的随机 ``source_marker`` 证明来源
归属。基于展示字段生成的来源 ``document_ref`` 仅为 legacy Facade 兼容保留，禁止新
Gateway 将其作为授权或成功判定依据。
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


DOCSENSE_SOURCE_MARKER_PREFIX = "docsense_ref:"
"""DocSense 写入 AnythingLLM ``docSource`` 的会话级来源标记前缀。"""

_DOCSENSE_SOURCE_MARKER_PATTERN = re.compile(
    rf"^{re.escape(DOCSENSE_SOURCE_MARKER_PREFIX)}[0-9a-f]{{32}}$"
)
_DOCUMENT_UUID_SUFFIX_PATTERN = re.compile(
    r"(?i)([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?=\.json$|$)"
)
"""匹配 AnythingLLM 上传路径末尾的文档 UUID。

真实上传位置通常形如 ``custom-documents/<文件名>-<uuid>.json``。该 UUID 与上传接口
返回的全局文档 ID 保持一致，可信度高于工作区文档列表中可能出现的本地行 ID。
"""


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


def normalize_source_marker(value: Any) -> str:
    """严格解析 DocSense 自有的结构化来源标记。

    标记必须是 ``docsense_ref:`` 加 128 bit 小写十六进制随机值。这里不执行大小写转换、
    子串搜索或正文解析：只有 AnythingLLM source 的结构化 ``docSource`` 字段原样返回
    Session 写入的值时，Gateway 才能把该来源归属于目标文档。

    返回空串表示字段缺失、格式错误或属于其他系统。调用方必须按来源契约失败，不能回退
    到 title、URL、sourceDocument 或疑似 UUID 后缀猜测。
    """
    normalized = str(value or "").strip()
    if not _DOCSENSE_SOURCE_MARKER_PATTERN.fullmatch(normalized):
        return ""
    return normalized


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


def _path_value_from_reference(value: str) -> str:
    """把普通路径或 ``file://`` URL 转换为待规范化的路径文本。

    AnythingLLM 的不同接口可能返回普通相对路径、宿主机绝对路径、百分号编码路径或
    Windows 风格 ``file://C:/...`` URL。身份比较必须先消除这些表现形式差异，否则同一
    个文档会因为路径外观不同而被误判为未绑定。
    """
    decoded_value = unquote(str(value or "").strip()).replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", decoded_value):
        return decoded_value

    parsed = urlsplit(decoded_value)
    if parsed.scheme.casefold() == "file":
        # Windows 常见的 file://C:/path 会把盘符解析为 netloc，必须与 path 重新合并。
        return f"{parsed.netloc}{parsed.path}"
    if parsed.scheme or parsed.netloc or parsed.query or parsed.fragment:
        return parsed.path
    return decoded_value


def normalize_document_location_key(value: str) -> str:
    """生成用于可信身份比较的完整文档位置键。

    与 ``normalize_document_ref`` 不同，本函数不会把路径折叠成 ``name:<文件名>``，也
    不会移除上传 UUID 后缀。永久知识库转交、工作区绑定确认和补偿删除必须比较完整
    ``custom-documents/...`` 路径，才能区分同名但不同上传批次的全局文档。
    """
    raw_value = str(value or "").strip()
    if not raw_value:
        return ""

    normalized = unicodedata.normalize("NFKC", _path_value_from_reference(raw_value))
    normalized = normalize_document_path(normalized).rstrip("/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _document_id_from_location(value: str) -> str:
    """从完整上传位置中提取 AnythingLLM 全局文档 UUID。

    工作区详情接口中的 ``id``/``docId`` 在部分版本里表示工作区文档关联行，而不是全局
    上传文档。只要 ``location`` 携带上传 UUID，就应优先使用该 UUID 构造稳定
    ``document_ref``，避免把本地行 ID 写入业务协调记录。
    """
    location_key = normalize_document_location_key(value)
    file_name = location_key.rsplit("/", 1)[-1]
    matched = _DOCUMENT_UUID_SUFFIX_PATTERN.search(file_name)
    if not matched:
        return ""
    return matched.group(1).lower()


def normalize_document_ref(
    value: str,
    *,
    document_id: str | None = None,
) -> str:
    """生成 legacy Facade 使用的规范化展示引用。

    AnythingLLM 上传后的 ``location`` 通常形如
    ``custom-documents/原文件名-文档ID.json``，模型来源则可能只返回原文件名、hotdir
    ``file://`` URL 或 ``sourceDocument``。因此稳定身份使用 location 中推导出的逻辑
    文件名，而不是供应商存储目录或宿主机绝对路径。它只能帮助旧接口稳定展示和执行
    非安全性的精确文件名比较，不能证明 source 属于某次上传。

    归一化依次执行：百分号解码、Windows/POSIX 分隔符统一、URL 路径提取、Unicode
    NFKC、上传后缀 ``-文档ID.json`` 精确移除、首尾空白清理和大小写折叠。最终返回
    ``name:<规范文件名>``。这里使用完整文件名精确相等，不使用模糊子串匹配；但同名
    文档仍会产生相同值，所以新 Gateway 禁止使用该结果做可信来源判定。

    返回空串表示没有足够信息生成身份，调用方必须按协议失败或来源缺失处理，不能自行
    猜测。``document_id`` 只用于移除与上传响应 ID 完全一致的后缀，不执行宽松 UUID
    猜测。
    """
    raw_value = str(value or "").strip()
    if not raw_value:
        return ""

    normalized = normalize_document_location_key(raw_value)
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
    ``document_ref`` 基于真实上游文档 ID 生成，不使用 title 或逻辑文件名，因此同名上传
    仍具有不同引用。``location`` 继续作为绑定、Pin、删除和所有权转交的不透明外部位置。
    """

    id: str
    location: str
    title: str
    document_ref: str
    raw_document_id: str = ""
    identity_source: str = "payload_id"

    @classmethod
    def from_payload(cls, value: Any) -> "AnythingLLMDocument":
        """从上传结果或工作区文档记录解析统一 DTO。"""
        payload = require_mapping(value, context="文档记录")
        # Workspace 文档记录里的 ``id``/``docId`` 字段在不同 AnythingLLM 版本中含义不
        # 稳定：有的版本返回全局上传文档 ID，有的版本返回工作区关联行 ID。这里先按历
        # 史优先级读取一个“原始 ID”，随后如果 location 携带上传 UUID，则以路径 UUID
        # 作为最终稳定身份，避免阶段 9 的永久知识库转交把本地行 ID 误判为文档不一致。
        document_id = first_text(payload, "docId", "documentId", "id")
        location = first_text(payload, "location", "docpath", "docPath")
        if not document_id:
            raise _protocol_error("AnythingLLM 文档记录缺少 id/docId")
        if not location:
            raise _protocol_error("AnythingLLM 文档记录缺少 location/docpath")

        normalized_location = normalize_document_path(location)
        explicit_title = first_text(payload, "title", "name", "filename", "fileName")
        title = explicit_title or normalized_location.rsplit("/", 1)[-1]
        normalized_document_id = unicodedata.normalize("NFKC", document_id).strip()
        if not normalized_document_id:
            raise _protocol_error("AnythingLLM 文档 ID 无法生成稳定 document_ref")
        location_document_id = _document_id_from_location(normalized_location)
        stable_document_id = location_document_id or normalized_document_id
        document_ref = f"document:{stable_document_id}"
        return cls(
            id=stable_document_id,
            location=normalized_location,
            title=title,
            document_ref=document_ref,
            raw_document_id=document_id,
            identity_source="location_uuid" if location_document_id else "payload_id",
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

    ``source_marker`` 只从结构化 ``docSource``/metadata 字段提取，并且必须符合 DocSense
    随机标记格式；正文即使出现相同文本也不会被识别为标记。新 Gateway 只信任该字段，
    不信任 ``id``、``title``、``url`` 或 ``sourceDocument`` 的身份含义。

    ``document_ref`` 是迁移期 legacy Facade 与向量检索返回结构仍在使用的展示型引用。
    它可能由文件名、URL 或分片 ID 归一化而来，不能证明来源属于本次上传文档。该字段在
    阶段 11 删除兼容层时一并移除；任何新业务代码不得读取它执行来源校验。

    ``metadata`` 与 ``distance`` 用于兼容向量检索接口；业务 Port 转换时只选择业务需要的
    稳定字段，不应把整个供应商 metadata 继续向业务层传播。
    """

    document_ref: str
    text: str
    source_marker: Optional[str] = None
    id: Optional[str] = None
    title: Optional[str] = None
    url: Optional[str] = None
    score: Optional[float] = None
    distance: Optional[float] = None
    metadata: Optional[Mapping[str, Any]] = None

    @classmethod
    def from_payload(cls, value: Any) -> "AnythingLLMSource":
        """解析来源标记、展示字段与 legacy 引用，且不从正文推导身份。

        ``docSource`` 既可能位于来源顶层，也可能位于 ``metadata`` 中。两者都属于
        AnythingLLM 的结构化来源元数据，可以承载 Session 上传时写入的关联标记；
        ``text/pageContent`` 只作为证据片段处理，即使正文包含合法格式标记也必须忽略。
        """
        payload = require_mapping(value, context="来源记录")
        metadata_value = payload.get("metadata")
        metadata = metadata_value if isinstance(metadata_value, Mapping) else {}

        top_level_doc_source = first_text(payload, "docSource")
        metadata_doc_source = first_text(metadata, "docSource")
        doc_source_conflicts = (
            bool(top_level_doc_source)
            and bool(metadata_doc_source)
            and top_level_doc_source != metadata_doc_source
        )
        # 同一来源同时提供两个不同 docSource 时无法判断哪一个经历了真实向量化链路。
        # 必须让标记校验失败，不能按字段优先级静默选择其中一个。
        raw_doc_source = "" if doc_source_conflicts else (
            top_level_doc_source or metadata_doc_source
        )
        source_marker = normalize_source_marker(raw_doc_source) or None

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
            source_marker=source_marker,
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
