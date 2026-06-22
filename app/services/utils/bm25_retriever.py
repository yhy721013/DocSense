"""
BM25 关键词检索器。

使用 rank-bm25 库实现 BM25 算法，用于与向量检索形成互补。
支持关键词提取和 LLM Query 重写优化。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from rank_bm25 import BM25Okapi

from app.services.utils.bm25_keyword_extractor import get_keyword_extractor
from app.services.utils.llm_query_rewriter import get_query_rewriter

logger = logging.getLogger(__name__)


class BM25Retriever:
    """基于 BM25 的关键词检索器。"""

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
        self.bm25: BM25Okapi | None = None
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
    ) -> None:
        """从文档列表构建 BM25 索引。

        Args:
            documents: AnythingLLM 返回的 chunk 列表，每个元素包含 text、id 等字段。
        """
        if not documents:
            logger.warning("文档列表为空，无法构建 BM25 索引")
            self._initialized = False
            return

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
            return

        # 对中文文本进行简单分词（按字符分割）
        tokenized_docs = [self._tokenize(doc) for doc in self.documents]
        self.bm25 = BM25Okapi(tokenized_docs)
        self._initialized = True
        logger.info("BM25 索引构建完成: %d 个文档", len(self.documents))

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
            logger.debug("关键词提取: '%s' -> '%s'", query_for_bm25[:50], keywords[:50])
            query_tokens = self._tokenize(keywords) if keywords else []
        else:
            query_tokens = self._tokenize(query_for_bm25)

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

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """对文本进行简单分词。

        对于中文：按字符分割（BM25Okapi 支持字符级索引）
        对于英文：按空格分割

        Args:
            text: 输入文本

        Returns:
            分词后的 token 列表
        """
        # 检测是否包含中文字符
        has_chinese = any('\u4e00' <= char <= '\u9fff' for char in text)

        if has_chinese:
            # 中文：按字符分割，同时保留英文单词完整性
            tokens = []
            current_word = []
            for char in text:
                if '\u4e00' <= char <= '\u9fff':
                    if current_word:
                        tokens.append(''.join(current_word))
                        current_word = []
                    tokens.append(char)
                elif char.isalnum():
                    current_word.append(char)
                else:
                    if current_word:
                        tokens.append(''.join(current_word))
                        current_word = []
            if current_word:
                tokens.append(''.join(current_word))
            return tokens
        else:
            # 英文：按非字母数字字符分割
            import re
            return re.findall(r'\w+', text.lower())

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
