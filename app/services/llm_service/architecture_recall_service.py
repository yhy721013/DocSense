from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import unicodedata
from collections import Counter, OrderedDict, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from app.services.core.architecture_tree import (
    ArchitectureNodeProfile,
    ArchitectureTreeIndex,
)


MAX_HEADINGS = 64
MAX_IDENTIFIERS = 128
MAX_BODY_CHARS = 20_000
MAX_BASE_LEAVES = 64
MAX_LEAF_CANDIDATES = 112
MAX_PARENT_CANDIDATES = 16
MAX_FINAL_CANDIDATES = 128
MAX_REMARK_CHARS = 512
MAX_CLASSIFICATION_PROMPT_CHARS = 32_000
CLASSIFICATION_PROMPT_OVERHEAD_CHARS = 1_024

BM25_K1 = 1.2
BM25_B = 0.75
BM25_LIMIT = 200
TREE_ROOT_BEAM = 4
TREE_INTERMEDIATE_BEAM = 8
TREE_LEAF_LIMIT = 100
RRF_K = 60

DETAIL_KINDS = (
    "基础数据",
    "战技指标",
    "运用数据",
    "效能数据",
    "模型数据",
    "目特数据",
    "声像数据",
)
DATA_STANDARD_KINDS = (
    "建模与仿真",
    "军用软件",
    "目标特性",
    "术语与定义",
    "通用要求",
    "元数据",
)

_DASH_RE = re.compile(r"[\-‐‑‒–—―－﹣]+")
_SPACE_RE = re.compile(r"\s+")
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_WORD_RE = re.compile(r"[a-z0-9]+")
_IDENTIFIER_RE = re.compile(
    r"(?<![a-z0-9])"
    r"(?:[a-z]{1,12}[\s\-‐‑‒–—―－﹣]*)"
    r"(?:\d{1,8}[a-z]?)"
    r"(?:[\s\-‐‑‒–—―－﹣]+\d{1,8}[a-z]?)?"
    r"(?![a-z0-9])",
    re.IGNORECASE,
)
_GJB_RE = re.compile(
    r"(?<![a-z0-9])(?:gjb|国军标|国家军用标准)(?![a-z])",
    re.IGNORECASE,
)
_JANE_UUV_TITLE_ALIAS_RE = re.compile(
    r"(?<![a-z0-9])(?:xluuv|uuv)s?(?![a-z0-9])",
    re.IGNORECASE,
)
_JANE_UUV_TYPE_DESCRIPTION_RE = re.compile(
    r"(?<![a-z])(?:unmanned|uncrewed)[\s-]+"
    r"(?:underwater|undersea)[\s-]+vehicles?(?![a-z])",
    re.IGNORECASE,
)
_JANE_UUV_CANONICAL_NAME = "无人潜航器"
_JANE_TITLE_TYPE_ALIAS_REASON = "jane_title_type_alias"


class ArchitectureRecallError(ValueError):
    """无法从文档信号产生受限领域候选时抛出的稳定合同异常。"""

    stage = "architecture_recall"


class ArchitecturePromptBudgetError(ArchitectureRecallError):
    """候选投影超过分类 Prompt 字符预算。"""

    stage = "architecture_prompt_budget"


@dataclass(frozen=True, slots=True)
class DocumentArchitectureSignals:
    """进入领域召回的有界文档判别信号。"""

    filename: str = ""
    original_filename: str = ""
    title: str = ""
    headings: tuple[str, ...] = ()
    identifiers: tuple[str, ...] = ()
    body_excerpt: str = ""

    @property
    def is_empty(self) -> bool:
        return not any(
            (
                self.filename,
                self.original_filename,
                self.title,
                self.headings,
                self.identifiers,
                self.body_excerpt,
            )
        )

    @property
    def query_text(self) -> str:
        return "\n".join(
            value
            for value in (
                self.filename,
                self.original_filename,
                self.title,
                *self.headings,
                *self.identifiers,
                self.body_excerpt,
            )
            if value
        )

    @property
    def strong_identity_text(self) -> str:
        """返回仅可用于强匹配的双源身份文本。

        业务原文件名存在时，不再把技术 ``fileName`` 当作身份来源；正文、章节和
        Fleetlist 型号仍留在 ``query_text`` 中参与普通词法召回。
        """

        identity_filename = self.original_filename or self.filename
        return "\n".join(
            value
            for value in (identity_filename, self.title)
            if value
        )

    @property
    def strong_identifiers(self) -> tuple[str, ...]:
        """返回原文件名（优先）与首页标题中的有序型号标识。"""

        return _extract_identifiers(self.strong_identity_text)


@dataclass(frozen=True, slots=True)
class RecallChannelRanking:
    channel: str
    node_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ArchitectureRecallCandidate:
    architecture_id: int
    path_name: str
    node_type: str
    remark: str
    rank: int
    rrf_score: float
    channel_ranks: tuple[tuple[str, int], ...]
    protected_reasons: tuple[str, ...]

    @property
    def id(self) -> int:
        return self.architecture_id

    def to_prompt_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.architecture_id,
            "pathName": self.path_name,
            "nodeType": self.node_type,
        }
        if self.remark:
            result["remark"] = self.remark
        return result


