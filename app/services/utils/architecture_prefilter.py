"""Architecture classification pre-filter (per-category).

When the caller passes a large architecture tree (100+ nodes) via the
``architectureList`` request parameter, local small-scale LLMs struggle
to accurately select from so many candidates.

This module implements a **traditional text retrieval** strategy that
narrows the candidate list *before* the LLM sees it:

1. **Tokenize** the document content (file name + opening text).
2. **Group** leaf nodes by their root ancestor category.
3. **Score** every node by token overlap with the document,
   with extra weight when the node name appears verbatim in the text.
4. **Per-category pruning**: for each root category, look up its config
   entry:
   - If the category **has** a config entry and its leaf count exceeds
     ``prune_threshold``, keep only the top-``top_k`` scoring leaves and
     their ancestor chains.
   - If the category **has** a config entry but its leaf count is within
     the threshold, keep all its leaves intact.
   - If the category has **no** config entry (or value is ``None``),
     **keep** all its leaf nodes intact (no pruning).
5. **Always retain root-level nodes** as structural context and
   potential fallback choices.

If the pre-filter encounters any error or every category is already
small enough, it returns the original list untouched — making the
current pure-LLM approach the natural **degradation strategy**.

Usage::

    from app.services.utils.architecture_prefilter import prune_architecture_list

    narrowed = prune_architecture_list(full_list, document_text)
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Per-category pruning configuration.
#:
#: Keys are **root category names** (matching the ``name`` field of
#: root-level nodes whose ``parentId`` is ``None``).
#:
#: Values are dicts with two keys:
#:
#: * ``prune_threshold`` — if the category's leaf-node count is at or
#:   below this number, the category is left intact (no pruning).
#: * ``top_k`` — when pruning is triggered, how many top-scoring leaf
#:   nodes to retain for this category.
#:
#: Categories **not** present in this config (or whose value is
#: ``None``) are **never pruned** — all their leaves are kept.
DEFAULT_CATEGORY_CONFIG: Dict[str, Optional[Dict[str, int]]] = {
    "军事基地": {"prune_threshold": 4, "top_k": 5},
    "体系运用": {"prune_threshold": 4, "top_k": 5},
    "装备型号": {"prune_threshold": 4, "top_k": 5},
    "作战环境": {"prune_threshold": 4, "top_k": 5},
    "作战指挥": {"prune_threshold": 4, "top_k": 5},
    "数据标准": {"prune_threshold": 4, "top_k": 5},
}

#: Minimum number of meaningful document tokens required to run scoring.
#: If fewer tokens are extracted the pre-filter bails out early.
MIN_DOCUMENT_TOKENS = 2


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def prune_architecture_list(
    architecture_list: List[Dict[str, Any]],
    document_text: str,
    *,
    category_config: Dict[str, Optional[Dict[str, int]]] | None = None,
) -> List[Dict[str, Any]]:
    """Narrow *architecture_list* using traditional text retrieval.

    Parameters
    ----------
    architecture_list:
        Full list of architecture node dicts.  Each node is expected to
        contain at least ``id``, ``name``, ``parentId`` and optionally
        ``path``, ``pathName``, ``remark``.
    document_text:
        Raw document content used as the search query.  The more text
        the better — typically the file name concatenated with the
        first ~2000 characters of document body.
    category_config:
        Per-category pruning configuration.  Defaults to
        :data:`DEFAULT_CATEGORY_CONFIG`.  See the module-level docstring
        for the expected structure.

    Returns
    -------
    list[dict]
        A (possibly smaller) architecture list.  On any internal error
        the *original* list is returned unchanged.
    """
    if not architecture_list:
        return architecture_list

    if category_config is None:
        category_config = DEFAULT_CATEGORY_CONFIG

    try:
        return _do_prune(architecture_list, document_text, category_config)
    except Exception:  # pragma: no cover — safety net
        logger.warning("Architecture pre-filter failed, returning full list", exc_info=True)
        return architecture_list


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_CJK_RANGE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")


def _normalize(text: str) -> str:
    """NFKC-normalize, lower-case and collapse whitespace."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", str(text)).casefold()
    return re.sub(r"\s+", " ", text).strip()


