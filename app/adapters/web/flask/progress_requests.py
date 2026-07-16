"""``/llm/progress`` 客户端消息到类型化请求的 Flask 入站适配器。"""

from __future__ import annotations

from collections.abc import Mapping

from app.adapters.web.report_ids import (
    ReportIdValidationError,
    normalize_report_id,
)
from app.modules.tasks.domain import (
    ProgressKey,
    ProgressSubscriptionRequest,
)


class ProgressRequestValidationError(ValueError):
    """当前客户端消息违反已确认的 Progress 请求契约。"""


def parse_progress_subscription(
    payload: object,
) -> ProgressSubscriptionRequest:
    """完整校验一条无 ``action`` 订阅消息。

    校验采用整条消息原子语义：``params`` 中任一元素不是 JSON 对象，或任一业务键
    无效时，均不返回部分请求，调用方应只发送 error 消息并保持连接。
    """

    if not isinstance(payload, Mapping):
        raise ProgressRequestValidationError("订阅消息格式无效")
    if "action" in payload:
        # action/ack 是已批准下线的旧内部扩展。只要字段出现就拒绝，不能把 null 或
        # 空字符串悄悄解释成无 action 请求。
        raise ProgressRequestValidationError("不支持的action")

    business_type = payload.get("businessType")
    if business_type not in {"file", "report", "weaponry"}:
        raise ProgressRequestValidationError("businessType无效")

    params = payload.get("params")
    if not isinstance(params, list) or not params:
        raise ProgressRequestValidationError("params不能为空")
    if any(not isinstance(item, Mapping) for item in params):
        raise ProgressRequestValidationError("params元素必须是对象")

    keys: list[ProgressKey] = []
    for item in params:
        if business_type == "file":
            file_name = item.get("fileName")
            if not isinstance(file_name, str) or not file_name.strip():
                raise ProgressRequestValidationError("fileName不能为空")
            business_key = file_name.strip()
        elif business_type == "report":
            report_id = item.get("reportId")
            if report_id is None:
                raise ProgressRequestValidationError("reportId不能为空")
            try:
                normalized_report_id = normalize_report_id(report_id)
            except ReportIdValidationError as exc:
                # WebSocket 参数错误只能拒绝当前消息并返回 error；转换异常不得越过
                # 请求适配边界导致连接关闭。
                raise ProgressRequestValidationError(str(exc)) from exc
            business_key = normalized_report_id.business_key
        else:
            architecture_id = item.get("architectureId")
            if architecture_id is None:
                raise ProgressRequestValidationError("architectureId不能为空")
            business_key = str(architecture_id)
        keys.append(ProgressKey(str(business_type), business_key))

    return ProgressSubscriptionRequest(tuple(keys))


__all__ = ["ProgressRequestValidationError", "parse_progress_subscription"]
