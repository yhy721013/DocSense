"""
RAG 增强检索软件包。

提供 BM25 + Embedding 双召回、RRF 融合、BGE-Reranker 精排的完整链路。
所有模块均为可选能力，依赖未安装时自动降级为纯向量检索，不阻塞服务启动。

模块结构：
    stopwords.py              - 停用词表和文本预处理工具（含统一 tokenize 函数）
    bm25_keyword_extractor.py - BM25 关键词提取器
    llm_query_rewriter.py     - LLM Query 重写器（Ollama）
    bm25_retriever.py         - BM25 关键词检索器（懒加载 rank_bm25）
    rrf_fusion.py             - RRF 融合算法
    bge_reranker.py           - BGE-Reranker 重排序器（懒加载 sentence_transformers）
    chunk_reader.py           - 文档 chunk 读取器（从存储目录读取全量文本）
    cache.py                  - BM25 索引缓存（workspace 级，TTL 过期）
    rag_enhancer.py           - RAG 增强器主模块（编排上述所有组件）
"""
from __future__ import annotations

from app.services.rag.rag_enhancer import (
    RAGEnhancer,
    get_rag_enhancer,
    reset_rag_enhancer,
)

__all__ = [
    "RAGEnhancer",
    "get_rag_enhancer",
    "reset_rag_enhancer",
]