def _tokenize(text: str) -> Set[str]:
    """Extract matching tokens from *text*.

    * CJK characters → individual characters (unigrams)
    * Latin/digit sequences → whole words or tokens
    """
    if not text:
        return set()
    normalized = _normalize(text)
    tokens: Set[str] = set()
    # CJK unigrams
    tokens.update(_CJK_RANGE.findall(normalized))
    # Latin/digit words (length >= 2)
    tokens.update(w for w in re.findall(r"[a-z0-9]{2,}", normalized))
    return tokens


def _coerce_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _node_feature_text(node: Dict[str, Any]) -> str:
    """Concatenate the descriptive fields of a node into one string."""
    parts: list[str] = []
    for field in ("name", "remark", "pathName"):
        value = node.get(field)
        if value and isinstance(value, str):
            parts.append(value)
    return " ".join(parts)


def _score_nodes(
    nodes: List[Dict[str, Any]],
    document_tokens: Set[str],
    normalized_doc: str,
) -> Dict[int, float]:
    """Score each node by token overlap with the document.

    Returns a mapping of ``node_id → score``.
    """
    scores: Dict[int, float] = {}
    for node in nodes:
        node_id = _coerce_int(node.get("id"))
        if node_id is None:
            continue

        node_text = _node_feature_text(node)
        node_tokens = _tokenize(node_text)
        if not node_tokens:
            continue

        overlap = len(set(node_tokens).intersection(document_tokens))
        if overlap == 0:
            continue

        # Jaccard-like similarity (lenient denominator)
        score = float(overlap) / max(len(node_tokens), 1)

        # Bonus: node name appears verbatim in document
        node_name = _normalize(node.get("name") or "")
        if node_name and len(node_name) >= 2 and node_name in normalized_doc:
            score += 2.0

        scores[node_id] = score

    return scores


def _collect_path_ids(node: Dict[str, Any]) -> List[int]:
    """Extract all ancestor IDs from the node's ``path`` field."""
    path_text = str(node.get("path") or "")
    return [int(m) for m in re.findall(r"\d+", path_text)]


def _find_root_id(
    node_id: int,
    nodes_by_id: Dict[int, Dict[str, Any]],
) -> int | None:
    """Walk up the parent chain from *node_id* to find its root ancestor ID."""
    current_id = node_id
    visited: Set[int] = {node_id}
    while True:
        node = nodes_by_id.get(current_id)
        if not node:
            return None
        parent_id = _coerce_int(node.get("parentId"))
        if parent_id is None:
            return current_id
        if parent_id in visited:
            return current_id  # cycle guard
        visited.add(parent_id)
        current_id = parent_id


