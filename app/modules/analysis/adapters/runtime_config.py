"""Analysis 的 16 个既有运行环境键及严格解析所有权。

本模块只移动内部配置边界，不增加环境键、不改变默认值、错误类型或错误文本。日志调用方
只能记录校验后的模式、数值或聚合 fingerprint，禁止输出环境原值、URL 与 Secret。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
import re

from app.modules.analysis.domain.models import (
    ANALYSIS_CLASSIFICATION_MODE_LEGACY,
    ANALYSIS_CLASSIFICATION_MODE_TOPK_SINGLE,
    ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE,
    ANALYSIS_CLASSIFICATION_MODES,
    ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
    ANALYSIS_DATA_STANDARD_MODE_LEGACY,
    ANALYSIS_DATA_STANDARD_MODES,
    ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD,
    ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY,
    ANALYSIS_FILENAME_CONSTRAINT_MODES,
    ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE,
    ANALYSIS_IDENTITY_RESELECT_MODE_OFF,
    ANALYSIS_IDENTITY_RESELECT_MODE_SHADOW,
    ANALYSIS_IDENTITY_RESELECT_MODES,
)
from app.modules.tasks.http_deadlines import required_http_lease_seconds


ANALYSIS_RUNTIME_MODE_SINGLE_INSTANCE = "single_instance"
_MAX_ANALYSIS_INFRASTRUCTURE_SECONDS = 7 * 24 * 60 * 60
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


class AnalysisClassificationConfigurationError(RuntimeError):
    """领域分类、保护模式或固定合同上限非法时抛出。"""


class AnalysisInfrastructureConfigurationError(RuntimeError):
    """文件分析 Dispatcher、Callback Guard 或恢复配置不满足安全边界。"""


@dataclass(frozen=True, slots=True)
class AnalysisExecutionCapabilityConfig:
    """部署方声明的 Analysis AnythingLLM 实例与模型身份摘要。

    原始 URL、API Key 和模型名称不得进入 Input v5。永久知识写与临时 RAG 当前使用
    同一 AnythingLLM 部署，所以知识 Provider 复用 ``rag_provider_fingerprint``；若
    未来拆分部署，必须升版 Profile 并引入独立能力声明，不能继续复用。
    """

    rag_provider_fingerprint: str
    rag_model_fingerprint: str

    def __post_init__(self) -> None:
        for name in ("rag_provider_fingerprint", "rag_model_fingerprint"):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or _SHA256_PATTERN.fullmatch(value.strip()) is None
            ):
                raise AnalysisInfrastructureConfigurationError(
                    f"{name} 必须是 64 位 SHA-256 十六进制摘要"
                )
            object.__setattr__(self, name, value.strip().lower())


@dataclass(frozen=True)
class AnalysisClassificationConfig:
    """``/llm/analysis`` 领域分类运行模式与不可变合同上限。"""

    mode: str = ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE
    filename_constraint_mode: str = ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
    data_standard_mode: str = ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD
    identity_reselect_mode: str = ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE
    model_candidate_limit: int = 128
    classification_prompt_char_limit: int = 32_000
    base_leaf_limit: int = 64
    parent_candidate_limit: int = 16

    def __post_init__(self) -> None:
        if not isinstance(self.mode, str):
            raise AnalysisClassificationConfigurationError(
                "DOCSENSE_ANALYSIS_CLASSIFICATION_MODE 必须是字符串"
            )
        mode = self.mode.strip().lower()
        if mode not in ANALYSIS_CLASSIFICATION_MODES:
            allowed = ", ".join(sorted(ANALYSIS_CLASSIFICATION_MODES))
            raise AnalysisClassificationConfigurationError(
                "DOCSENSE_ANALYSIS_CLASSIFICATION_MODE 配置非法："
                f"{self.mode!r}；仅支持 {allowed}"
            )
        if not isinstance(self.filename_constraint_mode, str):
            raise AnalysisClassificationConfigurationError(
                "DOCSENSE_ANALYSIS_FILENAME_CONSTRAINT_MODE 必须是字符串"
            )
        filename_mode = self.filename_constraint_mode.strip().lower()
        if filename_mode not in ANALYSIS_FILENAME_CONSTRAINT_MODES:
            allowed = ", ".join(sorted(ANALYSIS_FILENAME_CONSTRAINT_MODES))
            raise AnalysisClassificationConfigurationError(
                "DOCSENSE_ANALYSIS_FILENAME_CONSTRAINT_MODE 配置非法："
                f"{self.filename_constraint_mode!r}；仅支持 {allowed}"
            )
        if not isinstance(self.identity_reselect_mode, str):
            raise AnalysisClassificationConfigurationError(
                "DOCSENSE_ANALYSIS_IDENTITY_RESELECT_MODE 必须是字符串"
            )
        identity_mode = self.identity_reselect_mode.strip().lower()
        if identity_mode not in ANALYSIS_IDENTITY_RESELECT_MODES:
            allowed = ", ".join(sorted(ANALYSIS_IDENTITY_RESELECT_MODES))
            raise AnalysisClassificationConfigurationError(
                "DOCSENSE_ANALYSIS_IDENTITY_RESELECT_MODE 配置非法："
                f"{self.identity_reselect_mode!r}；仅支持 {allowed}"
            )
        for field_name in (
            "model_candidate_limit",
            "classification_prompt_char_limit",
            "base_leaf_limit",
            "parent_candidate_limit",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise AnalysisClassificationConfigurationError(
                    f"{field_name} 必须是正整数"
                )
        hard_limits = {
            "model_candidate_limit": 128,
            "classification_prompt_char_limit": 32_000,
            "base_leaf_limit": 64,
            "parent_candidate_limit": 16,
        }
        for field_name, hard_limit in hard_limits.items():
            if getattr(self, field_name) > hard_limit:
                raise AnalysisClassificationConfigurationError(
                    f"{field_name} 不得超过硬上限 {hard_limit}"
                )
        if self.base_leaf_limit + self.parent_candidate_limit > self.model_candidate_limit:
            raise AnalysisClassificationConfigurationError(
                "base_leaf_limit 与 parent_candidate_limit 之和"
                "不得超过 model_candidate_limit"
            )
        if not isinstance(self.data_standard_mode, str):
            raise AnalysisClassificationConfigurationError(
                "DOCSENSE_ANALYSIS_DATA_STANDARD_MODE 必须是字符串"
            )
        data_mode = self.data_standard_mode.strip().lower()
        if data_mode not in ANALYSIS_DATA_STANDARD_MODES:
            allowed = ", ".join(sorted(ANALYSIS_DATA_STANDARD_MODES))
            raise AnalysisClassificationConfigurationError(
                "DOCSENSE_ANALYSIS_DATA_STANDARD_MODE 配置非法："
                f"{self.data_standard_mode!r}；仅支持 {allowed}"
            )
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "filename_constraint_mode", filename_mode)
        object.__setattr__(self, "identity_reselect_mode", identity_mode)
        object.__setattr__(self, "data_standard_mode", data_mode)

    @classmethod
    def topk_two_stage(cls) -> "AnalysisClassificationConfig":
        return cls(mode=ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE)


@dataclass(frozen=True)
class AnalysisInfrastructureConfig:
    """现有单实例 Analysis 调度、维护与 Callback Guard 参数。"""

    runtime_mode: str = ANALYSIS_RUNTIME_MODE_SINGLE_INSTANCE
    scan_interval_seconds: float = 1.0
    accepted_batch_size: int = 50
    dispatch_retry_base_seconds: float = 5.0
    dispatch_retry_max_seconds: float = 300.0
    resource_sweep_interval_seconds: float = 30.0
    resource_sweep_batch_size: int = 50
    resource_close_running_grace_seconds: float = 300.0
    running_alert_seconds: float = 30.0
    stop_timeout_seconds: float = 5.0
    callback_http_timeout_seconds: float = 10.0
    callback_lease_seconds: float = 30.0

    def __post_init__(self) -> None:
        mode = str(self.runtime_mode or "").strip().lower()
        if mode != ANALYSIS_RUNTIME_MODE_SINGLE_INSTANCE:
            raise AnalysisInfrastructureConfigurationError(
                "当前仅安装 single_instance 文件分析调度基础设施；"
                "多实例模式必须等待共享数据库、可靠队列和分布式租约完成"
            )
        object.__setattr__(self, "runtime_mode", mode)
        for name in (
            "scan_interval_seconds",
            "dispatch_retry_base_seconds",
            "dispatch_retry_max_seconds",
            "resource_sweep_interval_seconds",
            "resource_close_running_grace_seconds",
            "running_alert_seconds",
            "stop_timeout_seconds",
            "callback_http_timeout_seconds",
            "callback_lease_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise AnalysisInfrastructureConfigurationError(
                    f"{name} 必须是正有限数字"
                )
            normalized = float(value)
            if (
                normalized != normalized
                or normalized in (float("inf"), float("-inf"))
                or normalized <= 0.0
                or normalized > _MAX_ANALYSIS_INFRASTRUCTURE_SECONDS
            ):
                raise AnalysisInfrastructureConfigurationError(
                    f"{name} 必须是 0 到 {_MAX_ANALYSIS_INFRASTRUCTURE_SECONDS} "
                    "之间的有限数字"
                )
            object.__setattr__(self, name, normalized)
        for name in ("accepted_batch_size", "resource_sweep_batch_size"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise AnalysisInfrastructureConfigurationError(
                    f"{name} 必须是 1~1000 的整数"
                )
            if value < 1 or value > 1000:
                raise AnalysisInfrastructureConfigurationError(
                    f"{name} 必须是 1~1000 的整数"
                )
        if self.dispatch_retry_max_seconds < self.dispatch_retry_base_seconds:
            raise AnalysisInfrastructureConfigurationError(
                "dispatch_retry_max_seconds 不能小于 dispatch_retry_base_seconds"
            )
        required_lease = required_http_lease_seconds(self.callback_http_timeout_seconds)
        if self.callback_lease_seconds <= required_lease:
            raise AnalysisInfrastructureConfigurationError(
                "callback_lease_seconds 必须严格大于连接、响应读取和安全余量，"
                f"当前必须大于 {required_lease:.3f} 秒"
            )

    @classmethod
    def single_instance(cls) -> "AnalysisInfrastructureConfig":
        return cls(runtime_mode=ANALYSIS_RUNTIME_MODE_SINGLE_INSTANCE)


def load_analysis_classification_config() -> AnalysisClassificationConfig:
    raw_mode = os.getenv("DOCSENSE_ANALYSIS_CLASSIFICATION_MODE")
    raw_filename_mode = os.getenv("DOCSENSE_ANALYSIS_FILENAME_CONSTRAINT_MODE")
    raw_data_mode = os.getenv("DOCSENSE_ANALYSIS_DATA_STANDARD_MODE")
    raw_identity_mode = os.getenv("DOCSENSE_ANALYSIS_IDENTITY_RESELECT_MODE")
    if all(
        item is None
        for item in (raw_mode, raw_filename_mode, raw_data_mode, raw_identity_mode)
    ):
        return AnalysisClassificationConfig.topk_two_stage()
    return AnalysisClassificationConfig(
        mode=(ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE if raw_mode is None else raw_mode),
        filename_constraint_mode=(
            ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
            if raw_filename_mode is None
            else raw_filename_mode
        ),
        data_standard_mode=(
            ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD
            if raw_data_mode is None
            else raw_data_mode
        ),
        identity_reselect_mode=(
            ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE
            if raw_identity_mode is None
            else raw_identity_mode
        ),
    )


def _strict_float(name: str, default: float) -> float:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        return float(raw_value)
    except (TypeError, ValueError) as exc:
        raise AnalysisInfrastructureConfigurationError(
            f"{name} 必须是正有限数字"
        ) from exc


def _strict_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, str(default)).strip()
    try:
        return int(raw_value)
    except (TypeError, ValueError) as exc:
        raise AnalysisInfrastructureConfigurationError(
            f"{name} 必须是 1~1000 的整数"
        ) from exc


def load_analysis_infrastructure_config() -> AnalysisInfrastructureConfig:
    return AnalysisInfrastructureConfig(
        runtime_mode=os.getenv(
            "DOCSENSE_ANALYSIS_RUNTIME_MODE",
            ANALYSIS_RUNTIME_MODE_SINGLE_INSTANCE,
        ),
        scan_interval_seconds=_strict_float(
            "DOCSENSE_ANALYSIS_DISPATCH_SCAN_INTERVAL_SECONDS", 1.0
        ),
        accepted_batch_size=_strict_int("DOCSENSE_ANALYSIS_DISPATCH_BATCH_SIZE", 50),
        dispatch_retry_base_seconds=_strict_float(
            "DOCSENSE_ANALYSIS_DISPATCH_RETRY_BASE_SECONDS", 5.0
        ),
        dispatch_retry_max_seconds=_strict_float(
            "DOCSENSE_ANALYSIS_DISPATCH_RETRY_MAX_SECONDS", 300.0
        ),
        resource_sweep_interval_seconds=_strict_float(
            "DOCSENSE_ANALYSIS_RESOURCE_SWEEP_INTERVAL_SECONDS", 30.0
        ),
        resource_sweep_batch_size=_strict_int(
            "DOCSENSE_ANALYSIS_RESOURCE_SWEEP_BATCH_SIZE", 50
        ),
        resource_close_running_grace_seconds=_strict_float(
            "DOCSENSE_ANALYSIS_RESOURCE_CLOSE_RUNNING_GRACE_SECONDS", 300.0
        ),
        running_alert_seconds=_strict_float(
            "DOCSENSE_ANALYSIS_RUNNING_ALERT_SECONDS", 30.0
        ),
        stop_timeout_seconds=_strict_float("DOCSENSE_ANALYSIS_STOP_TIMEOUT_SECONDS", 5.0),
        callback_http_timeout_seconds=_strict_float(
            "DOCSENSE_ANALYSIS_CALLBACK_HTTP_TIMEOUT_SECONDS", 10.0
        ),
        callback_lease_seconds=_strict_float(
            "DOCSENSE_ANALYSIS_CALLBACK_LEASE_SECONDS", 30.0
        ),
    )


def load_analysis_execution_capability_config(
    environment: Mapping[str, str] | None = None,
) -> AnalysisExecutionCapabilityConfig:
    """读取 Input v5 新受理必需的两项部署能力；缺失或伪值时失败关闭。"""

    source = os.environ if environment is None else environment
    return AnalysisExecutionCapabilityConfig(
        rag_provider_fingerprint=str(
            source.get("DOCSENSE_ANALYSIS_RAG_PROVIDER_FINGERPRINT", "")
        ),
        rag_model_fingerprint=str(
            source.get("DOCSENSE_ANALYSIS_RAG_MODEL_FINGERPRINT", "")
        ),
    )


__all__ = [
    "ANALYSIS_CLASSIFICATION_MODE_LEGACY",
    "ANALYSIS_CLASSIFICATION_MODE_TOPK_SINGLE",
    "ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE",
    "ANALYSIS_DATA_STANDARD_MODE_LEGACY",
    "ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD",
    "ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY",
    "ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD",
    "ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE",
    "ANALYSIS_IDENTITY_RESELECT_MODE_OFF",
    "ANALYSIS_IDENTITY_RESELECT_MODE_SHADOW",
    "ANALYSIS_RUNTIME_MODE_SINGLE_INSTANCE",
    "AnalysisClassificationConfig",
    "AnalysisClassificationConfigurationError",
    "AnalysisInfrastructureConfig",
    "AnalysisInfrastructureConfigurationError",
    "AnalysisExecutionCapabilityConfig",
    "load_analysis_classification_config",
    "load_analysis_infrastructure_config",
    "load_analysis_execution_capability_config",
]
