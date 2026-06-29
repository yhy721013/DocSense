"""
BM25 索引缓存。

按 workspace_slug 缓存 BM25 检索器实例（含已构建的索引），
避免每次字段检索都重新拉取 chunks、重建 BM25 索引。

v3.0 新增：
- workspace 级别索引缓存，TTL 自动过期
- 缓存命中/未命中/构建/过期日志记录到 .runtime/rag_cache.log
- 线程安全（threading.Lock）
"""
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# 缓存日志专用 logger
_cache_logger = logging.getLogger("rag_cache")
_cache_logger.setLevel(logging.INFO)

# .runtime 日志文件路径
_RUNTIME_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", ".runtime")
_RUNTIME_DIR = os.path.normpath(_RUNTIME_DIR)
_CACHE_LOG_FILE = os.path.join(_RUNTIME_DIR, "rag_cache.log")


def _ensure_cache_logger():
    """确保缓存日志的文件 handler 已初始化。"""
    if not _cache_logger.handlers:
        try:
            os.makedirs(_RUNTIME_DIR, exist_ok=True)
            file_handler = logging.FileHandler(_CACHE_LOG_FILE, encoding="utf-8")
            file_handler.setLevel(logging.INFO)
            formatter = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            file_handler.setFormatter(formatter)
            _cache_logger.addHandler(file_handler)
            _cache_logger.propagate = False
        except Exception as e:
            logger.warning("初始化 RAG 缓存日志文件失败: %s", e)


def _log_cache_event(event: str, workspace_slug: str, **kwargs):
    """记录缓存事件到 .runtime/rag_cache.log。

    Args:
        event: 事件类型（hit, miss, build, expire, invalidate, clear）
        workspace_slug: 工作区标识
        **kwargs: 额外信息
    """
    _ensure_cache_logger()
    extra = " ".join(f"{k}={v}" for k, v in kwargs.items())
    msg = f"[{event}] workspace={workspace_slug}"
    if extra:
        msg += f" {extra}"
    _cache_logger.info(msg)


class BM25IndexCache:
    """BM25 索引缓存，按 workspace_slug 缓存检索器实例。

    线程安全，支持 TTL 自动过期。
    """

    def __init__(self, ttl_seconds: int = 1800):
        """初始化缓存。

        Args:
            ttl_seconds: 缓存存活时间（秒），默认 1800（30 分钟）
        """
        self._cache: Dict[str, Tuple[Any, float]] = {}
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        # 统计信息
        self._stats: Dict[str, int] = {
            "hits": 0,
            "misses": 0,
            "builds": 0,
            "expires": 0,
            "invalidates": 0,
        }

    def get(self, workspace_slug: str) -> Optional[Any]:
        """获取缓存的 BM25 检索器。

        Args:
            workspace_slug: 工作区标识

        Returns:
            BM25Retriever 实例，如果缓存未命中或已过期则返回 None
        """
        with self._lock:
            entry = self._cache.get(workspace_slug)
            if entry is None:
                self._stats["misses"] += 1
                _log_cache_event("miss", workspace_slug)
                return None

            retriever, timestamp = entry
            elapsed = time.time() - timestamp
            if elapsed > self._ttl:
                # 缓存已过期
                del self._cache[workspace_slug]
                self._stats["expires"] += 1
                _log_cache_event("expire", workspace_slug, ttl=self._ttl, elapsed=f"{elapsed:.1f}s")
                return None

            self._stats["hits"] += 1
            _log_cache_event("hit", workspace_slug, age=f"{elapsed:.1f}s", doc_count=len(getattr(retriever, "documents", [])))
            return retriever

    def put(self, workspace_slug: str, retriever: Any) -> None:
        """缓存 BM25 检索器。

        Args:
            workspace_slug: 工作区标识
            retriever: BM25Retriever 实例
        """
        with self._lock:
            self._cache[workspace_slug] = (retriever, time.time())
            self._stats["builds"] += 1
            doc_count = len(getattr(retriever, "documents", []))
            _log_cache_event("build", workspace_slug, doc_count=doc_count, ttl=self._ttl)

    def invalidate(self, workspace_slug: str) -> None:
        """使指定 workspace 的缓存失效。

        Args:
            workspace_slug: 工作区标识
        """
        with self._lock:
            if workspace_slug in self._cache:
                del self._cache[workspace_slug]
                self._stats["invalidates"] += 1
                _log_cache_event("invalidate", workspace_slug)

    def clear(self) -> None:
        """清空所有缓存。"""
        with self._lock:
            count = len(self._cache)
            self._cache.clear()
            _log_cache_event("clear", "ALL", cleared=count)

    def get_stats(self) -> Dict[str, int]:
        """获取缓存统计信息。

        Returns:
            统计信息字典
        """
        with self._lock:
            stats = dict(self._stats)
            stats["current_size"] = len(self._cache)
            return stats

    def get_cached_workspaces(self) -> list:
        """获取当前缓存的所有 workspace slug。"""
        with self._lock:
            return list(self._cache.keys())


# 全局单例
_cache_instance: Optional[BM25IndexCache] = None
_cache_lock = threading.Lock()


def get_bm25_cache(ttl_seconds: int = 1800) -> BM25IndexCache:
    """获取 BM25 索引缓存单例。

    Args:
        ttl_seconds: 缓存存活时间（秒），仅在首次调用时生效

    Returns:
        BM25IndexCache 实例
    """
    global _cache_instance
    if _cache_instance is None:
        with _cache_lock:
            if _cache_instance is None:
                _cache_instance = BM25IndexCache(ttl_seconds=ttl_seconds)
    return _cache_instance


def reset_bm25_cache() -> None:
    """重置 BM25 索引缓存单例（用于测试或重新配置）。"""
    global _cache_instance
    with _cache_lock:
        if _cache_instance is not None:
            _cache_instance.clear()
        _cache_instance = None