def _do_prune(
    architecture_list: List[Dict[str, Any]],
    document_text: str,
    category_config: Dict[str, Optional[Dict[str, int]]],
) -> List[Dict[str, Any]]:
    """Core pruning logic with per-category thresholds."""
    normalized_doc = _normalize(document_text)
    document_tokens = _tokenize(document_text)

    if len(document_tokens) < MIN_DOCUMENT_TOKENS:
        logger.info(
            "Architecture pre-filter: insufficient document tokens (%d), keeping full list",
            len(document_tokens),
        )
        return architecture_list

    # ── Index ──
    nodes_by_id: Dict[int, Dict[str, Any]] = {}
    root_nodes: List[Dict[str, Any]] = []
    parent_ids: Set[int] = set()  # IDs that are referenced as parentId by other nodes

    for node in architecture_list:
        if not isinstance(node, dict):
            continue
        node_id = _coerce_int(node.get("id"))
        if node_id is None:
            continue
        nodes_by_id[node_id] = node
        parent_id = _coerce_int(node.get("parentId"))
        if parent_id is None:
            root_nodes.append(node)
        else:
            # Track which IDs are used as parentId (these are NOT leaves)
            parent_ids.add(parent_id)

    # Identify leaf nodes (not referenced as parentId by any other node)
    leaf_nodes: List[Dict[str, Any]] = []
    for node in architecture_list:
        if not isinstance(node, dict):
            continue
        node_id = _coerce_int(node.get("id"))
        if node_id is not None and node_id not in parent_ids:
            leaf_nodes.append(node)

    if not leaf_nodes:
        return architecture_list

    # ── Group leaves by root ancestor ──
    leaves_by_root: Dict[int, List[Dict[str, Any]]] = {}
    for leaf in leaf_nodes:
        leaf_id = _coerce_int(leaf.get("id"))
        if leaf_id is None:
            continue
        root_id = _find_root_id(leaf_id, nodes_by_id)
        if root_id is not None:
            leaves_by_root.setdefault(root_id, []).append(leaf)

    # ── Score all nodes ──
    scores = _score_nodes(architecture_list, document_tokens, normalized_doc)

    # ── Per-category pruning ──
    keep_ids: Set[int] = set()
    pruned_categories: List[str] = []

    for root in root_nodes:
        root_id = _coerce_int(root.get("id"))
        if root_id is None:
            continue

        root_name = root.get("name", "")
        root_leaves = leaves_by_root.get(root_id, [])
        config = category_config.get(root_name)

        # No config for this category → keep all leaves intact (no pruning)
        if config is None:
            for leaf in root_leaves:
                leaf_id = _coerce_int(leaf.get("id"))
                if leaf_id is not None:
                    keep_ids.add(leaf_id)
            continue

        threshold = config.get("prune_threshold", 4)
        top_k = config.get("top_k", 5)

        # Leaf count within threshold → no pruning for this category
        if len(root_leaves) <= threshold:
            for leaf in root_leaves:
                leaf_id = _coerce_int(leaf.get("id"))
                if leaf_id is not None:
                    keep_ids.add(leaf_id)
            continue

        # ── Prune: score, rank and select top-K leaves ──
        pruned_categories.append(root_name)
        scored = [
            (scores.get(_coerce_int(lf.get("id")) or 0, 0.0), lf)
            for lf in root_leaves
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)

        for _score, leaf in scored[:top_k]:
            leaf_id = _coerce_int(leaf.get("id"))
            if leaf_id is None:
                continue
            keep_ids.add(leaf_id)

            # Walk up the parent chain
            current_id = _coerce_int(leaf.get("parentId"))
            visited: Set[int] = {leaf_id}
            while current_id is not None and current_id not in visited:
                visited.add(current_id)
                keep_ids.add(current_id)
                parent_node = nodes_by_id.get(current_id)
                if parent_node is None:
                    break
                current_id = _coerce_int(parent_node.get("parentId"))

            # Also include IDs from the path field (handles incomplete chains)
            for path_id in _collect_path_ids(leaf):
                if path_id in nodes_by_id:
                    keep_ids.add(path_id)

    # If no category was pruned, return original list
    if not pruned_categories:
        logger.info(
            "Architecture pre-filter: all categories within thresholds, skipping (%d leaves)",
            len(leaf_nodes),
        )
        return architecture_list

    # Always keep root nodes — they are cheap and provide structural context
    for root in root_nodes:
        root_id = _coerce_int(root.get("id"))
        if root_id is not None:
            keep_ids.add(root_id)

    # ── Build result ──
    pruned = [n for n in architecture_list if _coerce_int(n.get("id")) in keep_ids]

    # Safety net: never return fewer than 3 nodes
    if len(pruned) < 3:
        logger.info(
            "Architecture pre-filter: too few results (%d), returning full list",
            len(pruned),
        )
        return architecture_list

    logger.info(
        "Architecture pre-filter: %d → %d nodes (pruned categories: %s)",
        len(architecture_list),
        len(pruned),
        ", ".join(pruned_categories),
    )

    return pruned
