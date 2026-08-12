"""统一 Task 控制面的可注入时钟协议。

持久化时间只允许 UTC RFC3339 微秒格式（例如
``2026-08-12T12:34:56.123456Z``）。Domain/Application 可以读取本 Port，
但不得直接调用 ``datetime.now`` 或 ``time.time``；测试因此可以用 FakeClock
确定性地推进、冻结或回拨时间。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


def _is_leap_year(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


class ClockAnomalyError(RuntimeError):
    """墙上时钟异常，调用方必须暂停新 claim 与 Recovery 判断。"""


def require_persisted_utc(value: object, *, name: str = "timestamp") -> str:
    """校验持久时间格式，不把本地时间或低精度时间静默归一化。"""

    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    # Port 层的标准库白名单刻意很窄，因此在这里直接检查固定字符位置与日历范围，
    # 不引入 datetime/re，也避免仅靠正则接受 13 月、32 日等无效时间。
    if len(value) != 27:
        raise ValueError(f"{name} 必须是 UTC RFC3339 微秒格式并以 Z 结尾")
    separators = {4: "-", 7: "-", 10: "T", 13: ":", 16: ":", 19: ".", 26: "Z"}
    if any(value[index] != expected for index, expected in separators.items()):
        raise ValueError(f"{name} 必须是 UTC RFC3339 微秒格式并以 Z 结尾")
    digit_indexes = tuple(index for index in range(27) if index not in separators)
    if any(not value[index].isascii() or not value[index].isdigit() for index in digit_indexes):
        raise ValueError(f"{name} 必须是 UTC RFC3339 微秒格式并以 Z 结尾")
    year = int(value[0:4])
    month = int(value[5:7])
    day = int(value[8:10])
    hour = int(value[11:13])
    minute = int(value[14:16])
    second = int(value[17:19])
    month_days = (31, 29 if _is_leap_year(year) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    if (
        year == 0
        or month < 1
        or month > 12
        or day < 1
        or day > month_days[month - 1]
        or hour > 23
        or minute > 59
        or second > 59
    ):
        raise ValueError(f"{name} 必须是 UTC RFC3339 微秒格式并以 Z 结尾")
    return value


@runtime_checkable
class ClockPort(Protocol):
    """提供可持久化 UTC 时间；实现负责检测不安全的时钟异常。"""

    def now_utc(self) -> str:
        """返回严格 UTC 时间；发现回拨/异常跃迁时抛出 ClockAnomalyError。"""
        ...


__all__ = ["ClockAnomalyError", "ClockPort", "require_persisted_utc"]
