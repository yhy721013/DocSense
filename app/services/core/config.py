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
        cache_dir=os.getenv("DOCSENSE_OCR_CACHE_DIR", ".runtime/ocr_markdown").strip() or ".runtime/ocr_markdown",
        analysis_scanned_pdf_engine=_parse_choice(
            os.getenv("DOCSENSE_ANALYSIS_SCANNED_PDF_ENGINE"),
            "mineru",
            {"mineru", "ocr"},
        ),
        mineru_cache_dir=os.getenv("DOCSENSE_MINERU_CACHE_DIR", ".runtime/mineru_markdown").strip()
        or ".runtime/mineru_markdown",
        mineru_lang=os.getenv("DOCSENSE_MINERU_LANG", "ch").strip() or "ch",
        mineru_api_url=_parse_optional_str(os.getenv("DOCSENSE_MINERU_API_URL")),
        tessdata_prefix=_parse_optional_str(os.getenv("TESSDATA_PREFIX")),
    )


@dataclass(frozen=True)
class RAGEnhancerConfig:
    """RAG 增强功能配置（BM25 + Embedding 双召回 + RRF + Reranker）。"""
    enabled: bool
    bm25_top_k: int
    embedding_top_k: int
    rrf_k: int
    rerank_enabled: bool
    rerank_model: str
    rerank_top_n: int
    rerank_batch_size: int
    # 新增：关键词提取和 LLM Query 重写配置
    use_bm25_keyword_extraction: bool
    use_llm_query_rewrite: bool
    llm_rewrite_model: str
    ollama_base_url: str


def load_rag_enhancer_config() -> RAGEnhancerConfig:
    return RAGEnhancerConfig(
        enabled=_parse_bool(os.getenv("RAG_ENHANCER_ENABLED"), False),
        bm25_top_k=_parse_int(os.getenv("RAG_BM25_TOP_K"), 20, min_value=1),
        embedding_top_k=_parse_int(os.getenv("RAG_EMBEDDING_TOP_K"), 20, min_value=1),
        rrf_k=_parse_int(os.getenv("RAG_RRF_K"), 60, min_value=1),
        rerank_enabled=_parse_bool(os.getenv("RAG_RERANK_ENABLED"), True),
        rerank_model=os.getenv("RAG_RERANK_MODEL", "BAAI/bge-reranker-v2-m3").strip() or "BAAI/bge-reranker-v2-m3",
        rerank_top_n=_parse_int(os.getenv("RAG_RERANK_TOP_N"), 5, min_value=1),
        rerank_batch_size=_parse_int(os.getenv("RAG_RERANK_BATCH_SIZE"), 32, min_value=1),
        # 新增配置项
        use_bm25_keyword_extraction=_parse_bool(os.getenv("RAG_BM25_KEYWORD_EXTRACTION"), True),
        use_llm_query_rewrite=_parse_bool(os.getenv("RAG_LLM_QUERY_REWRITE"), False),
        llm_rewrite_model=os.getenv("RAG_QUERY_REWRITE_MODEL", "qwen3:4b-instruct-2507-q4_K_M").strip() or "qwen3:4b-instruct-2507-q4_K_M",
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip() or "http://localhost:11434",
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
