#!/usr/bin/env python3
"""对领域树本地 Top-K 召回执行可复现、无正文输出的 benchmark。"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.core.architecture_tree import build_architecture_tree_index  # noqa: E402
from app.services.llm_service.architecture_recall_service import (  # noqa: E402
    ArchitectureRecallDecision,
    build_document_architecture_signals,
    recall_architecture_candidates,
)


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    name: str
    filename: str
    original_filename: str
    title: str
    headings: tuple[str, ...]
    identifiers: tuple[str, ...]
    body: str
    gold_ids: tuple[int, ...]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ValueError(f"JSON 文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"JSON 格式错误: {path}，第 {exc.lineno} 行第 {exc.colno} 列"
        ) from exc


def load_architecture_nodes(path: Path) -> list[Mapping[str, Any]]:
    """读取直接节点数组或 ``params[0].architectureList`` 请求形状。"""

    payload = _read_json(path)
    nodes: Any
    if isinstance(payload, list):
        nodes = payload
    elif isinstance(payload, Mapping) and isinstance(payload.get("architectureList"), list):
        nodes = payload["architectureList"]
    elif isinstance(payload, Mapping):
        params = payload.get("params")
        if not isinstance(params, list) or not params or not isinstance(params[0], Mapping):
            raise ValueError(
                "领域树 JSON 必须是节点数组，或包含 params[0].architectureList 的请求对象"
            )
        nodes = params[0].get("architectureList")
    else:
        nodes = None

    if not isinstance(nodes, list) or not nodes:
        raise ValueError("architectureList 必须是非空节点数组")
    if any(not isinstance(node, Mapping) for node in nodes):
        raise ValueError("architectureList 中的每个节点都必须是对象")
    return nodes


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _as_text_tuple(value: Any, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if not isinstance(value, list):
        raise ValueError(f"case.{field} 必须是字符串数组")
    return tuple(text for item in value if (text := _as_text(item).strip()))


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是正整数")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str):
        normalized = value.strip()
        if not normalized or not all("0" <= char <= "9" for char in normalized):
            raise ValueError(f"{field} 必须是正整数")
        result = int(normalized, 10)
    else:
        raise ValueError(f"{field} 必须是正整数")
    if result < 1:
        raise ValueError(f"{field} 必须是正整数")
    return result


def _gold_ids(value: Any, *, field: str = "case.gold_ids") -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field} 必须是正整数数组")
    result: list[int] = []
    for item in value:
        gold_id = _positive_int(item, field=field)
        if gold_id not in result:
            result.append(gold_id)
    return tuple(result)


def parse_benchmark_case(value: Any, *, ordinal: int) -> BenchmarkCase:
    if not isinstance(value, Mapping):
        raise ValueError(f"cases[{ordinal}] 必须是对象")
    return BenchmarkCase(
        name=_as_text(value.get("name") or value.get("caseName") or f"case-{ordinal + 1}"),
        filename=_as_text(value.get("filename") or value.get("fileName")),
        original_filename=_as_text(
            value.get("original_filename") or value.get("originalFilename")
        ),
        title=_as_text(value.get("title")),
        headings=_as_text_tuple(value.get("headings"), field="headings"),
        identifiers=_as_text_tuple(value.get("identifiers"), field="identifiers"),
        body=_as_text(
            value.get("body")
            if "body" in value
            else value.get("bodyExcerpt", value.get("content"))
        ),
        gold_ids=_gold_ids(
            value.get("gold_ids")
            if "gold_ids" in value
            else value.get("goldIds")
        ),
    )


def load_benchmark_cases(path: Path) -> tuple[BenchmarkCase, ...]:
    payload = _read_json(path)
    raw_cases = payload.get("cases") if isinstance(payload, Mapping) else payload
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError("cases JSON 必须是非空数组或包含非空 cases 数组")
    return tuple(
        parse_benchmark_case(case, ordinal=ordinal)
        for ordinal, case in enumerate(raw_cases)
    )


def _single_case_from_args(args: argparse.Namespace) -> BenchmarkCase:
    return BenchmarkCase(
        name=args.case_name,
        filename=args.filename,
        original_filename=args.original_filename,
        title=args.title,
        headings=tuple(args.heading),
        identifiers=tuple(args.identifier),
        body=args.body,
        gold_ids=tuple(
            dict.fromkeys(
                _positive_int(value, field="--gold-id") for value in args.gold_id
            )
        ),
    )


def _rank_by_id(node_ids: Sequence[int]) -> dict[int, int]:
    return {node_id: rank for rank, node_id in enumerate(node_ids, start=1)}


def _case_metrics(
    case: BenchmarkCase,
    decision: ArchitectureRecallDecision,
) -> dict[str, Any]:
    top64_rank = _rank_by_id(decision.base_leaf_ids)
    candidate_rank = _rank_by_id(decision.final_candidate_ids)
    gold_ranks_at64 = {
        str(gold_id): top64_rank.get(gold_id) for gold_id in case.gold_ids
    }
    gold_candidate_ranks = {
        str(gold_id): candidate_rank.get(gold_id) for gold_id in case.gold_ids
    }
    ranks_at64 = [rank for rank in gold_ranks_at64.values() if rank is not None]
    candidate_ranks = [
        rank for rank in gold_candidate_ranks.values() if rank is not None
    ]
    has_gold = bool(case.gold_ids)
    gold_ids_found_at64 = sum(rank is not None for rank in gold_ranks_at64.values())
    gold_ids_found_in_final_candidates = sum(
        rank is not None for rank in gold_candidate_ranks.values()
    )

    return {
        "name": case.name,
        "queryDigest": decision.query_digest,
        "candidateCount": len(decision.candidates),
        "promptChars": decision.prompt_chars,
        "elapsedMs": decision.elapsed_ms,
        "goldIds": list(case.gold_ids),
        "goldRanksAt64": gold_ranks_at64,
        "goldCandidateRanks": gold_candidate_ranks,
        "goldRank": min(ranks_at64) if ranks_at64 else None,
        "goldCandidateRank": min(candidate_ranks) if candidate_ranks else None,
        "goldIdsFoundAt64": gold_ids_found_at64,
        "goldIdsFoundInFinalCandidates": (
            gold_ids_found_in_final_candidates
        ),
        "goldCoverageAt64": (
            gold_ids_found_at64 / len(case.gold_ids) if has_gold else None
        ),
        "goldCoverageInFinalCandidates": (
            gold_ids_found_in_final_candidates / len(case.gold_ids)
            if has_gold
            else None
        ),
        # 多个 gold ID 可表示等价的允许答案；命中任意一个即计该 case Recall@64。
        "recallAt64": (1.0 if ranks_at64 else 0.0) if has_gold else None,
        # 最终模型候选允许包含可靠父节点，因此发布门禁应以该集合为主指标。
        "finalCandidateRecall": (
            (1.0 if candidate_ranks else 0.0)
            if has_gold
            else None
        ),
    }


def run_benchmark(
    nodes: Sequence[Mapping[str, Any]],
    cases: Sequence[BenchmarkCase],
) -> dict[str, Any]:
    if not cases:
        raise ValueError("至少需要一个 query case")
    index = build_architecture_tree_index(nodes)

    case_metrics: list[dict[str, Any]] = []
    for case in cases:
        missing_gold_ids = [
            gold_id for gold_id in case.gold_ids if gold_id not in index.nodes_by_id
        ]
        if missing_gold_ids:
            raise ValueError(
                f"benchmark case {case.name!r} 的 gold ID 不在领域树中: {missing_gold_ids}"
            )
        signals = build_document_architecture_signals(
            filename=case.filename,
            original_filename=case.original_filename,
            title=case.title,
            headings=case.headings,
            identifiers=case.identifiers,
            body=case.body,
        )
        decision = recall_architecture_candidates(index, signals)
        case_metrics.append(_case_metrics(case, decision))

    gold_cases = [metrics for metrics in case_metrics if metrics["recallAt64"] is not None]
    hit_cases = sum(metrics["recallAt64"] == 1.0 for metrics in gold_cases)
    final_candidate_hit_cases = sum(
        metrics["finalCandidateRecall"] == 1.0
        for metrics in gold_cases
    )
    gold_id_count = sum(len(metrics["goldIds"]) for metrics in gold_cases)
    found_gold_id_count = sum(metrics["goldIdsFoundAt64"] for metrics in gold_cases)
    found_final_candidate_gold_id_count = sum(
        metrics["goldIdsFoundInFinalCandidates"]
        for metrics in gold_cases
    )

    return {
        "treeFingerprint": index.fingerprint,
        "nodeCount": len(index.nodes),
        "leafCount": index.leaf_count,
        "caseCount": len(case_metrics),
        "cases": case_metrics,
        "summary": {
            "goldCaseCount": len(gold_cases),
            "hitCaseCountAt64": hit_cases,
            "recallAt64": hit_cases / len(gold_cases) if gold_cases else None,
            "hitCaseCountInFinalCandidates": (
                final_candidate_hit_cases
            ),
            "finalCandidateRecall": (
                final_candidate_hit_cases / len(gold_cases)
                if gold_cases
                else None
            ),
            "goldIdCount": gold_id_count,
            "goldIdFoundCountAt64": found_gold_id_count,
            "goldIdFoundCountInFinalCandidates": (
                found_final_candidate_gold_id_count
            ),
            "goldCoverageAt64": (
                found_gold_id_count / gold_id_count if gold_id_count else None
            ),
            "goldCoverageInFinalCandidates": (
                found_final_candidate_gold_id_count / gold_id_count
                if gold_id_count
                else None
            ),
            "maxCandidateCount": max(
                metrics["candidateCount"] for metrics in case_metrics
            ),
            "maxPromptChars": max(metrics["promptChars"] for metrics in case_metrics),
            "totalRecallElapsedMs": round(
                sum(metrics["elapsedMs"] for metrics in case_metrics), 3
            ),
        },
    }


def _unit_interval(raw_value: str) -> float:
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "阈值必须是 0 到 1 之间的有限数值"
        ) from exc
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError(
            "阈值必须是 0 到 1 之间的有限数值"
        )
    return value


def evaluate_quality_gate(
    result: Mapping[str, Any],
    *,
    min_final_candidate_recall: float | None,
    min_base_leaf_recall_at64: float | None,
) -> dict[str, Any]:
    """按显式阈值评估有 gold 的 benchmark 结果。"""

    summary = result["summary"]
    checks: dict[str, dict[str, Any]] = {}
    if min_final_candidate_recall is not None:
        actual = summary["finalCandidateRecall"]
        checks["finalCandidateRecall"] = {
            "minimum": min_final_candidate_recall,
            "actual": actual,
            "passed": (
                actual is not None
                and actual >= min_final_candidate_recall
            ),
        }
    if min_base_leaf_recall_at64 is not None:
        actual = summary["recallAt64"]
        checks["baseLeafRecallAt64"] = {
            "minimum": min_base_leaf_recall_at64,
            "actual": actual,
            "passed": (
                actual is not None
                and actual >= min_base_leaf_recall_at64
            ),
        }
    return {
        "enabled": bool(checks),
        "passed": (
            all(check["passed"] for check in checks.values())
            if checks
            else None
        ),
        "checks": checks,
    }


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "使用完整领域树运行本地 Top-K 召回 benchmark；标准输出只包含摘要指标和 query digest，"
            "不会回显正文。"
        )
    )
    parser.add_argument(
        "--tree-json",
        required=True,
        help="完整请求 JSON（读取 params[0].architectureList）或直接节点数组 JSON。",
    )
    parser.add_argument(
        "--cases-json",
        help="case 数组 JSON，或包含 cases 数组的 JSON；提供后忽略单例 query 参数。",
    )
    parser.add_argument("--case-name", default="cli-case")
    parser.add_argument("--filename", default="")
    parser.add_argument("--original-filename", default="")
    parser.add_argument("--title", default="")
    parser.add_argument("--heading", action="append", default=[])
    parser.add_argument("--identifier", action="append", default=[])
    parser.add_argument("--body", default="")
    parser.add_argument("--gold-id", action="append", default=[])
    parser.add_argument(
        "--min-final-candidate-recall",
        type=_unit_interval,
        help=(
            "可选发布门禁：最终模型候选的 case recall 最低值；"
            "启用后每个 case 都必须提供 gold ID。"
        ),
    )
    parser.add_argument(
        "--min-base-leaf-recall-at-64",
        type=_unit_interval,
        help=(
            "可选诊断门禁：基础叶子 Recall@64 最低值；"
            "启用后每个 case 都必须提供 gold ID。"
        ),
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="缩进输出 JSON，便于人工阅读。",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        nodes = load_architecture_nodes(Path(args.tree_json).expanduser())
        cases = (
            load_benchmark_cases(Path(args.cases_json).expanduser())
            if args.cases_json
            else (_single_case_from_args(args),)
        )
        quality_gate_enabled = (
            args.min_final_candidate_recall is not None
            or args.min_base_leaf_recall_at_64 is not None
        )
        goldless_case_count = sum(not case.gold_ids for case in cases)
        if quality_gate_enabled and goldless_case_count:
            raise ValueError(
                "启用质量门禁时每个 case 都必须提供 gold ID；"
                f"当前缺少 {goldless_case_count} 项"
            )
        result = run_benchmark(nodes, cases)
        quality_gate = evaluate_quality_gate(
            result,
            min_final_candidate_recall=(
                args.min_final_candidate_recall
            ),
            min_base_leaf_recall_at64=(
                args.min_base_leaf_recall_at_64
            ),
        )
        if quality_gate["enabled"]:
            result["qualityGate"] = quality_gate
    except (OSError, TypeError, ValueError) as exc:
        sys.stderr.write(f"benchmark 失败: {exc}\n")
        return 2

    sys.stdout.write(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
        )
        + "\n"
    )
    if quality_gate["enabled"] and not quality_gate["passed"]:
        failed_checks = [
            (
                f"{name}={check['actual']} < "
                f"{check['minimum']}"
            )
            for name, check in quality_gate["checks"].items()
            if not check["passed"]
        ]
        sys.stderr.write(
            "benchmark 质量门禁未通过: "
            + ", ".join(failed_checks)
            + "\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
