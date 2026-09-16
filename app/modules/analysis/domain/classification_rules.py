"""文件分析有限候选分类、范围保护与身份重选纯规则。

本模块只处理调用方已提供的有限领域树、文档文本和模型 JSON；不得创建任务、执行模型、
写入日志或访问任何外部资源。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Mapping, Sequence

from .architecture_recall import (
    ArchitecturePromptBudgetError,
    DocumentArchitectureSignals,
    build_document_architecture_signals,
)
from .architecture_tree import ArchitectureNodeProfile, ArchitectureTreeIndex
from .errors import (
    AnalysisContractError,
    ArchitectureContractError,
    DataStandardParentContractError,
)
from .models import (
    ANALYSIS_CLASSIFICATION_MODES,
    ANALYSIS_DATA_STANDARD_MODES,
    ANALYSIS_DATA_STANDARD_MODE_LEGACY,
    ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
    ANALYSIS_FILENAME_CONSTRAINT_MODES,
    ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY,
    ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD,
    ANALYSIS_IDENTITY_RESELECT_MODES,
    ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE,
    MAX_ANALYSIS_PROMPT_CHARS,
)
from .prompts import data_standard_candidate_remark
from .ranges import build_effective_analysis_ranges
from .result_mapping import (
    _architecture_candidate_topology,
    _as_text,
    _coerce_int,
    _contains_gjb_standard_reference,
    _data_standard_candidate_ids,
    _extract_title,
    _first_non_empty_value,
    _general_data_standard_leaf_id,
    _is_data_standard_parent_id,
    _match_data_standard_architecture_id,
    _opening_identity_evidence_text,
    _opening_text,
    _ordered_data_standard_leaf_ids,
    _resolve_field,
)


def _normalize_analysis_prompt(prompt: str) -> str:
    """复用历史 RAG Prompt 的换行与首尾空白规范，保持 Domain 无外层 Port 依赖。"""

    if not isinstance(prompt, str):
        raise TypeError("prompt 必须是 str")
    normalized = prompt.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ValueError("prompt 不能为空")
    return normalized


def _reject_nonstandard_json_constant(value: str) -> None:
    """拒绝 Python JSON 解码器默认接受的 NaN 与 Infinity 扩展值。"""
    raise ValueError(f"非法 JSON 常量: {value}")


def _parse_strict_json_object(raw_result: Any) -> Dict[str, Any] | None:
    """只接受原生对象或严格 JSON 对象，不执行猜括号等有损修补。

    旧实现会从任意文本中截取最外层花括号并反复补 ``}``。这种做法可能把截断回答误判
    为有效业务数据。阶段 9 改为显式发起一次 ``JSON_REPAIR`` 模型调用，因此本地解析器
    必须保持确定性：语法不合法就返回 ``None``，由编排层决定是否修复；合法的空对象
    则继续进入宽松字段映射和独立的 architectureId 契约处理，不能伪装成语法问题。
    """
    if isinstance(raw_result, dict):
        try:
            serialized = json.dumps(
                raw_result,
                ensure_ascii=False,
                allow_nan=False,
            )
            parsed_object = json.loads(serialized)
        except (TypeError, ValueError):
            return None
        return parsed_object if isinstance(parsed_object, dict) else None
    if not isinstance(raw_result, str) or not raw_result.strip():
        return None
    try:
        parsed = json.loads(
            raw_result.strip(),
            parse_constant=_reject_nonstandard_json_constant,
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _architecture_candidates(
        request_params: Dict[str, Any],
) -> tuple[list[Dict[str, Any]], set[int]]:
    """返回请求中的有效候选及其全部 ID。

    当请求只携带一个节点时，无论它在完整体系中是否还有子节点，都按阶段 9 契约直接
    使用该唯一 ID。多候选时允许模型选择请求中任意候选节点，不再由服务端强制要求叶子。
    """
    items: list[Dict[str, Any]] = []
    ids: set[int] = set()
    for item in build_effective_analysis_ranges(request_params)["architectureList"]:
        if not isinstance(item, dict):
            continue
        item_id = _coerce_int(item.get("id"))
        if item_id is None or item_id < 1:
            continue
        if item_id in ids:
            raise AnalysisContractError(f"architectureList 包含重复 id: {item_id}")
        ids.add(item_id)
        items.append(item)
    if not items:
        raise AnalysisContractError("architectureList 不包含有效候选")
    return items, ids


def _validate_data_standard_leaf_requirement(
        architecture_id: int,
        candidates: Iterable[Dict[str, Any]],
) -> None:
    """阻止数据标准父节点进入成功回调与永久知识库。

    普通分类父节点仍是合法业务结果，不能复用旧版“所有候选必须叶子”的全局校验。只有
    数据标准分支中、且在本次候选树内明确拥有子节点的父节点会触发此特殊规则。
    """
    if _is_data_standard_parent_id(architecture_id, candidates):
        raise DataStandardParentContractError(
            "architectureId 是数据标准父节点，必须兜底到其下叶子节点"
        )


def _resolve_analysis_architecture_id(
        parsed_result: Dict[str, Any],
        request_params: Dict[str, Any],
) -> int:
    """按单候选直返、多候选显式 ID 的规则解析 architectureId。"""
    candidates, allowed_ids = _architecture_candidates(request_params)
    if len(allowed_ids) == 1:
        architecture_id = next(iter(allowed_ids))
    else:
        if "architectureId" not in parsed_result or parsed_result.get("architectureId") in (None, ""):
            raise ArchitectureContractError("architectureId 缺失")
        raw_id = parsed_result.get("architectureId")
        if isinstance(raw_id, bool) or not isinstance(raw_id, int):
            raise ArchitectureContractError("architectureId 必须是数字 ID")
        if raw_id not in allowed_ids:
            raise ArchitectureContractError("architectureId 不属于请求 architectureList 候选")
        architecture_id = raw_id

    _validate_data_standard_leaf_requirement(architecture_id, candidates)
    return architecture_id


def _match_gjb_architecture_candidate(
        parsed_result: Dict[str, Any],
        request_params: Dict[str, Any],
        original_text: str,
        candidates: Iterable[Dict[str, Any]],
) -> int | None:
    """首次分类不合规时，按 GJB 线索匹配请求中的数据标准叶子节点。"""
    file_item = parsed_result.get("fileDataItem")
    if not isinstance(file_item, dict):
        file_item = {}
    return _match_data_standard_architecture_id(
        candidates,
        original_text,
        request_params.get("originalFileName"),
        _resolve_field(parsed_result, file_item, "summary", "摘要"),
        _first_non_empty_value(file_item, "keyword", "keywords", "关键词"),
        _resolve_field(parsed_result, file_item, "documentOverview", "文件概述", "概述"),
    )


def _validate_architecture_repair_result(
        raw_result: Any,
        request_params: Dict[str, Any],
) -> int:
    """要求分类修复只能返回一个 architectureId 键并再次执行候选校验。"""
    repaired = _parse_strict_json_object(raw_result)
    if repaired is None:
        raise ArchitectureContractError("architecture 修复结果不是严格 JSON 对象")
    if set(repaired) != {"architectureId"}:
        raise ArchitectureContractError("architecture 修复结果只能包含 architectureId")
    return _resolve_analysis_architecture_id(repaired, request_params)


def _normalize_analysis_classification_mode(value: Any) -> str:
    mode = _as_text(value) or "topk_two_stage"
    if mode not in ANALYSIS_CLASSIFICATION_MODES:
        raise ValueError(
            "analysis_classification_mode 必须是 "
            "topk_two_stage、topk_single 或 legacy"
        )
    return mode


def _normalize_analysis_filename_constraint_mode(value: Any) -> str:
    mode = _as_text(value) or ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY
    if mode not in ANALYSIS_FILENAME_CONSTRAINT_MODES:
        raise ValueError(
            "analysis_filename_constraint_mode 必须是 legacy 或 scope_guard"
        )
    return mode


def _normalize_analysis_data_standard_mode(value: Any) -> str:
    mode = _as_text(value) or ANALYSIS_DATA_STANDARD_MODE_LEGACY
    if mode not in ANALYSIS_DATA_STANDARD_MODES:
        raise ValueError(
            "analysis_data_standard_mode 必须是 legacy 或 scope_guard"
        )
    return mode


def _normalize_analysis_identity_reselect_mode(value: Any) -> str:
    mode = _as_text(value) or ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE
    if mode not in ANALYSIS_IDENTITY_RESELECT_MODES:
        raise ValueError(
            "analysis_identity_reselect_mode 必须是 off、shadow 或 enforce"
        )
    return mode


def _extract_recall_headings(original_text: str) -> tuple[str, ...]:
    """从正文提取最多 64 条短标题信号，不把长正文行重复塞入召回查询。"""
    headings: list[str] = []
    heading_pattern = re.compile(
        r"^(?:#{1,6}\s+|第[一二三四五六七八九十百0-9]+[章节篇部]\s*|"
        r"(?:\d{1,3}|[一二三四五六七八九十]+)(?:[.、．]\d{0,3})*[.、．)）]?\s*)"
    )
    for raw_line in original_text.splitlines():
        line = raw_line.strip()
        if not line or len(line) > 160:
            continue
        if heading_pattern.match(line) or (
                len(line) <= 80
                and line.endswith(("章", "节", "概述", "简介", "说明", "要求", "范围"))
        ):
            if line not in headings:
                headings.append(line)
        if len(headings) >= 64:
            break
    return tuple(headings)


def _build_analysis_architecture_signals(
    *,
    file_name: str,
    original_name: str,
    original_text: str,
    title_override: str = "",
) -> DocumentArchitectureSignals:
    return build_document_architecture_signals(
        filename=file_name,
        original_filename=original_name,
        title=title_override or _extract_title(original_text),
        headings=_extract_recall_headings(original_text),
        body=original_text,
    )


def _data_standard_candidate_scope(
    *,
    tree_index: ArchitectureTreeIndex,
    architecture_list: Iterable[Dict[str, Any]],
) -> tuple[tuple[int, ...], dict[int, str]]:
    scope_ids = tuple(_ordered_data_standard_leaf_ids(architecture_list))
    remark_overrides = {
        node_id: remark
        for node_id in scope_ids
        for remark in (
            data_standard_candidate_remark(
                tree_index.require(node_id).semantic_path
            ),
        )
        if remark
    }
    return scope_ids, remark_overrides


def _architecture_signal_digest(signals: DocumentArchitectureSignals) -> str:
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


def _node_prompt_projection(node: ArchitectureNodeProfile) -> Dict[str, Any]:
    projected: Dict[str, Any] = {
        "id": node.id,
        "pathName": node.semantic_path,
        "nodeType": "leaf" if node.is_leaf else "parent",
    }
    if node.remark:
        projected["remark"] = node.remark[:512]
    return projected


def _normalize_bounded_analysis_prompt(prompt: str) -> str:
    normalized = _normalize_analysis_prompt(prompt)
    if len(normalized) > MAX_ANALYSIS_PROMPT_CHARS:
        raise ArchitecturePromptBudgetError(
            f"模型 Prompt 共 {len(normalized)} 字符，超过 "
            f"{MAX_ANALYSIS_PROMPT_CHARS} 字符上限"
        )
    return normalized


def _validate_topk_architecture_id(
        raw_id: Any,
        *,
        visible_ids: set[int],
        tree_index: ArchitectureTreeIndex,
        architecture_list: Iterable[Dict[str, Any]],
) -> int:
    if isinstance(raw_id, bool) or not isinstance(raw_id, int):
        raise ArchitectureContractError("architectureId 必须是数字 ID")
    if raw_id not in visible_ids or raw_id not in tree_index.nodes_by_id:
        raise ArchitectureContractError("architectureId 不属于模型可见候选")
    node = tree_index.require(raw_id)
    if node.parent_id is None:
        raise ArchitectureContractError("领域树根节点不能作为最终分类")
    if not node.is_leaf:
        if _is_data_standard_parent_id(raw_id, architecture_list):
            raise DataStandardParentContractError(
                "architectureId 是数据标准父节点，必须兜底到其下叶子节点"
            )
    return raw_id


def _parse_topk_classification_result(
        raw_result: Any,
        *,
        visible_ids: set[int],
        tree_index: ArchitectureTreeIndex,
        architecture_list: Iterable[Dict[str, Any]],
) -> tuple[Dict[str, Any], int]:
    parsed = _parse_strict_json_object(raw_result)
    if parsed is None:
        raise ArchitectureContractError("分类结果不是严格 JSON 对象")
    if set(parsed) != {"architectureId"}:
        raise ArchitectureContractError("分类结果只能包含 architectureId")
    if parsed.get("architectureId") is None:
        raise ArchitectureContractError("architectureId 为 null，证据不足")
    return parsed, _validate_topk_architecture_id(
        parsed.get("architectureId"),
        visible_ids=visible_ids,
        tree_index=tree_index,
        architecture_list=architecture_list,
    )


def _visible_data_standard_fallback_id(
        *,
        visible_ids: set[int],
        architecture_list: Iterable[Dict[str, Any]],
        force: bool,
        context_values: Iterable[Any],
) -> int | None:
    if not force and not _contains_gjb_standard_reference(*context_values):
        return None
    node_id = _general_data_standard_leaf_id(architecture_list)
    return node_id if node_id in visible_ids else None


_STRONG_FILENAME_IDENTIFIER_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"([A-Za-z]{1,12}(?:[/_]+[A-Za-z]{1,12})?[/_\-\s]*"
    r"\d{1,8}[A-Za-z]?)"
    r"(?![A-Za-z0-9])"
)
_FILENAME_DATE_IDENTIFIER_PREFIXES = frozenset(
    {
        "jan", "january", "feb", "february", "mar", "march",
        "apr", "april", "may", "jun", "june", "jul", "july",
        "aug", "august", "sep", "sept", "september", "oct", "october",
        "nov", "november", "dec", "december",
    }
)
_EQUIPMENT_DETAIL_KINDS = frozenset(
    (
        "基础数据",
        "战技指标",
        "运用数据",
        "效能数据",
        "模型数据",
        "目特数据",
        "声像数据",
    )
)
_ORDERED_EQUIPMENT_DETAIL_KINDS = (
    "基础数据",
    "战技指标",
    "运用数据",
    "效能数据",
    "模型数据",
    "目特数据",
    "声像数据",
)
_STRONG_GJB_FILENAME_RE = re.compile(
    r"(?<![A-Za-z0-9])GJB(?:\s*[/_-]\s*Z)?\s*[- ]?\s*\d+[A-Za-z]?",
    re.IGNORECASE,
)
_GJB_STANDARD_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?P<prefix>GJB(?:\s*[/_-]?\s*Z)?)"
    r"\s*[- ]?\s*(?P<number>\d{1,8}[A-Za-z]?)"
    r"(?:\s*-\s*(?P<year>\d{4}))?",
    re.IGNORECASE,
)
_STANDARD_COMMENTARY_TITLE_RE = re.compile(
    r"(?:标准解读|标准释义|宣贯材料|培训材料|实施说明|编制说明|"
    r"审核报告|检测报告|检验报告|符合性(?:评价|报告))"
)
_STANDARD_STRUCTURE_MARKERS = (
    "范围",
    "规范性引用文件",
    "术语和定义",
    "术语与定义",
)
_JANE_COPYRIGHT_RE = re.compile(
    r"©\s*\d{4}\s+Jane[’']s\s+Group\s+UK\s+Limited",
    re.IGNORECASE,
)
_JANE_PAGE_ONE_RE = re.compile(
    r"(?im)^\s*Page\s+1\s+of\s+(?P<total_pages>\d+)\s*$",
)
_JANE_METADATA_RE = re.compile(
    r"(?im)^\s*(?:Date\s+Posted|Publication)\s*:",
)
_SCOPE_QUALIFIER_RE = re.compile(
    r"\b(?P<kind>Flight|Block|Batch)\s*"
    r"(?P<value>(?:"
    r"[0-9]+[A-Z]?(?:\s*(?:[/_-]\s*)?[IVXLCDM]+[A-Z]?)?"
    r"|[IVXLCDM]+[A-Z]?(?:\s*[/_-]\s*[IVXLCDM]+[A-Z]?)?"
    r"))(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_JANE_CLASS_RE = re.compile(r"(?<![A-Za-z])class(?![A-Za-z])", re.IGNORECASE)
_JANE_AIRCRAFT_TOTALS_RE = re.compile(
    r"(?im)^\s*Aircraft\s+totals\s*$",
)
_JANE_CATALOG_FILENAME_RE = re.compile(
    r"^(?:jfs|jaem|jawa|jumv)(?=[a-z0-9_-]*\d)[a-z0-9_-]+$",
    re.IGNORECASE,
)
_OPAQUE_IDENTITY_FILENAME_RE = re.compile(
    r"^(?:"
    r"[0-9a-f]{8,64}"
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|(?:upload|hash|temporary|temp)[_-][a-z0-9_-]{6,}"
    r"|technical[_-]upload(?:[_-][a-z0-9_-]+)?"
    r")$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class _DataStandardClassificationProfile:
    active: bool = False
    standard_number: str = ""
    title: str = ""
    document_kind: str = "unknown"
    filename_identifiers: tuple[str, ...] = ()
    cover_identifiers: tuple[str, ...] = ()
    evidence_sources: tuple[str, ...] = ()
    identity_confirmed: bool = False
    identity_conflict: bool = False


def _normalized_gjb_source_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", _as_text(value))
    return re.sub(r"[\-‐‑‒–—―－﹣]+", "-", normalized)


def _extract_gjb_standard_identifiers(
    *values: Any,
) -> tuple[tuple[str, str], ...]:
    """返回 ``(identity_key, display_number)``，用于文件名与首页双源核验。"""

    result: list[tuple[str, str]] = []
    for value in values:
        normalized = _normalized_gjb_source_text(value)
        for match in _GJB_STANDARD_NUMBER_RE.finditer(normalized):
            prefix = re.sub(
                r"[\s/_-]+",
                "",
                match.group("prefix"),
            ).upper()
            number = match.group("number").upper()
            year = match.group("year") or ""
            identity_key = f"{prefix}{number}{year}"
            display_prefix = "GJB/Z" if prefix == "GJBZ" else "GJB"
            display = f"{display_prefix} {number}"
            if year:
                display += f"-{year}"
            item = (identity_key, display)
            if item not in result:
                result.append(item)
    return tuple(result)


def _extract_data_standard_title(original_text: str) -> str:
    """从标准封面前部提取正式标题，不改动其他文档共用的通用标题解析。"""

    opening = _opening_text(original_text, max_chars=8_000, max_lines=160)
    if not opening:
        return ""
    for raw_line in opening.splitlines():
        normalized_line = _normalized_gjb_source_text(raw_line)
        line_without_number = _GJB_STANDARD_NUMBER_RE.sub(
            "",
            normalized_line,
        )
        candidate = re.sub(r"\s+", " ", line_without_number).strip(" -")
        if not candidate or not re.search(r"[\u4e00-\u9fff]", candidate):
            continue
        if candidate.startswith("FL ") or candidate.startswith("代替"):
            continue
        if candidate in {"中华人民共和国国家军用标准", "国家军用标准", "国军标"}:
            continue
        if any(
            marker in candidate
            for marker in (
                "发布",
                "实施",
                "颁布",
                "批准",
                "目次",
                "目录",
            )
        ):
            continue
        if re.match(r"^(?:第?[一二三四五六七八九十百0-9]+[章节篇部.]|\d+\s)", candidate):
            continue
        if len(candidate) <= 120:
            return candidate[:120]
    return ""


def _build_data_standard_classification_profile(
    *,
    file_name: str,
    original_name: str,
    original_text: str,
) -> _DataStandardClassificationProfile:
    identity_filename = _as_text(original_name) or _as_text(file_name)
    filename_items = _extract_gjb_standard_identifiers(identity_filename)
    opening = _opening_text(original_text, max_chars=8_000, max_lines=160)
    cover_items = _extract_gjb_standard_identifiers(opening)
    filename_keys = {item[0] for item in filename_items}
    cover_keys = {item[0] for item in cover_items}
    shared_keys = filename_keys & cover_keys
    identity_conflict = bool(
        filename_keys
        and cover_keys
        and not shared_keys
    )
    title = _extract_data_standard_title(original_text)
    header_present = (
        "国家军用标准" in opening
        or "国军标" in opening
    )
    structure_markers = tuple(
        marker
        for marker in _STANDARD_STRUCTURE_MARKERS
        if marker in opening
    )
    commentary = bool(
        _STANDARD_COMMENTARY_TITLE_RE.search(
            "\n".join((title, opening[:1_000]))
        )
    )
    # 同号可能只是正文引用，任意中文首行也不能充当标准封面证据。宁可保持旧链路，
    # 也只在军用标准页眉或至少两个标准结构章节存在时启用受限候选。
    cover_evidence = bool(
        header_present
        or len(structure_markers) >= 2
    )
    identity_confirmed = bool(
        not identity_conflict
        and not commentary
        and (
            (shared_keys and cover_evidence)
            or (
                cover_keys
                and header_present
                and (title or len(structure_markers) >= 2)
            )
        )
    )
    evidence_sources: list[str] = []
    if filename_items:
        evidence_sources.append("originalFileName")
    if cover_items:
        evidence_sources.append("coverIdentifier")
    if header_present:
        evidence_sources.append("coverStandardHeader")
    if title:
        evidence_sources.append("coverTitle")
    if len(structure_markers) >= 2:
        evidence_sources.append("standardStructure")

    display_number = ""
    for key, display in cover_items:
        if not shared_keys or key in shared_keys:
            display_number = display
            break
    if not display_number and filename_items:
        display_number = filename_items[0][1]
    if commentary:
        document_kind = "commentary"
    elif identity_confirmed:
        document_kind = "standard_body"
    elif filename_items or cover_items:
        document_kind = "reference_only"
    else:
        document_kind = "unknown"
    return _DataStandardClassificationProfile(
        active=bool(filename_items or cover_items),
        standard_number=display_number,
        title=title,
        document_kind=document_kind,
        filename_identifiers=tuple(item[1] for item in filename_items),
        cover_identifiers=tuple(item[1] for item in cover_items),
        evidence_sources=tuple(evidence_sources),
        identity_confirmed=identity_confirmed,
        identity_conflict=identity_conflict,
    )


def _data_standard_prompt_context(
    profile: _DataStandardClassificationProfile,
) -> dict[str, Any]:
    return {
        "standardNumber": profile.standard_number,
        "standardTitle": profile.title,
        "documentKind": profile.document_kind,
        "evidenceSources": list(profile.evidence_sources),
    }


@dataclass(frozen=True, slots=True)
class _JaneClassificationProfile:
    active: bool = False
    title: str = ""
    identity_filename: str = ""
    filename_identity_kind: str = "absent"
    filename_identifiers: tuple[str, ...] = ()
    trusted_filename_identifiers: tuple[str, ...] = ()
    title_identifiers: tuple[str, ...] = ()
    primary_identifier: str = ""
    qualifier: str = ""
    scope_kind: str = ""
    high_level_branch_hint: str = ""
    dominant_detail_kind: str = ""
    recall_identity_enabled: bool = False
    identity_confirmed: bool = False
    identity_conflict: bool = False


@dataclass(frozen=True, slots=True)
class _EquipmentIdentityReselectProfile:
    """候选召回之外建立的普通装备双证据身份快照。"""

    active: bool = False
    filename_identity_kind: str = "absent"
    filename_identifiers: tuple[str, ...] = ()
    opening_identifiers: tuple[str, ...] = ()
    shared_identifiers: tuple[str, ...] = ()
    conflicting_identifiers: tuple[str, ...] = ()
    identifier: str = ""
    target_parent_id: int | None = None
    target_parent_path: str = ""
    candidate_ids: tuple[int, ...] = ()
    evidence_sources: tuple[str, ...] = ()
    reason_code: str = "no_explicit_original_filename"


@dataclass(frozen=True, slots=True)
class _IdentityReselectGateDecision:
    should_reselect: bool = False
    relation: str = "not_evaluated"
    reason_code: str = "profile_inactive"


def _jane_recall_filename_signals(
    *,
    file_name: str,
    original_name: str,
    profile: _JaneClassificationProfile,
    scope_guard_active: bool,
) -> tuple[str, str]:
    """返回可进入召回的业务文件名和原文件名。"""

    if not scope_guard_active:
        return file_name, original_name
    if profile.filename_identity_kind in {"catalog", "opaque"}:
        return "", ""
    if original_name:
        return "", original_name
    return file_name, ""


@dataclass(frozen=True, slots=True)
class _ArchitectureScopeResolution:
    matched_scope_parent_id: int | None = None
    matched_branch_parent_id: int | None = None
    clustered_parent_ids: tuple[int, ...] = ()
    protected_parent_reasons: tuple[tuple[int, tuple[str, ...]], ...] = ()
    reason_code: str = "no_constraint_insufficient_evidence"
    tree_gap: bool = False

    @property
    def preferred_parent_reasons(self) -> dict[int, tuple[str, ...]]:
        return dict(self.protected_parent_reasons)


@dataclass(frozen=True, slots=True)
class _ArchitectureConstraintDecision:
    pre_architecture_id: int
    post_architecture_id: int
    reason_code: str
    matched_scope_parent_id: int | None
    tree_gap: bool


def _ordered_strong_identifiers(
    *values: Any,
    limit: int = 128,
) -> tuple[str, ...]:
    """按文本出现顺序提取数字型标识，过滤日期和版式限定词。"""

    identifiers: list[str] = []
    for value in values:
        normalized = unicodedata.normalize("NFKC", _as_text(value)).casefold()
        for match in _STRONG_FILENAME_IDENTIFIER_RE.finditer(normalized):
            raw_identifier = match.group(1).strip()
            compact = re.sub(r"[^a-z0-9]+", "", raw_identifier)
            letters = re.sub(r"[^a-z]+", "", compact)
            digits = re.sub(r"[^0-9]+", "", compact)
            if not letters or not digits:
                continue
            # 单字母紧凑写法（如正文序号 B2）过于宽泛；带显式分隔符的 F-35 仍保留。
            if len(letters) == 1 and re.fullmatch(r"[a-z]\d+", raw_identifier):
                continue
            if letters in _FILENAME_DATE_IDENTIFIER_PREFIXES:
                continue
            if letters in {"flight", "block", "batch", "page", "figure"}:
                continue
            if compact not in identifiers:
                identifiers.append(compact)
            if len(identifiers) >= limit:
                return tuple(identifiers)
    return tuple(identifiers)


def _strong_filename_identifiers(*values: Any) -> set[str]:
    """提取文件名中的数字型强标识，忽略正文、短词和纯年份。"""

    return set(_ordered_strong_identifiers(*values))


def _jane_filename_identity_kind(value: Any) -> str:
    """区分描述性文件名与不能代表装备身份的出版/上传技术名。"""

    normalized = unicodedata.normalize("NFKC", _as_text(value)).casefold()
    basename = re.split(r"[/\\]", normalized)[-1].strip()
    if not basename:
        return "absent"
    stem = re.sub(r"\.[a-z0-9]{1,12}$", "", basename).strip()
    if not stem:
        return "absent"
    if _JANE_CATALOG_FILENAME_RE.fullmatch(stem):
        return "catalog"
    if _OPAQUE_IDENTITY_FILENAME_RE.fullmatch(stem):
        return "opaque"
    return "descriptive"


def _identifier_prefix(identifier: str) -> str:
    match = re.match(r"[a-z]+", identifier.casefold())
    return match.group(0) if match else ""


def _extract_scope_qualifier(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", _as_text(value))
    match = _SCOPE_QUALIFIER_RE.search(normalized)
    if not match:
        return ""
    kind = match.group("kind").casefold()
    kind_label = {
        "flight": "Flight",
        "block": "Block",
        "batch": "Batch",
    }[kind]
    qualifier_value = re.sub(
        r"\s+",
        " ",
        match.group("value").upper().strip(),
    )
    return f"{kind_label} {qualifier_value}"


def _normalize_scope_qualifier(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", _as_text(value)).casefold()
    return re.sub(r"[^a-z0-9]+", "", normalized)


def _extract_jane_title(original_text: str) -> tuple[bool, str]:
    """只在可靠 Jane's 首页版式中抽取版权声明之后的真实标题。"""

    if not original_text or not _JANE_COPYRIGHT_RE.search(original_text):
        return False, ""
    page_match = _JANE_PAGE_ONE_RE.search(original_text)
    if page_match is None:
        return False, ""
    metadata_match = _JANE_METADATA_RE.search(original_text, page_match.end())
    if metadata_match is None:
        return False, ""
    title_lines = [
        line.strip()
        for line in original_text[page_match.end():metadata_match.start()].splitlines()
        if line.strip()
    ]
    title = re.sub(r"\s+", " ", " ".join(title_lines)).strip()[:512]
    return bool(title), title


