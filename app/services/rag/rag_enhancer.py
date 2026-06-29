"""
RAG 增强器主模块。

提供 BM25 + Embedding 双召回、RRF 融合、BGE-Reranker 精排的完整链路。
支持关键词提取和 LLM Query 重写优化。
通过配置开关控制是否启用增强，未启用时降级为原始 AnythingLLM 向量检索。

v3.0 改进：
1. 修复硬依赖问题：rank_bm25 和 sentence_transformers 均为懒加载，未安装时不阻塞服务启动
2. 修复 is_enhanced() bug：仅检查配置是否启用，不再要求 BM25 索引预先就绪
3. 修复 _fetch_all_chunks 问题：从 AnythingLLM 存储目录直接读取文档全量文本构建 BM25 索引
4. 修复中文关键词提取问题：使用统一 tokenize() 函数（支持 jieba），不再过滤中文字符
5. 新增 workspace 级 BM25 索引缓存：避免每次检索都重新拉取 chunks 和重建索引
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.services.core.config import RAGEnhancerConfig, load_rag_enhancer_config
from app.services.utils.anythingllm_client import AnythingLLMClient
from app.services.rag.bm25_retriever import BM25Retriever
from app.services.rag.rrf_fusion import reciprocal_rank_fusion
from app.services.rag.bge_reranker import BGEReranker
from app.services.rag.chunk_reader import DocumentChunkReader
from app.services.rag.cache import get_bm25_cache

logger = logging.getLogger(__name__)


class RAGEnhancer:
    """RAG 增强器，封装双召回+融合+重排序完整流程。"""

    def __init__(self, config: Optional[RAGEnhancerConfig] = None):
        """初始化 RAG 增强器。

        所有可选依赖（rank_bm25、sentence_transformers）均为懒加载，
        未安装时不影响服务启动，仅在实际使用时降级。

        Args:
            config: RAG 增强配置，None 则从环境变量加载
        """
        self.config = config or load_rag_enhancer_config()

        # BM25 检索器模板（用于创建新实例）
        self._bm25_template = BM25Retriever(
            use_keyword_extraction=self.config.use_bm25_keyword_extraction,
            use_llm_rewrite=self.config.use_llm_query_rewrite,
        )

        # Reranker 懒加载
        self._reranker: Optional[BGEReranker] = None
        self._reranker_initialized = False

        # 文档 chunk 读取器
        self._chunk_reader = DocumentChunkReader()

        # BM25 索引缓存（按 workspace_slug 缓存）
        self._cache = get_bm25_cache(
            ttl_seconds=getattr(self.config, "bm25_cache_ttl", 1800),
        )

        logger.info(
            "RAG Enhancer 初始化完成: enabled=%s, bm25_top_k=%d, embedding_top_k=%d, "
            "rerank=%s, keyword_extraction=%s, llm_rewrite=%s, cache_ttl=%ds",
            self.config.enabled,
            self.config.bm25_top_k,
            self.config.embedding_top_k,
            self.config.rerank_enabled,
            self.config.use_bm25_keyword_extraction,
            self.config.use_llm_query_rewrite,
            getattr(self.config, "bm25_cache_ttl", 1800),
        )

    def hybrid_search(
        self,
        client: AnythingLLMClient,
        workspace_slug: str,
        query: str,
        top_n: int = 5,
        user_id: int = 1,
    ) -> List[Dict[str, Any]]:
        """执行混合检索（BM25 + Embedding + RRF + Rerank）。

        如果增强功能未启用或任何环节失败，自动降级为纯向量检索。

        v3.0 改进：
        - 使用 workspace 级 BM25 索引缓存，避免每次检索都重建索引
        - 从存储目录读取文档全量文本构建 BM25 索引
        - 配置启用即尝试进入增强检索，失败再 fallback

        Args:
            client: AnythingLLM 客户端
            workspace_slug: 工作区标识
            query: 查询文本
            top_n: 最终返回的结果数量
            user_id: 用户 ID

        Returns:
            检索结果列表，按相关性降序排列。
        """
        # 检查是否启用增强
        if not self.config.enabled:
            logger.debug("RAG 增强未启用，使用原始向量检索")
            return self._fallback_vector_search(client, workspace_slug, query, top_n, user_id)

        try:
            # 步骤 1: 获取或构建 BM25 索引（带缓存）
            bm25_retriever = self._get_or_build_bm25_index(client, workspace_slug, user_id)

            if bm25_retriever is None:
                logger.warning("BM25 索引构建失败，降级为向量检索")
                return self._fallback_vector_search(client, workspace_slug, query, top_n, user_id)

            # 步骤 2: 执行双路检索
            # 2a. BM25 检索
            bm25_results = bm25_retriever.search(query, top_k=self.config.bm25_top_k)

            # 2b. Embedding 检索（通过 AnythingLLM）
            embedding_results = client.vector_search(workspace_slug, query, user_id=user_id)
            embedding_results = embedding_results[: self.config.embedding_top_k]

            if not bm25_results and not embedding_results:
                logger.warning("双路检索均无结果，返回空列表")
                return []

            # 步骤 3: RRF 融合
            ranked_lists = []
            if bm25_results:
                ranked_lists.append(bm25_results)
            if embedding_results:
                ranked_lists.append(embedding_results)

            fused_results = reciprocal_rank_fusion(ranked_lists, k=self.config.rrf_k)

            if not fused_results:
                logger.warning("RRF 融合后无结果，返回空列表")
                return []

            # 步骤 4: Rerank 重排序（可选，懒加载）
            reranker = self._get_reranker()
            if reranker and reranker.is_ready():
                reranked_results = reranker.rerank(
                    query=query,
                    documents=fused_results,
                    top_n=top_n,
                    batch_size=self.config.rerank_batch_size,
                )
                logger.info(
                    "RAG 增强检索完成: query='%s', BM25=%d, Embedding=%d, Fused=%d, Reranked=%d",
                    query[:80],
                    len(bm25_results),
                    len(embedding_results),
                    len(fused_results),
                    len(reranked_results),
                )
                return reranked_results
            else:
                # 无 reranker，直接返回 RRF 融合结果的 Top-N
                final_results = fused_results[:top_n]
                logger.info(
                    "RAG 增强检索完成（无重排序）: query='%s', BM25=%d, Embedding=%d, Fused=%d, Final=%d",
                    query[:80],
                    len(bm25_results),
                    len(embedding_results),
                    len(fused_results),
                    len(final_results),
                )
                return final_results

        except Exception as e:
            logger.exception("RAG 增强检索异常，降级为向量检索: %s", e)
            return self._fallback_vector_search(client, workspace_slug, query, top_n, user_id)

    def _get_or_build_bm25_index(
        self,
        client: AnythingLLMClient,
        workspace_slug: str,
        user_id: int = 1,
    ) -> Optional[BM25Retriever]:
        """获取或构建 BM25 索引（带 workspace 级缓存）。

        v3.0 改进：
        - 优先从缓存获取已构建的 BM25 索引
        - 缓存未命中时，从存储目录读取文档全量文本并构建索引
        - 构建成功后缓存索引，避免重复构建

        Args:
            client: AnythingLLM 客户端
            workspace_slug: 工作区标识
            user_id: 用户 ID

        Returns:
            BM25Retriever 实例，构建失败返回 None
        """
        # 1. 尝试从缓存获取
        cached_retriever = self._cache.get(workspace_slug)
        if cached_retriever is not None:
            logger.debug("BM25 索引缓存命中: workspace=%s", workspace_slug)
            return cached_retriever

        # 2. 缓存未命中，构建新索引
        logger.info("BM25 索引缓存未命中，开始构建: workspace=%s", workspace_slug)

        # 2a. 获取文档全量 chunks
        all_chunks = self._chunk_reader.fetch_workspace_chunks(
            client=client,
            workspace_slug=workspace_slug,
            user_id=user_id,
        )

        if not all_chunks:
            logger.warning("无法获取文档 chunks: workspace=%s", workspace_slug)
            return None

        # 2b. 创建新的 BM25Retriever 并构建索引
        bm25_retriever = BM25Retriever(
            use_keyword_extraction=self.config.use_bm25_keyword_extraction,
            use_llm_rewrite=self.config.use_llm_query_rewrite,
        )

        if not bm25_retriever.build_index(all_chunks):
            logger.warning("BM25 索引构建失败: workspace=%s", workspace_slug)
            return None

        # 2c. 缓存索引
        self._cache.put(workspace_slug, bm25_retriever)

        return bm25_retriever

    def _get_reranker(self) -> Optional[BGEReranker]:
        """懒加载 Reranker。

        Reranker 仅在首次实际需要时初始化（加载模型），
        避免在服务启动时就下载/加载模型。
        """
        if not self._reranker_initialized:
            self._reranker_initialized = True
            if self.config.rerank_enabled:
                try:
                    self._reranker = BGEReranker(
                        model_name=self.config.rerank_model,
                        use_onnx=True,
                    )
                except Exception as e:
                    logger.warning("初始化 Reranker 失败，将跳过重排序步骤: %s", e)
                    self._reranker = None
        return self._reranker

    def _fallback_vector_search(
        self,
        client: AnythingLLMClient,
        workspace_slug: str,
        query: str,
        top_n: int,
        user_id: int = 1,
    ) -> List[Dict[str, Any]]:
        """降级方案：使用原始 AnythingLLM 向量检索。

        Args:
            client: AnythingLLM 客户端
            workspace_slug: 工作区标识
            query: 查询文本
            top_n: 返回结果数量
            user_id: 用户 ID

        Returns:
            向量检索结果列表
        """
        results = client.vector_search(workspace_slug, query, user_id=user_id)
        return results[:top_n]

    def is_enhanced(self) -> bool:
        """检查增强功能是否可用。

        v3.0 修复：仅检查配置是否启用，不再要求 BM25 索引预先就绪。
        索引在 hybrid_search() 中按需构建并缓存。
        这修复了原版中 is_enhanced() 要求 BM25 索引 ready 但索引又只在
        hybrid_search() 里才构建导致首次调用静默失效的问题。
        """
        return self.config.enabled

    def invalidate_cache(self, workspace_slug: str) -> None:
        """使指定 workspace 的 BM25 索引缓存失效。

        当 workspace 中的文档发生变化（新增、删除、更新）时调用。

        Args:
            workspace_slug: 工作区标识
        """
        self._cache.invalidate(workspace_slug)

    def clear_cache(self) -> None:
        """清空所有 BM25 索引缓存。"""
        self._cache.clear()

    def get_cache_stats(self) -> Dict[str, int]:
        """获取缓存统计信息。

        Returns:
            统计信息字典，包含 hits、misses、builds、expires 等计数
        """
        return self._cache.get_stats()


# 全局单例（可选，便于在多处复用）
_enhancer_instance: Optional[RAGEnhancer] = None


def get_rag_enhancer(config: Optional[RAGEnhancerConfig] = None) -> RAGEnhancer:
    """获取 RAG 增强器单例。

    Args:
        config: 配置对象，仅在首次调用时生效

    Returns:
        RAGEnhancer 实例
    """
    global _enhancer_instance
    if _enhancer_instance is None:
        _enhancer_instance = RAGEnhancer(config)
    return _enhancer_instance


def reset_rag_enhancer() -> None:
    """重置 RAG 增强器单例（用于测试或重新配置）。"""
    global _enhancer_instance
    _enhancer_instance = None
