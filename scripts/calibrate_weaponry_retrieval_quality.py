"""对既有 AnythingLLM workspace 执行只读武器谱检索质量校准。

本工具只调用 workspace 读取和 vector-search，不创建、更新、删除、上传、绑定或重新嵌入
任何远端资源。输出只包含 workspace 哈希、分数、计数和 Chunk 内容哈希，不输出 API Key、
Base URL、workspace slug、文档名或正文。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.modules.weaponry.domain import (  # noqa: E402
    assess_chunk_quality,
    normalize_evidence_text,
)
from app.integrations.anythingllm.workspaces import (  # noqa: E402
    AnythingLLMWorkspaceClient,
)
from app.modules.weaponry.adapters import (  # noqa: E402
    AnythingLLMWeaponryClientFactory,
)
from app.services.core.config import load_anythingllm_config  # noqa: E402


logger = logging.getLogger("scripts.calibrate_weaponry_retrieval_quality")
_ALLOWED_LABELS = frozenset(
    {"positive", "negative", "hard-negative", "unknown"}
)


def _reject_nonstandard_json(value: str) -> None:
    raise ValueError(f"不允许非标准 JSON 数字: {value}")


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空字符串")
    return value.strip()


@dataclass(frozen=True)
class CalibrationQuery:
    query_id: str
    label: str
    text: str
    expected_terms: tuple[str, ...]

    @classmethod
    def from_value(cls, value: object, *, index: int) -> "CalibrationQuery":
        if not isinstance(value, Mapping):
            raise ValueError(f"calibrationQueries[{index}] 必须是对象")
        label = _required_text(value.get("label"), name="label").casefold()
        if label not in _ALLOWED_LABELS:
            raise ValueError(
                f"calibrationQueries[{index}].label 不受支持"
            )
        expected_value = value.get("expectedTerms", [])
        if not isinstance(expected_value, list):
            raise ValueError(
                f"calibrationQueries[{index}].expectedTerms 必须是数组"
            )
        expected_terms = tuple(
            _required_text(item, name=f"expectedTerms[{term_index}]")
            for term_index, item in enumerate(expected_value)
        )
        return cls(
            query_id=_required_text(value.get("id"), name="id"),
            label=label,
            text=_required_text(value.get("text"), name="text"),
            expected_terms=expected_terms,
        )


def _load_queries(path: Path) -> tuple[CalibrationQuery, ...]:
    payload = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=_reject_nonstandard_json,
    )
    if not isinstance(payload, Mapping):
        raise ValueError("校准资产根节点必须是对象")
    raw_queries = payload.get("calibrationQueries")
    if not isinstance(raw_queries, list) or not raw_queries:
        raise ValueError("calibrationQueries 必须是非空数组")
    queries = tuple(
        CalibrationQuery.from_value(value, index=index)
        for index, value in enumerate(raw_queries)
    )
    if len({item.query_id for item in queries}) != len(queries):
        raise ValueError("calibrationQueries.id 不能重复")
    return queries


def _content_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _finite_score(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = float(value)
    if not math.isfinite(score):
        return None
    return score


def _query_result(
    workspace_client: AnythingLLMWorkspaceClient,
    *,
    workspace_slug: str,
    query: CalibrationQuery,
    top_n: int,
    user_id: int | None,
) -> tuple[dict[str, Any], tuple[str, ...]]:
    # 校准不能经过“异常时返回空数组”的 legacy Facade，否则网络失败会被误记为
    # 合法零命中。直接调用只读 Workspace Adapter，让任何传输/协议错误终止本次校准。
    candidates = workspace_client.vector_search(
        workspace_slug,
        query.text,
        user_id=user_id,
        top_n=top_n,
        score_threshold=0.0,
    )
    summaries: list[dict[str, Any]] = []
    digests: list[str] = []
    expected_terms = tuple(term.casefold() for term in query.expected_terms)
    for rank, candidate in enumerate(candidates, start=1):
        raw_text = candidate.text
        content = normalize_evidence_text(raw_text)
        digest = _content_digest(content)
        digests.append(digest)
        quality = assess_chunk_quality(raw_text)
        folded = content.casefold()
        term_hits = sum(term in folded for term in expected_terms)
        summaries.append(
            {
                "rank": rank,
                "contentHash": digest,
                "score": _finite_score(candidate.score),
                "contentChars": quality.content_chars,
                "referenceLike": quality.reference_like,
                "qualityAccepted": not quality.rejection_reasons,
                "expectedTermHitCount": term_hits,
            }
        )
    valid_scores = [
        item["score"] for item in summaries if item["score"] is not None
    ]
    first_expected_rank = next(
        (
            item["rank"]
            for item in summaries
            if item["expectedTermHitCount"] > 0
        ),
        None,
    )
    quality_accepted = [
        item for item in summaries if item["qualityAccepted"]
    ]
    quality_scores = [
        item["score"]
        for item in quality_accepted
        if item["score"] is not None
    ]
    first_expected_rank_after_quality = next(
        (
            accepted_rank
            for accepted_rank, item in enumerate(quality_accepted, start=1)
            if item["expectedTermHitCount"] > 0
        ),
        None,
    )
    return (
        {
            "id": query.query_id,
            "label": query.label,
            "resultCount": len(summaries),
            "validScoreCount": len(valid_scores),
            "topScore": max(valid_scores) if valid_scores else None,
            "bottomScore": min(valid_scores) if valid_scores else None,
            "referenceLikeCount": sum(
                bool(item["referenceLike"]) for item in summaries
            ),
            "qualityAcceptedCount": len(quality_accepted),
            "topScoreAfterQuality": max(quality_scores) if quality_scores else None,
            "bottomScoreAfterQuality": min(quality_scores) if quality_scores else None,
            "firstExpectedTermRank": first_expected_rank,
            "firstExpectedTermRankAfterQuality": first_expected_rank_after_quality,
            "candidates": summaries,
        },
        tuple(digests),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读校准现有 AnythingLLM workspace 的武器谱检索质量",
    )
    parser.add_argument("--workspace-slug", required=True)
    parser.add_argument("--query-file", type=Path, required=True)
    parser.add_argument("--top-n", type=int, default=100)
    parser.add_argument("--user-id", type=int, default=1)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.top_n < 1 or args.top_n > 1000:
        raise ValueError("top-n 必须是 1~1000 的整数")
    if args.user_id < 1:
        raise ValueError("user-id 必须是正整数")
    workspace_slug = _required_text(
        args.workspace_slug,
        name="workspace-slug",
    )
    queries = _load_queries(args.query_file.resolve())
    workspace_hash = hashlib.sha256(
        workspace_slug.encode("utf-8")
    ).hexdigest()[:16]

    query_results: list[dict[str, Any]] = []
    digest_frequency: Counter[str] = Counter()
    config = load_anythingllm_config()
    with AnythingLLMWeaponryClientFactory(config).create() as clients:
        # 与 vector-search 一样，文档清单也必须使用 fail-fast 的只读 Adapter。
        # “无法读取清单”与“清单为空”是两种不同事实，校准不得静默混淆。
        documents = clients.workspaces.list_documents(
            workspace_slug,
            user_id=args.user_id,
        )
        for query in queries:
            result, digests = _query_result(
                clients.workspaces,
                workspace_slug=workspace_slug,
                query=query,
                top_n=args.top_n,
                user_id=args.user_id,
            )
            query_results.append(result)
            digest_frequency.update(set(digests))

    output = {
        "schemaVersion": 1,
        "operation": "read-only-existing-workspace-vector-search",
        "remoteMutation": False,
        "workspaceHash": workspace_hash,
        "documentCount": len(documents),
        "queryCount": len(query_results),
        "scoreThreshold": 0.0,
        "topN": args.top_n,
        "queryResults": query_results,
        "crossQueryRepeatedCandidateCount": sum(
            count >= 2 for count in digest_frequency.values()
        ),
    }
    rendered_output = json.dumps(
        output,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )
    # JSON 是脚本的机器可读主产物，应走标准输出；运行诊断继续走 logger，避免混入 JSON。
    sys.stdout.write(rendered_output + "\n")
    logger.info(
        "武器谱只读校准完成: workspace_hash=%s document_count=%d query_count=%d",
        workspace_hash,
        len(documents),
        len(query_results),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
