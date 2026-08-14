"""Report 最终 Step Registry 与稳定 Step Key 解析规则。

注册表是 Report Runtime 唯一允许创建的业务 Step 集合。带序号的 Key 只接受从 1 开始的
十进制整数；未知 Key、前导零、零/负数或未登记后缀均失败关闭，避免恢复时把拼写错误
当作新步骤继续执行。
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

from app.modules.report.domain import ReportDomainValidationError


_SEQUENCE_PLACEHOLDERS = ("{source_sequence}", "{artifact_sequence}")


@dataclass(frozen=True, slots=True)
class ReportStepDefinition:
    key_pattern: str
    definition_version: int
    effect_kind: StepEffectKind
    replay_policy: StepReplayPolicy
    schema_ref: str
    recovery_matrix_ref: str
    success_result_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.key_pattern, str) or not self.key_pattern:
            raise ReportDomainValidationError("Report Step key_pattern 不能为空")
        if self.definition_version != 1:
            raise ReportDomainValidationError("Report Step definition_version 仅支持 1")
        if not isinstance(self.effect_kind, StepEffectKind):
            raise ReportDomainValidationError("Report Step effect_kind 无效")
        if not isinstance(self.replay_policy, StepReplayPolicy):
            raise ReportDomainValidationError("Report Step replay_policy 无效")
        for name in ("schema_ref", "recovery_matrix_ref", "success_result_ref"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ReportDomainValidationError(f"Report Step {name} 不能为空")

    def matches(self, step_key: str) -> bool:
        """匹配精确 Key 或一个无前导零的正整数序号 Key。"""

        if not isinstance(step_key, str):
            return False
        placeholders = tuple(
            token for token in _SEQUENCE_PLACEHOLDERS if token in self.key_pattern
        )
        if not placeholders:
            return step_key == self.key_pattern
        if len(placeholders) != 1 or self.key_pattern.count(placeholders[0]) != 1:
            # Registry 构造目前不会产生该形状；这里仍失败关闭，防止未来误登记的复合
            # 模式被宽松解释为可执行 Step。
            return False
        prefix, suffix = self.key_pattern.split(placeholders[0], 1)
        if not step_key.startswith(prefix) or not step_key.endswith(suffix):
            return False
        sequence_end = len(step_key) - len(suffix) if suffix else len(step_key)
        sequence = step_key[len(prefix):sequence_end]
        return bool(sequence) and sequence.isascii() and sequence.isdecimal() and sequence[0] != "0"

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
            raise ReportDomainValidationError("step_key 与 Report Step 定义不匹配")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ReportDomainValidationError("idempotency_key 不能为空")
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


REPORT_STEP_REGISTRY = (
    ReportStepDefinition("artifact.scope.begin", 1, StepEffectKind.LOCAL_WRITE, StepReplayPolicy.IDEMPOTENT_AFTER_PROBE, "local_artifact.v1", "local_idempotent.v1", "ReportArtifactScope"),
    ReportStepDefinition("source.download:{source_sequence}", 1, StepEffectKind.EXTERNAL_READ, StepReplayPolicy.IDEMPOTENT_AFTER_PROBE, "external_read_artifact.v1", "external_read.v1", "ReportArtifactRef/source"),
    ReportStepDefinition("document.prepare:{source_sequence}", 1, StepEffectKind.LOCAL_WRITE, StepReplayPolicy.IDEMPOTENT_AFTER_PROBE, "local_artifact.v1", "local_idempotent.v1", "ReportArtifactRef/rag_input[]+DocumentProcessingRef"),
    ReportStepDefinition("template.download", 1, StepEffectKind.EXTERNAL_READ, StepReplayPolicy.IDEMPOTENT_AFTER_PROBE, "external_read_artifact.v1", "external_read.v1", "ReportArtifactRef/template"),
    ReportStepDefinition("template.extract", 1, StepEffectKind.PURE, StepReplayPolicy.SAFE, "pure.v1", "deterministic.v1", "template_text_digest"),
    ReportStepDefinition("rag.session.open", 1, StepEffectKind.EXTERNAL_WRITE, StepReplayPolicy.RECONCILE_ONLY, "external_write.v1", "external_write_reconcile.v1", "workspace_ref+conversation_ref"),
    ReportStepDefinition("rag.document.upload:{artifact_sequence}", 1, StepEffectKind.EXTERNAL_WRITE, StepReplayPolicy.RECONCILE_ONLY, "external_write.v1", "external_write_reconcile.v1", "document_location+document_ref"),
    ReportStepDefinition("rag.document.bind:{artifact_sequence}", 1, StepEffectKind.EXTERNAL_WRITE, StepReplayPolicy.IDEMPOTENT_AFTER_PROBE, "external_write.v1", "external_write_reconcile.v1", "workspace_ref+document_location"),
    ReportStepDefinition("rag.generate", 1, StepEffectKind.EXTERNAL_WRITE, StepReplayPolicy.NEVER_AUTO, "external_write.v1", "external_write_reconcile.v1", "raw_response_digest+trace_ref"),
    ReportStepDefinition("interaction_audit.commit", 1, StepEffectKind.LOCAL_WRITE, StepReplayPolicy.IDEMPOTENT_AFTER_PROBE, "audit_write.v1", "local_idempotent.v1", "ReportAuditReceipt"),
    ReportStepDefinition("report.render", 1, StepEffectKind.PURE, StepReplayPolicy.SAFE, "pure.v1", "deterministic.v1", "ReportResult+html_digest"),
    ReportStepDefinition("artifact.publish", 1, StepEffectKind.LOCAL_WRITE, StepReplayPolicy.IDEMPOTENT_AFTER_PROBE, "local_artifact.v1", "local_idempotent.v1", "ReportArtifactRef/report_html"),
    ReportStepDefinition("terminal.commit", 1, StepEffectKind.LOCAL_WRITE, StepReplayPolicy.IDEMPOTENT_AFTER_PROBE, "terminal.v1", "terminal.v1", "TaskExecution terminal projection"),
)


def resolve_report_step(step_key: str) -> ReportStepDefinition:
    """按完整 Key 返回唯一注册定义；任何歧义或未知值都失败关闭。"""

    matches = tuple(item for item in REPORT_STEP_REGISTRY if item.matches(step_key))
    if len(matches) != 1:
        raise ReportDomainValidationError("Report step_key 未登记或匹配不唯一")
    return matches[0]


__all__ = ["REPORT_STEP_REGISTRY", "ReportStepDefinition", "resolve_report_step"]