def _jane_dominant_detail_kind(original_text: str) -> str:
    """识别整份简氏资料是否由单一明细章节主导，避免把普通目录章节误当全文作用域。"""

    page_match = _JANE_PAGE_ONE_RE.search(original_text)
    if page_match is None:
        return ""
    try:
        total_pages = int(page_match.group("total_pages"))
    except (TypeError, ValueError):
        return ""
    # 只对极短资料做这一强判断。长篇综合资料即使包含 Specifications，也只能
    # 由分类模型结合全文决定，不能把普通目录章节提升为全文作用域。
    if total_pages > 3:
        return ""
    metadata_match = _JANE_METADATA_RE.search(original_text, page_match.end())
    if metadata_match is None:
        return ""
    first_page_end = re.search(
        r"(?im)^\s*Page\s+2\s+of\s+\d+\s*$",
        original_text[metadata_match.end():],
    )
    first_page_body = original_text[
        metadata_match.end():
        (
            metadata_match.end() + first_page_end.start()
            if first_page_end is not None
            else len(original_text)
        )
    ]
    lines = [
        line.strip().casefold()
        for line in first_page_body.splitlines()
        if line.strip()
    ]
    try:
        specifications_index = lines.index("specifications")
    except ValueError:
        return ""
    if "contents" in lines[:specifications_index]:
        return ""
    return "technical_specifications"


