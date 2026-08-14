"""统一 Task 控制面的可注入时钟协议。

持久化时间只允许 UTC RFC3339 微秒格式（例如
``2026-08-12T12:34:56.123456Z``）。Domain/Application 可以读取本 Port，
但不得直接调用 ``datetime.now`` 或 ``time.time``；测试因此可以用 FakeClock
确定性地推进、冻结或回拨时间。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.modules.tasks.domain.lease_time import require_persisted_utc


class ClockAnomalyError(RuntimeError):
    """墙上时钟异常，调用方必须暂停新 claim 与 Recovery 判断。"""


@runtime_checkable
class ClockPort(Protocol):
    """提供可持久化 UTC 时间；实现负责检测不安全的时钟异常。"""

    def now_utc(self) -> str:
        """返回严格 UTC 时间；发现回拨/异常跃迁时抛出 ClockAnomalyError。"""
        ...


__all__ = ["ClockAnomalyError", "ClockPort", "require_persisted_utc"]
