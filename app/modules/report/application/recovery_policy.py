"""Report Step Registry 使用的五类恢复矩阵定义。

本文件冻结纯规则说明，并提供阶段 2-7 的 ``TaskRecoveryPolicyPort`` 实现。Policy 只读取
调用方传入的冻结 Task/Step/Observation，不访问数据库或供应商；自动 retry 仍严格限制为
尚未创建任何 Step 的现场。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from app.modules.tasks.domain import RecoveryClassification
from app.modules.tasks.application.recovery_policies import RegistryTaskRecoveryPolicy

from app.modules.report.domain import ReportDomainValidationError


@dataclass(frozen=True, slots=True)
class ReportRecoveryMatrixDefinition:
    matrix_ref: str
    rules: Mapping[RecoveryClassification, str]

    def __post_init__(self) -> None:
        if not isinstance(self.matrix_ref, str) or not self.matrix_ref.strip():
            raise ReportDomainValidationError("matrix_ref 不能为空")
        normalized = dict(self.rules)
        if set(normalized) != set(RecoveryClassification):
            raise ReportDomainValidationError("恢复矩阵必须覆盖全部五类结论")
        for classification, description in normalized.items():
            if not isinstance(classification, RecoveryClassification):
                raise ReportDomainValidationError("恢复矩阵包含未知分类")
            if not isinstance(description, str) or not description.strip():
                raise ReportDomainValidationError("恢复矩阵规则说明不能为空")
        object.__setattr__(self, "rules", MappingProxyType(normalized))


def _matrix(matrix_ref: str, *, retry_safe: str, finalize: str, reconcile: str, defer: str, stale: str) -> ReportRecoveryMatrixDefinition:
    return ReportRecoveryMatrixDefinition(
        matrix_ref,
        {
            RecoveryClassification.RETRY_SAFE: retry_safe,
            RecoveryClassification.FINALIZE_FROM_CHECKPOINT: finalize,
            RecoveryClassification.RECONCILE_REQUIRED: reconcile,
            RecoveryClassification.DEFER: defer,
            RecoveryClassification.MARK_STALE: stale,
        },
    )


REPORT_RECOVERY_MATRICES = MappingProxyType(
    {
        "deterministic.v1": _matrix(
            "deterministic.v1",
            retry_safe="无成功检查点、Profile 匹配且 latest 仍指向本 Task",
            finalize="结果摘要与引用完整且校验通过",
            reconcile="检查点损坏、输入身份冲突或出现未登记副作用",
            defer="同一 Recovery Case 的证据收集尚未完成",
            stale="latest 已由更高世代合法 Task 取代",
        ),
        "local_idempotent.v1": _matrix(
            "local_idempotent.v1",
            retry_safe="意图未提交，或目标不存在且没有未知所有权",
            finalize="稳定键事实存在且 owner、摘要和大小全部匹配",
            reconcile="稳定键 owner/摘要冲突，或提交结果未知且无法原子核验",
            defer="本地 Store 正由有效维护租约核验",
            stale="latest 已变化且目标事实不属于当前 Task/Attempt",
        ),
        "external_read.v1": _matrix(
            "external_read.v1",
            retry_safe="尚无完整内容检查点；再次读取只能用于首次冻结摘要",
            finalize="Artifact、source identity、SHA-256 与大小全部匹配",
            reconcile="同一来源与冻结摘要不一致，或 Artifact 所有权不明",
            defer="来源暂不可探测且没有证据证明内容漂移",
            stale="latest 已变化且旧 Artifact 不会被新 Task 复用",
        ),
        "external_write_reconcile.v1": _matrix(
            "external_write_reconcile.v1",
            retry_safe="持久证据明确 definitely_not_sent，且没有外部引用或创建意图",
            finalize="稳定身份探测到唯一效果，外部引用、owner 与摘要全部匹配",
            reconcile="请求可能已发送、结果未知、探测歧义、owner 不明或响应无法重建",
            defer="probe intent 已提交，但 Observation 未返回或供应商暂不可用",
            stale="latest 已变化且旧效果已隔离或完成受控补偿",
        ),
        "terminal.v1": _matrix(
            "terminal.v1",
            retry_safe="终态意图未开始，且全部前置 Step 都有可验证结果",
            finalize="终态已提交，或完整结果检查点允许新 Authority 单次 CAS 收敛",
            reconcile="终态与 latest/结果/Artifact 冲突，或前置核心 Step 未知",
            defer="同一 Task 的终态事务结果正在只读核验",
            stale="latest 已由合法新 Task 取代且旧 Task 未提交终态",
        ),
    }
)


def recovery_matrix(matrix_ref: str) -> ReportRecoveryMatrixDefinition:
    try:
        return REPORT_RECOVERY_MATRICES[matrix_ref]
    except (KeyError, TypeError) as exc:
        raise ReportDomainValidationError("Report recovery matrix 未登记") from exc


class ReportTaskRecoveryPolicy(RegistryTaskRecoveryPolicy):
    """Report Step Registry 的唯一过期 Attempt 分类策略。"""

    def __init__(self) -> None:
        # 局部导入避免 execution_steps 为加载静态矩阵反向依赖本模块。
        from .execution_steps import resolve_report_step

        super().__init__(
            task_type="report",
            policy_version="report-task-recovery.v1",
            resolve_step=resolve_report_step,
            finalization_step_key="artifact.publish",
            resumable_step=lambda step_key: step_key == "artifact.scope.begin",
        )


__all__ = [
    "REPORT_RECOVERY_MATRICES",
    "ReportRecoveryMatrixDefinition",
    "ReportTaskRecoveryPolicy",
    "recovery_matrix",
]