def _build_jane_classification_profile(
    *,
    file_name: str,
    original_name: str,
    original_text: str,
) -> _JaneClassificationProfile:
    active, title = _extract_jane_title(original_text)
    identity_filename = _as_text(original_name) or _as_text(file_name)
    filename_identity_kind = _jane_filename_identity_kind(identity_filename)
    if not active:
        return _JaneClassificationProfile(
            identity_filename=identity_filename,
            filename_identity_kind=filename_identity_kind,
        )

    filename_identifiers = _ordered_strong_identifiers(identity_filename)
    trusted_filename_identifiers = (
        filename_identifiers
        if filename_identity_kind == "descriptive"
        else ()
    )
    title_identifiers = _ordered_strong_identifiers(title)
    shared_identifiers = [
        identifier
        for identifier in title_identifiers
        if identifier in set(trusted_filename_identifiers)
    ]
    filename_qualifier = (
        _extract_scope_qualifier(identity_filename)
        if filename_identity_kind == "descriptive"
        else ""
    )
    title_qualifier = _extract_scope_qualifier(title)
    identifier_conflict = bool(
        trusted_filename_identifiers
        and title_identifiers
        and not shared_identifiers
    )
    qualifier_conflict = bool(
        filename_qualifier
        and title_qualifier
        and _normalize_scope_qualifier(filename_qualifier)
        != _normalize_scope_qualifier(title_qualifier)
    )
    identity_conflict = identifier_conflict or qualifier_conflict
    if shared_identifiers:
        primary_identifier = shared_identifiers[0]
    elif title_identifiers and not trusted_filename_identifiers:
        # 可靠 Jane's 首页标题可以保护召回候选；没有可信文件名独立佐证时，
        # 仍不得据此开启最终确定性覆盖。
        primary_identifier = title_identifiers[0]
    else:
        primary_identifier = ""
    qualifier = title_qualifier or filename_qualifier
    qualifier_kind = qualifier.partition(" ")[0].casefold()
    if qualifier_kind == "flight":
        scope_kind = "flight"
    elif qualifier_kind in {"block", "batch"}:
        scope_kind = "block"
    elif _JANE_CLASS_RE.search(title):
        scope_kind = "class"
    else:
        scope_kind = "single_model"
    high_level_branch_hint = (
        "air_equipment"
        if _JANE_AIRCRAFT_TOTALS_RE.search(_opening_text(original_text))
        else ""
    )
    dominant_detail_kind = (
        _jane_dominant_detail_kind(original_text)
        if scope_kind == "single_model"
        else ""
    )
    return _JaneClassificationProfile(
        active=True,
        title=title,
        identity_filename=identity_filename,
        filename_identity_kind=filename_identity_kind,
        filename_identifiers=filename_identifiers,
        trusted_filename_identifiers=trusted_filename_identifiers,
        title_identifiers=title_identifiers,
        primary_identifier=primary_identifier,
        qualifier=qualifier,
        scope_kind=scope_kind,
        high_level_branch_hint=high_level_branch_hint,
        dominant_detail_kind=dominant_detail_kind,
        recall_identity_enabled=bool(primary_identifier) and not identity_conflict,
        identity_confirmed=bool(shared_identifiers) and not identity_conflict,
        identity_conflict=identity_conflict,
    )


