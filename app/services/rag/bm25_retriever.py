"""
BM25 关键词检索器。

使用 rank-bm25 库实现 BM25 算法，用于与向量检索形成互补。
支持关键词提取和 LLM Query 重写优化。

v3.0 改进：
- rank_bm25 改为懒加载导入，未安装时不阻塞服务启动
- 使用统一 tokenize() 函数替代内部 _tokenize()，确保索引和查询使用一致的分词策略
- 支持外部传入已构建的 chunks 数据（配合缓存使用）
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.services.rag.bm25_keyword_extractor import get_keyword_extractor
from app.services.rag.llm_query_rewriter import get_query_rewriter
from app.services.rag.stopwords import tokenize

logger = logging.getLogger(__name__)


class BM25Retriever:
    """基于 BM25 的关键词检索器。

    依赖 rank-bm25 库，通过懒加载方式导入。
    未安装 rank-bm25 时，build_index() 会失败但不影响服务启动。
    """

    def __init__(
        self,
        use_keyword_extraction: bool = True,
        use_llm_rewrite: bool = False,
    ):
        """初始化 BM25 检索器。

        Args:
            use_keyword_extraction: 是否启用关键词提取（停用词过滤等）
            use_llm_rewrite: 是否启用 LLM Query 重写（需要 Ollama 服务）
        """
        self.bm25 = None
        self.documents: List[str] = []
        self.doc_ids: List[str] = []
        self._initialized = False
        self.use_keyword_extraction = use_keyword_extraction
        self.use_llm_rewrite = use_llm_rewrite

        # 延迟初始化组件
        self._keyword_extractor = None
        self._query_rewriter = None

    def build_index(
        self,
        documents: List[Dict[str, Any]],
    ) -> bool:
        """从文档列表构建 BM25 索引。

        Args:
            documents: chunk 列表，每个元素包含 text、id 等字段。

        Returns:
            True 表示构建成功，False 表示失败（如依赖缺失）
        """
        if not documents:
            logger.warning("文档列表为空，无法构建 BM25 索引")
            self._initialized = False
            return False

        # 懒加载导入 rank_bm25
        try:
            from rank_bm25 import BM25Okapi
        except ImportError:
            logger.error(
                "缺少 rank-bm25 依赖，无法构建 BM25 索引。"
                "请运行: pip install rank-bm25"
            )
            self._initialized = False
            return False

        self.documents = []
        self.doc_ids = []

        for doc in documents:
            if not isinstance(doc, dict):
                continue
            text = doc.get("text", "")
            doc_id = doc.get("id") or doc.get("docId")
            if not text or not doc_id:
                continue
            self.documents.append(text)
            self.doc_ids.append(str(doc_id))

        if not self.documents:
            logger.warning("有效文档数量为 0，无法构建 BM25 索引")
            self._initialized = False
            return False

        # 使用统一 tokenize 函数进行分词
        tokenized_docs = [tokenize(doc) for doc in self.documents]
        self.bm25 = BM25Okapi(tokenized_docs)
        self._initialized = True
        logger.info("BM25 索引构建完成: %d 个文档", len(self.documents))
        return True

    def search(self, query: str, top_k: int = 20) -> List[Dict[str, Any]]:
        """执行 BM25 检索。

        Args:
            query: 查询文本
            top_k: 返回结果数量

        Returns:
            检索结果列表，每个元素为 {
                "id": str,
                "text": str,
                "score": float,  # BM25 分数
                "rank": int      # 排名
            }
        """
        if not self._initialized or not self.bm25:
            logger.warning("BM25 索引未初始化，返回空结果")
            return []

        # Step 1: LLM Query 重写（可选）
        if self.use_llm_rewrite:
            rewritten_query = self._get_query_rewriter().rewrite(query)
            logger.debug("LLM 重写后: '%s' -> '%s'", query[:50], rewritten_query[:50])
            query_for_bm25 = rewritten_query
        else:
            query_for_bm25 = query

        # Step 2: 关键词提取（停用词过滤等）
        if self.use_keyword_extraction:
            keywords = self._get_keyword_extractor().extract_keywords(query_for_bm25)
            logger.debug("关键词提取: '%s' -> '%s'", query_for_bm25[:50], keywords[:50] if keywords else "")
            query_tokens = tokenize(keywords) if keywords else []
        else:
            query_tokens = tokenize(query_for_bm25)

        if not query_tokens:
            logger.warning("Query 分词后为空，返回空结果")
            return []

        scores = self.bm25.get_scores(query_tokens)

        # 获取 Top-K 索引
        top_indices = scores.argsort()[::-1][:top_k]

        results = []
        for rank, idx in enumerate(top_indices):
            if scores[idx] <= 0:
                break
            results.append({
                "id": self.doc_ids[idx],
                "text": self.documents[idx],
                "score": float(scores[idx]),
                "rank": rank + 1,
            })

        logger.debug(
            "BM25 检索完成: query='%s', tokens=%d, 返回 %d 个结果",
            query,
            len(query_tokens),
            len(results),
        )
        return results

    def _get_keyword_extractor(self):
        """懒加载关键词提取器。"""
        if self._keyword_extractor is None:
            self._keyword_extractor = get_keyword_extractor()
        return self._keyword_extractor

    def _get_query_rewriter(self):
        """懒加载 Query 重写器。"""
        if self._query_rewriter is None:
            self._query_rewriter = get_query_rewriter()
        return self._query_rewriter

    def is_ready(self) -> bool:
        """检查 BM25 索引是否已就绪。"""
        return self._initialized and self.bm25 is not None
