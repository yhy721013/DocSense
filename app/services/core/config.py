from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv
load_dotenv()  # 加载 .env 文件到环境变量，但不覆盖已显式传入的值

from app.modules.tasks.http_deadlines import required_http_lease_seconds
from app.services.core.settings import (
    LLM_DOWNLOAD_DIR,
    LLM_TASK_DB_PATH,
    MINERU_CACHE_DIR,
    OCR_CACHE_DIR,
)


@dataclass(frozen=True)
class AnythingLLMConfig:
    base_url: str
    api_key: str
    timeout: Optional[float]
    storage_root: Optional[str]


@dataclass(frozen=True)
class OCRConfig:
    enabled: bool
    languages: str
    dpi: int
    sample_pages: int
    text_threshold: int
    cache_dir: str
    analysis_scanned_pdf_engine: str
    mineru_cache_dir: str
    mineru_lang: str
    mineru_api_url: Optional[str]
    tessdata_prefix: Optional[str]


@dataclass(frozen=True)
class LLMIntegrationConfig:
    callback_url: Optional[str]
    callback_timeout: float
    task_db_path: str
    download_timeout: float
    download_dir: str


ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE = "topk_two_stage"
ANALYSIS_CLASSIFICATION_MODE_TOPK_SINGLE = "topk_single"
ANALYSIS_CLASSIFICATION_MODE_LEGACY = "legacy"
ANALYSIS_CLASSIFICATION_MODES = frozenset(
    {
        ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE,
        ANALYSIS_CLASSIFICATION_MODE_TOPK_SINGLE,
        ANALYSIS_CLASSIFICATION_MODE_LEGACY,
    }
)

ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY = "legacy"
ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD = "scope_guard"
ANALYSIS_FILENAME_CONSTRAINT_MODES = frozenset(
    {
        ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY,
        ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD,
    }
)

ANALYSIS_DATA_STANDARD_MODE_LEGACY = "legacy"
ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD = "scope_guard"
ANALYSIS_DATA_STANDARD_MODES = frozenset(
    {
        ANALYSIS_DATA_STANDARD_MODE_LEGACY,
        ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
    }
)

ANALYSIS_IDENTITY_RESELECT_MODE_OFF = "off"
ANALYSIS_IDENTITY_RESELECT_MODE_SHADOW = "shadow"
ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE = "enforce"
ANALYSIS_IDENTITY_RESELECT_MODES = frozenset(
    {
        ANALYSIS_IDENTITY_RESELECT_MODE_OFF,
        ANALYSIS_IDENTITY_RESELECT_MODE_SHADOW,
        ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE,
    }
)


class AnalysisClassificationConfigurationError(RuntimeError):
    """领域分类、保护模式或固定合同上限非法时抛出。"""


@dataclass(frozen=True)
class AnalysisClassificationConfig:
    """``/llm/analysis`` 领域分类运行模式与不可变合同上限。"""

    mode: str = ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE
    filename_constraint_mode: str = (
        ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
    )
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
        filename_constraint_mode = self.filename_constraint_mode.strip().lower()
        if filename_constraint_mode not in ANALYSIS_FILENAME_CONSTRAINT_MODES:
            allowed = ", ".join(sorted(ANALYSIS_FILENAME_CONSTRAINT_MODES))
            raise AnalysisClassificationConfigurationError(
                "DOCSENSE_ANALYSIS_FILENAME_CONSTRAINT_MODE 配置非法："
                f"{self.filename_constraint_mode!r}；仅支持 {allowed}"
            )

        if not isinstance(self.identity_reselect_mode, str):
            raise AnalysisClassificationConfigurationError(
                "DOCSENSE_ANALYSIS_IDENTITY_RESELECT_MODE 必须是字符串"
            )
        identity_reselect_mode = self.identity_reselect_mode.strip().lower()
        if identity_reselect_mode not in ANALYSIS_IDENTITY_RESELECT_MODES:
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

        object.__setattr__(self, "mode", mode)
        object.__setattr__(
            self,
            "filename_constraint_mode",
            filename_constraint_mode,
        )
        object.__setattr__(
            self,
            "identity_reselect_mode",
            identity_reselect_mode,
        )

        if not isinstance(self.data_standard_mode, str):
            raise AnalysisClassificationConfigurationError(
                "DOCSENSE_ANALYSIS_DATA_STANDARD_MODE 必须是字符串"
            )
        data_standard_mode = self.data_standard_mode.strip().lower()
        if data_standard_mode not in ANALYSIS_DATA_STANDARD_MODES:
            allowed = ", ".join(sorted(ANALYSIS_DATA_STANDARD_MODES))
            raise AnalysisClassificationConfigurationError(
                "DOCSENSE_ANALYSIS_DATA_STANDARD_MODE 配置非法："
                f"{self.data_standard_mode!r}；仅支持 {allowed}"
            )
        object.__setattr__(
            self,
            "data_standard_mode",
            data_standard_mode,
        )

    @classmethod
    def topk_two_stage(cls) -> "AnalysisClassificationConfig":
        """创建不依赖环境变量的两阶段默认配置。"""
        return cls(mode=ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE)