def _has_strong_gjb_filename_identity(*values: Any) -> bool:
    for value in values:
        normalized = unicodedata.normalize("NFKC", _as_text(value))
        if not normalized:
            continue
        if "国家军用标准" in normalized or "国军标" in normalized:
            return True
        if _STRONG_GJB_FILENAME_RE.search(normalized):
            return True
    return False


def _equipment_detail_kind(node_name: str, parent_name: str) -> str:
    if node_name in _EQUIPMENT_DETAIL_KINDS:
        return node_name
    for separator in ("-", "－", "—", "–", "﹣"):
        prefix = f"{parent_name}{separator}"
        if node_name.startswith(prefix):
            return node_name[len(prefix):].strip()
    return ""


def _has_seven_equipment_detail_leaves(
    node: ArchitectureNodeProfile,
    tree_index: ArchitectureTreeIndex,
) -> bool:
    detail_kinds = {
        _equipment_detail_kind(tree_index.require(child_id).name, node.name)
        for child_id in tree_index.children_by_id[node.id]
        if tree_index.require(child_id).is_leaf
    }
    return _EQUIPMENT_DETAIL_KINDS.issubset(detail_kinds)


def _equipment_entities_by_identifier(
    tree_index: ArchitectureTreeIndex,
) -> dict[str, tuple[int, ...]]:
    values: dict[str, list[int]] = {}
    for node in tree_index.nodes:
        if node.is_leaf or not _has_seven_equipment_detail_leaves(node, tree_index):
            continue
        for identifier in _ordered_strong_identifiers(node.name, limit=8):
            values.setdefault(identifier, []).append(node.id)
    return {
        identifier: tuple(dict.fromkeys(node_ids))
        for identifier, node_ids in values.items()
    }


