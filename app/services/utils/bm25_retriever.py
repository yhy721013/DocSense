"""
BM25 关键词检索器。

使用 rank-bm25 库实现 BM25 算法，用于与向量检索形成互补。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from rank_bm25 import BM25Okapi

logger = logging.getLogger(__name__)


class BM25Retriever:
    """基于 BM25 的关键词检索器。"""

    def __init__(self):
        self.bm25: BM25Okapi | None = None
        self.documents: List[str] = []
        self.doc_ids: List[str] = []
        self._initialized = False

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

        query_tokens = self._tokenize(query)
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

        logger.debug("BM25 检索完成: query='%s', 返回 %d 个结果", query, len(results))
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

    def is_ready(self) -> bool:
        """检查 BM25 索引是否已就绪。"""
        return self._initialized and self.bm25 is not None
