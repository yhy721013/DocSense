"""报告提交应用结果到已批准 HTTP 202/400/409 的框架无关 Presenter。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging

from app.modules.report.application import SubmitReportResult


logger = logging.getLogger(__name__)

_JSON_CONTENT_TYPE = "application/json; charset=utf-8"
_REPORT_CONFLICT_MESSAGE = "任务正在处理中"


def _error_body(message: str) -> bytes:
    if not isinstance(message, str):
        raise TypeError("message 必须是 str")
    normalized = message.strip()
    if not normalized:
        raise ValueError("message 不能为空")
    return json.dumps(
        {"error": normalized},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True)
class ReportSubmissionHttpPresentation:
    """Web Adapter 可机械转换为 Flask/FastAPI Response 的不可变值。"""

    status_code: int
    body: bytes
    content_type: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.status_code, bool)
            or not isinstance(self.status_code, int)
            or self.status_code < 100
            or self.status_code > 599
        ):
            raise ValueError("status_code 必须是合法 HTTP 状态码")
        if not isinstance(self.body, bytes):
            raise TypeError("body 必须是 bytes")
        if self.content_type is not None:
            if not isinstance(self.content_type, str) or not self.content_type.strip():
                raise ValueError("content_type 必须是非空 str 或 None")
            object.__setattr__(self, "content_type", self.content_type.strip())


class ReportSubmissionResponsePresenter:
    """隐藏内部 TaskId、通知结果和 Dispatcher 状态，只保留公开契约。"""

    def present_success(
        self,
        result: SubmitReportResult,
    ) -> ReportSubmissionHttpPresentation:
        if not isinstance(result, SubmitReportResult):
            raise TypeError("result 必须是 SubmitReportResult")
        logger.debug(
            "映射报告受理空响应: status_code=202 progress_notified=%s "
            "dispatcher_notified=%s",
            result.progress_notified,
            result.dispatcher_notified,
        )
        # 已确认契约要求严格零字节响应体；内部 task_id 不得泄漏给甲方。
        return ReportSubmissionHttpPresentation(status_code=202, body=b"")

    def present_conflict(self) -> ReportSubmissionHttpPresentation:
        logger.debug("映射报告活动任务冲突: status_code=409")
        return ReportSubmissionHttpPresentation(
            status_code=409,
            body=_error_body(_REPORT_CONFLICT_MESSAGE),
            content_type=_JSON_CONTENT_TYPE,
        )

    def present_bad_request(
        self,
        message: str,
    ) -> ReportSubmissionHttpPresentation:
        logger.debug("映射报告请求参数错误: status_code=400")
        return ReportSubmissionHttpPresentation(
            status_code=400,
            body=_error_body(message),
            content_type=_JSON_CONTENT_TYPE,
        )


__all__ = [
    "ReportSubmissionHttpPresentation",
    "ReportSubmissionResponsePresenter",
]
