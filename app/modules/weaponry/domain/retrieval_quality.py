"""武器谱专用检索 Query、Chunk 诊断和 Evidence Selection 纯规则。

本模块不导入配置、日志、数据库、HTTP Client 或 AnythingLLM DTO。Adapter 必须先把供应商
结果转换为 :class:`EvidenceCandidate`，Application 再显式传入 execution 已冻结的 profile
和供应商指纹。这样旧任务重试不会读取新的运行时阈值，也不会因供应商升级而静默改变结果。
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

from .errors import WeaponryRetrievalValidationError


RETRIEVAL_QUERY_VERSION = "field-semantic-v2"
EVIDENCE_SCORE_SEMANTICS = "higher-is-more-relevant-in-unit-interval"
EVIDENCE_SCORE_PROTOCOL = "explicit-unit-score-or-stable-rank-v2"
EVIDENCE_RANKING_STRATEGY = "score-desc-or-rank-asc-stable-v2"
EVIDENCE_DEDUP_STRATEGY = "normalized-exact-within-document-v1"
EVIDENCE_SCORE_MODE_SCORE = "score"
EVIDENCE_SCORE_MODE_RANK = "rank"
_EVIDENCE_SCORE_MODES = frozenset(
    {EVIDENCE_SCORE_MODE_SCORE, EVIDENCE_SCORE_MODE_RANK}
)
_PROVIDER_METADATA_PATTERN = re.compile(
    # AnythingLLM 的 vector-search 当前会在 metadata XML 前增加 ``passage: ``。
    # 该前缀和 XML 都不是业务证据，且上传文件名、执行标识等易变值会污染候选哈希与
    # 同文档去重。这里只接受文本起始处完整闭合的固定供应商包装，正文中偶然出现标签
    # 或单独出现 ``passage:`` 时均不会被删除。
    r"^\s*(?:passage:\s*)?<document_metadata>[\s\S]*?</document_metadata>\s*",
    flags=re.IGNORECASE,
)
_CJK_WHITESPACE_PATTERN = re.compile(
    r"(?<=[\u3400-\u4dbf\u4e00-\u9fff])\s+(?=[\u3400-\u4dbf\u4e00-\u9fff])"
)
_URL_PATTERN = re.compile(r"https?://|www\.", flags=re.IGNORECASE)
_YEAR_PATTERN = re.compile(r"\b(?:19|20)\d{2}\b")
_REFERENCE_MARKERS = (
    "参考文献",
    "參考文獻",
    "參考資料",
    "bibliography",
    "references",
    "retrieved",
    "archived",
    "原始内容存档",
    "原始內容存檔",
    "存档于",
    "存檔於",
    "查阅于",
    "查閱於",
    "isbn",
    "doi:",
    "usni news",
)
_GENERIC_CJK_SUFFIXES = (
    "名称",
    "名稱",
    "信息",
    "資訊",
    "数据",
    "資料",
    "内容",
    "內容",
    "参数",
    "參數",
    "指标",
    "指標",
    "型号",
    "型號",
)


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WeaponryRetrievalValidationError(f"{name} 必须是非空 str")
    return value.strip()


def _optional_text(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise WeaponryRetrievalValidationError(f"{name} 必须是 str")
    return value.strip()


def _text_tuple(value: object, *, name: str) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise WeaponryRetrievalValidationError(f"{name} 必须是有序文本序列")
    normalized: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise WeaponryRetrievalValidationError(
                f"{name}[{index}] 必须是非空 str"
            )
        normalized.append(item.strip())
    return tuple(normalized)


def _positive_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise WeaponryRetrievalValidationError(f"{name} 必须是正整数")
    return value


def _unit_float(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WeaponryRetrievalValidationError(f"{name} 必须是 0~1 的有限数字")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0.0 or normalized > 1.0:
        raise WeaponryRetrievalValidationError(f"{name} 必须是 0~1 的有限数字")
    return normalized


def _optional_unit_float(value: object, *, name: str) -> float | None:
    if value is None:
        return None
    return _unit_float(value, name=name)


def _candidate_unit_float(value: object) -> float | None:
    """解析候选分数；非法值返回 None，交由选择结果记录稳定拒绝原因。"""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    normalized = float(value)
    if not math.isfinite(normalized) or normalized < 0.0 or normalized > 1.0:
        return None
    return normalized


@dataclass(frozen=True)
class RetrievalColumn:
    """TABLE 检索所需的列名和列语义，不包含公开解析结果。"""

    field_name: str
    field_description: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "field_name",
            _required_text(self.field_name, name="field_name"),
        )
        object.__setattr__(
            self,
            "field_description",
            _optional_text(self.field_description, name="field_description"),
        )


@dataclass(frozen=True)
class RetrievalField:
    """一次字段召回的供应商无关输入。"""

    field_name: str
    field_description: str = ""
    field_type: str = "INPUT"
    columns: tuple[RetrievalColumn, ...] = ()
    expanded_terms: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "field_name",
            _required_text(self.field_name, name="field_name"),
        )
        object.__setattr__(
            self,
            "field_description",
            _optional_text(self.field_description, name="field_description"),
        )
        normalized_type = _required_text(
            self.field_type,
            name="field_type",
        ).upper()
        if normalized_type not in {"INPUT", "TABLE"}:
            raise WeaponryRetrievalValidationError(
                "field_type 只能是 INPUT 或 TABLE"
            )
        object.__setattr__(self, "field_type", normalized_type)
        if not isinstance(self.columns, (tuple, list)):
            raise WeaponryRetrievalValidationError("columns 必须是有序列定义")
        normalized_columns = tuple(self.columns)
        if any(not isinstance(item, RetrievalColumn) for item in normalized_columns):
            raise WeaponryRetrievalValidationError(
                "columns 只能包含 RetrievalColumn"
            )
        if normalized_type == "INPUT" and normalized_columns:
            raise WeaponryRetrievalValidationError("INPUT 不能包含 TABLE columns")
        if normalized_type == "TABLE" and not normalized_columns:
            raise WeaponryRetrievalValidationError("TABLE 必须包含至少一列")
        object.__setattr__(self, "columns", normalized_columns)
        object.__setattr__(
            self,
            "expanded_terms",
            _text_tuple(self.expanded_terms, name="expanded_terms"),
        )


@dataclass(frozen=True)
class RetrievalQuery:
    """只用于召回的精炼 Query；不得混入 Extraction 输出指令。"""

    text: str
    semantic_terms: tuple[str, ...]
    version: str = RETRIEVAL_QUERY_VERSION

    def __post_init__(self) -> None:
        text = _required_text(self.text, name="text")
        object.__setattr__(self, "text", text)
        object.__setattr__(
            self,
            "semantic_terms",
            _text_tuple(self.semantic_terms, name="semantic_terms"),
        )
        object.__setattr__(
            self,
            "version",
            _required_text(self.version, name="version"),
        )


@dataclass(frozen=True)
class EvidenceCandidate:
    """Retrieval Adapter 输出的候选，不携带供应商 metadata 字典。

    ``provider_rank`` 和 ``provider_score`` 保留供应商解码后的原始类型，使 Selection 能把
    非法值归类为“整批协议损坏”，而不是在 Adapter 中用 ``int()``/``float()`` 静默修正。
    ``provider_score_present`` 明确区分“供应商声明本批次没有分数”和“供应商返回非法分数”；
    后者绝不能伪装成 rank-only 批次。
    """

    candidate_id: str
    document_key: str
    text: str
    provider_rank: object
    provider_score: object
    provider_score_present: bool
    score_profile_id: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidate_id",
            _required_text(self.candidate_id, name="candidate_id"),
        )
        object.__setattr__(
            self,
            "document_key",
            _required_text(self.document_key, name="document_key"),
        )
        if not isinstance(self.text, str):
            raise WeaponryRetrievalValidationError("text 必须是 str")
        if not isinstance(self.provider_score_present, bool):
            raise WeaponryRetrievalValidationError(
                "provider_score_present 必须是 bool"
            )
        if not self.provider_score_present and self.provider_score is not None:
            raise WeaponryRetrievalValidationError(
                "未声明分数时 provider_score 必须是 None"
            )
        object.__setattr__(
            self,
            "score_profile_id",
            _required_text(self.score_profile_id, name="score_profile_id"),
        )


@dataclass(frozen=True)
class EvidenceSelectionPolicy:
    """随 execution 冻结的完整 Evidence Selection policy。

    ``candidate_top_n`` 只描述向检索供应商请求的候选批次，不是最终 Evidence 配额。
    按已确认口径，Selection 不再保存或执行总行数、单文档行数、总字符数和单文档
    字符数上限；所有通过来源、score/rank 协议、正文质量和精确去重门禁的完整正文都会
    被保留。本策略不包含绝对相关性阈值、anchor 命中要求或独立 reranker。
    """

    profile_id: str
    provider_fingerprint: str
    embedding_fingerprint: str
    document_processing_fingerprint: str
    query_version: str = RETRIEVAL_QUERY_VERSION
    score_semantics: str = EVIDENCE_SCORE_SEMANTICS
    score_protocol: str = EVIDENCE_SCORE_PROTOCOL
    ranking_strategy: str = EVIDENCE_RANKING_STRATEGY
    input_candidate_top_n: int = 8
    table_candidate_top_n: int = 16
    dedup_strategy: str = EVIDENCE_DEDUP_STRATEGY
    reject_reference_like: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "profile_id",
            _required_text(self.profile_id, name="profile_id"),
        )
        object.__setattr__(
            self,
            "provider_fingerprint",
            _required_text(
                self.provider_fingerprint,
                name="provider_fingerprint",
            ),
        )
        object.__setattr__(
            self,
            "embedding_fingerprint",
            _required_text(
                self.embedding_fingerprint,
                name="embedding_fingerprint",
            ),
        )
        object.__setattr__(
            self,
            "document_processing_fingerprint",
            _required_text(
                self.document_processing_fingerprint,
                name="document_processing_fingerprint",
            ),
        )
        object.__setattr__(
            self,
            "query_version",
            _required_text(self.query_version, name="query_version"),
        )
        if self.query_version != RETRIEVAL_QUERY_VERSION:
            raise WeaponryRetrievalValidationError(
                "当前 profile 的 query_version 不受支持"
            )
        object.__setattr__(
            self,
            "score_semantics",
            _required_text(self.score_semantics, name="score_semantics"),
        )
        if self.score_semantics != EVIDENCE_SCORE_SEMANTICS:
            raise WeaponryRetrievalValidationError(
                "当前 profile 的 score_semantics 不受支持"
            )
        object.__setattr__(
            self,
            "score_protocol",
            _required_text(self.score_protocol, name="score_protocol"),
        )
        if self.score_protocol != EVIDENCE_SCORE_PROTOCOL:
            raise WeaponryRetrievalValidationError(
                "当前 profile 的 score_protocol 不受支持"
            )
        object.__setattr__(
            self,
            "ranking_strategy",
            _required_text(self.ranking_strategy, name="ranking_strategy"),
        )
        if self.ranking_strategy != EVIDENCE_RANKING_STRATEGY:
            raise WeaponryRetrievalValidationError(
                "当前 profile 的 ranking_strategy 不受支持"
            )
        for name in ("input_candidate_top_n", "table_candidate_top_n"):
            object.__setattr__(
                self,
                name,
                _positive_int(getattr(self, name), name=name),
            )
        if self.dedup_strategy != EVIDENCE_DEDUP_STRATEGY:
            raise WeaponryRetrievalValidationError(
                "当前 profile 的 dedup_strategy 不受支持"
            )
        if not isinstance(self.reject_reference_like, bool):
            raise WeaponryRetrievalValidationError(
                "reject_reference_like 必须是 bool"
            )

@dataclass(frozen=True)
class ChunkQualityReport:
    """不包含正文的 Chunk 质量诊断，可安全写入结构化日志或审计。"""

    raw_chars: int
    content_chars: int
    provider_metadata_chars: int
    provider_metadata_ratio: float
    url_count: int
    year_count: int
    reference_marker_count: int
    reference_like: bool
    rejection_reasons: tuple[str, ...]


@dataclass(frozen=True)
class EvidenceRejection:
    candidate_id: str
    reason: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "candidate_id",
            _required_text(self.candidate_id, name="candidate_id"),
        )
        object.__setattr__(
            self,
            "reason",
            _required_text(self.reason, name="reason"),
        )


@dataclass(frozen=True)
class SelectedEvidence:
    candidate_id: str
    document_key: str
    text: str
    provider_rank: int
    provider_score: float | None
    score_profile_id: str
    score_mode: str
    original_index: int

    def __post_init__(self) -> None:
        for name in ("candidate_id", "document_key", "text", "score_profile_id"):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name=name),
            )
        object.__setattr__(
            self,
            "provider_rank",
            _positive_int(self.provider_rank, name="provider_rank"),
        )
        object.__setattr__(
            self,
            "provider_score",
            _optional_unit_float(self.provider_score, name="provider_score"),
        )
        if self.score_mode not in _EVIDENCE_SCORE_MODES:
            raise WeaponryRetrievalValidationError(
                "score_mode 只能是 score 或 rank"
            )
        if (
            self.score_mode == EVIDENCE_SCORE_MODE_SCORE
            and self.provider_score is None
        ):
            raise WeaponryRetrievalValidationError(
                "score 模式必须携带 provider_score"
            )
        if (
            self.score_mode == EVIDENCE_SCORE_MODE_RANK
            and self.provider_score is not None
        ):
            raise WeaponryRetrievalValidationError(
                "rank 模式不得携带 provider_score"
            )
        if (
            isinstance(self.original_index, bool)
            or not isinstance(self.original_index, int)
            or self.original_index < 0
        ):
            raise WeaponryRetrievalValidationError(
                "original_index 必须是非负整数"
            )


@dataclass(frozen=True)
class EvidenceSelectionResult:
    selected: tuple[SelectedEvidence, ...]
    rejected: tuple[EvidenceRejection, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.selected, (tuple, list)) or any(
            not isinstance(item, SelectedEvidence) for item in self.selected
        ):
            raise WeaponryRetrievalValidationError(
                "selected 只能包含 SelectedEvidence"
            )
        if not isinstance(self.rejected, (tuple, list)) or any(
            not isinstance(item, EvidenceRejection) for item in self.rejected
        ):
            raise WeaponryRetrievalValidationError(
                "rejected 只能包含 EvidenceRejection"
            )
        selected = tuple(self.selected)
        selected_ids = tuple(item.candidate_id for item in selected)
        if len(set(selected_ids)) != len(selected_ids):
            raise WeaponryRetrievalValidationError(
                "selected 不能包含重复 candidate_id"
            )
        object.__setattr__(self, "selected", selected)
        object.__setattr__(self, "rejected", tuple(self.rejected))

    @property
    def rejection_counts(self) -> tuple[tuple[str, int], ...]:
        """按首次出现顺序返回拒绝原因计数，便于审计但不暴露正文。"""

        counts: dict[str, int] = {}
        for item in self.rejected:
            counts[item.reason] = counts.get(item.reason, 0) + 1
        return tuple(counts.items())


def _normalize_for_matching(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = normalized.replace("\u200b", "").replace("\ufeff", "")
    for _ in range(3):
        replaced = _CJK_WHITESPACE_PATTERN.sub("", normalized)
        if replaced == normalized:
            break
        normalized = replaced
    return re.sub(r"\s+", " ", normalized).strip()


def _normalize_for_exact_dedup(value: str) -> str:
    """只统一换行并去除首尾空白，不折叠正文内部空白或大小写。"""

    return value.replace("\r\n", "\n").replace("\r", "\n").strip()


def normalize_evidence_text(value: str) -> str:
    """移除供应商 metadata 前缀并修复 PDF 常见的 CJK 字间空白。

    只删除位于文本起始位置、且同时具有开闭标签的 metadata 块，并兼容 AnythingLLM
    vector-search 的固定 ``passage: `` 包装。正文中偶然出现相同结束标签时不会被截断。
    该函数不会翻译、改写事实或拼接跨 Chunk 上下文。
    """

    if not isinstance(value, str):
        raise WeaponryRetrievalValidationError("Evidence text 必须是 str")
    normalized = unicodedata.normalize("NFKC", value)
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = _PROVIDER_METADATA_PATTERN.sub("", normalized, count=1)
    normalized = normalized.replace("\u200b", "").replace("\ufeff", "")
    for _ in range(3):
        replaced = _CJK_WHITESPACE_PATTERN.sub("", normalized)
        if replaced == normalized:
            break
        normalized = replaced
    lines = [line.rstrip() for line in normalized.splitlines()]
    compact = "\n".join(lines).strip()
    return re.sub(r"\n{3,}", "\n\n", compact)


def _append_semantic_variants(target: list[str], raw_term: str) -> None:
    normalized = _normalize_for_matching(raw_term)
    # 公开字段名只要求非空。单字中文字段（例如“长”“宽”“高”）具有完整业务语义，
    # 不能因为字符数为 1 就在任务受理后异步失败。标点字段在规范化后可以为空；召回仍
    # 使用包含原始字段名和 fieldDescription 的 Query 正文，因此这里不额外制造隐藏门禁。
    if normalized:
        target.append(normalized)

    # “舰级名称”“雷达型号”等字段名的通用尾缀不是检索核心。保留完整字段名的同时，
    # 增加去尾缀版本，提升对原文短标题和表头的匹配能力。
    for suffix in _GENERIC_CJK_SUFFIXES:
        if normalized.endswith(suffix) and len(normalized) > len(suffix) + 1:
            target.append(normalized[: -len(suffix)].strip())

    # 显式短语不能自动降级为单词。例如 ``ship class`` 若被拆成 ``ship``，任何只提到
    # 舰船的 Chunk 都可能被误判为舰级证据。调用方需要单词同义词时必须显式提供，使其能进入
    # 校准资产和 execution profile，而不是由本函数产生不可审计的弱扩展。


def _build_semantic_terms(field: RetrievalField) -> tuple[str, ...]:
    candidates: list[str] = []
    _append_semantic_variants(candidates, field.field_name)
    for column in field.columns:
        _append_semantic_variants(candidates, column.field_name)
    for term in field.expanded_terms:
        _append_semantic_variants(candidates, term)

    # 有序去重让同一字段在不同进程中生成完全相同的检索词顺序。
    seen: set[str] = set()
    terms: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            terms.append(candidate)
    # ``semantic_terms`` 是可审计的辅助元数据，不是 AnythingLLM 向量召回的必要输入。
    # HTTP 契约只要求 fieldName 非空，若名称只有符号也必须允许 Query 正文继续携带原始
    # 字段名/说明执行召回，不能在异步 Worker 内用一条未公开规则把已受理任务判失败。
    return tuple(terms)


def build_retrieval_query(field: RetrievalField) -> RetrievalQuery:
    """构建不包含抽取格式指令的专用召回 Query。"""

    if not isinstance(field, RetrievalField):
        raise WeaponryRetrievalValidationError("field 必须是 RetrievalField")
    lines = [f"字段：{field.field_name}"]
    if field.field_description:
        lines.append(f"语义说明：{field.field_description}")
    if field.field_type == "TABLE":
        rendered_columns = []
        for column in field.columns:
            if column.field_description:
                rendered_columns.append(
                    f"{column.field_name}（{column.field_description}）"
                )
            else:
                rendered_columns.append(column.field_name)
        lines.append("列：" + "；".join(rendered_columns))
    if field.expanded_terms:
        lines.append("检索同义词：" + "；".join(field.expanded_terms))
    return RetrievalQuery(
        text="\n".join(lines),
        semantic_terms=_build_semantic_terms(field),
    )


def assess_chunk_quality(raw_text: str) -> ChunkQualityReport:
    """识别供应商 metadata、空内容和参考文献密集块，不输出正文。"""

    if not isinstance(raw_text, str):
        raise WeaponryRetrievalValidationError("raw_text 必须是 str")
    metadata_match = _PROVIDER_METADATA_PATTERN.match(raw_text)
    metadata_chars = len(metadata_match.group(0)) if metadata_match else 0
    content = normalize_evidence_text(raw_text)
    raw_chars = len(raw_text)
    content_chars = len(content)
    ratio = metadata_chars / raw_chars if raw_chars else 0.0
    folded = _normalize_for_matching(content)
    url_count = len(_URL_PATTERN.findall(folded))
    year_count = len(_YEAR_PATTERN.findall(folded))
    marker_count = sum(folded.count(marker.casefold()) for marker in _REFERENCE_MARKERS)
    latin_chars = len(re.findall(r"[a-z]", folded))
    cjk_chars = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", folded))
    language_chars = latin_chars + cjk_chars
    latin_ratio = latin_chars / language_chars if language_chars else 0.0

    explicit_reference_block = any(
        marker in folded
        for marker in ("参考文献", "參考文獻", "參考資料", "bibliography", "references")
    )
    reference_like = (
        explicit_reference_block
        or url_count >= 4
        or (
            year_count >= 5
            and (url_count > 0 or marker_count > 0 or latin_ratio >= 0.35)
        )
    )
    reasons: list[str] = []
    if content_chars < 20:
        reasons.append("content-empty-or-too-short")
    if ratio >= 0.5:
        reasons.append("provider-metadata-dominates")
    if reference_like:
        reasons.append("reference-like-content")
    return ChunkQualityReport(
        raw_chars=raw_chars,
        content_chars=content_chars,
        provider_metadata_chars=metadata_chars,
        provider_metadata_ratio=ratio,
        url_count=url_count,
        year_count=year_count,
        reference_marker_count=marker_count,
        reference_like=reference_like,
        rejection_reasons=tuple(reasons),
    )


@dataclass(frozen=True)
class _RankedEvidence:
    selected: SelectedEvidence


def _reject_batch(
    candidates: tuple[EvidenceCandidate, ...],
    *,
    reason: str,
) -> EvidenceSelectionResult:
    """把协议级错误稳定映射到整批 Candidate，且不泄漏正文或原始分数。"""

    return EvidenceSelectionResult(
        selected=(),
        rejected=tuple(
            EvidenceRejection(candidate.candidate_id, reason)
            for candidate in candidates
        ),
    )


def select_evidence(
    candidates: Iterable[EvidenceCandidate],
    *,
    score_mode: str,
    query: RetrievalQuery,
    profile: EvidenceSelectionPolicy,
    provider_fingerprint: str,
    embedding_fingerprint: str,
    expected_document_keys: Iterable[str],
) -> EvidenceSelectionResult:
    """选择可进入模型上下文和回调 rows 的证据。

    ``score`` 批次要求每项都显式携带 0~1 有限分数；``rank`` 批次要求每项都明确无分数。
    两种模式均要求整批 rank 是互不重复的正整数。任一协议项损坏会拒绝整批，避免部分
    Candidate 被悄悄降级后改变重试结果。合法批次只做来源、正文质量、同文档精确去重和
    稳定排名，不执行绝对阈值、anchor、reranker 或内容配额裁剪。
    """

    if not isinstance(query, RetrievalQuery):
        raise WeaponryRetrievalValidationError("query 必须是 RetrievalQuery")
    if not isinstance(profile, EvidenceSelectionPolicy):
        raise WeaponryRetrievalValidationError(
            "profile 必须是 EvidenceSelectionPolicy"
        )
    if score_mode not in _EVIDENCE_SCORE_MODES:
        raise WeaponryRetrievalValidationError(
            "score_mode 只能是 score 或 rank"
        )
    actual_fingerprint = _required_text(
        provider_fingerprint,
        name="provider_fingerprint",
    )
    if actual_fingerprint != profile.provider_fingerprint:
        raise WeaponryRetrievalValidationError(
            "provider fingerprint 与 execution profile 不一致"
        )
    actual_embedding_fingerprint = _required_text(
        embedding_fingerprint,
        name="embedding_fingerprint",
    )
    if actual_embedding_fingerprint != profile.embedding_fingerprint:
        raise WeaponryRetrievalValidationError(
            "embedding fingerprint 与 execution profile 不一致"
        )
    if query.version != profile.query_version:
        raise WeaponryRetrievalValidationError(
            "Retrieval Query 版本与 execution profile 不一致"
        )
    expected_key_sequence = tuple(
        _required_text(item, name="expected_document_key")
        for item in expected_document_keys
    )
    if not expected_key_sequence:
        raise WeaponryRetrievalValidationError(
            "expected_document_keys 不能为空"
        )
    if len(set(expected_key_sequence)) != len(expected_key_sequence):
        raise WeaponryRetrievalValidationError(
            "expected_document_keys 不能包含重复文档身份"
        )
    expected_keys = frozenset(expected_key_sequence)
    document_sequence = {
        document_key: index
        for index, document_key in enumerate(expected_key_sequence)
    }

    frozen_candidates = tuple(candidates)
    if any(not isinstance(candidate, EvidenceCandidate) for candidate in frozen_candidates):
        raise WeaponryRetrievalValidationError(
            "candidates 只能包含 EvidenceCandidate"
        )
    candidate_ids = tuple(candidate.candidate_id for candidate in frozen_candidates)
    if len(set(candidate_ids)) != len(candidate_ids):
        return _reject_batch(
            frozen_candidates,
            reason="duplicate-candidate-id",
        )

    # rank 是两种批次共同的确定性排序事实。bool 不能作为整数接受；重复 rank 也不能依靠
    # 供应商偶然返回顺序消歧，否则相同候选集合在重试时可能产生不同 rows。
    ranks = tuple(candidate.provider_rank for candidate in frozen_candidates)
    if any(
        isinstance(rank, bool) or not isinstance(rank, int) or rank < 1
        for rank in ranks
    ):
        return _reject_batch(frozen_candidates, reason="invalid-provider-rank")
    if len(set(ranks)) != len(ranks):
        return _reject_batch(frozen_candidates, reason="duplicate-provider-rank")

    score_presence = tuple(
        candidate.provider_score_present for candidate in frozen_candidates
    )
    if score_mode == EVIDENCE_SCORE_MODE_SCORE:
        if not all(score_presence):
            return _reject_batch(
                frozen_candidates,
                reason="mixed-provider-score-mode",
            )
        normalized_scores = tuple(
            _candidate_unit_float(candidate.provider_score)
            for candidate in frozen_candidates
        )
        if any(score is None for score in normalized_scores):
            return _reject_batch(
                frozen_candidates,
                reason="invalid-provider-score",
            )
    else:
        if any(score_presence) or any(
            candidate.provider_score is not None
            for candidate in frozen_candidates
        ):
            return _reject_batch(
                frozen_candidates,
                reason="mixed-provider-score-mode",
            )
        normalized_scores = tuple(None for _ in frozen_candidates)

    ranked: list[_RankedEvidence] = []
    rejected: list[EvidenceRejection] = []
    for original_index, (candidate, provider_score) in enumerate(
        zip(frozen_candidates, normalized_scores, strict=True)
    ):
        if candidate.document_key not in expected_keys:
            rejected.append(
                EvidenceRejection(candidate.candidate_id, "unexpected-document")
            )
            continue
        if candidate.score_profile_id != profile.profile_id:
            rejected.append(
                EvidenceRejection(candidate.candidate_id, "score-profile-mismatch")
            )
            continue
        quality = assess_chunk_quality(candidate.text)
        if "content-empty-or-too-short" in quality.rejection_reasons:
            rejected.append(
                EvidenceRejection(candidate.candidate_id, "content-empty-or-too-short")
            )
            continue
        if "provider-metadata-dominates" in quality.rejection_reasons:
            rejected.append(
                EvidenceRejection(candidate.candidate_id, "provider-metadata-dominates")
            )
            continue
        if profile.reject_reference_like and quality.reference_like:
            rejected.append(
                EvidenceRejection(candidate.candidate_id, "reference-like-content")
            )
            continue

        content = normalize_evidence_text(candidate.text)
        ranked.append(
            _RankedEvidence(
                selected=SelectedEvidence(
                    candidate_id=candidate.candidate_id,
                    document_key=candidate.document_key,
                    text=content,
                    provider_rank=candidate.provider_rank,
                    provider_score=provider_score,
                    score_profile_id=candidate.score_profile_id,
                    score_mode=score_mode,
                    original_index=original_index,
                )
            )
        )

    if score_mode == EVIDENCE_SCORE_MODE_SCORE:
        ranked.sort(
            key=lambda item: (
                -float(item.selected.provider_score),
                item.selected.provider_rank,
                document_sequence[item.selected.document_key],
                item.selected.candidate_id,
                item.selected.original_index,
            )
        )
    else:
        ranked.sort(
            key=lambda item: (
                item.selected.provider_rank,
                document_sequence[item.selected.document_key],
                item.selected.candidate_id,
                item.selected.original_index,
            )
        )

    # 去重必须发生在稳定排序之后。否则供应商偶然返回顺序会决定同文档重复 Chunk 的
    # 胜者，重试时即使候选集合完全相同，也可能产生不同 rows。
    deduplicated: list[_RankedEvidence] = []
    seen_per_document: set[tuple[str, str]] = set()
    for item in ranked:
        evidence = item.selected
        dedup_key = (
            evidence.document_key,
            _normalize_for_exact_dedup(evidence.text),
        )
        if dedup_key in seen_per_document:
            rejected.append(
                EvidenceRejection(evidence.candidate_id, "duplicate-in-document")
            )
            continue
        seen_per_document.add(dedup_key)
        deduplicated.append(item)

    return EvidenceSelectionResult(
        selected=tuple(item.selected for item in deduplicated),
        rejected=tuple(rejected),
    )
