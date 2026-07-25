"""分类节点变更 AnythingLLM 适配器的内部预算配置。

分类节点变更仍是同步接口，不能把一次远端调用无限期地拖在请求线程中。本文件把单次 HTTP
上限、从 Application 接收命令起计算的远端调用总预算，以及失败后用于探测/补偿的保留窗口
固定为显式内部配置。它不读取 Flask Request，也不属于公开接口参数；Container 只在启动装配
时构造并校验一次配置。SQLite 锁等待会扣减后续远端预算，但当前阶段不宣称它能强制取消正在
等待的本地事务；严格请求级硬截止需在未来数据库适配阶段统一实现。
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass
from time import monotonic
from typing import Callable, Mapping


logger = logging.getLogger(__name__)


class ReassignmentInfrastructureConfigurationError(ValueError):
    """分类节点变更基础设施配置不满足同步 Saga 安全边界时抛出。"""


class ReassignmentDeadlineExceededError(RuntimeError):
    """本次同步执行已耗尽可用远端调用预算时抛出。

    该异常只在 Adapter 内部转换为 ``OUTCOME_UNKNOWN``，禁止由 Web 层直接透传；因为预算
    耗尽时无法证明上一条远端写是否已经完成，继续重发反而会放大跨系统不一致风险。
    """


def _positive_finite_seconds(value: object, *, name: str) -> float:
    """拒绝 Python ``float`` 的 NaN/Infinity/布尔值等隐蔽非法配置。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReassignmentInfrastructureConfigurationError(
            f"{name} 必须是正有限秒数"
        )
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0.0:
        raise ReassignmentInfrastructureConfigurationError(
            f"{name} 必须是正有限秒数"
        )
    return normalized


@dataclass(frozen=True)
class ReassignmentInfrastructureConfig:
    """分类节点变更远端调用的固定资源边界。

    ``http_timeout_seconds`` 是单个 AnythingLLM HTTP 请求的最大等待时间；
    ``total_timeout_seconds`` 是从 Application 收到命令起计算的远端调用总预算；
    ``compensation_reserve_seconds`` 始终从前向调用可用预算中扣除，保证远端写异常后仍有
    时间完成只读探测或交给后续补偿路径。这里不假装已经实现后台队列或多实例调度，只保护
    当前同步请求不吞掉全部可恢复窗口。
    """

    runtime_mode: str = "single_instance"
    http_timeout_seconds: float = 15.0
    total_timeout_seconds: float = 75.0
    compensation_reserve_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not isinstance(self.runtime_mode, str):
            raise ReassignmentInfrastructureConfigurationError(
                "runtime_mode 必须是字符串"
            )
        runtime_mode = self.runtime_mode.strip().lower()
        if runtime_mode != "single_instance":
            raise ReassignmentInfrastructureConfigurationError(
                "分类节点变更当前仅支持 single_instance；"
                "SQLite lease 使用进程本地时钟，禁止伪装为集群运行"
            )
        http_timeout = _positive_finite_seconds(
            self.http_timeout_seconds,
            name="http_timeout_seconds",
        )
        total_timeout = _positive_finite_seconds(
            self.total_timeout_seconds,
            name="total_timeout_seconds",
        )
        compensation_reserve = _positive_finite_seconds(
            self.compensation_reserve_seconds,
            name="compensation_reserve_seconds",
        )
        # 总预算必须至少留出一段严格为正的前向窗口。单次 HTTP 可以在实际调用时被
        # 剩余前向窗口裁剪，因此不要求它小于总预算，避免把可安全裁剪的配置误判为非法。
        if total_timeout <= compensation_reserve:
            raise ReassignmentInfrastructureConfigurationError(
                "total_timeout_seconds 必须大于 compensation_reserve_seconds，"
                "以保留至少一次前向调用窗口"
            )
        object.__setattr__(self, "http_timeout_seconds", http_timeout)
        object.__setattr__(self, "runtime_mode", runtime_mode)
        object.__setattr__(self, "total_timeout_seconds", total_timeout)
        object.__setattr__(
            self,
            "compensation_reserve_seconds",
            compensation_reserve,
        )


