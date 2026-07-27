"""报告 HTML、回调载荷和任务级名称的纯领域规则。"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata

from .errors import ReportDomainValidationError
from .models import (
    REPORT_FAILURE_MESSAGE,
    REPORT_STATUS_FAILED,
    REPORT_STATUS_SUCCEEDED,
    REPORT_SUCCESS_MESSAGE,
    ReportCallbackPayload,
    ReportId,
    ReportResult,
)


EMPTY_REPORT_HTML = '<div class="report-content"><pre></pre></div>'
_REPORT_CONTEXT_NAME_MAX_CHARS = 96
_REPORT_CONVERSATION_NAME_MAX_CHARS = 64
_PUBLIC_SOURCE_NAME_MAX_CHARS = 255
_PUBLIC_SOURCE_NAME_MAX_UTF8_BYTES = 255
_PUBLIC_SOURCE_NAME_SAFE_PUNCTUATION = frozenset("._-")
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def _strict_percent_decode(value: str) -> str | None:
    """严格解码 URL path 段；畸形转义或非 UTF-8 字节按不可信名称处理。"""

    decoded = bytearray()
    cursor = 0
    try:
        while cursor < len(value):
            character = value[cursor]
            if character != "%":
                decoded.extend(character.encode("utf-8", errors="strict"))
                cursor += 1
                continue
            if (
                cursor + 2 >= len(value)
                or value[cursor + 1] not in _HEX_DIGITS
                or value[cursor + 2] not in _HEX_DIGITS
            ):
                return None
            decoded.append(int(value[cursor + 1 : cursor + 3], 16))
            cursor += 3
        return bytes(decoded).decode("utf-8", errors="strict")
    except (UnicodeDecodeError, UnicodeEncodeError, ValueError):
        return None


def _url_path_without_query_or_fragment(source_url: str) -> str:
    """只提取常规绝对/相对 URL 的 path，不把 authority 当作文件名。"""

    without_fragment = source_url.partition("#")[0]
    value = without_fragment.partition("?")[0]
    scheme_separator = value.find("://")
    if scheme_separator >= 0:
        authority_and_path = value[scheme_separator + 3 :]
        path_at = authority_and_path.find("/")
        return authority_and_path[path_at:] if path_at >= 0 else ""
    if value.startswith("//"):
        authority_and_path = value[2:]
        path_at = authority_and_path.find("/")
        return authority_and_path[path_at:] if path_at >= 0 else ""
    return value


def _is_safe_public_source_name(value: str) -> bool:
    """限制可进入任意 HTML 上下文的 URL basename，避免属性或协议注入。"""

    if (
        not value
        or value in {".", ".."}
        or value.strip() != value
        or len(value) > _PUBLIC_SOURCE_NAME_MAX_CHARS
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        return False
    for character in value:
        if character in _PUBLIC_SOURCE_NAME_SAFE_PUNCTUATION:
            continue
        if unicodedata.category(character)[:1] not in {"L", "M", "N"}:
            return False
    try:
        return len(value.encode("utf-8")) <= _PUBLIC_SOURCE_NAME_MAX_UTF8_BYTES
    except UnicodeEncodeError:
        return False


def _public_source_name(source_url: str, sequence_no: int) -> str:
    """从 URL path 提取业务文件名；query/fragment 永不参与展示名。"""

    fallback = f"来源文件{sequence_no}"
    if not isinstance(source_url, str):
        return fallback
    path = _url_path_without_query_or_fragment(source_url)
    encoded_basename = path.rsplit("/", 1)[-1]
    decoded_basename = _strict_percent_decode(encoded_basename)
    if decoded_basename is None or not _is_safe_public_source_name(decoded_basename):
        return fallback
    return decoded_basename


def _artifact_basename(artifact_id: str) -> str:
    """从不透明引用中兼容提取 POSIX/Windows 风格的精确末段。"""

    return artifact_id.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]


def sanitize_public_report_content(
    content: str | None,
    *,
    source_urls: tuple[str, ...],
    artifact_sources: tuple[tuple[str, int | None], ...],
) -> str | None:
    """把本次内部 RAG 标识替换为安全业务来源名。

    调用方必须先保存原始 RAG trace，再把本函数结果用于报告 Artifact 和 callback。
    """

    if content is None:
        return None
    if not isinstance(content, str):
        raise ReportDomainValidationError("报告 RAG 内容类型无效")
    if not isinstance(source_urls, tuple) or not isinstance(artifact_sources, tuple):
        raise ReportDomainValidationError("报告来源映射必须是 tuple")

    replacement_by_folded_token: dict[str, str] = {}
    exact_tokens: set[str] = set()
    for artifact_id, sequence_no in artifact_sources:
        if (
            not isinstance(artifact_id, str)
            or sequence_no is None
            or not isinstance(sequence_no, int)
            or isinstance(sequence_no, bool)
            or sequence_no <= 0
            or sequence_no > len(source_urls)
        ):
            raise ReportDomainValidationError("RAG Artifact 来源顺序超出任务输入范围")
        public_name = _public_source_name(
            source_urls[sequence_no - 1],
            sequence_no,
        )
        tokens = (artifact_id, _artifact_basename(artifact_id))
        for token in tokens:
            if not token:
                raise ReportDomainValidationError("RAG Artifact 内部标识无效")
            folded = token.casefold()
            previous = replacement_by_folded_token.get(folded)
            if previous is not None and previous != public_name:
                raise ReportDomainValidationError("RAG Artifact 展示名映射冲突")
            replacement_by_folded_token[folded] = public_name
            exact_tokens.add(token)

    if not exact_tokens or not content:
        return content

    pattern = re.compile(
        "|".join(
            re.escape(token)
            for token in sorted(exact_tokens, key=lambda value: (-len(value), value))
        ),
        flags=re.IGNORECASE,
    )

    def _replacement(match: re.Match[str]) -> str:
        replacement = replacement_by_folded_token.get(match.group(0).casefold())
        if replacement is None:
            # Unicode IGNORECASE 的少数扩展等价字符不得绕过 casefold 冲突门禁。
            raise ReportDomainValidationError("RAG Artifact 展示名匹配不确定")
        return replacement

    return pattern.sub(_replacement, content)


def ensure_report_html(content: str | None) -> str:
    """保持旧报告服务的 HTML 规范化语义。

    已经首尾呈现为 HTML 标签的内容只去除首尾空白；普通文本进行 HTML 转义后放入
    ``pre``。``None``、空字符串和纯空白内容均得到稳定空 HTML，并按负责人确认继续走
    成功语义。该函数不记录日志，避免纯领域层依赖日志设施；调用方使用
    :attr:`ReportResult.empty_rag_result` 统一记录可观测信号。
    """

    if content is not None and not isinstance(content, str):
        raise ReportDomainValidationError("content 必须是 str 或 None")
    text = (content or "").strip()
    if text.startswith("<") and text.endswith(">"):
        return text
    return f'<div class="report-content"><pre>{html.escape(text)}</pre></div>'


def build_report_result(
    report_id: ReportId,
    content: str | None,
) -> ReportResult:
    """把一次 RAG 文本转换为可审计的强类型报告结果。"""

    if not isinstance(report_id, ReportId):
        raise ReportDomainValidationError("report_id 必须是 ReportId")
    if content is not None and not isinstance(content, str):
        raise ReportDomainValidationError("content 必须是 str 或 None")
    raw_content = content or ""
    return ReportResult(
        report_id=report_id,
        raw_content=raw_content,
        html_details=ensure_report_html(raw_content),
    )


def build_report_callback(
    report_id: ReportId,
    details: str,
    *,
    status: str,
) -> ReportCallbackPayload:
    """构造字段和消息固定的报告终态回调。"""

    if status == REPORT_STATUS_SUCCEEDED:
        message = REPORT_SUCCESS_MESSAGE
    elif status == REPORT_STATUS_FAILED:
        message = REPORT_FAILURE_MESSAGE
    else:
        raise ReportDomainValidationError("status 只能是 1 或 2")
    return ReportCallbackPayload(
        report_id=report_id,
        status=status,
        details=details,
        message=message,
    )


def build_report_context_name(
    report_id: ReportId,
    execution_suffix: str,
) -> str:
    """生成一次报告执行独占的供应商无关 Context 名称。

    兼容路径暂时传入毫秒时间戳；后续 ``RunReportTask(task_id)`` 必须传入不可变执行身份，
    从而避免并发任务共享可变外部资源。名称只描述业务意图，不暴露 workspace slug。
    """

    if not isinstance(report_id, ReportId):
        raise ReportDomainValidationError("report_id 必须是 ReportId")
    if not isinstance(execution_suffix, str) or not execution_suffix.strip():
        raise ReportDomainValidationError("execution_suffix 必须是非空 str")
    normalized_suffix = execution_suffix.strip()
    plain_name = f"llm-report-{report_id.business_key}-{normalized_suffix}"
    if len(plain_name) <= _REPORT_CONTEXT_NAME_MAX_CHARS:
        return plain_name
    # reportId 允许 128 位十进制数字，若直接拼入 Workspace 名称，会把供应商字段长度
    # 风险扩散到 Adapter。超限时仅压缩内部名称；数据库业务键、公开回调和接口值仍保留
    # 完整十进制文本。两个独立摘要同时覆盖业务键与 execution，维持任务级隔离。
    report_digest = hashlib.sha256(
        report_id.business_key.encode("utf-8")
    ).hexdigest()[:24]
    execution_digest = hashlib.sha256(
        normalized_suffix.encode("utf-8")
    ).hexdigest()[:32]
    return f"llm-report-{report_digest}-{execution_digest}"


def build_report_conversation_name(report_id: ReportId) -> str:
    """生成报告 Context 内保持兼容的 Conversation 名称。"""

    if not isinstance(report_id, ReportId):
        raise ReportDomainValidationError("report_id 必须是 ReportId")
    plain_name = f"report-{report_id.business_key}"
    if len(plain_name) <= _REPORT_CONVERSATION_NAME_MAX_CHARS:
        return plain_name
    report_digest = hashlib.sha256(
        report_id.business_key.encode("utf-8")
    ).hexdigest()[:32]
    return f"report-{report_digest}"


def build_report_prompt(
    *,
    template_desc: str,
    template_outline: str,
    requirement: str,
) -> str:
    """构建与遗留 ``services.core.prompts`` 精确等价的报告 Prompt。

    Application 只传入已经冻结的业务文本和模板提取结果，不再把可变请求字典交给
    Prompt 层。完整 Prompt 可能包含业务信息，调用方不得把返回值写入普通日志。
    """

    for name, value in (
        ("template_desc", template_desc),
        ("template_outline", template_outline),
        ("requirement", requirement),
    ):
        if not isinstance(value, str):
            raise ReportDomainValidationError(f"{name} 必须是 str")
    return (
        "请基于提供的全部文件内容生成 HTML 报告片段。\n"
        f"模板说明：{template_desc}\n"
        f"模板大纲：{template_outline}\n"
        f"业务需求：{requirement}\n"
        "输出必须可直接嵌入页面，不要附加 Markdown 代码块。\n"
    )


__all__ = [
    "EMPTY_REPORT_HTML",
    "build_report_callback",
    "build_report_context_name",
    "build_report_conversation_name",
    "build_report_prompt",
    "build_report_result",
    "ensure_report_html",
]
