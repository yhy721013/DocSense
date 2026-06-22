"""
RRF (Reciprocal Rank Fusion) 融合算法。

用于将多路检索结果（如 BM25 + Embedding）统一到同一排序维度。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def reciprocal_rank_fusion(
    ranked_lists: List[List[Dict[str, Any]]],
    k: int = 60,
) -> List[Dict[str, Any]]:
    """执行 RRF 融合。

    RRF 公式: score(doc) = Σ 1 / (k + rank_i(doc))
    其中 rank_i 是文档在第 i 路检索结果中的排名。

    Args:
        ranked_lists: 多路已排序的检索结果列表。
                      每路结果应为 [{"id": str, "score": float, ...}, ...]
        k: RRF 常数，通常取 60。较大的 k 会降低排名差异的影响。

    Returns:
        融合后的排序结果列表，按 RRF 分数降序排列。
        每个元素包含原始字段及新增的 "rrf_score" 字段。
    """
    if not ranked_lists:
        logger.warning("RRF 输入为空，返回空列表")
        return []

    # 统计每个文档的 RRF 分数
    rrf_scores: Dict[str, float] = {}
    doc_map: Dict[str, Dict[str, Any]] = {}

    for list_idx, ranked_list in enumerate(ranked_lists):
        if not ranked_list:
            continue

        for rank, doc in enumerate(ranked_list):
            if not isinstance(doc, dict):
                continue

            doc_id = doc.get("id")
            if not doc_id:
                continue

            doc_id = str(doc_id)

            # 记录文档信息（以第一路为准）
            if doc_id not in doc_map:
                doc_map[doc_id] = dict(doc)

            # 累加 RRF 分数
            rrf_score = 1.0 / (k + rank + 1)  # rank 从 0 开始
            rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + rrf_score

    if not rrf_scores:
        logger.warning("RRF 融合后无有效结果")
        return []

    # 按 RRF 分数降序排序
    sorted_docs = sorted(
        rrf_scores.items(),
        key=lambda x: x[1],
        reverse=True,
    )

    # 构建最终结果
    results = []
    for final_rank, (doc_id, rrf_score) in enumerate(sorted_docs):
        doc = doc_map.get(doc_id, {})
        doc["rrf_score"] = rrf_score
        doc["final_rank"] = final_rank + 1
        results.append(doc)

    logger.debug(
        "RRF 融合完成: 输入 %d 路检索, 输出 %d 个结果",
        len(ranked_lists),
        len(results),
    )
    return results


def normalize_scores_for_fusion(
    results: List[Dict[str, Any]],
    score_key: str = "score",
) -> List[Dict[str, Any]]:
    """将检索结果的分数归一化到 [0, 1] 区间，便于后续处理。

    Args:
        results: 检索结果列表
        score_key: 分数字段名

    Returns:
        归一化后的结果列表（原地修改）
    """
    if not results:
        return results

    scores = [doc.get(score_key, 0.0) for doc in results]
    min_score = min(scores)
    max_score = max(scores)

    if max_score == min_score:
        # 所有分数相同，统一设为 1.0
        for doc in results:
            doc[f"{score_key}_normalized"] = 1.0
    else:
        for doc in results:
            raw_score = doc.get(score_key, 0.0)
            normalized = (raw_score - min_score) / (max_score - min_score)
            doc[f"{score_key}_normalized"] = normalized

    return results
