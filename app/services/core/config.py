from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv
load_dotenv()  # 加载 .env 文件到环境变量，但不覆盖已显式传入的值

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


class AnalysisClassificationConfigurationError(RuntimeError):
    """领域分类运行模式或固定合同上限非法时抛出。"""


@dataclass(frozen=True)
class AnalysisClassificationConfig:
    """``/llm/analysis`` 领域分类运行模式与不可变合同上限。"""

    mode: str = ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE
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

    @classmethod
    def topk_two_stage(cls) -> "AnalysisClassificationConfig":
        """创建不依赖环境变量的两阶段默认配置。"""
        return cls(mode=ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE)


CHAT_RUNTIME_MODE_SINGLE_INSTANCE = "single_instance"


class ChatInfrastructureConfigurationError(RuntimeError):
    """文件对话部署模式与已安装基础设施能力不匹配时抛出。"""


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
    """读取并严格校验领域分类运行模式。

    仅在环境变量缺失时使用 ``topk_two_stage``；显式空值或未知值
    都必须拒绝，避免误配时静默切换分类链路。
    """
    raw_mode = os.getenv("DOCSENSE_ANALYSIS_CLASSIFICATION_MODE")
    if raw_mode is None:
        return AnalysisClassificationConfig.topk_two_stage()
    return AnalysisClassificationConfig(mode=raw_mode)


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