class ReassignmentExecutionDeadline:
    """单次同步编排的单调时钟 deadline。

    实例必须按请求创建，不可在多个 Operation 或线程之间共享。前向调用只能使用总预算扣除
    补偿预留后的时间；探测、补偿和恢复读取可以使用剩余总预算，但仍受单次 HTTP 上限约束。
    """

    def __init__(
        self,
        config: ReassignmentInfrastructureConfig,
        *,
        monotonic_clock: Callable[[], float] = monotonic,
        elapsed_seconds: float = 0.0,
    ) -> None:
        if not isinstance(config, ReassignmentInfrastructureConfig):
            raise TypeError("config 必须是 ReassignmentInfrastructureConfig")
        if not callable(monotonic_clock):
            raise TypeError("monotonic_clock 必须可调用")
        if (
            isinstance(elapsed_seconds, bool)
            or not isinstance(elapsed_seconds, (int, float))
            or not math.isfinite(float(elapsed_seconds))
            or float(elapsed_seconds) < 0.0
        ):
            raise ReassignmentInfrastructureConfigurationError(
                "elapsed_seconds 必须是有限非负秒数"
            )
        started_at = self._read_clock(monotonic_clock, name="monotonic_clock")
        self._config = config
        self._monotonic_clock = monotonic_clock
        self._started_at = started_at
        self._last_observed_at = started_at
        # Application 在创建 Adapter 前已经发生的保留、状态推进和锁等待也必须扣除，避免
        # Factory 创建较晚时重新获得一份完整远端预算。
        self._deadline_at = (
            started_at + config.total_timeout_seconds - float(elapsed_seconds)
        )

    @property
    def total_timeout_seconds(self) -> float:
        """返回固定总预算，供只读诊断和离线测试使用。"""

        return self._config.total_timeout_seconds

    def remaining_seconds(self) -> float:
        """返回不会因异常测试时钟倒退而增加的剩余总预算。"""

        current_at = self._read_clock(self._monotonic_clock, name="monotonic_clock")
        # 真实 monotonic 时钟不会倒退；对注入的错误时钟采取保守策略，防止它意外延长
        # 远端调用时间。这里不记录每次读取日志，避免正常请求产生高频噪声。
        current_at = max(current_at, self._last_observed_at)
        self._last_observed_at = current_at
        return max(0.0, self._deadline_at - current_at)

    def forward_http_timeout_seconds(self) -> float:
        """取得一次前向 HTTP 调用的裁剪超时，并始终保留恢复窗口。"""

        available = self.remaining_seconds() - self._config.compensation_reserve_seconds
        if available <= 0.0:
            raise ReassignmentDeadlineExceededError(
                "分类节点变更前向预算已耗尽，必须保留探测/补偿窗口"
            )
        return min(self._config.http_timeout_seconds, available)

    def recovery_http_timeout_seconds(self) -> float:
        """取得探测或补偿使用的裁剪超时，不再重复扣除保留窗口。"""

        available = self.remaining_seconds()
        if available <= 0.0:
            raise ReassignmentDeadlineExceededError(
                "分类节点变更总预算已耗尽，无法继续确认远端状态"
            )
        return min(self._config.http_timeout_seconds, available)

    @staticmethod
    def _read_clock(clock: Callable[[], float], *, name: str) -> float:
        try:
            value = clock()
        except Exception as exc:
            raise ReassignmentInfrastructureConfigurationError(
                f"{name} 调用失败"
            ) from exc
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ReassignmentInfrastructureConfigurationError(
                f"{name} 必须返回有限数字"
            )
        normalized = float(value)
        if not math.isfinite(normalized):
            raise ReassignmentInfrastructureConfigurationError(
                f"{name} 必须返回有限数字"
            )
        return normalized


def _environment_seconds(
    environment: Mapping[str, str],
    *,
    name: str,
    default: float,
) -> float:
    """从环境读取严格秒数；空字符串和非数值均视为启动错误。"""

    raw_value = environment.get(name)
    if raw_value is None:
        return default
    if not isinstance(raw_value, str):
        raise ReassignmentInfrastructureConfigurationError(
            f"环境变量 {name} 必须是秒数字符串"
        )
    try:
        return _positive_finite_seconds(float(raw_value.strip()), name=name)
    except ValueError as exc:
        raise ReassignmentInfrastructureConfigurationError(
            f"环境变量 {name} 必须是正有限秒数"
        ) from exc


def load_reassignment_infrastructure_config(
    environment: Mapping[str, str] | None = None,
) -> ReassignmentInfrastructureConfig:
    """加载分类节点变更内部配置，供当前生产 Container 在启动阶段 fail-fast。

    本函数不由当前遗留路由调用，因此新增环境变量不会改变现有 `/llm/reassign` 的请求或响应
    契约。测试可显式传入映射，避免读取开发机环境污染离线验证。
    """

    resolved_environment = os.environ if environment is None else environment
    config = ReassignmentInfrastructureConfig(
        runtime_mode=resolved_environment.get(
            "DOCSENSE_REASSIGN_RUNTIME_MODE",
            "single_instance",
        ),
        http_timeout_seconds=_environment_seconds(
            resolved_environment,
            name="DOCSENSE_REASSIGN_HTTP_TIMEOUT_SECONDS",
            default=15.0,
        ),
        total_timeout_seconds=_environment_seconds(
            resolved_environment,
            name="DOCSENSE_REASSIGN_TOTAL_TIMEOUT_SECONDS",
            default=75.0,
        ),
        compensation_reserve_seconds=_environment_seconds(
            resolved_environment,
            name="DOCSENSE_REASSIGN_COMPENSATION_RESERVE_SECONDS",
            default=30.0,
        ),
    )
    logger.info(
        "分类节点变更 AnythingLLM 内部预算配置已加载: runtime_mode=%s "
        "http_timeout_seconds=%.3f "
        "total_timeout_seconds=%.3f compensation_reserve_seconds=%.3f",
        config.runtime_mode,
        config.http_timeout_seconds,
        config.total_timeout_seconds,
        config.compensation_reserve_seconds,
    )
    return config


__all__ = [
    "ReassignmentDeadlineExceededError",
    "ReassignmentExecutionDeadline",
    "ReassignmentInfrastructureConfig",
    "ReassignmentInfrastructureConfigurationError",
    "load_reassignment_infrastructure_config",
]