def _ordered_equipment_family_scope_ids(
    parent_id: int,
    tree_index: ArchitectureTreeIndex,
) -> tuple[int, ...]:
    """返回七类明细叶与父节点；拓扑不唯一时拒绝建立受限分支。"""

    parent = tree_index.get(parent_id)
    if parent is None or parent.is_leaf:
        return ()
    children_by_kind: dict[str, list[int]] = {
        kind: [] for kind in _ORDERED_EQUIPMENT_DETAIL_KINDS
    }
    for child_id in tree_index.children_by_id[parent.id]:
        child = tree_index.require(child_id)
        if not child.is_leaf:
            continue
        detail_kind = _equipment_detail_kind(child.name, parent.name)
        if detail_kind in children_by_kind:
            children_by_kind[detail_kind].append(child.id)
    if any(len(children_by_kind[kind]) != 1 for kind in children_by_kind):
        return ()
    return tuple(
        children_by_kind[kind][0]
        for kind in _ORDERED_EQUIPMENT_DETAIL_KINDS
    ) + (parent.id,)


def _build_equipment_identity_reselect_profile(
    *,
    requested_original_name: str,
    original_text: str,
    tree_index: ArchitectureTreeIndex,
    visible_ids: set[int],
    jane_active: bool,
    data_standard_active: bool,
) -> _EquipmentIdentityReselectProfile:
    """用显式原文件名和 opening 身份区确认普通装备的唯一树分支。"""

    original_name = _as_text(requested_original_name)
    filename_identity_kind = _jane_filename_identity_kind(original_name)
    if not original_name:
        return _EquipmentIdentityReselectProfile()
    if jane_active:
        return _EquipmentIdentityReselectProfile(
            filename_identity_kind=filename_identity_kind,
            reason_code="jane_scope_owned",
        )
    if data_standard_active:
        return _EquipmentIdentityReselectProfile(
            filename_identity_kind=filename_identity_kind,
            reason_code="data_standard_scope_owned",
        )
    if filename_identity_kind != "descriptive":
        return _EquipmentIdentityReselectProfile(
            filename_identity_kind=filename_identity_kind,
            reason_code="filename_not_descriptive",
        )

    entities_by_identifier = _equipment_entities_by_identifier(tree_index)
    filename_identifiers = tuple(
        identifier
        for identifier in _ordered_strong_identifiers(original_name)
        if entities_by_identifier.get(identifier)
    )
    if not filename_identifiers:
        return _EquipmentIdentityReselectProfile(
            filename_identity_kind=filename_identity_kind,
            reason_code="filename_identifier_not_in_tree",
        )
    target_parent_ids = tuple(
        dict.fromkeys(
            parent_id
            for identifier in filename_identifiers
            for parent_id in entities_by_identifier[identifier]
        )
    )
    if len(target_parent_ids) != 1:
        return _EquipmentIdentityReselectProfile(
            filename_identity_kind=filename_identity_kind,
            filename_identifiers=filename_identifiers,
            reason_code="filename_identity_ambiguous",
        )

    target_parent_id = target_parent_ids[0]
    filename_prefixes = {
        prefix
        for prefix in map(_identifier_prefix, filename_identifiers)
        if prefix
    }
    opening_identifiers = tuple(
        identifier
        for identifier in _ordered_strong_identifiers(
            _opening_identity_evidence_text(
                original_text,
                original_name,
                max_chars=2000,
                max_lines=80,
            )
        )
        if (
            _identifier_prefix(identifier) in filename_prefixes
            and entities_by_identifier.get(identifier)
        )
    )
    shared_identifiers = tuple(
        identifier
        for identifier in filename_identifiers
        if identifier in set(opening_identifiers)
    )
    conflicting_identifiers = tuple(
        identifier
        for identifier in opening_identifiers
        if any(
            parent_id != target_parent_id
            for parent_id in entities_by_identifier[identifier]
        )
    )
    common_fields = {
        "filename_identity_kind": filename_identity_kind,
        "filename_identifiers": filename_identifiers,
        "opening_identifiers": opening_identifiers,
        "shared_identifiers": shared_identifiers,
        "conflicting_identifiers": conflicting_identifiers,
        "target_parent_id": target_parent_id,
        "target_parent_path": tree_index.require(target_parent_id).semantic_path,
    }
    if conflicting_identifiers:
        return _EquipmentIdentityReselectProfile(
            **common_fields,
            reason_code="opening_identity_conflict",
        )
    if not shared_identifiers:
        return _EquipmentIdentityReselectProfile(
            **common_fields,
            reason_code="independent_identity_missing",
        )

    candidate_ids = _ordered_equipment_family_scope_ids(
        target_parent_id,
        tree_index,
    )
    if len(candidate_ids) != 8:
        return _EquipmentIdentityReselectProfile(
            **common_fields,
            reason_code="equipment_family_topology_invalid",
        )
    if not set(candidate_ids).issubset(visible_ids):
        return _EquipmentIdentityReselectProfile(
            **common_fields,
            candidate_ids=candidate_ids,
            reason_code="equipment_family_scope_incomplete",
        )
    return _EquipmentIdentityReselectProfile(
        active=True,
        **common_fields,
        identifier=shared_identifiers[0],
        candidate_ids=candidate_ids,
        evidence_sources=("originalFileName", "openingIdentity"),
        reason_code="identity_confirmed",
    )


