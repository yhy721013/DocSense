"""Weaponry v2 唯一允许执行的业务 Step Registry。

动态序号必须是无前导零的正整数；``call_id`` 只接受既有内部稳定标识字符。未知、
歧义或格式不合法的 Step Key 一律失败关闭，不能由普通 Worker 临时创造步骤。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.modules.tasks.domain import (
    StepEffectKind,
    StepReplayPolicy,
    TaskId,
    TaskStep,
    TaskStepState,
)
from app.modules.weaponry.domain import WeaponryDomainValidationError


_PLACEHOLDER_VALIDATORS = {
    "{field_sequence}": lambda value: _is_positive_sequence(value),
    "{document_sequence}": lambda value: _is_positive_sequence(value),
    "{item_sequence}": lambda value: _is_positive_sequence(value),
    "{model_attempt}": lambda value: _is_positive_sequence(value),
    "{call_id}": lambda value: _is_call_id(value),
}


def _is_positive_sequence(value: str) -> bool:
    """只接受 ASCII、无前导零的正整数，避免 Unicode 数字产生歧义。"""

    return bool(value) and value[0] in "123456789" and all(
        character in "0123456789" for character in value
    )


def _is_call_id(value: str) -> bool:
    """校验冻结的内部 call_id 字符集，不引入正则运行时依赖。"""

    allowed_punctuation = "._:-"
    return 1 <= len(value) <= 512 and all(
        character.isascii()
        and (character.isalnum() or character in allowed_punctuation)
        for character in value
    )


def _parse_key_pattern(key_pattern: str) -> tuple[tuple[str, str], ...]:
    """将模板解析为 ``(固定前缀, 占位符)`` 段；未知或破损模板失败关闭。"""

    cursor = 0
    segments: list[tuple[str, str]] = []
    while cursor < len(key_pattern):
        opening = key_pattern.find("{", cursor)
        if opening < 0:
            segments.append((key_pattern[cursor:], ""))
            break
        closing = key_pattern.find("}", opening + 1)
        if closing < 0 or "{" in key_pattern[opening + 1 : closing]:
            raise WeaponryDomainValidationError("Weaponry Step 占位符格式不合法")
        token = key_pattern[opening : closing + 1]
        if token not in _PLACEHOLDER_VALIDATORS:
            raise WeaponryDomainValidationError(
                f"Weaponry Step 包含未登记占位符: {token}"
            )
        segments.append((key_pattern[cursor:opening], token))
        cursor = closing + 1
    if not segments:
        segments.append((key_pattern, ""))
    if any("}" in literal or "{" in literal for literal, _token in segments):
        raise WeaponryDomainValidationError("Weaponry Step 占位符格式不合法")
    return tuple(segments)


def _matches_key_pattern(key_pattern: str, step_key: str) -> bool:
    """按冻结模板逐段匹配；不接受空动态值、前导零或多余尾部。"""

    segments = _parse_key_pattern(key_pattern)
    cursor = 0
    for index, (literal, token) in enumerate(segments):
        if not step_key.startswith(literal, cursor):
            return False
        cursor += len(literal)
        if not token:
            continue
        next_literal = segments[index + 1][0] if index + 1 < len(segments) else ""
        if next_literal:
            boundary = step_key.find(next_literal, cursor)
            if boundary < 0:
                return False
        else:
            boundary = len(step_key)
        value = step_key[cursor:boundary]
        if not _PLACEHOLDER_VALIDATORS[token](value):
            return False
        cursor = boundary
    return cursor == len(step_key)


@dataclass(frozen=True, slots=True)
class WeaponryStepDefinition:
    key_pattern: str
    definition_version: int
    effect_kind: StepEffectKind
    replay_policy: StepReplayPolicy
    schema_ref: str
    recovery_matrix_ref: str
    success_result_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.key_pattern, str) or not self.key_pattern:
            raise WeaponryDomainValidationError("Weaponry Step key_pattern 不能为空")
        if self.definition_version != 1:
            raise WeaponryDomainValidationError("Weaponry Step definition_version 仅支持 1")
        if not isinstance(self.effect_kind, StepEffectKind):
            raise WeaponryDomainValidationError("Weaponry Step effect_kind 无效")
        if not isinstance(self.replay_policy, StepReplayPolicy):
            raise WeaponryDomainValidationError("Weaponry Step replay_policy 无效")
        for name in ("schema_ref", "recovery_matrix_ref", "success_result_ref"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise WeaponryDomainValidationError(f"Weaponry Step {name} 不能为空")
        _parse_key_pattern(self.key_pattern)

    def matches(self, step_key: str) -> bool:
        return isinstance(step_key, str) and _matches_key_pattern(
            self.key_pattern,
            step_key,
        )

    def new_step(
        self,
        *,
        task_id: TaskId,
        step_key: str,
        idempotency_key: str,
    ) -> TaskStep:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not self.matches(step_key):
            raise WeaponryDomainValidationError("step_key 与 Weaponry Step 定义不匹配")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise WeaponryDomainValidationError("idempotency_key 不能为空")
        return TaskStep(
            task_id=task_id,
            step_key=step_key,
            definition_version=self.definition_version,
            effect_kind=self.effect_kind,
            replay_policy=self.replay_policy,
            state=TaskStepState.PENDING,
            current_step_attempt_no=0,
            idempotency_key=idempotency_key.strip(),
            checkpoint=None,
            row_version=0,
        )


WEAPONRY_STEP_REGISTRY = (
    WeaponryStepDefinition("resource.record.begin", 1, StepEffectKind.LOCAL_WRITE, StepReplayPolicy.IDEMPOTENT_AFTER_PROBE, "local_artifact.v1", "local_idempotent.v1", "WeaponryResourceRecord"),
    WeaponryStepDefinition("document_scope.load", 1, StepEffectKind.PURE, StepReplayPolicy.SAFE, "pure.v1", "deterministic.v1", "validated_document_scope_digest"),
    WeaponryStepDefinition("rag.workspace.create", 1, StepEffectKind.EXTERNAL_WRITE, StepReplayPolicy.RECONCILE_ONLY, "external_write.v1", "external_write_reconcile.v1", "TargetEvidenceScope.scope_ref+creation_intent_ref"),
    WeaponryStepDefinition("rag.document.bind:{document_sequence}", 1, StepEffectKind.EXTERNAL_WRITE, StepReplayPolicy.IDEMPOTENT_AFTER_PROBE, "external_write.v1", "external_write_reconcile.v1", "document_binding_resource_ref"),
    WeaponryStepDefinition("auxiliary_guidance.load:{field_sequence}", 1, StepEffectKind.EXTERNAL_WRITE, StepReplayPolicy.NEVER_AUTO, "external_write.v1", "external_write_reconcile.v1", "WeaponryCallAuditReceipt+guidance_digest"),
    WeaponryStepDefinition("retrieval.execute:{field_sequence}", 1, StepEffectKind.EXTERNAL_WRITE, StepReplayPolicy.NEVER_AUTO, "external_write.v1", "external_write_reconcile.v1", "WeaponryCallAuditReceipt+TargetEvidenceSearchResult"),
    WeaponryStepDefinition("evidence.select:{field_sequence}", 1, StepEffectKind.PURE, StepReplayPolicy.SAFE, "pure.v1", "deterministic.v1", "selected_evidence_digest"),
    WeaponryStepDefinition("field_model.execute:{field_sequence}:{document_sequence}:{model_attempt}", 1, StepEffectKind.EXTERNAL_WRITE, StepReplayPolicy.NEVER_AUTO, "external_write.v1", "external_write_reconcile.v1", "WeaponryCallAuditReceipt+ExtractionAnswer+workspace/thread refs"),
    WeaponryStepDefinition("translation.execute:{field_sequence}:{document_sequence}:{item_sequence}", 1, StepEffectKind.EXTERNAL_WRITE, StepReplayPolicy.NEVER_AUTO, "external_write.v1", "optional_degrade.v1", "WeaponryCallAuditReceipt+translation_digest"),
    WeaponryStepDefinition("interaction_audit.commit:{call_id}", 1, StepEffectKind.LOCAL_WRITE, StepReplayPolicy.IDEMPOTENT_AFTER_PROBE, "audit_write.v1", "local_idempotent.v1", "WeaponryCallAuditReceipt"),
    WeaponryStepDefinition("result.map", 1, StepEffectKind.PURE, StepReplayPolicy.SAFE, "pure.v1", "deterministic.v1", "WeaponryResult"),
    WeaponryStepDefinition("terminal.commit", 1, StepEffectKind.LOCAL_WRITE, StepReplayPolicy.IDEMPOTENT_AFTER_PROBE, "terminal.v1", "terminal.v1", "TaskExecution terminal projection"),
)


def resolve_weaponry_step(step_key: str) -> WeaponryStepDefinition:
    matches = tuple(item for item in WEAPONRY_STEP_REGISTRY if item.matches(step_key))
    if len(matches) != 1:
        raise WeaponryDomainValidationError("Weaponry step_key 未登记或匹配不唯一")
    return matches[0]


__all__ = [
    "WEAPONRY_STEP_REGISTRY",
    "WeaponryStepDefinition",
    "resolve_weaponry_step",
]
