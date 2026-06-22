"""
LLM Query 重写器。

使用本地 LLM（Ollama）将自然语言 query 重写为适合 BM25 检索的关键词形式。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class LLMQueryRewriter:
    """基于 LLM 的 Query 重写器。"""

    def __init__(
        self,
        ollama_url: str = "http://localhost:11434",
        model: str = "qwen3:4b-instruct-2507-q4_K_M",
        timeout: int = 10,
        enabled: bool = True,
    ):
        """初始化 Query 重写器。

        Args:
            ollama_url: Ollama 服务地址
            model: 使用的模型名称
            timeout: 请求超时时间（秒）
            enabled: 是否启用 LLM 重写
        """
        self.ollama_url = ollama_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.enabled = enabled
        self.session = requests.Session()

        logger.info(
            "LLM Query Rewriter 初始化: url=%s, model=%s, enabled=%s",
            self.ollama_url,
            self.model,
            self.enabled,
        )

    def rewrite(self, original_query: str) -> str:
        """使用 LLM 重写 query。

        将自然语言查询改写为关键词形式，例如：
        - 原始: "请从文档中提取最大航速字段信息，单位为节"
        - 重写: "最大航速 节 kn 速度 航行"

        Args:
            original_query: 原始查询文本

        Returns:
            重写后的关键词字符串。如果 LLM 调用失败，返回原始 query。
        """
        if not self.enabled:
            logger.debug("LLM Query 重写未启用，返回原始 query")
            return original_query

        if not original_query or not original_query.strip():
            return ""

        try:
            rewritten = self._call_ollama(original_query)
            if rewritten and rewritten.strip():
                logger.info(
                    "Query 重写成功: '%s' -> '%s'",
                    original_query[:50],
                    rewritten[:50],
                )
                return rewritten.strip()
            else:
                logger.warning("LLM 返回空结果，使用原始 query")
                return original_query

        except Exception as e:
            logger.exception("LLM Query 重写失败，降级为原始 query: %s", e)
            return original_query

    def _call_ollama(self, query: str) -> str:
        """调用 Ollama API 进行 query 重写。

        Args:
            query: 原始查询文本

        Returns:
            重写后的关键词字符串
        """
        url = f"{self.ollama_url}/api/generate"

        prompt = self._build_rewrite_prompt(query)

        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.1,  # 低温度，保证输出稳定
                "num_predict": 100,  # 限制输出长度
            },
        }

        response = self.session.post(
            url,
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()

        result = response.json()
        rewritten = result.get("response", "")

        # 清理可能的多余标点
        rewritten = rewritten.strip().rstrip("。，、；：！？")

        return rewritten

    @staticmethod
    def _build_rewrite_prompt(query: str) -> str:
        """构建 query 重写的 prompt。

        Args:
            query: 原始查询文本

        Returns:
            完整的 prompt
        """
        return f"""你是一个专业的信息检索助手。请将以下自然语言查询改写为适合关键词检索的形式。

要求：
1. 只保留核心名词、动词、数字和专业术语
2. 去除停用词、助词、介词等无意义词汇
3. 保留型号、编号、单位等专业信息
4. 多个关键词之间用空格分隔
5. 不要添加任何解释或额外内容，只输出关键词

示例：
输入：请从文档中提取最大航速字段信息，单位为节
输出：最大航速 节 kn 速度 航行

输入：CVN-78航母的满载排水量是多少吨
输出：CVN-78 航母 满载排水量 吨

输入：DDG-1000驱逐舰的雷达系统类型
输出：DDG-1000 驱逐舰 雷达系统 类型

现在请处理以下查询：
输入：{query}
输出："""


# 全局单例
_rewriter_instance: LLMQueryRewriter | None = None


def get_query_rewriter(
    ollama_url: Optional[str] = None,
    model: Optional[str] = None,
    timeout: int = 10,
    enabled: bool = True,
) -> LLMQueryRewriter:
    """获取 Query 重写器单例。

    Args:
        ollama_url: Ollama 服务地址，None 则从环境变量读取
        model: 模型名称，None 则从环境变量读取
        timeout: 请求超时时间
        enabled: 是否启用

    Returns:
        LLMQueryRewriter 实例
    """
    global _rewriter_instance

    if _rewriter_instance is None:
        url = ollama_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        mdl = model or os.getenv("RAG_QUERY_REWRITE_MODEL", "qwen2.5:7b")
        _rewriter_instance = LLMQueryRewriter(
            ollama_url=url,
            model=mdl,
            timeout=timeout,
            enabled=enabled,
        )

    return _rewriter_instance


def reset_query_rewriter() -> None:
    """重置 Query 重写器单例（用于测试或重新配置）。"""
    global _rewriter_instance
    _rewriter_instance = None
