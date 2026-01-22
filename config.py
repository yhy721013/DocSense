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


def _parse_timeout(raw_value: Optional[str]) -> Optional[float]:
    # 支持空值 / None 字符串，返回 None 表示不设超时
    if raw_value is None:
        return None
    value = raw_value.strip().lower()
    if value in {"", "none", "null"}:
        return None
    return float(value)


def load_anythingllm_config() -> AnythingLLMConfig:
    return AnythingLLMConfig(
        base_url=os.getenv("ANYTHINGLLM_BASE_URL", DEFAULT_API_BASE_URL),
        api_key=os.getenv("ANYTHINGLLM_API_KEY", DEFAULT_API_KEY),
        timeout=_parse_timeout(os.getenv("ANYTHINGLLM_TIMEOUT")),
    )
