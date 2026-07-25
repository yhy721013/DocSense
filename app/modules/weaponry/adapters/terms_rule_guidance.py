"""可整体移除的 AnythingLLM 术语规则辅助 Adapter。"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.integrations.anythingllm import AnythingLLMTransportError
from app.modules.weaponry.domain import (
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
        if request.policy.policy_id != AUXILIARY_GUIDANCE_TERMS_RULES_V1:
            raise WeaponryPortStateError(
                "auxiliary_policy_adapter_mismatch",
                "术语规则 Adapter 只能执行 terms-rules-v1 策略",
            )
        if request.policy.catalog_fingerprint != self._catalog_fingerprint:
            raise WeaponryPortStateError(
                "auxiliary_catalog_fingerprint_mismatch",
                "术语目录指纹与 execution 快照不一致",
            )
        query = self._query(request)
        try:
            chunks = self._provider.search(query, top_n=request.policy.top_n)
            guidance = self._to_guidance(
                chunks,
                max_context_chars=request.policy.max_context_chars,
            )
        except Exception as exc:
            error_code = (
                exc.code
                if isinstance(exc, AnythingLLMTransportError)
                else "terms_rule_provider_failed"
            )
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
            "武器谱术语规则辅助完成: task_id=%s call_id=%s guidance_count=%d",
            request.call.task_id.value,
            request.call.call_id,
            len(guidance),
        )
        return AuxiliaryGuidanceResult(
            call=request.call,
            guidance=guidance,
            outcome=outcome,
        )

    @staticmethod
    def _query(request: AuxiliaryGuidanceRequest) -> str:
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
    def _to_guidance(
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


__all__ = [
    "AnythingLLMReadOnlyTermsRuleProvider",
    "TermsRuleChunk",
    "TermsRuleGuidanceAdapter",
    "TermsRuleProviderProtocol",
]