def _scope_parent_clusters(
    profile: _JaneClassificationProfile,
    original_text: str,
    tree_index: ArchitectureTreeIndex,
    entities_by_identifier: Mapping[str, Sequence[int]],
) -> tuple[tuple[int, tuple[int, ...]], ...]:
    primary_prefix = _identifier_prefix(profile.primary_identifier)
    if not primary_prefix:
        return ()

    parent_members: dict[int, list[int]] = {}
    body_identifiers = _ordered_strong_identifiers(
        original_text[:20_000],
        limit=256,
    )
    for identifier in body_identifiers:
        if _identifier_prefix(identifier) != primary_prefix:
            continue
        for entity_id in entities_by_identifier.get(identifier, ()):
            entity = tree_index.require(entity_id)
            parent_id = entity.parent_id
            if parent_id not in tree_index.nodes_by_id:
                continue
            members = parent_members.setdefault(parent_id, [])
            if entity_id not in members:
                members.append(entity_id)

    clusters = [
        (parent_id, tuple(member_ids))
        for parent_id, member_ids in parent_members.items()
        if len(member_ids) >= 2
    ]
    clusters.sort(key=lambda item: tree_index.require(item[0]).ordinal)
    return tuple(clusters)


def _parent_matches_scope_qualifier(
    parent: ArchitectureNodeProfile,
    qualifier: str,
) -> bool:
    normalized_qualifier = _normalize_scope_qualifier(qualifier)
    for prefix in ("flight", "block", "batch"):
        if normalized_qualifier.startswith(prefix):
            normalized_qualifier = normalized_qualifier[len(prefix):]
            break
    if not normalized_qualifier:
        return False
    normalized_parent = re.sub(
        r"[^a-z0-9\u3400-\u4dbf\u4e00-\u9fff]+",
        "",
        unicodedata.normalize("NFKC", parent.name).casefold(),
    )
    return normalized_qualifier in normalized_parent


def _parent_declares_scope_qualifier(
    parent: ArchitectureNodeProfile,
) -> bool:
    normalized = unicodedata.normalize("NFKC", parent.name).casefold()
    return bool(
        re.search(r"(?:flight|block)\s*[0-9ivxlcdm]", normalized)
        or re.search(r"[ivxlcdm]+[a-z]?\s*型", normalized)
        or re.search(
            r"第[一二三四五六七八九十百0-9]+\s*批次",
            normalized,
        )
    )


def _unique_air_equipment_parent_id(
    tree_index: ArchitectureTreeIndex,
) -> int | None:
    matches = [
        node.id
        for node in tree_index.nodes
        if (
            not node.is_leaf
            and node.parent_id is not None
            and unicodedata.normalize("NFKC", node.name).strip() == "空中装备"
        )
    ]
    return matches[0] if len(matches) == 1 else None


def _resolve_jane_architecture_scope(
    profile: _JaneClassificationProfile,
    *,
    original_text: str,
    tree_index: ArchitectureTreeIndex,
) -> _ArchitectureScopeResolution:
    if not profile.active:
        return _ArchitectureScopeResolution()
    if profile.identity_conflict:
        return _ArchitectureScopeResolution(reason_code="no_constraint_conflict")
    if not profile.recall_identity_enabled:
        return _ArchitectureScopeResolution()

    entities_by_identifier = _equipment_entities_by_identifier(tree_index)
    primary_entity_ids = tuple(
        dict.fromkeys(
            entities_by_identifier.get(profile.primary_identifier, ())
        )
    )
    if profile.scope_kind in {"class", "flight", "block"}:
        clusters = _scope_parent_clusters(
            profile,
            original_text,
            tree_index,
            entities_by_identifier,
        )
        clustered_parent_ids = tuple(parent_id for parent_id, _members in clusters)
        if not clustered_parent_ids:
            return _ArchitectureScopeResolution()

        qualifier_matches = tuple(
            parent_id
            for parent_id in clustered_parent_ids
            if _parent_matches_scope_qualifier(
                tree_index.require(parent_id),
                profile.qualifier,
            )
        )
        primary_parent_ids = tuple(
            dict.fromkeys(
                tree_index.require(entity_id).parent_id
                for entity_id in primary_entity_ids
                if tree_index.require(entity_id).parent_id
                in tree_index.nodes_by_id
            )
        )
        selected_parent_id: int | None = None
        tree_gap = False
        reason_code = "jane_scope_parent"
        if len(qualifier_matches) == 1:
            selected_parent_id = qualifier_matches[0]
        elif (
            len(clustered_parent_ids) == 1
            and profile.scope_kind in {"flight", "block"}
            and not _parent_declares_scope_qualifier(
                tree_index.require(clustered_parent_ids[0])
            )
        ):
            # 部分树只建总类节点而未把 Jane's Flight 0/Block 限定写进节点名。
            # 仅当父节点本身没有声明批次时才允许降级；若父节点已声明了
            # 不匹配的 Flight/Block/批次，即使主型号挂在该父下也不能选中。
            selected_parent_id = clustered_parent_ids[0]
        elif (
            len(clustered_parent_ids) == 1
            and profile.scope_kind == "class"
        ):
            selected_parent_id = clustered_parent_ids[0]
        elif profile.scope_kind == "class":
            lead_matches = tuple(
                parent_id
                for parent_id in primary_parent_ids
                if parent_id in clustered_parent_ids
            )
            if len(lead_matches) == 1:
                selected_parent_id = lead_matches[0]
                tree_gap = True
                reason_code = "jane_tree_gap_lead_parent"

        if selected_parent_id is None:
            return _ArchitectureScopeResolution(
                clustered_parent_ids=clustered_parent_ids,
            )
        return _ArchitectureScopeResolution(
            matched_scope_parent_id=selected_parent_id,
            matched_branch_parent_id=selected_parent_id,
            clustered_parent_ids=clustered_parent_ids,
            protected_parent_reasons=(
                (selected_parent_id, (reason_code,)),
            ),
            reason_code=reason_code,
            tree_gap=tree_gap,
        )

    if len(primary_entity_ids) == 1:
        branch_parent_id = primary_entity_ids[0]
        return _ArchitectureScopeResolution(
            matched_scope_parent_id=branch_parent_id,
            matched_branch_parent_id=branch_parent_id,
            protected_parent_reasons=(
                (branch_parent_id, ("jane_branch_guard",)),
            ),
            reason_code="jane_branch_guard",
        )

    if (
        not primary_entity_ids
        and profile.high_level_branch_hint == "air_equipment"
    ):
        air_parent_id = _unique_air_equipment_parent_id(tree_index)
        if air_parent_id is not None:
            return _ArchitectureScopeResolution(
                matched_scope_parent_id=air_parent_id,
                matched_branch_parent_id=air_parent_id,
                protected_parent_reasons=(
                    (air_parent_id, ("jane_high_level_branch",)),
                ),
                reason_code="jane_high_level_branch",
            )
    return _ArchitectureScopeResolution()


