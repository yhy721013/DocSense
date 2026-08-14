"""Report 调度、资源恢复与下载上限的类型化运行配置。

本模块是阶段 2-4 后 Report 十一个内部环境键的唯一目标所有者。读取函数只读取传入的
环境映射（默认使用 ``os.environ``），不修改 ``.env``、不启动线程，也不记录原始环境值。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import math
import os
import re

from app.modules.tasks.http_deadlines import required_http_lease_seconds


REPORT_RUNTIME_MODE_SINGLE_INSTANCE = "single_instance"
_MAX_DOWNLOAD_BYTES = 10 * 1024**4
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


class ReportRuntimeConfigurationError(RuntimeError):
    """Report 内部运行配置不满足当前单实例安全边界。"""


@dataclass(frozen=True, slots=True)
class ReportExecutionCapabilityConfig:
    """只能由部署方声明的 AnythingLLM 实例与模型身份摘要。

    URL、API Key 和模型名称原文均不得进入 Task Input；部署流程应对经过评审的能力身份
    计算 SHA-256，并通过环境变量注入。相同 URL 背后的模型更换必须产生新摘要。
    """

    rag_provider_fingerprint: str
    rag_model_fingerprint: str

    def __post_init__(self) -> None:
        for name in ("rag_provider_fingerprint", "rag_model_fingerprint"):
            value = getattr(self, name)
            if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value.strip()) is None:
                raise ReportRuntimeConfigurationError(
                    f"{name} 必须是 64 位 SHA-256 十六进制摘要"
                )
            object.__setattr__(self, name, value.strip().lower())


@dataclass(frozen=True)
class ReportRuntimeConfig:
    """Report 运行时配置快照。

    当前仅安装本地 SQLite 与进程内线程，因此 ``single_instance`` 是唯一合法模式。
    这里的限制不代表多实例能力；未来接入共享数据库和可靠队列时应新增独立实现。
    """

    runtime_mode: str = REPORT_RUNTIME_MODE_SINGLE_INSTANCE
    scan_interval_seconds: float = 1.0
    accepted_batch_size: int = 50
    dispatch_failure_retry_seconds: float = 30.0
    resource_sweep_interval_seconds: float = 30.0
    resource_sweep_limit: int = 50
    running_sample_limit: int = 20
    stop_timeout_seconds: float = 5.0
    cleanup_http_timeout_seconds: float = 60.0
    cleanup_lease_seconds: float = 130.0
    max_download_bytes: int = 512 * 1024 * 1024

    def __post_init__(self) -> None:
        mode = str(self.runtime_mode or "").strip().lower()
        if mode != REPORT_RUNTIME_MODE_SINGLE_INSTANCE:
            raise ReportRuntimeConfigurationError(
                "当前仅安装 single_instance Report 运行时；多实例模式必须先安装共享数据库、"
                "可靠队列和分布式租约"
            )
        object.__setattr__(self, "runtime_mode", mode)

        for name in (
            "scan_interval_seconds",
            "dispatch_failure_retry_seconds",
            "resource_sweep_interval_seconds",
            "stop_timeout_seconds",
            "cleanup_http_timeout_seconds",
            "cleanup_lease_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ReportRuntimeConfigurationError(f"{name} 必须是正有限数字")
            normalized = float(value)
            if not math.isfinite(normalized) or normalized <= 0.0:
                raise ReportRuntimeConfigurationError(f"{name} 必须是正有限数字")
            object.__setattr__(self, name, normalized)

        for name in ("accepted_batch_size", "resource_sweep_limit", "running_sample_limit"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
                raise ReportRuntimeConfigurationError(f"{name} 必须是 1~1000 的整数")

        required_lease = required_http_lease_seconds(self.cleanup_http_timeout_seconds)
        if self.cleanup_lease_seconds < required_lease:
            raise ReportRuntimeConfigurationError(
                "cleanup_lease_seconds 必须覆盖连接、响应读取和安全余量；"
                f"当前至少需要 {required_lease:.3f} 秒"
            )
        if (
            isinstance(self.max_download_bytes, bool)
            or not isinstance(self.max_download_bytes, int)
            or not 1 <= self.max_download_bytes <= _MAX_DOWNLOAD_BYTES
        ):
            raise ReportRuntimeConfigurationError(
                "max_download_bytes 必须是 1 字节到 10 TiB 的整数"
            )

    @property
    def fingerprint(self) -> str:
        """返回不含环境原文的确定性配置摘要，供启动日志和诊断使用。"""

        payload = {
            name: getattr(self, name)
            for name in self.__dataclass_fields__
        }
        material = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest().upper()

    @classmethod
    def single_instance(cls) -> "ReportRuntimeConfig":
        """构造不读取环境变量的安全默认值，供定向离线测试显式注入。"""

        return cls(runtime_mode=REPORT_RUNTIME_MODE_SINGLE_INSTANCE)


def _read(environment: Mapping[str, str], name: str, default: object) -> str:
    value = environment.get(name, str(default))
    if not isinstance(value, str):
        raise ReportRuntimeConfigurationError(f"{name} 必须是文本环境值")
    return value.strip()


def _strict_float(environment: Mapping[str, str], name: str, default: float) -> float:
    try:
        return float(_read(environment, name, default))
    except ValueError as exc:
        raise ReportRuntimeConfigurationError(f"{name} 必须是正有限数字") from exc


def _strict_int(environment: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(_read(environment, name, default))
    except ValueError as exc:
        raise ReportRuntimeConfigurationError(f"{name} 必须是整数") from exc


def load_report_runtime_config(
    environment: Mapping[str, str] | None = None,
) -> ReportRuntimeConfig:
    """读取并严格校验现有十一个 Report 环境键。

    显式传入映射可让测试完全隔离真实进程环境；生产组合根在切换时传入 ``os.environ``。
    """

    source = os.environ if environment is None else environment
    return ReportRuntimeConfig(
        runtime_mode=_read(
            source,
            "DOCSENSE_REPORT_RUNTIME_MODE",
            REPORT_RUNTIME_MODE_SINGLE_INSTANCE,
        ),
        scan_interval_seconds=_strict_float(
            source, "DOCSENSE_REPORT_SCAN_INTERVAL_SECONDS", 1.0
        ),
        accepted_batch_size=_strict_int(
            source, "DOCSENSE_REPORT_ACCEPTED_BATCH_SIZE", 50
        ),
        dispatch_failure_retry_seconds=_strict_float(
            source, "DOCSENSE_REPORT_DISPATCH_FAILURE_RETRY_SECONDS", 30.0
        ),
        resource_sweep_interval_seconds=_strict_float(
            source, "DOCSENSE_REPORT_RESOURCE_SWEEP_INTERVAL_SECONDS", 30.0
        ),
        resource_sweep_limit=_strict_int(
            source, "DOCSENSE_REPORT_RESOURCE_SWEEP_LIMIT", 50
        ),
        running_sample_limit=_strict_int(
            source, "DOCSENSE_REPORT_RUNNING_SAMPLE_LIMIT", 20
        ),
        stop_timeout_seconds=_strict_float(
            source, "DOCSENSE_REPORT_STOP_TIMEOUT_SECONDS", 5.0
        ),
        cleanup_http_timeout_seconds=_strict_float(
            source, "DOCSENSE_REPORT_CLEANUP_HTTP_TIMEOUT_SECONDS", 60.0
        ),
        cleanup_lease_seconds=_strict_float(
            source, "DOCSENSE_REPORT_CLEANUP_LEASE_SECONDS", 130.0
        ),
        max_download_bytes=_strict_int(
            source, "DOCSENSE_REPORT_MAX_DOWNLOAD_BYTES", 512 * 1024 * 1024
        ),
    )


def load_report_execution_capability_config(
    environment: Mapping[str, str] | None = None,
) -> ReportExecutionCapabilityConfig:
    """读取 v2 新受理必需的部署能力身份；缺失时失败关闭。"""

    source = os.environ if environment is None else environment
    return ReportExecutionCapabilityConfig(
        rag_provider_fingerprint=_read(
            source,
            "DOCSENSE_REPORT_RAG_PROVIDER_FINGERPRINT",
            "",
        ),
        rag_model_fingerprint=_read(
            source,
            "DOCSENSE_REPORT_RAG_MODEL_FINGERPRINT",
            "",
        ),
    )


__all__ = [
    "REPORT_RUNTIME_MODE_SINGLE_INSTANCE",
    "ReportRuntimeConfig",
    "ReportRuntimeConfigurationError",
    "ReportExecutionCapabilityConfig",
    "load_report_execution_capability_config",
    "load_report_runtime_config",
]