CHAT_RUNTIME_MODE_SINGLE_INSTANCE = "single_instance"
REPORT_RUNTIME_MODE_SINGLE_INSTANCE = "single_instance"


class ChatInfrastructureConfigurationError(RuntimeError):
    """文件对话部署模式与已安装基础设施能力不匹配时抛出。"""


class ReportInfrastructureConfigurationError(RuntimeError):
    """报告调度或资源恢复配置不满足当前单实例安全边界时抛出。"""


@dataclass(frozen=True)
class ChatInfrastructureConfig:
    """文件对话的部署门禁配置。

    当前代码只安装了 SQLite 单实例持久化、进程内同步执行器和轮询式取消检查。
    因此 ``single_instance`` 是唯一可启动的值；集群、外部调度或共享持久化模式
    必须等到对应适配器、迁移和运维验证完成后，才能在此处开放。
    """

    runtime_mode: str = CHAT_RUNTIME_MODE_SINGLE_INSTANCE

    def __post_init__(self) -> None:
        mode = str(self.runtime_mode or "").strip().lower()
        if mode != CHAT_RUNTIME_MODE_SINGLE_INSTANCE:
            raise ChatInfrastructureConfigurationError(
                "当前仅安装 single_instance 文件对话基础设施；"
                "集群或外部调度模式必须先完成持久化、调度和通知适配器部署"
            )
        object.__setattr__(self, "runtime_mode", mode)

    @classmethod
    def single_instance(cls) -> "ChatInfrastructureConfig":
        """创建不依赖环境变量的单实例配置，供显式注入的离线测试使用。"""
        return cls(runtime_mode=CHAT_RUNTIME_MODE_SINGLE_INSTANCE)