def _jane_classification_prompt_context(
    profile: _JaneClassificationProfile,
    resolution: _ArchitectureScopeResolution,
) -> dict[str, Any]:
    if not profile.active:
        return {}
    context: dict[str, Any] = {
        "title": profile.title,
        "primaryIdentifier": profile.primary_identifier,
        "qualifier": profile.qualifier,
        "scopeKind": profile.scope_kind,
        "highLevelBranchHint": profile.high_level_branch_hint,
        # 只有领域树已确认到具体装备实体时才把全文明细作用域交给模型。
        # MH-60R 这类仅能确认高层分支的树缺口场景不得据此猜测具体机型叶子。
        "dominantDetailKind": (
            profile.dominant_detail_kind
            if resolution.reason_code == "jane_branch_guard"
            else ""
        ),
    }
    if profile.scope_kind in {"class", "flight", "block"}:
        context["matchedScopeParentId"] = resolution.matched_scope_parent_id
        context["treeGap"] = resolution.tree_gap
    return context


def _unique_visible_equipment_identifier_parent(
        *,
        file_name: str,
        original_name: str,
        visible_ids: set[int],
        tree_index: ArchitectureTreeIndex,
        architecture_list: Iterable[Dict[str, Any]],
) -> int | None:
    """返回文件名唯一强匹配的、合法且模型可见的装备父节点。"""
    filename_identifiers = _strong_filename_identifiers(file_name, original_name)
    if not filename_identifiers:
        return None

    # 数据标准拓扑只计算一次。逐节点调用 _is_data_standard_parent_id() 会对完整
    # 6k+ 节点树反复重建拓扑，导致确定性约束退化为 O(n²)。
    topology_nodes, parent_ids = _architecture_candidate_topology(architecture_list)
    data_standard_parent_ids = _data_standard_candidate_ids(topology_nodes).intersection(
        parent_ids
    )

    matched_parent_ids: list[int] = []
    for node in tree_index.nodes:
        if (
                node.is_leaf
                or node.parent_id is None
                or node.id in data_standard_parent_ids
                or not _has_seven_equipment_detail_leaves(node, tree_index)
        ):
            continue
        node_identifiers = _strong_filename_identifiers(node.name)
        if not filename_identifiers.intersection(node_identifiers):
            continue
        matched_parent_ids.append(node.id)

    unique_ids = tuple(dict.fromkeys(matched_parent_ids))
    if len(unique_ids) != 1 or unique_ids[0] not in visible_ids:
        return None
    return unique_ids[0]


def _architecture_id_is_in_branch(
    architecture_id: int,
    branch_parent_id: int,
    tree_index: ArchitectureTreeIndex,
) -> bool:
    return (
        architecture_id == branch_parent_id
        or branch_parent_id in tree_index.ancestors_by_id[architecture_id]
    )


def _identity_reselect_relation(
    architecture_id: int,
    target_parent_id: int,
    tree_index: ArchitectureTreeIndex,
) -> str:
    if _architecture_id_is_in_branch(
        architecture_id,
        target_parent_id,
        tree_index,
    ):
        return "in_target_branch"
    if architecture_id in tree_index.ancestors_by_id[target_parent_id]:
        return "target_ancestor"

    selected = tree_index.require(architecture_id)
    target = tree_index.require(target_parent_id)
    selected_family_id = (
        selected.id
        if not selected.is_leaf
        and _has_seven_equipment_detail_leaves(selected, tree_index)
        else selected.parent_id
    )
    if (
        selected_family_id in tree_index.nodes_by_id
        and target.parent_id is not None
        and tree_index.require(selected_family_id).parent_id == target.parent_id
    ):
        return "sibling_equipment"
    return "cross_branch"


def _decide_identity_reselect_gate(
    architecture_id: int,
    *,
    profile: _EquipmentIdentityReselectProfile,
    tree_index: ArchitectureTreeIndex,
) -> _IdentityReselectGateDecision:
    if not profile.active or profile.target_parent_id is None:
        return _IdentityReselectGateDecision(reason_code=profile.reason_code)
    relation = _identity_reselect_relation(
        architecture_id,
        profile.target_parent_id,
        tree_index,
    )
    if relation == "in_target_branch":
        return _IdentityReselectGateDecision(
            relation=relation,
            reason_code="initial_result_in_target_branch",
        )
    return _IdentityReselectGateDecision(
        should_reselect=True,
        relation=relation,
        reason_code="branch_conflict_reselect",
    )


def _parse_architecture_reselect_result(
    raw_result: Any,
    *,
    scoped_ids: set[int],
    tree_index: ArchitectureTreeIndex,
    architecture_list: Iterable[Dict[str, Any]],
) -> int | None:
    parsed = _parse_strict_json_object(raw_result)
    if parsed is None or set(parsed) != {"architectureId"}:
        raise ArchitectureContractError(
            "受限重选结果必须是仅含 architectureId 的严格 JSON 对象"
        )
    if parsed["architectureId"] is None:
        return None
    return _validate_topk_architecture_id(
        parsed["architectureId"],
        visible_ids=scoped_ids,
        tree_index=tree_index,
        architecture_list=architecture_list,
    )


def _decide_topk_deterministic_architecture_constraint(
    architecture_id: int,
    *,
    file_name: str,
    original_name: str,
    visible_ids: set[int],
    tree_index: ArchitectureTreeIndex,
    architecture_list: Iterable[Dict[str, Any]],
    filename_constraint_mode: str = ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY,
    data_standard_mode: str = ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
    data_standard_profile: _DataStandardClassificationProfile | None = None,
    jane_profile: _JaneClassificationProfile | None = None,
    scope_resolution: _ArchitectureScopeResolution | None = None,
) -> _ArchitectureConstraintDecision:
    """应用高置信文件信号约束，防止模型越过明确的数据标准或装备分支。"""

    filename_constraint_mode = _normalize_analysis_filename_constraint_mode(
        filename_constraint_mode
    )
    data_standard_mode = _normalize_analysis_data_standard_mode(
        data_standard_mode
    )
    standard_profile = (
        data_standard_profile
        or _DataStandardClassificationProfile()
    )
    profile = jane_profile or _JaneClassificationProfile()
    resolution = scope_resolution or _ArchitectureScopeResolution()
    use_data_standard_scope_guard = (
        data_standard_mode == ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD
    )
    has_confirmed_standard_identity = (
        standard_profile.identity_confirmed
        and standard_profile.document_kind == "standard_body"
    )
    use_gjb_constraint = (
        has_confirmed_standard_identity
        if use_data_standard_scope_guard
        else _has_strong_gjb_filename_identity(file_name, original_name)
    )
    if use_gjb_constraint:
        visible_standard_ids = tuple(
            node_id
            for node_id in _ordered_data_standard_leaf_ids(architecture_list)
            if node_id in visible_ids
        )
        if architecture_id in visible_standard_ids:
            constrained_id = architecture_id
            reason_code = "data_standard_model_leaf"
        else:
            general_id = _general_data_standard_leaf_id(architecture_list)
            if general_id is None or general_id not in visible_ids:
                raise ArchitectureContractError(
                    "已确认标准正文，但模型无法确定专业类别且候选中不存在通用要求叶节点"
                )
            constrained_id = general_id
            reason_code = "data_standard_general_fallback"
        validated_id = _validate_topk_architecture_id(
            constrained_id,
            visible_ids=visible_ids,
            tree_index=tree_index,
            architecture_list=architecture_list,
        )
        return _ArchitectureConstraintDecision(
            pre_architecture_id=architecture_id,
            post_architecture_id=validated_id,
            reason_code=reason_code,
            matched_scope_parent_id=None,
            tree_gap=False,
        )

    use_scope_guard = (
        filename_constraint_mode
        == ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
        and profile.active
    )
    if use_scope_guard:
        if profile.identity_conflict:
            return _ArchitectureConstraintDecision(
                architecture_id,
                architecture_id,
                "no_constraint_conflict",
                None,
                False,
            )
        if not profile.identity_confirmed:
            return _ArchitectureConstraintDecision(
                architecture_id,
                architecture_id,
                "no_constraint_insufficient_evidence",
                resolution.matched_scope_parent_id,
                resolution.tree_gap,
            )

        matched_parent_id = resolution.matched_branch_parent_id
        if matched_parent_id is None or matched_parent_id not in visible_ids:
            return _ArchitectureConstraintDecision(
                architecture_id,
                architecture_id,
                "no_constraint_insufficient_evidence",
                resolution.matched_scope_parent_id,
                resolution.tree_gap,
            )

        if profile.scope_kind in {"class", "flight", "block"}:
            constrained_id = matched_parent_id
            reason_code = (
                "jane_tree_gap_lead_parent"
                if resolution.tree_gap
                else (
                    "jane_scope_parent"
                    if _architecture_id_is_in_branch(
                        architecture_id,
                        matched_parent_id,
                        tree_index,
                    )
                    else "jane_branch_guard"
                )
            )
        elif _architecture_id_is_in_branch(
            architecture_id,
            matched_parent_id,
            tree_index,
        ):
            constrained_id = architecture_id
            reason_code = resolution.reason_code
        else:
            constrained_id = matched_parent_id
            reason_code = resolution.reason_code

        validated_id = _validate_topk_architecture_id(
            constrained_id,
            visible_ids=visible_ids,
            tree_index=tree_index,
            architecture_list=architecture_list,
        )
        return _ArchitectureConstraintDecision(
            pre_architecture_id=architecture_id,
            post_architecture_id=validated_id,
            reason_code=reason_code,
            matched_scope_parent_id=resolution.matched_scope_parent_id,
            tree_gap=resolution.tree_gap,
        )

    if (
        filename_constraint_mode
        == ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
    ):
        # scope_guard 只允许经过正文首页识别和文件名交叉确认的 Jane 作用域约束。
        # 非 Jane 文件没有第二证据源时，文件名仍可参与召回，但不得落回 legacy
        # 的单源硬覆盖路径；需要旧行为的部署可显式选择 legacy。
        return _ArchitectureConstraintDecision(
            architecture_id,
            architecture_id,
            "no_constraint_insufficient_evidence",
            None,
            False,
        )

    matched_parent_id = _unique_visible_equipment_identifier_parent(
        file_name=file_name,
        original_name=original_name,
        visible_ids=visible_ids,
        tree_index=tree_index,
        architecture_list=architecture_list,
    )
    if matched_parent_id is None:
        return _ArchitectureConstraintDecision(
            architecture_id,
            architecture_id,
            "no_constraint_insufficient_evidence",
            None,
            False,
        )

    if _architecture_id_is_in_branch(
        architecture_id,
        matched_parent_id,
        tree_index,
    ):
        return _ArchitectureConstraintDecision(
            architecture_id,
            architecture_id,
            "legacy_identifier_parent",
            matched_parent_id,
            False,
        )

    validated_id = _validate_topk_architecture_id(
        matched_parent_id,
        visible_ids=visible_ids,
        tree_index=tree_index,
        architecture_list=architecture_list,
    )
    return _ArchitectureConstraintDecision(
        pre_architecture_id=architecture_id,
        post_architecture_id=validated_id,
        reason_code="legacy_identifier_parent",
        matched_scope_parent_id=matched_parent_id,
        tree_gap=False,
    )


