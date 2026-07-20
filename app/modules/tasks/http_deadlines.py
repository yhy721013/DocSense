"""需要持有数据库租约的 HTTP 调用统一时间预算。"""

from __future__ import annotations

import math


HTTP_LEASE_SAFETY_MARGIN_SECONDS = 5.0


def required_http_lease_seconds(timeout_seconds: float) -> float:
    """返回覆盖一次 Requests 连接与响应头读取的最小租约时长。

    Requests 的单个 ``timeout`` 数值会分别应用于连接阶段和读取阶段，并不是整个请求
    的墙钟总时限。因此最坏情况下二者会依次消耗约两倍配置值；额外五秒用于覆盖
    调用前后的数据库 CAS、线程调度和响应关闭。该函数只计算内部基础设施预算，不改变
    任何公开 HTTP 契约。
    """

    if isinstance(timeout_seconds, bool) or not isinstance(
        timeout_seconds,
        (int, float),
    ):
        raise TypeError("timeout_seconds 必须是数字")
    normalized = float(timeout_seconds)
    if not math.isfinite(normalized) or normalized <= 0.0:
        raise ValueError("timeout_seconds 必须是正有限数字")
    return normalized * 2.0 + HTTP_LEASE_SAFETY_MARGIN_SECONDS


__all__ = [
    "HTTP_LEASE_SAFETY_MARGIN_SECONDS",
    "required_http_lease_seconds",
]
