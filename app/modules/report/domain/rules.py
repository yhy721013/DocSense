"""报告 HTML、回调载荷和任务级名称的纯领域规则。"""

from __future__ import annotations

import hashlib
import html

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
