"""
BM25 关键词提取器。

从自然语言 query 中提取适合 BM25 检索的关键词，
支持停用词过滤、专业术语提取和 token 优化。
"""
from __future__ import annotations

import logging
import re
from typing import List

from app.services.utils.stopwords import (
    CHINESE_STOP_WORDS,
    ENGLISH_STOP_WORDS,
    extract_numeric_terms,
    filter_short_tokens,
    normalize_text,
    remove_stop_words,
)

logger = logging.getLogger(__name__)


class BM25KeywordExtractor:
    """BM25 关键词提取器。"""

    def __init__(
        self,
        use_stop_words: bool = True,
        extract_numbers: bool = True,
        min_token_length: int = 2,
    ):
        """初始化关键词提取器。

        Args:
            use_stop_words: 是否使用停用词过滤
            extract_numbers: 是否提取数字和专业术语
            min_token_length: 最小 token 长度阈值
        """
        self.use_stop_words = use_stop_words
        self.extract_numbers = extract_numbers
        self.min_token_length = min_token_length

    def extract_keywords(self, query: str) -> str:
        """从 query 中提取关键词。

        流程：
        1. 文本标准化
        2. 分词（中文按字符，英文按单词）
        3. 去除停用词
        4. 提取专业术语（型号、编号等）
        5. 过滤过短 token
        6. 合并为关键词字符串

        Args:
            query: 原始查询文本

        Returns:
            提取后的关键词字符串（空格分隔）
        """
        if not query or not query.strip():
            logger.warning("查询文本为空")
            return ""

        # Step 1: 文本标准化
        normalized = normalize_text(query)
        logger.debug("标准化后: %s", normalized)

        # Step 2: 分词
        tokens = self._tokenize(normalized)
        logger.debug("分词结果: %s", tokens)

        # Step 3: 去除停用词
        if self.use_stop_words:
            tokens = remove_stop_words(tokens, language="auto")
            logger.debug("去停用词后: %s", tokens)

        # Step 4: 提取专业术语（数字、型号等）
        numeric_terms = []
        if self.extract_numbers:
            numeric_terms = extract_numeric_terms(normalized)
            logger.debug("提取的专业术语: %s", numeric_terms)

        # Step 5: 过滤过短 token
        tokens = filter_short_tokens(tokens, min_length=self.min_token_length)
        logger.debug("过滤短 token 后: %s", tokens)

        # Step 6: 合并所有关键词
        all_keywords = tokens + numeric_terms

        # 去重并保持顺序
        seen = set()
        unique_keywords = []
        for kw in all_keywords:
            kw_lower = kw.lower()
            if kw_lower not in seen:
                seen.add(kw_lower)
                unique_keywords.append(kw)

        result = " ".join(unique_keywords)
        logger.info("最终关键词: '%s' (原始: '%s')", result, query[:50])

        return result

    def _tokenize(self, text: str) -> List[str]:
        """对文本进行分词。

        对于中文：按字符分割，同时保留英文单词完整性
        对于英文：按非字母数字字符分割

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
            return re.findall(r'\w+', text.lower())


# 全局单例
_extractor_instance: BM25KeywordExtractor | None = None


def get_keyword_extractor(
    use_stop_words: bool = True,
    extract_numbers: bool = True,
    min_token_length: int = 2,
) -> BM25KeywordExtractor:
    """获取关键词提取器单例。

    Args:
        use_stop_words: 是否使用停用词过滤
        extract_numbers: 是否提取数字和专业术语
        min_token_length: 最小 token 长度

    Returns:
        BM25KeywordExtractor 实例
    """
    global _extractor_instance
    if _extractor_instance is None:
        _extractor_instance = BM25KeywordExtractor(
            use_stop_words=use_stop_words,
            extract_numbers=extract_numbers,
            min_token_length=min_token_length,
        )
    return _extractor_instance


def reset_keyword_extractor() -> None:
    """重置关键词提取器单例（用于测试或重新配置）。"""
    global _extractor_instance
    _extractor_instance = None