@dataclass(frozen=True)
class ReportInfrastructureConfig:
    """阶段 1C 报告本地调度器与资源恢复的内部配置。

    当前 Dispatcher 依赖 SQLite 作为持久积压事实，并使用一条报告执行线程、两条隔离
    维护线程和 ``threading.Event`` 常量空间唤醒，因此只允许 ``single_instance``。
    报告生成继续沿用 ``ANYTHINGLLM_TIMEOUT``；``cleanup_http_timeout_seconds`` 仅用于
    幂等 DELETE 清理租约，必须是有限值，才能在 Worker 失联后安全判断租约是否过期。
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
            raise ReportInfrastructureConfigurationError(
                "当前仅安装 single_instance 报告调度基础设施；"
                "多实例模式必须等待共享数据库、可靠队列和分布式租约完成"
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
                raise ReportInfrastructureConfigurationError(
                    f"{name} 必须是正有限数字"
                )
            normalized = float(value)
            if (
                normalized != normalized
                or normalized in (float("inf"), float("-inf"))
                or normalized <= 0.0
            ):
                raise ReportInfrastructureConfigurationError(
                    f"{name} 必须是正有限数字"
                )
            object.__setattr__(self, name, normalized)

        for name in (
            "accepted_batch_size",
            "resource_sweep_limit",
            "running_sample_limit",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ReportInfrastructureConfigurationError(
                    f"{name} 必须是 1~1000 的整数"
                )
            if value < 1 or value > 1000:
                raise ReportInfrastructureConfigurationError(
                    f"{name} 必须是 1~1000 的整数"
                )

        required_lease = required_http_lease_seconds(
            self.cleanup_http_timeout_seconds
        )
        if self.cleanup_lease_seconds < required_lease:
            raise ReportInfrastructureConfigurationError(
                "cleanup_lease_seconds 必须覆盖连接、响应读取和安全余量，"
                f"当前至少需要 {required_lease:.3f} 秒"
            )
        if (
            isinstance(self.max_download_bytes, bool)
            or not isinstance(self.max_download_bytes, int)
            or self.max_download_bytes < 1
            or self.max_download_bytes > 10 * 1024**4
        ):
            raise ReportInfrastructureConfigurationError(
                "max_download_bytes 必须是 1 字节到 10 TiB 的整数"
            )

    @classmethod
    def single_instance(cls) -> "ReportInfrastructureConfig":
        """创建不读取环境变量的安全默认配置，供离线组合测试显式注入。"""

        return cls(runtime_mode=REPORT_RUNTIME_MODE_SINGLE_INSTANCE)


def _parse_timeout(raw_value: Optional[str]) -> Optional[float]:
    # 支持空值 / None 字符串，返回 None 表示不设超时
    if raw_value is None:
        return None
    value = raw_value.strip().lower()
    if value in {"", "none", "null"}:
        return None
    return float(value)


def _parse_optional_str(raw_value: Optional[str]) -> Optional[str]:
    if raw_value is None:
        return None
    value = raw_value.strip()
    return value if value else None


def _parse_bool(raw_value: Optional[str], default: bool) -> bool:
    if raw_value is None:
        return default
    value = raw_value.strip().lower()
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _parse_int(raw_value: Optional[str], default: int, *, min_value: int = 0) -> int:
    if raw_value is None:
        return default
    try:
        value = int(raw_value.strip())
    except (TypeError, ValueError):
        return default
    return value if value >= min_value else default


def _parse_choice(raw_value: Optional[str], default: str, allowed: set[str]) -> str:
    if raw_value is None:
        return default
    value = raw_value.strip().lower()
    return value if value in allowed else default


def load_anythingllm_config() -> AnythingLLMConfig:
    return AnythingLLMConfig(
        base_url=os.getenv("ANYTHINGLLM_BASE_URL").strip(),
        api_key=os.getenv("ANYTHINGLLM_API_KEY").strip(),
        timeout=_parse_timeout(os.getenv("ANYTHINGLLM_TIMEOUT")),
        storage_root=_parse_optional_str(os.getenv("ANYTHINGLLM_STORAGE_ROOT")),
    )


def load_ocr_config() -> OCRConfig:
    return OCRConfig(
        enabled=_parse_bool(os.getenv("DOCSENSE_OCR_ENABLED"), True),
        languages=os.getenv("DOCSENSE_OCR_LANGUAGES", "chi_sim+eng").strip() or "chi_sim+eng",
        dpi=_parse_int(os.getenv("DOCSENSE_OCR_DPI"), 300, min_value=50),
        sample_pages=_parse_int(os.getenv("DOCSENSE_OCR_SAMPLE_PAGES"), 3, min_value=1),
        text_threshold=_parse_int(os.getenv("DOCSENSE_OCR_TEXT_THRESHOLD"), 50, min_value=0),
        cache_dir=str(OCR_CACHE_DIR),
        analysis_scanned_pdf_engine=_parse_choice(
            os.getenv("DOCSENSE_ANALYSIS_SCANNED_PDF_ENGINE"),
            "mineru",
            {"mineru", "ocr"},
        ),
        mineru_cache_dir=str(MINERU_CACHE_DIR),
        mineru_lang=os.getenv("DOCSENSE_MINERU_LANG", "ch").strip() or "ch",
        mineru_api_url=_parse_optional_str(os.getenv("DOCSENSE_MINERU_API_URL")),
        tessdata_prefix=_parse_optional_str(os.getenv("TESSDATA_PREFIX")),
    )


def load_llm_integration_config() -> LLMIntegrationConfig:
    return LLMIntegrationConfig(
        callback_url=_parse_optional_str(os.getenv("CALLBACK_URL")),
        callback_timeout=float(os.getenv("CALLBACK_TIMEOUT", "10").strip() or "10"),
        task_db_path=str(LLM_TASK_DB_PATH),
        download_timeout=float(os.getenv("FILE_DOWNLOAD_TIMEOUT", "60").strip() or "60"),
        download_dir=str(LLM_DOWNLOAD_DIR),
    )


def load_analysis_classification_config() -> AnalysisClassificationConfig:
    """读取并严格校验领域分类、文件名约束和保护模式。

    环境变量缺失时分类、文件名约束、数据标准和身份重选分别使用
    ``topk_two_stage``、``scope_guard``、``scope_guard`` 和 ``enforce``。显式空值
    或未知值都必须拒绝，避免误配时静默切换分类链路。
    """
    raw_mode = os.getenv("DOCSENSE_ANALYSIS_CLASSIFICATION_MODE")
    raw_filename_constraint_mode = os.getenv(
        "DOCSENSE_ANALYSIS_FILENAME_CONSTRAINT_MODE"
    )
    raw_data_standard_mode = os.getenv(
        "DOCSENSE_ANALYSIS_DATA_STANDARD_MODE"
    )
    raw_identity_reselect_mode = os.getenv(
        "DOCSENSE_ANALYSIS_IDENTITY_RESELECT_MODE"
    )
    if (
        raw_mode is None
        and raw_filename_constraint_mode is None
        and raw_data_standard_mode is None
        and raw_identity_reselect_mode is None
    ):
        return AnalysisClassificationConfig.topk_two_stage()
    return AnalysisClassificationConfig(
        mode=(
            ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE
            if raw_mode is None
            else raw_mode
        ),
        filename_constraint_mode=(
            ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
            if raw_filename_constraint_mode is None
            else raw_filename_constraint_mode
        ),
        data_standard_mode=(
            ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD
            if raw_data_standard_mode is None
            else raw_data_standard_mode
        ),
        identity_reselect_mode=(
            ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE
            if raw_identity_reselect_mode is None
            else raw_identity_reselect_mode
        ),
    )


def load_chat_infrastructure_config() -> ChatInfrastructureConfig:
    """读取并严格校验文件对话部署模式，错误模式在应用装配前直接失败。

    不使用宽松的默认回退：若操作人员误配 ``cluster``、``external_dispatcher``
    或任意未知值，服务必须拒绝启动，而不是在 SQLite 文件上伪装多实例能力。
    """
    raw_mode = os.getenv(
        "DOCSENSE_CHAT_RUNTIME_MODE",
        CHAT_RUNTIME_MODE_SINGLE_INSTANCE,
    )
    return ChatInfrastructureConfig(runtime_mode=raw_mode)


def _strict_report_float(name: str, default: float) -> float:
    """读取报告内部浮点配置；误配时拒绝启动而不是静默回退。"""

    raw_value = os.getenv(name, str(default)).strip()
    try:
        return float(raw_value)
    except (TypeError, ValueError) as exc:
        raise ReportInfrastructureConfigurationError(
            f"{name} 必须是正有限数字"
        ) from exc


def _strict_report_int(name: str, default: int) -> int:
    """读取报告有界批量配置；布尔文本和小数均不得被宽松转换。"""

    raw_value = os.getenv(name, str(default)).strip()
    try:
        return int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ReportInfrastructureConfigurationError(
            f"{name} 必须是 1~1000 的整数"
        ) from exc


def _strict_report_bytes(name: str, default: int) -> int:
    """读取报告单文件字节上限；具体安全范围由配置对象统一校验。"""

    raw_value = os.getenv(name, str(default)).strip()
    try:
        return int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ReportInfrastructureConfigurationError(
            f"{name} 必须是正整数字节数"
        ) from exc


def load_report_infrastructure_config() -> ReportInfrastructureConfig:
    """读取并严格校验阶段 1C 报告 Dispatcher/清理恢复配置。

    这些值全部是后端内部容量与租约参数，不属于甲方 HTTP/WebSocket 契约。清理
    HTTP 超时与租约分离，避免全局 ``ANYTHINGLLM_TIMEOUT`` 为空时让幂等删除永久
    占用恢复权；报告生成和模型查询仍继续使用原全局超时口径。
    """

    return ReportInfrastructureConfig(
        runtime_mode=os.getenv(
            "DOCSENSE_REPORT_RUNTIME_MODE",
            REPORT_RUNTIME_MODE_SINGLE_INSTANCE,
        ),
        scan_interval_seconds=_strict_report_float(
            "DOCSENSE_REPORT_SCAN_INTERVAL_SECONDS",
            1.0,
        ),
        accepted_batch_size=_strict_report_int(
            "DOCSENSE_REPORT_ACCEPTED_BATCH_SIZE",
            50,
        ),
        dispatch_failure_retry_seconds=_strict_report_float(
            "DOCSENSE_REPORT_DISPATCH_FAILURE_RETRY_SECONDS",
            30.0,
        ),
        resource_sweep_interval_seconds=_strict_report_float(
            "DOCSENSE_REPORT_RESOURCE_SWEEP_INTERVAL_SECONDS",
            30.0,
        ),
        resource_sweep_limit=_strict_report_int(
            "DOCSENSE_REPORT_RESOURCE_SWEEP_LIMIT",
            50,
        ),
        running_sample_limit=_strict_report_int(
            "DOCSENSE_REPORT_RUNNING_SAMPLE_LIMIT",
            20,
        ),
        stop_timeout_seconds=_strict_report_float(
            "DOCSENSE_REPORT_STOP_TIMEOUT_SECONDS",
            5.0,
        ),
        cleanup_http_timeout_seconds=_strict_report_float(
            "DOCSENSE_REPORT_CLEANUP_HTTP_TIMEOUT_SECONDS",
            60.0,
        ),
        cleanup_lease_seconds=_strict_report_float(
            "DOCSENSE_REPORT_CLEANUP_LEASE_SECONDS",
            130.0,
        ),
        max_download_bytes=_strict_report_bytes(
            "DOCSENSE_REPORT_MAX_DOWNLOAD_BYTES",
            512 * 1024 * 1024,
        ),
    )
