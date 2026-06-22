"""
中文停用词表和文本预处理工具。

提供常见的中文停用词列表，用于 BM25 检索前的 query 清洗。
"""
from __future__ import annotations

import re
from typing import List, Set

# 常见中文停用词（精简版，覆盖高频无意义词）
CHINESE_STOP_WORDS: Set[str] = {
    # 代词
    "我", "你", "他", "她", "它", "我们", "你们", "他们", "她们", "它们",
    "这", "那", "这些", "那些", "此", "该", "其",
    
    # 助词
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "一个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着", "没有",
    "看", "好", "自己", "这", "他", "她", "它", "们", "那", "些", "什么", "怎么",
    "吗", "呢", "吧", "啊", "呀", "哦", "嗯",
    
    # 介词/连词
    "从", "向", "往", "到", "于", "以", "与", "和", "或", "但", "而", "因",
    "为", "如果", "虽然", "但是", "因为", "所以",
    
    # 动词（通用）
    "请", "提取", "获取", "查询", "搜索", "查找", "返回", "显示", "提供",
    "包含", "包括", "涉及", "关于", "对于",
    
    # 名词（通用）
    "信息", "内容", "数据", "资料", "文档", "文件", "字段", "以下", "以上",
    "相关", "有关", "具体", "详细",
    
    # 量词/副词
    "一些", "很多", "非常", "特别", "比较", "相对",
}

# 英文停用词
ENGLISH_STOP_WORDS: Set[str] = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could", "should",
    "may", "might", "can", "shall", "to", "of", "in", "for", "on", "with",
    "at", "by", "from", "as", "into", "through", "during", "before", "after",
    "above", "below", "between", "out", "off", "over", "under", "again",
    "further", "then", "once", "here", "there", "when", "where", "why", "how",
    "all", "both", "each", "few", "more", "most", "other", "some", "such",
    "no", "nor", "not", "only", "own", "same", "so", "than", "too", "very",
    "just", "because", "but", "and", "or", "if", "while", "about",
    "please", "extract", "get", "retrieve", "search", "find", "return", "show",
    "provide", "include", "information", "data", "document", "file", "field",
}


def remove_stop_words(tokens: List[str], language: str = "auto") -> List[str]:
    """去除停用词。
    
    Args:
        tokens: 分词后的 token 列表
        language: 语言类型 ("zh", "en", "auto")
    
    Returns:
        过滤后的 token 列表
    """
    if not tokens:
        return []
    
    if language == "auto":
        # 自动检测：如果包含中文字符则使用中文停用词
        has_chinese = any('\u4e00' <= char <= '\u9fff' for token in tokens for char in token)
        language = "zh" if has_chinese else "en"
    
    stop_words = CHINESE_STOP_WORDS if language == "zh" else ENGLISH_STOP_WORDS
    
    filtered = [token for token in tokens if token.lower() not in stop_words and len(token.strip()) > 0]
    return filtered


def normalize_text(text: str) -> str:
    """标准化文本：去除多余空格、标点等。
    
    Args:
        text: 原始文本
    
    Returns:
        标准化后的文本
    """
    if not text:
        return ""
    
    # 去除首尾空白
    text = text.strip()
    
    # 将多个连续空格替换为单个空格
    text = re.sub(r'\s+', ' ', text)
    
    return text


def extract_numeric_terms(text: str) -> List[str]:
    """提取文本中的数字、型号、编号等专业术语。
    
    例如：
    - "CVN-78" -> ["CVN-78"]
    - "DDG-1000" -> ["DDG-1000"]
    - "最大航速30节" -> ["30"]
    
    Args:
        text: 输入文本
    
    Returns:
        提取的专业术语列表
    """
    terms = []
    
    # 匹配字母+数字组合（如 CVN-78, DDG-1000）
    pattern_alphanumeric = r'[A-Za-z]+[-_]?\d+'
    matches = re.findall(pattern_alphanumeric, text)
    terms.extend(matches)
    
    # 匹配纯数字
    pattern_numeric = r'\b\d+\b'
    matches = re.findall(pattern_numeric, text)
    terms.extend(matches)
    
    return terms


def filter_short_tokens(tokens: List[str], min_length: int = 2) -> List[str]:
    """过滤过短的 token（保留专业术语）。
    
    Args:
        tokens: token 列表
        min_length: 最小长度阈值
    
    Returns:
        过滤后的 token 列表
    """
    filtered = []
    for token in tokens:
        # 保留长度 >= min_length 的 token
        if len(token) >= min_length:
            filtered.append(token)
        # 或者保留包含数字的短 token（如型号、编号）
        elif re.search(r'\d', token):
            filtered.append(token)
        # 或者保留全大写的短 token（如缩写）
        elif token.isupper() and len(token) >= 2:
            filtered.append(token)
    
    return filtered
