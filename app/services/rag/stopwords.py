"""
中文停用词表和文本预处理工具。

提供常见的中文停用词列表、统一分词函数和 token 优化工具，用于 BM25 检索前的 query 清洗。

v3.0 改进：
- 新增统一 tokenize() 函数，优先使用 jieba 分词，未安装时降级为字符级切分
- filter_short_tokens() 不再过滤中文字符（中文单字也是有意义的检索 token）
- 修复"最大航速"等中文字段名被错误过滤的问题
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


def _has_chinese(text: str) -> bool:
    """检测文本是否包含中文字符。"""
    return any('\u4e00' <= char <= '\u9fff' for char in text)


def segment_chinese(text: str) -> List[str]:
    """中文分词。

    优先使用 jieba 分词（如果已安装），未安装时降级为字符级切分。
    字符级切分时保留英文单词和数字组合的完整性。

    Args:
        text: 输入文本

    Returns:
        分词后的 token 列表
    """
    # 尝试使用 jieba
    try:
        import jieba
        # 精确模式分词
        return [t for t in jieba.cut(text, cut_all=False) if t.strip()]
    except ImportError:
        pass

    # 降级：字符级切分（保留英文单词/数字组合完整性）
    tokens: List[str] = []
    current_word: List[str] = []
    for char in text:
        if '\u4e00' <= char <= '\u9fff':
            # 中文字符：先输出正在积累的英文/数字词
            if current_word:
                tokens.append(''.join(current_word))
                current_word = []
            tokens.append(char)
        elif char.isalnum() or char in '-_./':
            # 英文/数字/连字符：积累到 current_word
            current_word.append(char)
        else:
            # 标点/空格等：先输出正在积累的英文/数字词
            if current_word:
                tokens.append(''.join(current_word))
                current_word = []
    if current_word:
        tokens.append(''.join(current_word))
    return tokens


def tokenize(text: str) -> List[str]:
    """统一分词函数。

    对于包含中文的文本，优先使用 jieba 分词，未安装时降级为字符级切分。
    对于纯英文文本，按非字母数字字符分割并小写化。

    本函数同时用于 BM25 索引构建和查询 token 化，确保两端使用一致的策略。

    Args:
        text: 输入文本

    Returns:
        分词后的 token 列表
    """
    if not text:
        return []

    if _has_chinese(text):
        return segment_chinese(text)
    else:
        # 纯英文：按非字母数字字符分割
        return re.findall(r'\w+', text.lower())


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


def filter_short_tokens(
    tokens: List[str],
    min_length: int = 2,
    *,
    keep_chinese_chars: bool = True,
) -> List[str]:
    """过滤过短的 token（保留专业术语和中文字符）。

    v3.0 改进：默认 keep_chinese_chars=True，不再过滤中文字符。
    这修复了"最大航速"被字符级切分后全部被过滤的问题。

    Args:
        tokens: token 列表
        min_length: 最小长度阈值
        keep_chinese_chars: 是否保留中文字符（单字）

    Returns:
        过滤后的 token 列表
    """
    filtered = []
    for token in tokens:
        # 保留长度 >= min_length 的 token
        if len(token) >= min_length:
            filtered.append(token)
        # 保留包含数字的短 token（如型号、编号）
        elif re.search(r'\d', token):
            filtered.append(token)
        # 保留全大写的短 token（如缩写）
        elif token.isupper() and len(token) >= 2:
            filtered.append(token)
        # v3.0 新增：保留中文字符（单字）
        elif keep_chinese_chars and len(token) == 1 and '\u4e00' <= token <= '\u9fff':
            filtered.append(token)
    return filtered
