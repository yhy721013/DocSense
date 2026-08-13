"""阶段 2 Task lease 运行时不等式的不可变纯配置。"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite


def _positive_number(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} 必须是数字")
    normalized = float(value)
    if not isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{name} 必须是正有限数")
    return normalized


def _non_negative_number(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} 必须是数字")
    normalized = float(value)
    if not isfinite(normalized) or normalized < 0:
        raise ValueError(f"{name} 必须是非负有限数")
    return normalized


@dataclass(frozen=True, slots=True)
class TaskLeaseRuntimeSettings:
    """claim/heartbeat/stop 共享的租约安全参数。

    本对象不读取环境变量，不表示生产配置已经接线。后续 Runtime Config Adapter 必须先
    完成环境解析，再构造本值对象；非法组合会在任何 heartbeat 线程启动前失败。
    """

    heartbeat_interval_seconds: float = 5.0
    lease_duration_seconds: float = 30.0
    sqlite_busy_budget_seconds: float = 2.0
    max_clock_jitter_seconds: float = 3.0
    stop_grace_seconds: float = 10.0

    def __post_init__(self) -> None:
        for name in (
            "heartbeat_interval_seconds",
            "lease_duration_seconds",
            "sqlite_busy_budget_seconds",
            "stop_grace_seconds",
        ):
            object.__setattr__(
                self,
                name,
                _positive_number(getattr(self, name), name=name),
            )
        object.__setattr__(
            self,
            "max_clock_jitter_seconds",
            _non_negative_number(
                self.max_clock_jitter_seconds,
                name="max_clock_jitter_seconds",
            ),
        )

        minimum_lease = (
            3 * self.heartbeat_interval_seconds
            + 2 * self.sqlite_busy_budget_seconds
            + self.max_clock_jitter_seconds
        )
        if self.lease_duration_seconds < minimum_lease:
            raise ValueError(
                "lease_duration_seconds 必须满足 "
                "lease >= 3 * heartbeat + 2 * sqlite_busy + clock_jitter"
            )
        if self.stop_grace_seconds < (
            self.heartbeat_interval_seconds + self.sqlite_busy_budget_seconds
        ):
            raise ValueError(
                "stop_grace_seconds 必须满足 stop_grace >= heartbeat + sqlite_busy"
            )


__all__ = ["TaskLeaseRuntimeSettings"]