@dataclass(frozen=True, slots=True)
class ArchitectureRecallDecision:
    tree_fingerprint: str
    query_digest: str
    base_leaf_ids: tuple[int, ...]
    candidates: tuple[ArchitectureRecallCandidate, ...]
    channel_rankings: tuple[RecallChannelRanking, ...]
    rrf_scores: tuple[tuple[int, float], ...]
    protected_reasons: tuple[tuple[int, tuple[str, ...]], ...]
    direct_exact_ids: tuple[int, ...]
    direct_tree_ids: tuple[int, ...]
    candidate_projection_chars: int
    prompt_chars: int
    elapsed_ms: float

    @property
    def final_candidate_ids(self) -> tuple[int, ...]:
        return tuple(candidate.architecture_id for candidate in self.candidates)

    @property
    def prompt_candidates(self) -> tuple[dict[str, Any], ...]:
        return tuple(candidate.to_prompt_dict() for candidate in self.candidates)

    def to_audit_dict(self) -> dict[str, Any]:
        """返回不含正文、可直接 JSON 序列化的召回审计信息。"""

        return {
            "treeFingerprint": self.tree_fingerprint,
            "queryDigest": self.query_digest,
            "baseTop64": list(self.base_leaf_ids),
            "finalCandidates": [
                {
                    **candidate.to_prompt_dict(),
                    "rank": candidate.rank,
                    "rrf": candidate.rrf_score,
                    "channelRanks": dict(candidate.channel_ranks),
                    "protectedReasons": list(candidate.protected_reasons),
                }
                for candidate in self.candidates
            ],
            "channelRankings": {
                ranking.channel: list(ranking.node_ids)
                for ranking in self.channel_rankings
            },
            "rrf": {str(node_id): score for node_id, score in self.rrf_scores},
            "protectedReasons": {
                str(node_id): list(reasons)
                for node_id, reasons in self.protected_reasons
            },
            "promptChars": self.prompt_chars,
            "candidateProjectionChars": self.candidate_projection_chars,
            "elapsedMs": self.elapsed_ms,
        }


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _bounded_text_tuple(values: Iterable[Any] | None, limit: int) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        values = (values,)
    result: list[str] = []
    for value in values:
        text = _as_text(value)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return tuple(result)


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = _DASH_RE.sub("-", normalized)
    return _SPACE_RE.sub(" ", normalized).strip()


def _compact_identifier(value: str) -> str:
    return re.sub(r"[^a-z0-9\u3400-\u4dbf\u4e00-\u9fff]+", "", _normalize_text(value))


def identifier_aliases(value: str) -> tuple[str, ...]:
    """为型号/标准号生成紧凑、连字符和空格三种稳定写法。"""

    normalized = _normalize_text(value)
    if not normalized:
        return ()
    parts = re.findall(r"[a-z]+|\d+[a-z]?", normalized)
    if len(parts) < 2 or not any(part[0].isdigit() for part in parts):
        compact = _compact_identifier(normalized)
        return (compact,) if compact else ()

    aliases: list[str] = []
    for candidate in ("".join(parts), "-".join(parts), " ".join(parts), normalized):
        candidate = _normalize_text(candidate)
        if candidate and candidate not in aliases:
            aliases.append(candidate)
    return tuple(aliases)


def _extract_identifiers(text: str, limit: int = MAX_IDENTIFIERS) -> tuple[str, ...]:
    result: list[str] = []
    for match in _IDENTIFIER_RE.finditer(_normalize_text(text)):
        identifier = match.group(0).strip()
        if identifier and identifier not in result:
            result.append(identifier)
        if len(result) >= limit:
            break
    return tuple(result)


def build_document_architecture_signals(
    *,
    filename: Any = "",
    original_filename: Any = "",
    title: Any = "",
    headings: Iterable[Any] | None = None,
    identifiers: Iterable[Any] | None = None,
    body: Any = "",
) -> DocumentArchitectureSignals:
    """规范化并截断主链提供的文档信号，同时补取型号和标准号。"""

    normalized_filename = _as_text(filename)
    normalized_original_filename = _as_text(original_filename)
    normalized_title = _as_text(title)
    normalized_headings = _bounded_text_tuple(headings, MAX_HEADINGS)
    normalized_body = _as_text(body)[:MAX_BODY_CHARS]

    explicit_identifiers = list(_bounded_text_tuple(identifiers, MAX_IDENTIFIERS))
    source_text = "\n".join(
        (
            normalized_filename,
            normalized_original_filename,
            normalized_title,
            *normalized_headings,
            normalized_body,
        )
    )
    for identifier in _extract_identifiers(source_text):
        if len(explicit_identifiers) >= MAX_IDENTIFIERS:
            break
        if identifier not in explicit_identifiers:
            explicit_identifiers.append(identifier)

    return DocumentArchitectureSignals(
        filename=normalized_filename,
        original_filename=normalized_original_filename,
        title=normalized_title,
        headings=normalized_headings,
        identifiers=tuple(explicit_identifiers),
        body_excerpt=normalized_body,
    )


def _tokenize(value: str) -> tuple[str, ...]:
    normalized = _normalize_text(value)
    tokens: list[str] = []
    for word in _WORD_RE.findall(normalized):
        if word:
            tokens.append(word)
    for run in _CJK_RUN_RE.findall(normalized):
        tokens.append(run)
        for width in (2, 3):
            if len(run) >= width:
                tokens.extend(run[index : index + width] for index in range(len(run) - width + 1))
    for identifier in _extract_identifiers(normalized):
        tokens.extend(identifier_aliases(identifier))
    return tuple(tokens)


