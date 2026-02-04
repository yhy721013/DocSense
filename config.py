from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


# AnythingLLM 默认连接配置，可通过环境变量覆盖
DEFAULT_API_BASE_URL = "http://localhost:3001/api/v1"
DEFAULT_API_KEY = "ZRVHTGG-6FN47RS-N2QHYDW-ZEHR8X4"


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
        base_url=os.getenv("ANYTHINGLLM_BASE_URL", DEFAULT_API_BASE_URL),
        api_key=os.getenv("ANYTHINGLLM_API_KEY", DEFAULT_API_KEY),
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