def _apply_topk_deterministic_architecture_constraints(
    architecture_id: int,
    *,
    file_name: str,
    original_name: str,
    visible_ids: set[int],
    tree_index: ArchitectureTreeIndex,
    architecture_list: Iterable[Dict[str, Any]],
    filename_constraint_mode: str = ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY,
    data_standard_mode: str = ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
    data_standard_profile: _DataStandardClassificationProfile | None = None,
    jane_profile: _JaneClassificationProfile | None = None,
    scope_resolution: _ArchitectureScopeResolution | None = None,
) -> int:
    """兼容旧内部调用，只返回确定性约束后的 ID。"""

    return _decide_topk_deterministic_architecture_constraint(
        architecture_id,
        file_name=file_name,
        original_name=original_name,
        visible_ids=visible_ids,
        tree_index=tree_index,
        architecture_list=architecture_list,
        filename_constraint_mode=filename_constraint_mode,
        data_standard_mode=data_standard_mode,
        data_standard_profile=data_standard_profile,
        jane_profile=jane_profile,
        scope_resolution=scope_resolution,
    ).post_architecture_id

__all__ = (
    "ANALYSIS_CLASSIFICATION_MODES",
    "ANALYSIS_DATA_STANDARD_MODES",
    "ANALYSIS_DATA_STANDARD_MODE_LEGACY",
    "ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD",
    "ANALYSIS_FILENAME_CONSTRAINT_MODES",
    "ANALYSIS_FILENAME_CONSTRAINT_MODE_LEGACY",
    "ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD",
    "ANALYSIS_IDENTITY_RESELECT_MODES",
    "ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE",
    "MAX_ANALYSIS_PROMPT_CHARS",
    "_normalize_analysis_prompt",
    "_reject_nonstandard_json_constant",
    "_parse_strict_json_object",
    "_architecture_candidates",
    "_validate_data_standard_leaf_requirement",
    "_resolve_analysis_architecture_id",
    "_match_gjb_architecture_candidate",
    "_validate_architecture_repair_result",
    "_normalize_analysis_classification_mode",
    "_normalize_analysis_filename_constraint_mode",
    "_normalize_analysis_data_standard_mode",
    "_normalize_analysis_identity_reselect_mode",
    "_extract_recall_headings",
    "_build_analysis_architecture_signals",
    "_data_standard_candidate_scope",
    "_architecture_signal_digest",
    "_node_prompt_projection",
    "_normalize_bounded_analysis_prompt",
    "_validate_topk_architecture_id",
    "_parse_topk_classification_result",
    "_visible_data_standard_fallback_id",
    "_STRONG_FILENAME_IDENTIFIER_RE",
    "_FILENAME_DATE_IDENTIFIER_PREFIXES",
    "_EQUIPMENT_DETAIL_KINDS",
    "_ORDERED_EQUIPMENT_DETAIL_KINDS",
    "_STRONG_GJB_FILENAME_RE",
    "_GJB_STANDARD_NUMBER_RE",
    "_STANDARD_COMMENTARY_TITLE_RE",
    "_STANDARD_STRUCTURE_MARKERS",
    "_JANE_COPYRIGHT_RE",
    "_JANE_PAGE_ONE_RE",
    "_JANE_METADATA_RE",
    "_SCOPE_QUALIFIER_RE",
    "_JANE_CLASS_RE",
    "_JANE_AIRCRAFT_TOTALS_RE",
    "_JANE_CATALOG_FILENAME_RE",
    "_OPAQUE_IDENTITY_FILENAME_RE",
    "_DataStandardClassificationProfile",
    "_normalized_gjb_source_text",
    "_extract_gjb_standard_identifiers",
    "_extract_data_standard_title",
    "_build_data_standard_classification_profile",
    "_data_standard_prompt_context",
    "_JaneClassificationProfile",
    "_EquipmentIdentityReselectProfile",
    "_IdentityReselectGateDecision",
    "_jane_recall_filename_signals",
    "_ArchitectureScopeResolution",
    "_ArchitectureConstraintDecision",
    "_ordered_strong_identifiers",
    "_strong_filename_identifiers",
    "_jane_filename_identity_kind",
    "_identifier_prefix",
    "_extract_scope_qualifier",
    "_normalize_scope_qualifier",
    "_extract_jane_title",
    "_jane_dominant_detail_kind",
    "_build_jane_classification_profile",
    "_has_strong_gjb_filename_identity",
    "_equipment_detail_kind",
    "_has_seven_equipment_detail_leaves",
    "_equipment_entities_by_identifier",
    "_ordered_equipment_family_scope_ids",
    "_build_equipment_identity_reselect_profile",
    "_scope_parent_clusters",
    "_parent_matches_scope_qualifier",
    "_parent_declares_scope_qualifier",
    "_unique_air_equipment_parent_id",
    "_resolve_jane_architecture_scope",
    "_jane_classification_prompt_context",
    "_unique_visible_equipment_identifier_parent",
    "_architecture_id_is_in_branch",
    "_identity_reselect_relation",
    "_decide_identity_reselect_gate",
    "_parse_architecture_reselect_result",
    "_decide_topk_deterministic_architecture_constraint",
    "_apply_topk_deterministic_architecture_constraints",
)
