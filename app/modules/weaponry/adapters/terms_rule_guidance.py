"""可整体移除的 AnythingLLM 术语规则辅助 Adapter。"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.integrations.anythingllm import AnythingLLMTransportError
from app.modules.weaponry.domain import (
    AUXILIARY_GUIDANCE_TERMS_RULES_COLUMN_COMPACT_V2,
    AUXILIARY_GUIDANCE_TERMS_RULES_V1,
    AuxiliaryGuidance,
)
from app.modules.weaponry.ports import (
    AuxiliaryGuidanceOutcome,
    AuxiliaryGuidanceRequest,
    AuxiliaryGuidanceResult,
    WeaponryPortStateError,
)

from .anythingllm_clients import WeaponryAnythingLLMClientFactoryProtocol


logger = logging.getLogger(__name__)

_RULE_FIELD_LABELS = {
    "standard_zh": "标准中文字段",
    "standard_en": "标准英文字段",
    "unit": "标准单位",
    "output_format": "输出格式",
    "standard_sample": "标准输出样例",
    "source_sample": "原文到标准输出示例",
}
_GENERIC_RULE_PREFIXES = (
    "当原文出现本卡片中的标准字段",
    "如果原文给出了字段值",
    "最终输出必须包含标准单位",
    "多个并列值时",
    "如果原文没有明确给出",
    "如果多个字段规则均可能匹配",
)


@dataclass(frozen=True)
class TermsRuleChunk:
    """术语 Provider 的最小只读输出，不包含 AnythingLLM metadata。"""

    source_ref: str
    text: str
    rank: int

    def __post_init__(self) -> None:
        if not isinstance(self.source_ref, str) or not self.source_ref.strip():
            raise ValueError("source_ref 必须是非空 str")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("text 必须是非空 str")
        if isinstance(self.rank, bool) or not isinstance(self.rank, int) or self.rank < 1:
            raise ValueError("rank 必须是正整数")


@runtime_checkable
class TermsRuleProviderProtocol(Protocol):
    def search(self, query: str, *, top_n: int) -> tuple[TermsRuleChunk, ...]:
        ...


class AnythingLLMReadOnlyTermsRuleProvider:
    """只读取配置好的共享术语 workspace；本类没有任何写 API。"""

    def __init__(
        self,
        client_factory: WeaponryAnythingLLMClientFactoryProtocol,
        *,
        workspace_slug: str,
        user_id: int | None = 1,
    ) -> None:
        if not isinstance(client_factory, WeaponryAnythingLLMClientFactoryProtocol):
            raise TypeError("client_factory 必须实现武器谱 AnythingLLM Client 工厂")
        if not isinstance(workspace_slug, str) or not workspace_slug.strip():
            raise ValueError("workspace_slug 必须是非空 str")
        if user_id is not None and (
            isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1
        ):
            raise ValueError("user_id 必须是正整数或 None")
        self._client_factory = client_factory
        self._workspace_slug = workspace_slug.strip()
        self._user_id = user_id

    def search(self, query: str, *, top_n: int) -> tuple[TermsRuleChunk, ...]:
        with self._client_factory.create() as clients:
            sources = clients.workspaces.vector_search(
                self._workspace_slug,
                query,
                top_n=top_n,
                score_threshold=0.0,
                user_id=self._user_id,
            )
        chunks: list[TermsRuleChunk] = []
        for rank, source in enumerate(sources, start=1):
            text = source.text.strip()
            if not text:
                continue
            source_identity = (
                source.source_marker
                or source.document_ref
                or source.id
                or f"rank:{rank}"
            )
            chunks.append(
                TermsRuleChunk(
                    source_ref=source_identity,
                    text=text,
                    rank=rank,
                )
            )
        return tuple(chunks)


@dataclass(frozen=True)
class _TermsRuleTarget:
    field_name: str
    field_description: str
    order: int


@dataclass(frozen=True)
class _ParsedTermsRule:
    source_ref: str
    rank: int
    card_id: str
    heading: str
    standard_zh: str
    standard_en: str
    unit: str
    output_format: str
    definition: str
    aliases: tuple[str, ...]
    special_rules: tuple[str, ...]
    standard_sample: str
    source_sample: str


@dataclass(frozen=True)
class _MatchedTermsRule:
    target: _TermsRuleTarget
    rule: _ParsedTermsRule


class TermsRuleGuidanceAdapter:
    """把术语检索结果转换为非事实 ``AuxiliaryGuidance``，失败时兼容降级。"""

    def __init__(
        self,
        provider: TermsRuleProviderProtocol,
        *,
        catalog_fingerprint: str,
    ) -> None:
        if not isinstance(provider, TermsRuleProviderProtocol):
            raise TypeError("provider 必须实现 TermsRuleProviderProtocol")
        if not isinstance(catalog_fingerprint, str) or not catalog_fingerprint.strip():
            raise ValueError("catalog_fingerprint 必须是非空 str")
        self._provider = provider
        self._catalog_fingerprint = catalog_fingerprint.strip()

    def load(self, request: AuxiliaryGuidanceRequest) -> AuxiliaryGuidanceResult:
        if not isinstance(request, AuxiliaryGuidanceRequest):
            raise TypeError("request 必须是 AuxiliaryGuidanceRequest")
        if request.policy.policy_id not in {
            AUXILIARY_GUIDANCE_TERMS_RULES_V1,
            AUXILIARY_GUIDANCE_TERMS_RULES_COLUMN_COMPACT_V2,
        }:
            raise WeaponryPortStateError(
                "auxiliary_policy_adapter_mismatch",
                "术语规则 Adapter 不支持当前辅助策略",
            )
        if request.policy.catalog_fingerprint != self._catalog_fingerprint:
            raise WeaponryPortStateError(
                "auxiliary_catalog_fingerprint_mismatch",
                "术语目录指纹与 execution 快照不一致",
            )
        try:
            if (
                request.policy.policy_id
                == AUXILIARY_GUIDANCE_TERMS_RULES_COLUMN_COMPACT_V2
            ):
                guidance, card_ids = self._load_column_compact(request)
            else:
                chunks = self._provider.search(
                    self._legacy_query(request),
                    top_n=request.policy.top_n,
                )
                guidance = self._legacy_to_guidance(
                    chunks,
                    max_context_chars=request.policy.max_context_chars,
                )
                card_ids = ()
        except Exception as exc:
            if isinstance(exc, WeaponryPortStateError):
                error_code = exc.error_code
            elif isinstance(exc, AnythingLLMTransportError):
                error_code = exc.code
            else:
                error_code = "terms_rule_provider_failed"
            logger.warning(
                "武器谱术语规则辅助降级为空: task_id=%s call_id=%s "
                "error_code=%s error_type=%s",
                request.call.task_id.value,
                request.call.call_id,
                error_code,
                type(exc).__name__,
            )
            return AuxiliaryGuidanceResult(
                call=request.call,
                guidance=(),
                outcome=AuxiliaryGuidanceOutcome.DEGRADED,
                error_code=error_code,
            )
        outcome = (
            AuxiliaryGuidanceOutcome.PROVIDED
            if guidance
            else AuxiliaryGuidanceOutcome.EMPTY
        )
        logger.info(
            "武器谱术语规则辅助完成: task_id=%s call_id=%s "
            "policy_id=%s guidance_count=%d card_ids=%s",
            request.call.task_id.value,
            request.call.call_id,
            request.policy.policy_id,
            len(guidance),
            ",".join(card_ids),
        )
        return AuxiliaryGuidanceResult(
            call=request.call,
            guidance=guidance,
            outcome=outcome,
        )

    @staticmethod
    def _legacy_query(request: AuxiliaryGuidanceRequest) -> str:
        field = request.field
        lines = [f"字段：{field.field_name}"]
        if field.field_description:
            lines.append(f"字段说明：{field.field_description}")
        if field.field_type == "TABLE":
            for column in field.columns:
                line = f"列：{column.field_name}"
                if column.field_description:
                    line += f"；说明：{column.field_description}"
                lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _legacy_to_guidance(
        chunks: tuple[TermsRuleChunk, ...],
        *,
        max_context_chars: int,
    ) -> tuple[AuxiliaryGuidance, ...]:
        remaining = max_context_chars
        guidance: list[AuxiliaryGuidance] = []
        seen_sources: set[str] = set()
        for chunk in sorted(chunks, key=lambda item: (item.rank, item.source_ref)):
            if chunk.source_ref in seen_sources or remaining <= 0:
                continue
            seen_sources.add(chunk.source_ref)
            text = chunk.text[:remaining]
            if not text:
                continue
            digest = hashlib.sha256(
                f"{chunk.source_ref}\x1f{chunk.rank}\x1f{text}".encode("utf-8")
            ).hexdigest()[:24]
            guidance.append(
                AuxiliaryGuidance(
                    guidance_id=f"terms-rule-{digest}",
                    text=text,
                )
            )
            remaining -= len(text)
        return tuple(guidance)

    def _load_column_compact(
        self,
        request: AuxiliaryGuidanceRequest,
    ) -> tuple[tuple[AuxiliaryGuidance, ...], tuple[str, ...]]:
        matches: list[_MatchedTermsRule] = []
        for target in self._targets(request):
            chunks = self._provider.search(
                self._target_query(target),
                top_n=request.policy.top_n,
            )
            matched = self._match_target(target, chunks)
            if matched is None:
                continue
            matches.append(matched)
        guidance = self._compact_guidance(
            tuple(matches),
            max_context_chars=request.policy.max_context_chars,
        )
        return guidance, tuple(item.rule.card_id for item in matches)

    @staticmethod
    def _targets(request: AuxiliaryGuidanceRequest) -> tuple[_TermsRuleTarget, ...]:
        field = request.field
        if field.field_type == "INPUT":
            return (
                _TermsRuleTarget(
                    field_name=field.field_name,
                    field_description=field.field_description,
                    order=1,
                ),
            )
        return tuple(
            _TermsRuleTarget(
                field_name=column.field_name,
                field_description=column.field_description,
                order=index,
            )
            for index, column in enumerate(field.columns, start=1)
        )

    @staticmethod
    def _target_query(target: _TermsRuleTarget) -> str:
        lines = [f"字段：{target.field_name}"]
        if target.field_description:
            lines.append(f"字段说明：{target.field_description}")
        lines.append("请返回与该字段精确对应的标准术语规则卡。")
        return "\n".join(lines)

    @classmethod
    def _match_target(
        cls,
        target: _TermsRuleTarget,
        chunks: tuple[TermsRuleChunk, ...],
    ) -> _MatchedTermsRule | None:
        candidates: list[tuple[int, int, str, _ParsedTermsRule]] = []
        for chunk in chunks:
            rule = cls._parse_rule(chunk)
            if rule is None:
                continue
            match_tier = cls._match_tier(target, rule)
            if match_tier is None:
                continue
            candidates.append((match_tier, rule.rank, rule.card_id, rule))
        if not candidates:
            return None
        _, _, _, rule = min(candidates)
        return _MatchedTermsRule(target=target, rule=rule)

    @staticmethod
    def _match_tier(
        target: _TermsRuleTarget,
        rule: _ParsedTermsRule,
    ) -> int | None:
        target_name = _normalize_term(target.field_name)
        exact_names = {
            _normalize_term(rule.standard_zh),
            _normalize_term(rule.standard_en),
        }
        if target_name and target_name in exact_names:
            return 0
        alias_names = {
            _normalize_term(rule.heading),
            *(_normalize_term(alias) for alias in rule.aliases),
        }
        if target_name and target_name in alias_names:
            return 1
        description = _normalize_term(target.field_description)
        semantic_names = {
            value
            for value in (*exact_names, *alias_names)
            if len(value) >= 2
        }
        if description and any(value in description for value in semantic_names):
            return 2
        return None

    @classmethod
    def _parse_rule(cls, chunk: TermsRuleChunk) -> _ParsedTermsRule | None:
        frontmatter = _parse_frontmatter(chunk.text)
        standard_zh = (
            frontmatter.get("standard_zh")
            or _extract_label(chunk.text, _RULE_FIELD_LABELS["standard_zh"])
        )
        standard_en = (
            frontmatter.get("standard_en")
            or _extract_label(chunk.text, _RULE_FIELD_LABELS["standard_en"])
        )
        if standard_en == "无":
            standard_en = ""
        card_id = frontmatter.get("card_id") or _card_id_from_text(
            chunk.source_ref,
            chunk.text,
        )
        if not card_id or not standard_zh:
            return None
        unit = (
            frontmatter.get("unit")
            or _extract_label(chunk.text, _RULE_FIELD_LABELS["unit"])
        )
        if unit == "无":
            unit = ""
        rules = _section_bullets(chunk.text, "抽取与标准化规则")
        return _ParsedTermsRule(
            source_ref=chunk.source_ref,
            rank=chunk.rank,
            card_id=card_id,
            heading=_extract_heading(chunk.text),
            standard_zh=standard_zh,
            standard_en=standard_en,
            unit=unit,
            output_format=_extract_label(
                chunk.text,
                _RULE_FIELD_LABELS["output_format"],
            ),
            definition=_collapse_markdown(_extract_section(chunk.text, "定义")),
            aliases=_section_bullets(
                chunk.text,
                "可命中的非标准表述 / 别名",
            ),
            special_rules=tuple(
                rule
                for rule in rules
                if not rule.startswith(_GENERIC_RULE_PREFIXES)
            ),
            standard_sample=_extract_label(
                chunk.text,
                _RULE_FIELD_LABELS["standard_sample"],
            ),
            source_sample=_extract_label(
                chunk.text,
                _RULE_FIELD_LABELS["source_sample"],
            ),
        )

    @classmethod
    def _compact_guidance(
        cls,
        matches: tuple[_MatchedTermsRule, ...],
        *,
        max_context_chars: int,
    ) -> tuple[AuxiliaryGuidance, ...]:
        if not matches:
            return ()
        ordered = tuple(
            sorted(
                matches,
                key=lambda item: (
                    item.target.order,
                    item.rule.rank,
                    item.rule.card_id,
                ),
            )
        )
        cores = tuple(cls._compact_core(item) for item in ordered)
        minimum_chars = sum(len(item) for item in cores)
        if minimum_chars > max_context_chars:
            raise WeaponryPortStateError(
                "terms_rule_context_budget_insufficient",
                "术语规则上下文预算不足以保留全部逐列核心合同",
            )
        optional = tuple(cls._compact_optional(item.rule) for item in ordered)
        remaining = max_context_chars - minimum_chars
        texts: list[str] = []
        for index, core in enumerate(cores):
            remaining_items = len(cores) - index
            fair_share = remaining // remaining_items
            appendix = optional[index][:fair_share]
            text = core + appendix
            texts.append(text)
            remaining -= len(appendix)
        guidance: list[AuxiliaryGuidance] = []
        for matched, text in zip(ordered, texts):
            target_digest = hashlib.sha256(
                matched.target.field_name.encode("utf-8")
            ).hexdigest()[:8]
            text_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
            guidance.append(
                AuxiliaryGuidance(
                    guidance_id=(
                        f"terms-rule-v2-{matched.rule.card_id}-"
                        f"{target_digest}-{text_digest}"
                    ),
                    text=text,
                )
            )
        return tuple(guidance)

    @staticmethod
    def _compact_core(matched: _MatchedTermsRule) -> str:
        target = matched.target
        rule = matched.rule
        output_format = rule.output_format or (
            f"[值] {rule.unit}" if rule.unit else "[字段值]"
        )
        lines = [
            "【列级术语合同】",
            f"目标：{target.field_name}",
            f"卡片：{rule.card_id}",
            f"字段：{rule.standard_zh}"
            + (f" / {rule.standard_en}" if rule.standard_en else ""),
            f"单位：{rule.unit or '无'}",
            f"格式：{output_format}",
        ]
        if rule.unit:
            lines.append(
                "约束：非空值须按格式；数字与单位间仅一个半角空格；"
                "禁止纯数字；删除数值千分位逗号（1,234→1234）；"
                "并列值逐项带单位并去重。异单位只可按本卡明确关系换算，"
                "禁止仅改单位名；无原文证据留空。规则不是事实来源。"
            )
        else:
            lines.append(
                "约束：非空值须按格式；无原文证据留空。"
                "规则不是事实来源。"
            )
        return "\n".join(lines)

    @staticmethod
    def _compact_optional(rule: _ParsedTermsRule) -> str:
        lines: list[str] = []
        if rule.definition:
            lines.append(f"定义：{rule.definition}")
        if rule.aliases:
            lines.append(f"别名：{'、'.join(rule.aliases)}")
        for special_rule in rule.special_rules:
            lines.append(f"特殊规则：{special_rule}")
        if rule.standard_sample and rule.standard_sample != "无":
            lines.append(f"标准样例：{rule.standard_sample}")
        if rule.source_sample and rule.source_sample != "无":
            lines.append(f"原文示例：{rule.source_sample}")
        return "\n" + "\n".join(lines) if lines else ""


def _normalize_term(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _parse_frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"\A---\s*\n(?P<body>.*?)\n---(?:\s*\n|$)", text, re.DOTALL)
    if match is None:
        return {}
    values: dict[str, str] = {}
    for line in match.group("body").splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        normalized = value.strip()
        if len(normalized) >= 2 and normalized[0] == normalized[-1] == '"':
            normalized = normalized[1:-1]
        values[key.strip()] = normalized
    return values


def _extract_label(text: str, label: str) -> str:
    match = re.search(
        rf"(?m)^-\s+\*\*{re.escape(label)}\*\*[：:]\s*(?P<value>.*?)\s*$",
        text,
    )
    return match.group("value").strip() if match is not None else ""


def _extract_heading(text: str) -> str:
    match = re.search(r"(?m)^#\s+(?P<heading>.+?)\s*$", text)
    return match.group("heading").strip() if match is not None else ""


def _extract_section(text: str, heading: str) -> str:
    match = re.search(
        rf"(?ms)^##\s+{re.escape(heading)}\s*\n(?P<body>.*?)(?=^##\s+|\Z)",
        text,
    )
    return match.group("body").strip() if match is not None else ""


def _section_bullets(text: str, heading: str) -> tuple[str, ...]:
    section = _extract_section(text, heading)
    return tuple(
        match.group("value").strip()
        for match in re.finditer(r"(?m)^-\s+(?P<value>.+?)\s*$", section)
        if match.group("value").strip()
    )


def _collapse_markdown(text: str) -> str:
    return " ".join(line.strip() for line in text.splitlines() if line.strip())


def _card_id_from_text(source_ref: str, text: str) -> str:
    for value in (source_ref, text):
        match = re.search(r"\bterm_rule_\d{4}\b", value)
        if match is not None:
            return match.group(0)
    return ""


__all__ = [
    "AnythingLLMReadOnlyTermsRuleProvider",
    "TermsRuleChunk",
    "TermsRuleGuidanceAdapter",
    "TermsRuleProviderProtocol",
]
