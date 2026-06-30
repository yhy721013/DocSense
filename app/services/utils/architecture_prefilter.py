"""Architecture classification pre-filter.

When the caller passes a large architecture tree (100+ nodes) via the
``architectureList`` request parameter, local small-scale LLMs struggle
to accurately select from so many candidates.

This module implements a **traditional text retrieval** strategy that
narrows the candidate list *before* the LLM sees it:

1. **Tokenize** the document content (file name + opening text).
2. **Score** every architecture node by token overlap with the document,
   with extra weight when the node name appears verbatim in the text.
3. **Select** the top-K scoring leaf nodes and **preserve their full
   ancestor chain** so the LLM still sees valid parent/child
   relationships.
4. **Always retain root-level nodes** as structural context and
   potential fallback choices.

If the pre-filter encounters any error or the list is already small
enough, it returns the original list untouched — making the current
pure-LLM approach the natural **degradation strategy**.

Usage::

    from app.services.utils.architecture_prefilter import prune_architecture_list

    narrowed = prune_architecture_list(full_list, document_text)
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Dict, List, Set

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: Only trigger pre-filtering when the architecture list exceeds this size.
DEFAULT_PRUNE_THRESHOLD = 20

#: Maximum number of leaf candidates to retain after pruning.
DEFAULT_TOP_K = 15

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
    top_k: int = DEFAULT_TOP_K,
    prune_threshold: int = DEFAULT_PRUNE_THRESHOLD,
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
    top_k:
        How many top-scoring leaf nodes to keep.
    prune_threshold:
        If the list is already smaller than this, return it as-is.

    Returns
    -------
    list[dict]
        A (possibly smaller) architecture list.  On any internal error
        the *original* list is returned unchanged.
    """
    if not architecture_list or len(architecture_list) <= prune_threshold:
        return architecture_list

    try:
        return _do_prune(architecture_list, document_text, top_k)
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

        overlap = len(node_tokens & document_tokens)
        if overlap == 0:
            continue

        # Jaccard-like similarity (lenient denominator)
        score = overlap / max(len(node_tokens), 1)

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


def _do_prune(
    architecture_list: List[Dict[str, Any]],
    document_text: str,
    top_k: int,
) -> List[Dict[str, Any]]:
    """Core pruning logic (may raise on unexpected data shapes)."""
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
    leaf_nodes: List[Dict[str, Any]] = []
    root_nodes: List[Dict[str, Any]] = []
    child_ids: Set[int] = set()

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
            child_ids.add(node_id)

    # A node is a "leaf" if no other node references it as parentId
    for node in architecture_list:
        if not isinstance(node, dict):
            continue
        node_id = _coerce_int(node.get("id"))
        if node_id is not None and node_id not in child_ids:
            leaf_nodes.append(node)

    # If there are no identifiable leaves (unusual), bail out
    if not leaf_nodes:
        return architecture_list

    # ── Score ──
    scores = _score_nodes(architecture_list, document_tokens, normalized_doc)

    # Score leaf nodes and rank them
    scored_leaves = [
        (scores.get(_coerce_int(n.get("id")) or 0, 0.0), n)
        for n in leaf_nodes
    ]
    scored_leaves.sort(key=lambda pair: pair[0], reverse=True)

    # ── Select top-K leaves + ancestor chains ──
    keep_ids: Set[int] = set()

    for score, leaf in scored_leaves[:top_k]:
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
        "Architecture pre-filter: %d → %d nodes (top %d leaves from %d)",
        len(architecture_list),
        len(pruned),
        min(top_k, len(scored_leaves)),
        len(leaf_nodes),
    )

    return pruned
