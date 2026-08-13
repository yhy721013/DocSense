"""Task lease 的纯 UTC 时间运算。

本模块只对调用方传入的持久时间做严格解析和加法，不读取系统时钟。ClockPort 仍是
运行时取得“当前时间”的唯一入口。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isfinite


_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def add_persisted_utc_seconds(value: str, *, seconds: float) -> str:
    """在严格 RFC3339 微秒 UTC 时间上增加正秒数。"""

    if not isinstance(value, str):
        raise TypeError("value 必须是 str")
    try:
        parsed = datetime.strptime(value, _UTC_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError("value 必须是 UTC RFC3339 微秒格式并以 Z 结尾") from exc
    # ``strptime`` 对部分平台输入可能比合同宽松；round-trip 必须逐字符一致。
    if parsed.strftime(_UTC_FORMAT) != value:
        raise ValueError("value 必须是 UTC RFC3339 微秒格式并以 Z 结尾")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise TypeError("seconds 必须是数字")
    if not isfinite(float(seconds)) or seconds <= 0:
        raise ValueError("seconds 必须是正有限数")
    return (parsed + timedelta(seconds=float(seconds))).strftime(_UTC_FORMAT)


__all__ = ["add_persisted_utc_seconds"]
