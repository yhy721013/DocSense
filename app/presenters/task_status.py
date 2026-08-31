"""check-task 应用结果到 HTTP 语义的框架无关 Presenter。

本模块不创建 Flask/FastAPI Response。它只冻结状态码、字节响应体和媒体类型，未来
Web Adapter 必须机械地使用这些值，不能重新读取数据库或把内部 TaskId、恢复请求 ID、
命令 outcome 拼回公开响应。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from app.modules.tasks.application.request_callback_recovery import (
    RequestCallbackRecoveryResult,
)
from app.modules.tasks.application.execute_check_task import ExecuteCheckTaskResult


logger = logging.getLogger(__name__)
_JSON_CONTENT_TYPE = "application/json; charset=utf-8"
_TASK_NOT_FOUND_MESSAGE = "任务不存在"


def _required_error_message(value: object) -> str:
    """校验既有错误文本，禁止把任意异常对象直接序列化到响应。"""

    if not isinstance(value, str):
        raise TypeError("error_message 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError("error_message 不能为空")
    return normalized


def _error_body(message: str) -> bytes:
    """生成稳定的 UTF-8 JSON 错误体，字段结构保持为既有 ``error``。"""

    return json.dumps(
        {"error": message},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True)
class TaskStatusHttpPresentation:
    """Web Adapter 可直接转换为具体框架 Response 的不可变结果。"""

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
            if not isinstance(self.content_type, str):
                raise TypeError("content_type 必须是 str 或 None")
            normalized = self.content_type.strip()
            if not normalized:
                raise ValueError("content_type 不能为空字符串")
            object.__setattr__(self, "content_type", normalized)


class CheckTaskResponsePresenter:
    """实现已批准的 check-task 空成功和既有错误映射。"""

    def present(
        self,
        result: RequestCallbackRecoveryResult | ExecuteCheckTaskResult,
    ) -> TaskStatusHttpPresentation:
        """映射可靠登记结果；不等待也不声称 Callback 已经完成。"""

        if not isinstance(
            result,
            (RequestCallbackRecoveryResult, ExecuteCheckTaskResult),
        ):
            raise TypeError("result 必须是受支持的 check-task 应用结果")
        if result.single_missing:
            logger.debug("映射 check-task 单项未命中响应: status_code=404")
            return self._error(404, _TASK_NOT_FOUND_MESSAGE)

        # 单项/批量成功都必须是真正的零字节体。即使内部结果带有 TaskId、恢复请求
        # ID 或 outcome，也绝不在这里序列化；批量缺失位置同样不再输出占位 JSON。
        logger.debug(
            "映射 check-task 空成功响应: status_code=200 item_count=%s "
            "missing_count=%s",
            (
                len(result.ordered_items)
                if isinstance(result, RequestCallbackRecoveryResult)
                else result.unique_count
            ),
            result.missing_count,
        )
        return TaskStatusHttpPresentation(status_code=200, body=b"")

    def present_bad_request(
        self,
        error_message: str,
    ) -> TaskStatusHttpPresentation:
        """映射 Web Adapter 已判定的参数错误，保持既有 JSON 字段结构。"""

        message = _required_error_message(error_message)
        logger.debug("映射 check-task 参数错误响应: status_code=400")
        return self._error(400, message)

    @staticmethod
    def _error(status_code: int, message: str) -> TaskStatusHttpPresentation:
        return TaskStatusHttpPresentation(
            status_code=status_code,
            body=_error_body(message),
            content_type=_JSON_CONTENT_TYPE,
        )


__all__ = [
    "CheckTaskResponsePresenter",
    "TaskStatusHttpPresentation",
]
