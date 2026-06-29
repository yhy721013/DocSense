"""
BGE-Reranker 重排序器。

使用 BAAI/bge-reranker 系列模型对检索结果进行精排，提升最终召回质量。
支持 ONNX 加速和 CPU/GPU 自动选择。

依赖 sentence-transformers，通过懒加载方式导入，未安装时不阻塞服务启动。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class BGEReranker:
    """基于 BGE-Reranker 的 Cross-Encoder 重排序器。"""

    def __init__(
        self,
        model_name: str = "BAAI/bge-reranker-v2-m3",
        use_onnx: bool = True,
        device: Optional[str] = None,
    ):
        """初始化 Reranker。

        Args:
            model_name: 模型名称，如 "BAAI/bge-reranker-v2-m3"
            use_onnx: 是否使用 ONNX 加速
            device: 设备类型 ("cpu", "cuda", "mps")，None 表示自动选择
        """
        self.model_name = model_name
        self.use_onnx = use_onnx
        self.device = device
        self.model = None
        self._initialized = False

        try:
            self._load_model()
        except Exception as e:
            logger.error("加载 BGE-Reranker 模型失败: %s", e)
            logger.warning("Reranker 不可用，将跳过重排序步骤")

    def _load_model(self) -> None:
        """加载 BGE-Reranker 模型（懒加载 sentence-transformers）。"""
        try:
            from sentence_transformers import CrossEncoder

            model_kwargs = {}
            if self.use_onnx:
                model_kwargs["onnx"] = True

            if self.device:
                model_kwargs["device"] = self.device

            logger.info("正在加载 BGE-Reranker 模型: %s", self.model_name)
            self.model = CrossEncoder(self.model_name, **model_kwargs)
            self._initialized = True
            logger.info("BGE-Reranker 模型加载成功")

        except ImportError:
            logger.error(
                "缺少 sentence-transformers 依赖，请运行: pip install sentence-transformers"
            )
            raise
        except Exception as e:
            logger.error("模型加载异常: %s", e)
            raise

    def rerank(
        self,
        query: str,
        documents: List[Dict[str, Any]],
        top_n: int = 5,
        batch_size: int = 32,
    ) -> List[Dict[str, Any]]:
        """对检索结果进行重排序。

        Args:
            query: 查询文本
            documents: 待重排序的文档列表，每个元素应包含 "text" 字段
            top_n: 返回的 Top-N 结果数量
            batch_size: 批处理大小

        Returns:
            重排序后的文档列表（已按相关性分数降序排列），
            每个元素新增 "rerank_score" 字段。
        """
        if not self._initialized or not self.model:
            logger.warning("Reranker 未初始化，返回原始文档列表")
            return documents[:top_n]

        if not documents:
            return []

        # 提取文档文本
        texts = []
        valid_docs = []
        for doc in documents:
            if not isinstance(doc, dict):
                continue
            text = doc.get("text", "")
            if not text:
                continue
            texts.append(text)
            valid_docs.append(doc)

        if not texts:
            logger.warning("无有效文档文本，返回空列表")
            return []

        # 构建查询-文档对
        pairs = [[query, text] for text in texts]

        try:
            # 批量预测相关性分数
            scores = self.model.predict(
                pairs,
                batch_size=batch_size,
                show_progress_bar=False,
            )

            # 附加分数到文档
            for doc, score in zip(valid_docs, scores):
                doc["rerank_score"] = float(score)

            # 按分数降序排序
            ranked_docs = sorted(
                valid_docs,
                key=lambda x: x.get("rerank_score", 0.0),
                reverse=True,
            )

            # 返回 Top-N
            results = ranked_docs[:top_n]

            logger.debug(
                "Rerank 完成: query='%s', 输入 %d 个文档, 输出 %d 个结果",
                query,
                len(valid_docs),
                len(results),
            )
            return results

        except Exception as e:
            logger.error("Rerank 过程异常: %s", e)
            # 降级：返回原始文档的前 top_n 个
            return documents[:top_n]

    def is_ready(self) -> bool:
        """检查 Reranker 是否已就绪。"""
        return self._initialized and self.model is not None
