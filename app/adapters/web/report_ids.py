"""报告业务标识的 Web 入站规范化规则。

该模块不依赖 Flask 或 FastAPI，只负责把公开请求中的 ``reportId`` 转换为稳定的
内部业务键。这样当前 Flask 路由与未来 FastAPI 适配器能够共享完全相同的校验语义，
避免 ``132``、``"132"`` 和 ``"00132"`` 被误认为三个不同任务。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.modules.report.domain import (
    REPORT_ID_ABSOLUTE_UPPER_BOUND,
    REPORT_ID_MAX_DECIMAL_DIGITS,
)


_DECIMAL_INTEGER_PATTERN = re.compile(r"^[+-]?[0-9]+$")
MAX_REPORT_ID_DIGITS = REPORT_ID_MAX_DECIMAL_DIGITS
"""公开 ``reportId`` 允许的十进制数字字符上限。

上限不计算可选正负号，但会计算前导零。三个公开入口必须复用同一个规范化函数，避免
报告生成、check-task 与 Progress 对同一个业务键给出不同结论。128 位既明显高于常见
业务标识需求，又能在进入 Python 大整数转换、数据库索引和外部资源命名之前阻断极端
输入造成的资源放大。
"""

class ReportIdValidationError(ValueError):
    """公开请求中的 ``reportId`` 不是十进制整数。"""


def _raise_too_many_digits() -> None:
    """以稳定公开文案拒绝超过已批准上限的报告标识。"""

    raise ReportIdValidationError(
        f"reportId不能超过{MAX_REPORT_ID_DIGITS}位十进制数字"
    )


@dataclass(frozen=True)
class NormalizedReportId:
    """一次完成校验后的报告标识。

    ``value`` 用于保持既有 JSON number 输出和遗留服务的整数入参；
    ``business_key`` 用于任务库、Progress Hub 及后续队列幂等键。二者始终表示同一个
    整数值。数值不受 32 位或 64 位限制，但输入最多包含 128 个十进制数字字符。
    """

    value: int
    business_key: str


def normalize_report_id(value: object) -> NormalizedReportId:
    """接受 JSON 整数或十进制整数字符串，并生成唯一规范形式。

    字符串允许首尾空白、可选正负号和前导零；规范化后会去除这些不影响整数值的
    表示差异。例如 ``132``、``"+132"`` 与 ``"00132"`` 的内部业务键均为
    ``"132"``。Python 整数可以表达超过 32/64 位的值，因此这里不增加机器整数范围
    门槛；为保证数据库索引、JSON 转换及外部资源命名具有确定上界，数字部分最多为
    128 位。

    ``bool`` 虽然在 Python 中是 ``int`` 的子类，但 JSON 布尔值不属于接口约定的
    整数，必须显式拒绝；浮点数同理，即使其值看起来没有小数部分也不能静默接收。
    """

    if isinstance(value, bool):
        raise ReportIdValidationError("reportId必须是整数或整数字符串")

    if isinstance(value, int):
        # 先比较整数范围，再调用 str；这样即使内部调用方传入数千位 Python int，也不会
        # 触发 CPython 的十进制字符串安全上限或制造超大业务键。
        if not (
            -REPORT_ID_ABSOLUTE_UPPER_BOUND
            < value
            < REPORT_ID_ABSOLUTE_UPPER_BOUND
        ):
            _raise_too_many_digits()
        integer_value = value
    elif isinstance(value, str):
        normalized_text = value.strip()
        if not _DECIMAL_INTEGER_PATTERN.fullmatch(normalized_text):
            raise ReportIdValidationError("reportId必须是整数或整数字符串")
        digit_text = normalized_text.lstrip("+-")
        # 按调用方实际提交的数字字符计数，前导零同样占用请求、日志及解析资源，因此不能
        # 通过 ``000...001`` 绕过长度门禁。
        if len(digit_text) > MAX_REPORT_ID_DIGITS:
            _raise_too_many_digits()
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
    "MAX_REPORT_ID_DIGITS",
    "NormalizedReportId",
    "ReportIdValidationError",
    "normalize_report_id",
]
