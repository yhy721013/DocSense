"""文件分析受理结果的 HTTP Presenter。

Presenter 只把 Application 的有限结果映射为已冻结 HTTP 形状；它不生成 Flask 响应、不写
数据库，也不吞掉未知异常。框架边界负责把其值转换为 Flask Response，不能附加内部身份。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.adapters.web.flask.analysis_requests import AnalysisRequestValidationError
from app.modules.analysis.ports.batch_commands import (
    AnalysisBatchAdmission,
    AnalysisBatchAdmissionOutcome,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnalysisPresentedResponse:
    """框架无关的 HTTP 呈现结果；``body=None`` 表示严格空响应。"""

    status_code: int
    body: dict[str, str] | None

    def __post_init__(self) -> None:
        if self.status_code not in {202, 400, 409, 413, 503}:
            raise ValueError("status_code 不属于已冻结的文件分析可呈现状态")
        if self.body is not None:
            if set(self.body) != {"error"} or not isinstance(self.body["error"], str):
                raise TypeError("错误响应只能包含字符串 error 字段")
        if self.status_code == 202 and self.body is not None:
            raise ValueError("202 必须是严格空响应")
        if self.status_code != 202 and self.body is None:
            raise ValueError("错误状态必须携带 error 响应体")


class AnalysisSubmissionResponsePresenter:
    """集中锁定 202/409/503 与验证错误的既有输出，不暴露内部 execution 身份。"""

    def present_validation_error(
        self,
        error: AnalysisRequestValidationError,
    ) -> AnalysisPresentedResponse:
        """把 Parser 已分类的公开错误原样投影为单一 ``error`` 字段。"""

        if not isinstance(error, AnalysisRequestValidationError):
            raise TypeError("error 必须是 AnalysisRequestValidationError")
        logger.warning(
            "文件分析请求呈现校验错误: status_code=%d error=%s",
            error.status_code,
            str(error),
        )
        return AnalysisPresentedResponse(
            status_code=error.status_code,
            body={"error": str(error)},
        )

    def present_admission(
        self,
        admission: AnalysisBatchAdmission,
    ) -> AnalysisPresentedResponse:
        """把内部受理结果映射为已冻结 HTTP 合同，不返回任务或 batch 身份。"""

        if not isinstance(admission, AnalysisBatchAdmission):
            raise TypeError("admission 必须是 AnalysisBatchAdmission")
        if admission.outcome is AnalysisBatchAdmissionOutcome.ACCEPTED:
            logger.info(
                "文件分析任务已受理并呈现严格空响应: task_count=%d status_code=202",
                len(admission.executions),
            )
            return AnalysisPresentedResponse(status_code=202, body=None)
        if admission.outcome is AnalysisBatchAdmissionOutcome.CONFLICT_ACTIVE:
            return self._error_response(409, "任务正在处理中")
        if admission.outcome is AnalysisBatchAdmissionOutcome.CONFLICT_CALLBACK_PENDING:
            return self._error_response(409, "上一次任务回调尚未结束")
        if admission.outcome is AnalysisBatchAdmissionOutcome.BUSY:
            return self._error_response(503, "任务服务繁忙，请稍后重试")
        # Enum 增加新值时必须显式审查公开映射，不能在这里默认为任意 5xx JSON。
        raise RuntimeError("未知的文件分析任务受理结果")

    @staticmethod
    def raise_unhandled(error: BaseException) -> None:
        """保留旧路由未处理异常边界，使 Flask 继续产生既有 HTML 500。"""

        if not isinstance(error, BaseException):
            raise TypeError("error 必须是 BaseException")
        raise error

    @staticmethod
    def _error_response(status_code: int, message: str) -> AnalysisPresentedResponse:
        logger.warning(
            "文件分析任务未受理: status_code=%d error=%s",
            status_code,
            message,
        )
        return AnalysisPresentedResponse(status_code=status_code, body={"error": message})


__all__ = (
    "AnalysisPresentedResponse",
    "AnalysisSubmissionResponsePresenter",
)
