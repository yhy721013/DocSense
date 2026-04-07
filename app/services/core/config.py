from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv
load_dotenv()  # 加载 .env 文件到环境变量，但不覆盖已显式传入的值


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
    tessdata_prefix: Optional[str]


@dataclass(frozen=True)
class LLMIntegrationConfig:
    callback_url: Optional[str]
    callback_timeout: float
    task_db_path: str
    download_timeout: float
    download_dir: str


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
        cache_dir=os.getenv("DOCSENSE_OCR_CACHE_DIR", ".runtime/ocr_markdown").strip() or ".runtime/ocr_markdown",
        tessdata_prefix=_parse_optional_str(os.getenv("TESSDATA_PREFIX")),
    )


def load_llm_integration_config() -> LLMIntegrationConfig:
    return LLMIntegrationConfig(
        callback_url=_parse_optional_str(os.getenv("CALLBACK_URL")),
        callback_timeout=float(os.getenv("CALLBACK_TIMEOUT", "10").strip() or "10"),
        task_db_path=os.getenv("DOCSENSE_LLM_TASK_DB", "../../../.runtime/llm_tasks.sqlite3").strip()
        or ".runtime/llm_tasks.sqlite3",
        download_timeout=float(os.getenv("FILE_DOWNLOAD_TIMEOUT", "60").strip() or "60"),
        download_dir=os.getenv("FILE_DOWNLOAD_DIR", "../../../.runtime/llm_downloads").strip()
        or ".runtime/llm_downloads",
    )
