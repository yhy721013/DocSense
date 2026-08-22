"""Analysis v2 唯一允许创建的 17 类业务 Step Registry。"""

from __future__ import annotations

from dataclasses import dataclass

from app.modules.analysis.domain.errors import AnalysisContractError
from app.modules.tasks.domain import (
    StepEffectKind,
    StepReplayPolicy,
    TaskId,
    TaskStep,
    TaskStepState,
)


_ATTEMPT_TOKENS = (
    "{attempt_number}",
)


def _matches_pattern(pattern: str, value: str) -> bool:
    tokens = tuple(token for token in _ATTEMPT_TOKENS if token in pattern)
    if not tokens:
        return value == pattern
    if len(tokens) != 1 or pattern.count(tokens[0]) != 1:
        return False
    prefix, suffix = pattern.split(tokens[0], 1)
    if not value.startswith(prefix) or not value.endswith(suffix):
        return False
    end = len(value) - len(suffix) if suffix else len(value)
    sequence = value[len(prefix):end]
    return (
        bool(sequence)
        and sequence.isascii()
        and sequence.isdecimal()
        and sequence[0] != "0"
    )


@dataclass(frozen=True, slots=True)
class AnalysisStepDefinition:
    key_pattern: str
    definition_version: int
    effect_kind: StepEffectKind
    replay_policy: StepReplayPolicy
    schema_ref: str
    recovery_matrix_ref: str
    success_result_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.key_pattern, str) or not self.key_pattern:
            raise AnalysisContractError("Analysis Step key_pattern 不能为空")
        if self.definition_version != 1:
            raise AnalysisContractError("Analysis Step definition_version 仅支持 1")
        if not isinstance(self.effect_kind, StepEffectKind):
            raise AnalysisContractError("Analysis Step effect_kind 无效")
        if not isinstance(self.replay_policy, StepReplayPolicy):
            raise AnalysisContractError("Analysis Step replay_policy 无效")
        for name in ("schema_ref", "recovery_matrix_ref", "success_result_ref"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise AnalysisContractError(f"Analysis Step {name} 不能为空")

    def matches(self, step_key: str) -> bool:
        return isinstance(step_key, str) and _matches_pattern(self.key_pattern, step_key)

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
            raise AnalysisContractError("step_key 与 Analysis Step 定义不匹配")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise AnalysisContractError("idempotency_key 不能为空")
        return TaskStep(
            task_id=task_id,
            step_key=step_key,
            definition_version=1,
            effect_kind=self.effect_kind,
            replay_policy=self.replay_policy,
            state=TaskStepState.PENDING,
            current_step_attempt_no=0,
            idempotency_key=idempotency_key.strip(),
            checkpoint=None,
            row_version=0,
        )


ANALYSIS_STEP_REGISTRY = (
    AnalysisStepDefinition("source.download", 1, StepEffectKind.EXTERNAL_READ, StepReplayPolicy.IDEMPOTENT_AFTER_PROBE, "external_read_artifact.v1", "external_read.v1", "source_artifact_ref"),
    AnalysisStepDefinition("document.prepare", 1, StepEffectKind.LOCAL_WRITE, StepReplayPolicy.IDEMPOTENT_AFTER_PROBE, "local_artifact.v1", "local_idempotent.v1", "PreparedAnalysisDocument+DocumentProcessingRef"),
    AnalysisStepDefinition("recall.reserve", 1, StepEffectKind.LOCAL_WRITE, StepReplayPolicy.IDEMPOTENT_AFTER_PROBE, "audit_write.v1", "local_idempotent.v1", "AnalysisRecallAuditReceipt"),
    AnalysisStepDefinition("rag.session.open", 1, StepEffectKind.EXTERNAL_WRITE, StepReplayPolicy.RECONCILE_ONLY, "external_write.v1", "external_write_reconcile.v1", "AnalysisRagSessionRef/context+conversation"),
    AnalysisStepDefinition("rag.document.upload", 1, StepEffectKind.EXTERNAL_WRITE, StepReplayPolicy.RECONCILE_ONLY, "external_write.v1", "external_write_reconcile.v1", "document_ref+document_location+content_sha256"),
    AnalysisStepDefinition("rag.document.bind", 1, StepEffectKind.EXTERNAL_WRITE, StepReplayPolicy.IDEMPOTENT_AFTER_PROBE, "external_write.v1", "external_write_reconcile.v1", "AnalysisRagSessionRef/document_bound"),
    AnalysisStepDefinition("classification.execute:{attempt_number}", 1, StepEffectKind.EXTERNAL_WRITE, StepReplayPolicy.NEVER_AUTO, "external_write.v1", "external_write_reconcile.v1", "AnalysisInteractionAttempt/classification"),
    AnalysisStepDefinition("identity.reselect", 1, StepEffectKind.EXTERNAL_WRITE, StepReplayPolicy.NEVER_AUTO, "external_write.v1", "optional_degrade.v1", "AnalysisInteractionAttempt/identity_reselect"),
    AnalysisStepDefinition("extraction.execute:{attempt_number}", 1, StepEffectKind.EXTERNAL_WRITE, StepReplayPolicy.NEVER_AUTO, "external_write.v1", "external_write_reconcile.v1", "AnalysisInteractionAttempt/extraction"),
    AnalysisStepDefinition("combined.execute:{attempt_number}", 1, StepEffectKind.EXTERNAL_WRITE, StepReplayPolicy.NEVER_AUTO, "external_write.v1", "external_write_reconcile.v1", "AnalysisInteractionAttempt/combined"),
    AnalysisStepDefinition("recall.finalize", 1, StepEffectKind.LOCAL_WRITE, StepReplayPolicy.IDEMPOTENT_AFTER_PROBE, "audit_write.v1", "local_idempotent.v1", "finalized AnalysisRecallAuditReceipt"),
    AnalysisStepDefinition("interaction_audit.commit", 1, StepEffectKind.LOCAL_WRITE, StepReplayPolicy.IDEMPOTENT_AFTER_PROBE, "audit_write.v1", "local_idempotent.v1", "AnalysisInteractionAuditReceipt"),
    AnalysisStepDefinition("result.map", 1, StepEffectKind.PURE, StepReplayPolicy.SAFE, "pure.v1", "deterministic.v1", "mapped_result_digest"),
    AnalysisStepDefinition("knowledge.workspace.ensure", 1, StepEffectKind.EXTERNAL_WRITE, StepReplayPolicy.IDEMPOTENT_AFTER_PROBE, "external_write.v1", "external_write_reconcile.v1", "permanent_workspace_ref"),
    AnalysisStepDefinition("knowledge.document.bind", 1, StepEffectKind.EXTERNAL_WRITE, StepReplayPolicy.IDEMPOTENT_AFTER_PROBE, "external_write.v1", "external_write_reconcile.v1", "AnalysisKnowledgeWriteResult/external_ref"),
    AnalysisStepDefinition("translation.execute", 1, StepEffectKind.EXTERNAL_WRITE, StepReplayPolicy.NEVER_AUTO, "external_write.v1", "optional_degrade.v1", "translation_result_digest+degrade_decision"),
    AnalysisStepDefinition("result.snapshot", 1, StepEffectKind.LOCAL_WRITE, StepReplayPolicy.IDEMPOTENT_AFTER_PROBE, "result_snapshot.v1", "local_idempotent.v1", "AnalysisResultSnapshot"),
    AnalysisStepDefinition("terminal.commit", 1, StepEffectKind.LOCAL_WRITE, StepReplayPolicy.IDEMPOTENT_AFTER_PROBE, "terminal.v1", "terminal.v1", "TaskExecution terminal projection"),
)


def resolve_analysis_step(step_key: str) -> AnalysisStepDefinition:
    matches = tuple(item for item in ANALYSIS_STEP_REGISTRY if item.matches(step_key))
    if len(matches) != 1:
        raise AnalysisContractError("Analysis step_key 未登记或匹配不唯一")
    return matches[0]


__all__ = [
    "ANALYSIS_STEP_REGISTRY",
    "AnalysisStepDefinition",
    "resolve_analysis_step",
]
