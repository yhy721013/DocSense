"""报告业务标识的 Web 入站规范化规则。

该模块不依赖 Flask 或 FastAPI，只负责把公开请求中的 ``reportId`` 转换为稳定的
内部业务键。这样当前 Flask 路由与未来 FastAPI 适配器能够共享完全相同的校验语义，
避免 ``132``、``"132"`` 和 ``"00132"`` 被误认为三个不同任务。
"""

from __future__ import annotations

import re
from dataclasses import dataclass


_DECIMAL_INTEGER_PATTERN = re.compile(r"^[+-]?[0-9]+$")


class ReportIdValidationError(ValueError):
    """公开请求中的 ``reportId`` 不是十进制整数。"""


@dataclass(frozen=True)
class NormalizedReportId:
    """一次完成校验后的报告标识。

    ``value`` 用于保持既有 JSON number 输出和遗留服务的整数入参；
    ``business_key`` 用于任务库、Progress Hub 及后续队列幂等键。二者始终表示同一个
    整数值，且不施加 32 位或 64 位业务范围限制。
    """

    value: int
    business_key: str


def normalize_report_id(value: object) -> NormalizedReportId:
    """接受 JSON 整数或十进制整数字符串，并生成唯一规范形式。

    字符串允许首尾空白、可选正负号和前导零；规范化后会去除这些不影响整数值的
    表示差异。例如 ``132``、``"+132"`` 与 ``"00132"`` 的内部业务键均为
    ``"132"``。Python 整数为任意精度，因此这里不增加 32/64 位范围门槛。

    ``bool`` 虽然在 Python 中是 ``int`` 的子类，但 JSON 布尔值不属于接口约定的
    整数，必须显式拒绝；浮点数同理，即使其值看起来没有小数部分也不能静默接收。
    """

    if isinstance(value, bool):
        raise ReportIdValidationError("reportId必须是整数或整数字符串")

    if isinstance(value, int):
        integer_value = value
    elif isinstance(value, str):
        normalized_text = value.strip()
        if not _DECIMAL_INTEGER_PATTERN.fullmatch(normalized_text):
            raise ReportIdValidationError("reportId必须是整数或整数字符串")
        try:
            integer_value = int(normalized_text, 10)
        except ValueError as exc:
            # CPython 对极端超长十进制文本可能启用安全长度上限。这里仍将其归类为
            # 入站参数错误，不能让运行时转换异常泄漏成 HTTP 500 或关闭 WebSocket。
            raise ReportIdValidationError(
                "reportId必须是整数或整数字符串"
            ) from exc
    else:
        raise ReportIdValidationError("reportId必须是整数或整数字符串")

    return NormalizedReportId(
        value=integer_value,
        business_key=str(integer_value),
    )


__all__ = [
    "NormalizedReportId",
    "ReportIdValidationError",
    "normalize_report_id",
]
