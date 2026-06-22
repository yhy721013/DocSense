"""
RAG 增强器主模块。

提供 BM25 + Embedding 双召回、RRF 融合、BGE-Reranker 精排的完整链路。
通过配置开关控制是否启用增强，未启用时降级为原始 AnythingLLM 向量检索。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.services.core.config import RAGEnhancerConfig, load_rag_enhancer_config
from app.services.utils.anythingllm_client import AnythingLLMClient
from app.services.utils.bm25_retriever import BM25Retriever
from app.services.utils.rrf_fusion import reciprocal_rank_fusion
from app.services.utils.bge_reranker import BGEReranker

logger = logging.getLogger(__name__)


class RAGEnhancer:
    """RAG 增强器，封装双召回+融合+重排序完整流程。"""

    def __init__(self, config: Optional[RAGEnhancerConfig] = None):
        """初始化 RAG 增强器。

        Args:
            config: RAG 增强配置，None 则从环境变量加载
        """
        self.config = config or load_rag_enhancer_config()
        self.bm25_retriever = BM25Retriever()
        self.reranker: Optional[BGEReranker] = None

        # 如果启用 rerank，初始化模型
        if self.config.rerank_enabled:
            try:
                self.reranker = BGEReranker(
                    model_name=self.config.rerank_model,
                    use_onnx=True,
                )
            except Exception as e:
                logger.warning("初始化 Reranker 失败，将跳过重排序步骤: %s", e)
                self.reranker = None

        logger.info(
            "RAG Enhancer 初始化完成: enabled=%s, bm25_top_k=%d, embedding_top_k=%d, rerank=%s",
            self.config.enabled,
            self.config.bm25_top_k,
            self.config.embedding_top_k,
            self.config.rerank_enabled and self.reranker is not None,
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

        如果增强功能未启用或初始化失败，自动降级为纯向量检索。

        Args:
            client: AnythingLLM 客户端
            workspace_slug: 工作区标识
            query: 查询文本
            top_n: 最终返回的结果数量
            user_id: 用户 ID

        Returns:
            检索结果列表，按相关性降序排列。
            每个元素包含原始字段及可能的 "rrf_score"、"rerank_score" 等增强字段。
        """
        # 检查是否启用增强
        if not self.config.enabled:
            logger.debug("RAG 增强未启用，使用原始向量检索")
            return self._fallback_vector_search(client, workspace_slug, query, top_n, user_id)

        try:
            # 步骤 1: 获取足够多的候选文档用于 BM25 索引构建
            # 需要从 AnythingLLM 获取全部或大量 chunks
            all_chunks = self._fetch_all_chunks(client, workspace_slug, user_id)

            if not all_chunks:
                logger.warning("无法获取文档 chunks，降级为向量检索")
                return self._fallback_vector_search(client, workspace_slug, query, top_n, user_id)

            # 步骤 2: 构建 BM25 索引
            self.bm25_retriever.build_index(all_chunks)

            if not self.bm25_retriever.is_ready():
                logger.warning("BM25 索引构建失败，降级为向量检索")
                return self._fallback_vector_search(client, workspace_slug, query, top_n, user_id)

            # 步骤 3: 执行双路检索
            # 3a. BM25 检索
            bm25_results = self.bm25_retriever.search(query, top_k=self.config.bm25_top_k)

            # 3b. Embedding 检索（通过 AnythingLLM）
            embedding_results = client.vector_search(workspace_slug, query, user_id=user_id)
            # 限制 embedding 结果数量
            embedding_results = embedding_results[: self.config.embedding_top_k]

            if not bm25_results and not embedding_results:
                logger.warning("双路检索均无结果，返回空列表")
                return []

            # 步骤 4: RRF 融合
            ranked_lists = []
            if bm25_results:
                ranked_lists.append(bm25_results)
            if embedding_results:
                ranked_lists.append(embedding_results)

            fused_results = reciprocal_rank_fusion(ranked_lists, k=self.config.rrf_k)

            if not fused_results:
                logger.warning("RRF 融合后无结果，返回空列表")
                return []

            # 步骤 5: Rerank 重排序（可选）
            if self.reranker and self.reranker.is_ready():
                reranked_results = self.reranker.rerank(
                    query=query,
                    documents=fused_results,
                    top_n=top_n,
                    batch_size=self.config.rerank_batch_size,
                )
                logger.info(
                    "RAG 增强检索完成: query='%s', BM25=%d, Embedding=%d, Fused=%d, Reranked=%d",
                    query,
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
                    query,
                    len(bm25_results),
                    len(embedding_results),
                    len(fused_results),
                    len(final_results),
                )
                return final_results

        except Exception as e:
            logger.exception("RAG 增强检索异常，降级为向量检索: %s", e)
            return self._fallback_vector_search(client, workspace_slug, query, top_n, user_id)

    def _fetch_all_chunks(
        self,
        client: AnythingLLMClient,
        workspace_slug: str,
        user_id: int = 1,
    ) -> List[Dict[str, Any]]:
        """从 AnythingLLM workspace 获取所有文档 chunks。

        注意：AnythingLLM API 未直接提供获取全部 chunks 的接口，
        这里通过 vector-search 使用空查询或通配符尝试获取尽可能多的结果。
        如果 API 不支持，可能需要扩展 AnythingLLMClient。

        Args:
            client: AnythingLLM 客户端
            workspace_slug: 工作区标识
            user_id: 用户 ID

        Returns:
            文档 chunks 列表
        """
        # 方案 1: 尝试使用空查询或通用查询获取大量结果
        # 注意：这取决于 AnythingLLM 的实现，可能无法获取全部 chunks
        try:
            # 使用通用查询词尝试获取更多结果
            all_results = client.vector_search(workspace_slug, " ", user_id=user_id)
            if all_results:
                logger.debug("通过通用查询获取到 %d 个 chunks", len(all_results))
                return all_results
        except Exception as e:
            logger.warning("通过通用查询获取 chunks 失败: %s", e)

        # 方案 2: 如果方案 1 失败，尝试从 workspace 文档列表推断
        # 这需要 AnythingLLMClient 支持获取文档详情的方法
        logger.warning("无法获取完整 chunks 列表，BM25 索引可能不完整")
        return []

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
        """检查增强功能是否可用。"""
        return (
            self.config.enabled
            and self.bm25_retriever.is_ready()
            and (not self.config.rerank_enabled or (self.reranker and self.reranker.is_ready()))
        )


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
