"""基于 wall clock 与 monotonic 对照的单进程安全 UTC Clock Adapter。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
from threading import Lock
import time
from typing import Callable

from app.modules.tasks.ports import ClockAnomalyError, require_persisted_utc


logger = logging.getLogger(__name__)
_UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class SystemSafeClock:
    """检测超过阈值的 wall clock 回拨/跃迁；异常一旦发生即保持失败关闭。"""

    def __init__(
        self,
        *,
        max_jitter_seconds: float,
        wall_clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        if isinstance(max_jitter_seconds, bool) or not isinstance(
            max_jitter_seconds, (int, float)
        ):
            raise TypeError("max_jitter_seconds 必须是数字")
        if float(max_jitter_seconds) < 0:
            raise ValueError("max_jitter_seconds 必须是非负数")
        self._max_jitter = float(max_jitter_seconds)
        self._wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic_clock or time.monotonic
        self._lock = Lock()
        self._unsafe_reason = ""
        self._base_wall = self._read_wall()
        self._base_monotonic = float(self._monotonic())
        self._last_monotonic = self._base_monotonic

    def _read_wall(self) -> datetime:
        value = self._wall_clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise TypeError("wall_clock 必须返回带时区 datetime")
        return value.astimezone(timezone.utc)

    def now_utc(self) -> str:
        with self._lock:
            if self._unsafe_reason:
                raise ClockAnomalyError(self._unsafe_reason)
            wall = self._read_wall()
            monotonic_value = float(self._monotonic())
            if monotonic_value < self._last_monotonic:
                self._mark_unsafe("monotonic_clock_moved_backwards")
            expected = self._base_wall + timedelta(
                seconds=monotonic_value - self._base_monotonic
            )
            drift = abs((wall - expected).total_seconds())
            if drift > self._max_jitter:
                self._mark_unsafe("wall_clock_drift_exceeded")
            self._last_monotonic = monotonic_value
            return require_persisted_utc(wall.strftime(_UTC_FORMAT))

    def is_safe(self) -> bool:
        with self._lock:
            return not self._unsafe_reason

    def _mark_unsafe(self, reason_code: str) -> None:
        self._unsafe_reason = reason_code
        logger.critical(
            "Task Clock 已失败关闭: reason_code=%s",
            reason_code,
        )
        raise ClockAnomalyError(reason_code)


__all__ = ["SystemSafeClock"]
