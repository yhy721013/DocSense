"""Task lease 的纯 UTC 时间运算。

本模块只对调用方传入的持久时间做严格解析和加法，不读取系统时钟。ClockPort 仍是
运行时取得“当前时间”的唯一入口。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isfinite


_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def require_persisted_utc(value: object, *, name: str = "timestamp") -> str:
    """严格校验持久化 UTC 时间，同时拒绝不存在的日历日期。

    该函数是 Domain、Port 与 SQLite Repository 共用的唯一格式入口。除了固定的
    RFC3339 微秒外观，还通过 ``strptime`` 与逐字符 round-trip 拒绝 13 月、2 月 30 日、
    非 ASCII 数字以及平台可能宽松接受的非规范输入；函数只做纯校验，不读取系统时钟。
    """

    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    try:
        parsed = datetime.strptime(value, _UTC_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是 UTC RFC3339 微秒格式并以 Z 结尾") from exc
    if parsed.strftime(_UTC_FORMAT) != value:
        raise ValueError(f"{name} 必须是 UTC RFC3339 微秒格式并以 Z 结尾")
    return value


def add_persisted_utc_seconds(value: str, *, seconds: float) -> str:
    """在严格 RFC3339 微秒 UTC 时间上增加正秒数。"""

    require_persisted_utc(value, name="value")
    parsed = datetime.strptime(value, _UTC_FORMAT).replace(tzinfo=timezone.utc)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise TypeError("seconds 必须是数字")
    if not isfinite(float(seconds)) or seconds <= 0:
        raise ValueError("seconds 必须是正有限数")
    return (parsed + timedelta(seconds=float(seconds))).strftime(_UTC_FORMAT)


__all__ = ["add_persisted_utc_seconds", "require_persisted_utc"]