def _query_digest(signals: DocumentArchitectureSignals) -> str:
    serialized = json.dumps(
        {
            "filename": signals.filename,
            "originalFilename": signals.original_filename,
            "title": signals.title,
            "headings": signals.headings,
            "identifiers": signals.identifiers,
            "bodyExcerpt": signals.body_excerpt,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


@dataclass(frozen=True, slots=True)
class _BM25Corpus:
    leaf_ids: tuple[int, ...]
    document_lengths: tuple[int, ...]
    average_length: float
    inverted_index: Mapping[str, tuple[tuple[int, int], ...]]


_BM25_CACHE_CAPACITY = 4
_BM25_CACHE: OrderedDict[str, _BM25Corpus] = OrderedDict()
_BM25_CACHE_LOCK = threading.Lock()


def _node_search_text(node: ArchitectureNodeProfile) -> str:
    return "\n".join((node.name, node.semantic_path, node.remark, *node.aliases))


def _build_bm25_corpus(index: ArchitectureTreeIndex) -> _BM25Corpus:
    leaf_ids = index.leaf_ids
    lengths: list[int] = []
    postings: defaultdict[str, list[tuple[int, int]]] = defaultdict(list)
    for document_index, leaf_id in enumerate(leaf_ids):
        frequencies = Counter(_tokenize(_node_search_text(index.require(leaf_id))))
        length = sum(frequencies.values())
        lengths.append(length)
        for token, frequency in frequencies.items():
            postings[token].append((document_index, frequency))
    return _BM25Corpus(
        leaf_ids=leaf_ids,
        document_lengths=tuple(lengths),
        average_length=(sum(lengths) / len(lengths)) if lengths else 0.0,
        inverted_index={token: tuple(values) for token, values in postings.items()},
    )


def _get_bm25_corpus(index: ArchitectureTreeIndex) -> _BM25Corpus:
    with _BM25_CACHE_LOCK:
        cached = _BM25_CACHE.get(index.fingerprint)
        if cached is not None:
            _BM25_CACHE.move_to_end(index.fingerprint)
            return cached

    corpus = _build_bm25_corpus(index)
    with _BM25_CACHE_LOCK:
        existing = _BM25_CACHE.get(index.fingerprint)
        if existing is not None:
            _BM25_CACHE.move_to_end(index.fingerprint)
            return existing
        _BM25_CACHE[index.fingerprint] = corpus
        while len(_BM25_CACHE) > _BM25_CACHE_CAPACITY:
            _BM25_CACHE.popitem(last=False)
    return corpus


def _bm25_rank(index: ArchitectureTreeIndex, query_tokens: Sequence[str]) -> tuple[int, ...]:
    corpus = _get_bm25_corpus(index)
    document_count = len(corpus.leaf_ids)
    if document_count == 0 or not query_tokens:
        return ()

    scores: defaultdict[int, float] = defaultdict(float)
    for token in set(query_tokens):
        postings = corpus.inverted_index.get(token)
        if not postings:
            continue
        document_frequency = len(postings)
        inverse_document_frequency = math.log(
            1.0 + (document_count - document_frequency + 0.5) / (document_frequency + 0.5)
        )
        for document_index, term_frequency in postings:
            document_length = corpus.document_lengths[document_index]
            length_ratio = (
                document_length / corpus.average_length
                if corpus.average_length
                else 0.0
            )
            denominator = term_frequency + BM25_K1 * (
                1.0 - BM25_B + BM25_B * length_ratio
            )
            scores[document_index] += inverse_document_frequency * (
                term_frequency * (BM25_K1 + 1.0) / denominator
            )

    ranked_documents = sorted(
        scores,
        key=lambda document_index: (
            -scores[document_index],
            index.require(corpus.leaf_ids[document_index]).ordinal,
        ),
    )[:BM25_LIMIT]
    return tuple(corpus.leaf_ids[document_index] for document_index in ranked_documents)


def _contains_ascii_name(query: str, name: str) -> bool:
    if not name:
        return False
    escaped = re.escape(name).replace(r"\-", r"[\s\-]*")
    return re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", query) is not None


def _direct_exact_ids(
    index: ArchitectureTreeIndex,
    signals: DocumentArchitectureSignals,
    *,
    strong_evidence_only: bool,
    strong_identity_enabled: bool,
) -> tuple[int, ...]:
    if strong_evidence_only and not strong_identity_enabled:
        return ()
    query = _normalize_text(
        signals.strong_identity_text
        if strong_evidence_only
        else signals.query_text
    )
    identifiers = (
        signals.strong_identifiers
        if strong_evidence_only
        else signals.identifiers
    )
    identifier_alias_set = {
        alias
        for identifier in identifiers
        for alias in identifier_aliases(identifier)
    }
    direct: list[int] = []
    for node in index.nodes:
        name = _normalize_text(node.name)
        compact_name = _compact_identifier(node.name)
        matched = False
        if any(alias in identifier_alias_set for alias in node.aliases):
            matched = True
        elif compact_name in identifier_alias_set:
            matched = True
        elif _CJK_RUN_RE.search(name):
            matched = len(name) >= 2 and name in query
        elif len(name) >= 3:
            matched = _contains_ascii_name(query, name)
        if not matched:
            semantic_path = _normalize_text(node.semantic_path)
            matched = len(semantic_path) >= 4 and semantic_path in query
        if matched:
            direct.append(node.id)
    return tuple(direct)


def _expand_exact_leaves(
    index: ArchitectureTreeIndex,
    direct_ids: Sequence[int],
) -> tuple[tuple[int, ...], dict[int, tuple[str, ...]]]:
    """按证据强度展开 exact 通道，并只保护可证明的候选。

    直接命中的叶节点优先，其次是具有完整七类明细结构的装备 family。普通中文父节点
    命中仍可为 exact 通道补充后代，但这些后代不再获得 protected-first 优先级，避免
    “海军”等通用短词把 BM25/树路由的高置信候选挤出 Top-64。
    """
    ranked: list[int] = []
    reasons: defaultdict[int, list[str]] = defaultdict(list)

    def append_ranked(node_id: int) -> bool:
        if node_id in ranked:
            return True
        if len(ranked) < BM25_LIMIT:
            ranked.append(node_id)
            return True
        return False

    # 直接节点自身始终保留 exact 证据；其中叶节点是最强、最窄的候选，优先入通道。
    for direct_id in direct_ids:
        node = index.require(direct_id)
        if not node.is_leaf or append_ranked(direct_id):
            direct_reason = f"exact:{direct_id}"
            if direct_reason not in reasons[direct_id]:
                reasons[direct_id].append(direct_reason)

    # 完整七类装备 family 是窄范围、结构化的型号证据，允许整体保护并先于普通父节点展开。
    family_leaf_ids_by_parent: dict[int, tuple[int, ...]] = {}
    for direct_id in direct_ids:
        node = index.require(direct_id)
        if node.is_leaf:
            continue
        family_leaf_ids = _equipment_family(index, direct_id)
        if not family_leaf_ids:
            continue
        family_leaf_ids_by_parent[direct_id] = family_leaf_ids
        for leaf_id in family_leaf_ids:
            reason = f"exact-descendant:{direct_id}"
            if append_ranked(leaf_id) and reason not in reasons[leaf_id]:
                reasons[leaf_id].append(reason)
            if len(ranked) >= BM25_LIMIT:
                break
        if len(ranked) >= BM25_LIMIT:
            break

    # 普通父节点后代只参与 exact 排名，不继承 protected 身份。这样仍保留召回覆盖面，
    # 同时让具有 lexical/tree RRF 分数的具体叶节点可以公平进入基础候选集。
    for direct_id in direct_ids:
        node = index.require(direct_id)
        if node.is_leaf:
            continue
        family_leaf_ids = set(family_leaf_ids_by_parent.get(direct_id, ()))
        for leaf_id in index.leaf_descendants_by_id[direct_id]:
            if leaf_id in family_leaf_ids:
                continue
            append_ranked(leaf_id)
            if len(ranked) >= BM25_LIMIT:
                break
        if len(ranked) >= BM25_LIMIT:
            break

    return tuple(ranked), {node_id: tuple(values) for node_id, values in reasons.items()}


def _local_route_scores(
    index: ArchitectureTreeIndex,
    query_tokens: Sequence[str],
    identifier_alias_set: set[str],
) -> dict[int, float]:
    query_frequency = Counter(query_tokens)
    scores: dict[int, float] = {}
    for node in index.nodes:
        # 路由节点的“直接命中”只观察节点自身，不能让祖先路径里的公共词把
        # 整个子树都标成直接命中；子树相关性由下方的 subtree_score 向上传播。
        node_tokens = set(_tokenize("\n".join((node.name, node.remark))))
        overlap_score = sum(1.0 + math.log1p(query_frequency[token]) for token in node_tokens if token in query_frequency)
        alias_bonus = 0.0
        if any(alias in identifier_alias_set for alias in node.aliases):
            alias_bonus = 100.0
        score = overlap_score + alias_bonus
        if score > 0:
            scores[node.id] = score
    return scores


def _tree_rank(
    index: ArchitectureTreeIndex,
    query_tokens: Sequence[str],
    identifier_alias_set: set[str],
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    local_scores = _local_route_scores(index, query_tokens, identifier_alias_set)
    if not local_scores:
        return (), ()

    subtree_scores: dict[int, float] = {}

    def subtree_score(node_id: int) -> float:
        cached = subtree_scores.get(node_id)
        if cached is not None:
            return cached
        child_score = max(
            (subtree_score(child_id) for child_id in index.children_by_id[node_id]),
            default=0.0,
        )
        result = max(local_scores.get(node_id, 0.0), child_score)
        subtree_scores[node_id] = result
        return result

    root_ids = sorted(
        (root_id for root_id in index.root_ids if subtree_score(root_id) > 0),
        key=lambda node_id: (-subtree_score(node_id), index.require(node_id).ordinal),
    )[:TREE_ROOT_BEAM]
    frontier = root_ids
    leaves: list[int] = []
    direct_nodes: list[int] = []

    while frontier and len(leaves) < TREE_LEAF_LIMIT:
        next_frontier: list[int] = []
        for node_id in frontier:
            node = index.require(node_id)
            if local_scores.get(node_id, 0.0) > 0 and node_id not in direct_nodes:
                direct_nodes.append(node_id)
            if node.is_leaf:
                if node_id not in leaves:
                    leaves.append(node_id)
                continue

            relevant_children = [
                child_id
                for child_id in index.children_by_id[node_id]
                if subtree_score(child_id) > 0
            ]
            if relevant_children:
                next_frontier.extend(relevant_children)
            elif local_scores.get(node_id, 0.0) > 0:
                for leaf_id in index.leaf_descendants_by_id[node_id]:
                    if leaf_id not in leaves:
                        leaves.append(leaf_id)
                    if len(leaves) >= TREE_LEAF_LIMIT:
                        break

        if not next_frontier:
            break
        frontier = sorted(
            set(next_frontier),
            key=lambda node_id: (-subtree_score(node_id), index.require(node_id).ordinal),
        )[:TREE_INTERMEDIATE_BEAM]

    return tuple(leaves[:TREE_LEAF_LIMIT]), tuple(direct_nodes)


def _detail_kind(node: ArchitectureNodeProfile) -> str | None:
    normalized_name = _normalize_text(node.name)
    for kind in DETAIL_KINDS:
        if normalized_name.endswith(_normalize_text(kind)):
            return kind
    return None


def _equipment_family(index: ArchitectureTreeIndex, parent_id: int) -> tuple[int, ...]:
    children = index.children_by_id[parent_id]
    by_kind = {
        kind: child_id
        for child_id in children
        if index.require(child_id).is_leaf
        for kind in (_detail_kind(index.require(child_id)),)
        if kind is not None
    }
    if not all(kind in by_kind for kind in DETAIL_KINDS):
        return ()
    return tuple(by_kind[kind] for kind in DETAIL_KINDS)


def _data_standard_roots(index: ArchitectureTreeIndex) -> tuple[int, ...]:
    return tuple(
        node.id
        for node in index.nodes
        if not node.is_leaf and _normalize_text(node.name) == "数据标准"
    )


def _data_standard_leaves(index: ArchitectureTreeIndex) -> tuple[int, ...]:
    result: list[int] = []
    for root_id in _data_standard_roots(index):
        descendants = index.leaf_descendants_by_id[root_id]
        by_kind = {
            _normalize_text(index.require(leaf_id).name): leaf_id
            for leaf_id in descendants
        }
        for kind in DATA_STANDARD_KINDS:
            leaf_id = by_kind.get(_normalize_text(kind))
            if leaf_id is not None and leaf_id not in result:
                result.append(leaf_id)
    return tuple(result)


def _rule_rank(
    index: ArchitectureTreeIndex,
    signals: DocumentArchitectureSignals,
    direct_exact_ids: Sequence[int],
    lexical_ids: Sequence[int],
    *,
    strong_evidence_only: bool,
    strong_identity_enabled: bool,
) -> tuple[int, ...]:
    ranked: list[int] = []
    query = _normalize_text(signals.query_text)
    if _GJB_RE.search(query):
        ranked.extend(_data_standard_leaves(index))

    family_parent_ids: list[int] = []
    family_identifiers = (
        signals.strong_identifiers
        if strong_evidence_only and strong_identity_enabled
        else ()
        if strong_evidence_only
        else signals.identifiers
    )
    family_identifier_aliases = {
        value
        for identifier in family_identifiers
        for value in identifier_aliases(identifier)
    }
    for node_id in (*direct_exact_ids, *lexical_ids[:16]):
        node = index.require(node_id)
        parent_id = node.id if not node.is_leaf else node.parent_id
        if parent_id in index.nodes_by_id and parent_id not in family_parent_ids:
            family = _equipment_family(index, parent_id)
            if family:
                if node.id in direct_exact_ids or any(
                    alias in family_identifier_aliases
                    for alias in index.require(parent_id).aliases
                ):
                    family_parent_ids.append(parent_id)
    for parent_id in family_parent_ids:
        for leaf_id in _equipment_family(index, parent_id):
            if leaf_id not in ranked:
                ranked.append(leaf_id)

    return tuple(ranked)


def _jane_title_type_alias_rule_ids(
    index: ArchitectureTreeIndex,
    signals: DocumentArchitectureSignals,
    *,
    strong_evidence_only: bool,
) -> tuple[int, ...]:
    """用可信 Jane's 标题和完整类型描述补入极窄的无人潜航器候选。"""

    if not strong_evidence_only:
        return ()
    title = _normalize_text(signals.title)
    body_excerpt = _normalize_text(signals.body_excerpt)
    if (
        not _JANE_UUV_TITLE_ALIAS_RE.search(title)
        or not _JANE_UUV_TYPE_DESCRIPTION_RE.search(body_excerpt)
    ):
        return ()

    matches = tuple(
        node.id
        for node in index.nodes
        if (
            node.is_leaf
            and node.parent_id is not None
            and _normalize_text(node.name) == _JANE_UUV_CANONICAL_NAME
        )
    )
    return matches if len(matches) == 1 else ()


def _rank_lookup(node_ids: Sequence[int]) -> dict[int, int]:
    return {node_id: rank for rank, node_id in enumerate(node_ids, start=1)}


def _rrf_scores(
    lexical_ids: Sequence[int],
    tree_ids: Sequence[int],
    rule_ids: Sequence[int],
) -> dict[int, float]:
    scores: defaultdict[int, float] = defaultdict(float)
    for weight, node_ids in ((1.0, lexical_ids), (0.8, tree_ids), (0.8, rule_ids)):
        for rank, node_id in enumerate(node_ids, start=1):
            scores[node_id] += weight / (RRF_K + rank)
    return dict(scores)


def _best_channel_rank(node_id: int, rank_maps: Mapping[str, Mapping[int, int]]) -> int:
    ranks = [mapping[node_id] for mapping in rank_maps.values() if node_id in mapping]
    return min(ranks, default=10**9)


def _ordered_fused_leaves(
    index: ArchitectureTreeIndex,
    exact_ids: Sequence[int],
    rrf: Mapping[int, float],
    protected: Mapping[int, tuple[str, ...]],
    rank_maps: Mapping[str, Mapping[int, int]],
) -> tuple[int, ...]:
    union = set(exact_ids) | set(rrf)
    return tuple(
        sorted(
            (node_id for node_id in union if index.require(node_id).is_leaf),
            key=lambda node_id: (
                0 if node_id in protected else 1,
                -rrf.get(node_id, 0.0),
                _best_channel_rank(node_id, rank_maps),
                index.require(node_id).ordinal,
            ),
        )
    )


def _append_unique(target: list[int], values: Iterable[int], limit: int) -> None:
    for value in values:
        if value not in target:
            target.append(value)
        if len(target) >= limit:
            return


def _augment_leaf_candidates(
    index: ArchitectureTreeIndex,
    base_ids: Sequence[int],
    fused_ids: Sequence[int],
    rule_ids: Sequence[int],
    direct_exact_ids: Sequence[int],
    direct_tree_ids: Sequence[int],
    rrf: Mapping[int, float],
    *,
    strong_evidence_only: bool,
    preferred_parent_ids: Sequence[int] = (),
) -> tuple[int, ...]:
    selected = list(base_ids)

    family_parent_ids: list[int] = []
    for parent_id in preferred_parent_ids:
        family = _equipment_family(index, parent_id)
        if not family:
            continue
        family_parent_ids.append(parent_id)
        _append_unique(selected, family, MAX_LEAF_CANDIDATES)

    trigger_ids = set(rule_ids) | set(direct_exact_ids)
    if not strong_evidence_only:
        trigger_ids.update(direct_tree_ids)
    for leaf_id in (*base_ids, *rule_ids):
        leaf = index.require(leaf_id)
        if leaf.parent_id not in index.nodes_by_id:
            continue
        family = _equipment_family(index, leaf.parent_id)
        if not family:
            continue
        if leaf_id in trigger_ids or leaf.parent_id in trigger_ids or leaf_id in rule_ids:
            if leaf.parent_id not in family_parent_ids:
                family_parent_ids.append(leaf.parent_id)
    for parent_id in family_parent_ids:
        _append_unique(selected, _equipment_family(index, parent_id), MAX_LEAF_CANDIDATES)

    standard_leaf_ids = _data_standard_leaves(index)
    if set(base_ids) & set(standard_leaf_ids) or set(rule_ids) & set(standard_leaf_ids):
        _append_unique(selected, standard_leaf_ids, MAX_LEAF_CANDIDATES)

    # 对已命中的小根分支，至少保留各直接子分支中的一个高分叶节点。
    hit_roots = {index.require(leaf_id).root_id for leaf_id in base_ids}
    fused_position = {node_id: position for position, node_id in enumerate(fused_ids)}
    for root_id in sorted(hit_roots, key=lambda node_id: index.require(node_id).ordinal):
        root_leaves = index.leaf_descendants_by_id[root_id]
        if len(root_leaves) > MAX_BASE_LEAVES:
            continue
        for child_id in index.children_by_id[root_id]:
            branch_leaves = index.leaf_descendants_by_id[child_id]
            if not branch_leaves:
                continue
            best_leaf = min(
                branch_leaves,
                key=lambda node_id: (
                    -rrf.get(node_id, 0.0),
                    fused_position.get(node_id, 10**9),
                    index.require(node_id).ordinal,
                ),
            )
            _append_unique(selected, (best_leaf,), MAX_LEAF_CANDIDATES)
            if len(selected) >= MAX_LEAF_CANDIDATES:
                break
        if len(selected) >= MAX_LEAF_CANDIDATES:
            break

    return tuple(selected[:MAX_LEAF_CANDIDATES])


def _eligible_parent_ids(
    index: ArchitectureTreeIndex,
    fused_ids: Sequence[int],
    direct_exact_ids: Sequence[int],
    direct_tree_ids: Sequence[int],
    rrf: Mapping[int, float],
) -> tuple[int, ...]:
    top16 = set(fused_ids[:16])
    direct_exact = set(direct_exact_ids)
    direct_tree = set(direct_tree_ids)
    standard_roots = set(_data_standard_roots(index))
    eligible: list[tuple[int, int, int, float, int]] = []
    for node in index.nodes:
        # parentId 指向未随请求传入的祖先时，节点虽是当前有限树的边界根，仍不是
        # 完整业务树的真实根；只有 parent_id=None 才禁止作为最终父候选。
        if (
            node.is_leaf
            or node.parent_id is None
            or node.id in standard_roots
        ):
            continue
        covered = len(top16 & set(index.leaf_descendants_by_id[node.id]))
        exact_hit = node.id in direct_exact
        tree_hit = node.id in direct_tree
        if not exact_hit and not tree_hit and covered < 2:
            continue
        descendant_score = max(
            (rrf.get(leaf_id, 0.0) for leaf_id in index.leaf_descendants_by_id[node.id]),
            default=0.0,
        )
        eligible.append(
            (
                node.id,
                0 if exact_hit else 1,
                0 if tree_hit else 1,
                -descendant_score,
                -covered,
            )
        )
    eligible.sort(
        key=lambda item: (
            item[1],
            item[2],
            item[3],
            item[4],
            index.require(item[0]).ordinal,
        )
    )
    return tuple(item[0] for item in eligible[:MAX_PARENT_CANDIDATES])


_PREFERRED_PARENT_REASON_PRIORITY = {
    "jane_scope_parent": 0,
    "jane_tree_gap_lead_parent": 1,
    "jane_high_level_branch": 2,
    "jane_branch_guard": 3,
}


def _normalize_preferred_parent_reasons(
    index: ArchitectureTreeIndex,
    values: Mapping[int, Sequence[str]] | None,
) -> dict[int, tuple[str, ...]]:
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise TypeError("preferred_parent_reasons 必须是 Mapping")

    normalized: dict[int, tuple[str, ...]] = {}
    for raw_node_id, raw_reasons in values.items():
        if isinstance(raw_node_id, bool) or not isinstance(raw_node_id, int):
            raise TypeError("preferred_parent_reasons 的键必须是数字 ID")
        node = index.require(raw_node_id)
        if node.is_leaf or node.parent_id is None:
            raise ArchitectureRecallError(
                f"受保护作用域候选必须是非根父节点: {raw_node_id}"
            )
        if isinstance(raw_reasons, (str, bytes)):
            raw_reasons = (str(raw_reasons),)
        reasons = tuple(
            dict.fromkeys(
                reason
                for reason in (_as_text(value) for value in raw_reasons)
                if reason
            )
        )
        if not reasons:
            raise ArchitectureRecallError(
                f"受保护作用域候选缺少保护原因: {raw_node_id}"
            )
        normalized[raw_node_id] = reasons

    return dict(
        sorted(
            normalized.items(),
            key=lambda item: (
                min(
                    (
                        _PREFERRED_PARENT_REASON_PRIORITY.get(reason, 99)
                        for reason in item[1]
                    ),
                    default=99,
                ),
                index.require(item[0]).ordinal,
            ),
        )
    )


def _projection_chars(candidates: Sequence[ArchitectureRecallCandidate]) -> int:
    payload = [candidate.to_prompt_dict() for candidate in candidates]
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def _normalize_candidate_scope_ids(
    index: ArchitectureTreeIndex,
    values: Sequence[int] | None,
) -> tuple[int, ...] | None:
    if values is None:
        return None
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise TypeError("candidate_scope_ids 必须是数字 ID 序列")
    normalized: list[int] = []
    for node_id in values:
        if isinstance(node_id, bool) or not isinstance(node_id, int):
            raise TypeError("candidate_scope_ids 必须只包含数字 ID")
        node = index.require(node_id)
        if not node.is_leaf:
            raise ArchitectureRecallError(
                f"candidate_scope_ids 只能包含叶子节点: {node_id}"
            )
        if node_id not in normalized:
            normalized.append(node_id)
    if not normalized:
        raise ArchitectureRecallError("candidate_scope_ids 不能为空")
    return tuple(normalized)


def _normalize_candidate_remark_overrides(
    index: ArchitectureTreeIndex,
    values: Mapping[int, str] | None,
) -> dict[int, str]:
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise TypeError("candidate_remark_overrides 必须是 Mapping")
    normalized: dict[int, str] = {}
    for node_id, value in values.items():
        if isinstance(node_id, bool) or not isinstance(node_id, int):
            raise TypeError("candidate_remark_overrides 的键必须是数字 ID")
        index.require(node_id)
        remark = _as_text(value)
        if remark:
            normalized[node_id] = remark[:MAX_REMARK_CHARS]
    return normalized


class ArchitectureRecallService:
    """在完整领域树上执行确定性的本地多通道 Top-K 召回。"""

    def __init__(
        self,
        index: ArchitectureTreeIndex,
        *,
        prompt_char_limit: int = MAX_CLASSIFICATION_PROMPT_CHARS,
        prompt_overhead_chars: int = CLASSIFICATION_PROMPT_OVERHEAD_CHARS,
    ) -> None:
        if (
            isinstance(prompt_char_limit, bool)
            or not isinstance(prompt_char_limit, int)
            or prompt_char_limit < 1
        ):
            raise ValueError("分类 Prompt 字符上限必须是正整数")
        if (
            isinstance(prompt_overhead_chars, bool)
            or not isinstance(prompt_overhead_chars, int)
            or prompt_overhead_chars < 0
        ):
            raise ValueError("分类 Prompt 固定开销必须是非负整数")
        self._index = index
        self._prompt_char_limit = prompt_char_limit
        self._prompt_overhead_chars = prompt_overhead_chars

    def recall(
        self,
        signals: DocumentArchitectureSignals,
        *,
        strong_evidence_only: bool = False,
        strong_identity_enabled: bool = True,
        preferred_parent_reasons: Mapping[int, Sequence[str]] | None = None,
        candidate_scope_ids: Sequence[int] | None = None,
        candidate_scope_reason: str = "",
        candidate_remark_overrides: Mapping[int, str] | None = None,
    ) -> ArchitectureRecallDecision:
        started_at = time.perf_counter()
        if not isinstance(signals, DocumentArchitectureSignals):
            raise TypeError("signals 必须是 DocumentArchitectureSignals")
        if not isinstance(strong_evidence_only, bool):
            raise TypeError("strong_evidence_only 必须是布尔值")
        if not isinstance(strong_identity_enabled, bool):
            raise TypeError("strong_identity_enabled 必须是布尔值")
        if signals.is_empty:
            raise ArchitectureRecallError("文档不包含可用于领域召回的有效信号")

        preferred_parents = _normalize_preferred_parent_reasons(
            self._index,
            preferred_parent_reasons,
        )
        scoped_ids = _normalize_candidate_scope_ids(
            self._index,
            candidate_scope_ids,
        )
        scoped_id_set = set(scoped_ids or ())
        if scoped_ids is not None and any(
            node_id not in scoped_id_set for node_id in preferred_parents
        ):
            raise ArchitectureRecallError(
                "受保护父节点不属于 candidate_scope_ids"
            )
        remark_overrides = _normalize_candidate_remark_overrides(
            self._index,
            candidate_remark_overrides,
        )
        query_tokens = _tokenize(signals.query_text)
        strong_identifiers = (
            signals.strong_identifiers
            if strong_evidence_only and strong_identity_enabled
            else ()
            if strong_evidence_only
            else signals.identifiers
        )
        identifier_alias_set = {
            alias
            for identifier in strong_identifiers
            for alias in identifier_aliases(identifier)
        }
        direct_exact_ids = _direct_exact_ids(
            self._index,
            signals,
            strong_evidence_only=strong_evidence_only,
            strong_identity_enabled=strong_identity_enabled,
        )
        exact_leaf_ids, exact_protected = _expand_exact_leaves(
            self._index,
            direct_exact_ids,
        )
        protected: dict[int, tuple[str, ...]] = dict(exact_protected)
        for node_id, reasons in preferred_parents.items():
            protected[node_id] = tuple(
                dict.fromkeys((*protected.get(node_id, ()), *reasons))
            )
        lexical_ids = _bm25_rank(self._index, query_tokens)
        tree_ids, direct_tree_ids = _tree_rank(
            self._index,
            query_tokens,
            identifier_alias_set,
        )
        rule_ids = _rule_rank(
            self._index,
            signals,
            direct_exact_ids,
            lexical_ids,
            strong_evidence_only=strong_evidence_only,
            strong_identity_enabled=strong_identity_enabled,
        )
        title_type_alias_ids = _jane_title_type_alias_rule_ids(
            self._index,
            signals,
            strong_evidence_only=strong_evidence_only,
        )
        rule_ids = tuple(dict.fromkeys((*rule_ids, *title_type_alias_ids)))
        for node_id in title_type_alias_ids:
            protected[node_id] = tuple(
                dict.fromkeys(
                    (
                        *protected.get(node_id, ()),
                        _JANE_TITLE_TYPE_ALIAS_REASON,
                    )
                )
            )

        if scoped_ids is not None:
            direct_exact_ids = tuple(
                node_id for node_id in direct_exact_ids if node_id in scoped_id_set
            )
            exact_leaf_ids = tuple(
                node_id for node_id in exact_leaf_ids if node_id in scoped_id_set
            )
            lexical_ids = tuple(
                node_id for node_id in lexical_ids if node_id in scoped_id_set
            )
            tree_ids = tuple(
                node_id for node_id in tree_ids if node_id in scoped_id_set
            )
            direct_tree_ids = tuple(
                node_id for node_id in direct_tree_ids if node_id in scoped_id_set
            )
            rule_ids = tuple(
                dict.fromkeys(
                    (
                        *(
                            node_id
                            for node_id in rule_ids
                            if node_id in scoped_id_set
                        ),
                        *scoped_ids,
                    )
                )
            )
            protected = {
                node_id: reasons
                for node_id, reasons in protected.items()
                if node_id in scoped_id_set
            }
            scope_reason = _as_text(candidate_scope_reason) or "candidate-scope"
            for node_id in scoped_ids:
                protected[node_id] = tuple(
                    dict.fromkeys(
                        (*protected.get(node_id, ()), scope_reason)
                    )
                )

        if not any(
            (exact_leaf_ids, lexical_ids, tree_ids, rule_ids, preferred_parents)
        ):
            raise ArchitectureRecallError("文档信号未命中任何领域树候选")

        channel_ids = {
            "exact": exact_leaf_ids,
            "lexical": lexical_ids,
            "tree": tree_ids,
            "rule": rule_ids,
        }
        if scoped_ids is not None:
            channel_ids["scope"] = scoped_ids
        rank_maps = {channel: _rank_lookup(ids) for channel, ids in channel_ids.items()}
        rrf = _rrf_scores(lexical_ids, tree_ids, rule_ids)
        fused_ids = _ordered_fused_leaves(
            self._index,
            exact_leaf_ids,
            rrf,
            protected,
            rank_maps,
        )
        base_ids = fused_ids[:MAX_BASE_LEAVES]
        leaf_ids = _augment_leaf_candidates(
            self._index,
            base_ids,
            fused_ids,
            rule_ids,
            direct_exact_ids,
            direct_tree_ids,
            rrf,
            strong_evidence_only=strong_evidence_only,
            preferred_parent_ids=tuple(preferred_parents),
        )
        if scoped_ids is not None:
            leaf_ids = tuple(
                node_id for node_id in leaf_ids if node_id in scoped_id_set
            )
        ordinary_parent_ids = _eligible_parent_ids(
            self._index,
            fused_ids,
            direct_exact_ids,
            direct_tree_ids,
            rrf,
        )
        parent_ids: list[int] = list(preferred_parents)
        _append_unique(
            parent_ids,
            ordinary_parent_ids,
            MAX_PARENT_CANDIDATES,
        )
        parent_ids = parent_ids[:MAX_PARENT_CANDIDATES]
        if scoped_ids is not None:
            parent_ids = [
                node_id for node_id in parent_ids if node_id in scoped_id_set
            ]
        final_ids = (*leaf_ids, *parent_ids)
        if len(final_ids) > MAX_FINAL_CANDIDATES:
            raise ArchitectureRecallError("领域候选数量超过 128 个")

        candidates: list[ArchitectureRecallCandidate] = []
        for rank, node_id in enumerate(final_ids, start=1):
            node = self._index.require(node_id)
            candidate_channel_ranks = tuple(
                (channel, mapping[node_id])
                for channel, mapping in rank_maps.items()
                if node_id in mapping
            )
            candidates.append(
                ArchitectureRecallCandidate(
                    architecture_id=node.id,
                    path_name=node.semantic_path,
                    node_type="leaf" if node.is_leaf else "parent",
                    remark=remark_overrides.get(
                        node.id,
                        node.remark[:MAX_REMARK_CHARS],
                    ),
                    rank=rank,
                    rrf_score=rrf.get(node_id, 0.0),
                    channel_ranks=candidate_channel_ranks,
                    protected_reasons=protected.get(node_id, ()),
                )
            )

        candidate_tuple = tuple(candidates)
        final_id_set = set(final_ids)
        visible_protected = {
            node_id: reasons
            for node_id, reasons in protected.items()
            if node_id in final_id_set
        }
        projection_chars = _projection_chars(candidate_tuple)
        prompt_chars = projection_chars + self._prompt_overhead_chars
        if prompt_chars > self._prompt_char_limit:
            raise ArchitecturePromptBudgetError(
                f"领域分类 Prompt 估算 {prompt_chars} 字符，超过 {self._prompt_char_limit} 字符上限"
            )

        elapsed_ms = round((time.perf_counter() - started_at) * 1000.0, 3)
        return ArchitectureRecallDecision(
            tree_fingerprint=self._index.fingerprint,
            query_digest=_query_digest(signals),
            base_leaf_ids=tuple(base_ids),
            candidates=candidate_tuple,
            channel_rankings=tuple(
                RecallChannelRanking(channel=channel, node_ids=tuple(ids))
                for channel, ids in channel_ids.items()
            ),
            rrf_scores=tuple(
                sorted(
                    rrf.items(),
                    key=lambda item: (-item[1], self._index.require(item[0]).ordinal),
                )
            ),
            protected_reasons=tuple(
                (node_id, visible_protected[node_id])
                for node_id in sorted(
                    visible_protected,
                    key=lambda value: self._index.require(value).ordinal,
                )
            ),
            direct_exact_ids=tuple(direct_exact_ids),
            direct_tree_ids=tuple(direct_tree_ids),
            candidate_projection_chars=projection_chars,
            prompt_chars=prompt_chars,
            elapsed_ms=elapsed_ms,
        )


def recall_architecture_candidates(
    index: ArchitectureTreeIndex,
    signals: DocumentArchitectureSignals,
    *,
    prompt_char_limit: int = MAX_CLASSIFICATION_PROMPT_CHARS,
    prompt_overhead_chars: int = CLASSIFICATION_PROMPT_OVERHEAD_CHARS,
    strong_evidence_only: bool = False,
    strong_identity_enabled: bool = True,
    preferred_parent_reasons: Mapping[int, Sequence[str]] | None = None,
    candidate_scope_ids: Sequence[int] | None = None,
    candidate_scope_reason: str = "",
    candidate_remark_overrides: Mapping[int, str] | None = None,
) -> ArchitectureRecallDecision:
    """便于主链按请求索引执行一次召回的无状态入口。"""

    return ArchitectureRecallService(
        index,
        prompt_char_limit=prompt_char_limit,
        prompt_overhead_chars=prompt_overhead_chars,
    ).recall(
        signals,
        strong_evidence_only=strong_evidence_only,
        strong_identity_enabled=strong_identity_enabled,
        preferred_parent_reasons=preferred_parent_reasons,
        candidate_scope_ids=candidate_scope_ids,
        candidate_scope_reason=candidate_scope_reason,
        candidate_remark_overrides=candidate_remark_overrides,
    )
