"""Analysis v2 的恢复矩阵和纯过期 Attempt 分类策略。

Analysis 持久任务类型继续使用 ``file``。本模块不访问数据库、文件、RAG、Knowledge 或
Translation；未知供应商效果、Profile 漂移和未登记 Step 均由通用严格骨架隔离。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from app.modules.analysis.domain.errors import AnalysisContractError
from app.modules.tasks.application.recovery_policies import RegistryTaskRecoveryPolicy
from app.modules.tasks.domain import RecoveryClassification


@dataclass(frozen=True, slots=True)
class AnalysisRecoveryMatrixDefinition:
    matrix_ref: str
    rules: Mapping[RecoveryClassification, str]

    def __post_init__(self) -> None:
        if not isinstance(self.matrix_ref, str) or not self.matrix_ref.strip():
            raise AnalysisContractError("matrix_ref 不能为空")
        normalized = dict(self.rules)
        if set(normalized) != set(RecoveryClassification):
            raise AnalysisContractError("恢复矩阵必须覆盖全部五类结论")
        if any(
            not isinstance(value, str) or not value.strip()
            for value in normalized.values()
        ):
            raise AnalysisContractError("恢复矩阵规则说明不能为空")
        object.__setattr__(self, "rules", MappingProxyType(normalized))


def _matrix(
    matrix_ref: str,
    *,
    retry_safe: str,
    finalize: str,
    reconcile: str,
    defer: str,
    stale: str,
) -> AnalysisRecoveryMatrixDefinition:
    return AnalysisRecoveryMatrixDefinition(
        matrix_ref,
        {
            RecoveryClassification.RETRY_SAFE: retry_safe,
            RecoveryClassification.FINALIZE_FROM_CHECKPOINT: finalize,
            RecoveryClassification.RECONCILE_REQUIRED: reconcile,
            RecoveryClassification.DEFER: defer,
            RecoveryClassification.MARK_STALE: stale,
        },
    )


ANALYSIS_RECOVERY_MATRICES = MappingProxyType(
    {
        "deterministic.v1": _matrix(
            "deterministic.v1",
            retry_safe="尚无 Step Intent 且冻结输入/Profile 匹配",
            finalize="结果摘要与引用完整且校验通过",
            reconcile="检查点损坏、输入身份冲突或未登记副作用",
            defer="同一 Case 的本地证据尚未收齐",
            stale="latest 已合法变化且旧现场完成隔离",
        ),
        "local_idempotent.v1": _matrix(
            "local_idempotent.v1",
            retry_safe="意图未提交，或任务自有可逆现场已清理并复核",
            finalize="稳定本地事实、owner 和摘要完全匹配",
            reconcile="本地事实冲突、所有权未知或提交结果无法核验",
            defer="本地 Store 正由有效维护租约核验",
            stale="latest 已变化且旧事实不被新 Task 复用",
        ),
        "external_read.v1": _matrix(
            "external_read.v1",
            retry_safe="尚未冻结内容且没有持久 Artifact",
            finalize="Artifact、source identity、SHA-256 和 size 全部匹配",
            reconcile="来源摘要漂移、Artifact 缺失或所有权不明",
            defer="来源暂不可读且没有漂移证据",
            stale="latest 已变化且旧 Artifact 已隔离",
        ),
        "external_write_reconcile.v1": _matrix(
            "external_write_reconcile.v1",
            retry_safe="审计明确 definitely_not_sent，且没有外部引用或创建意图",
            finalize="稳定身份只对应唯一效果，owner 与结果摘要匹配",
            reconcile="请求可能已发送、探测歧义、owner 不明或响应无法重建",
            defer="probe Intent 已提交但 Observation 尚未返回",
            stale="latest 已变化且旧效果已隔离或受控补偿",
        ),
        "optional_degrade.v1": _matrix(
            "optional_degrade.v1",
            retry_safe="审计明确 definitely_not_sent 且冻结 Profile 匹配",
            finalize="成功结果已保存或上游检查点支持既有降级",
            reconcile="可选外部结果未知且上游检查点不完整",
            defer="可选效果正在进行有界探测",
            stale="latest 已变化且可选效果不会污染新 Task",
        ),
        "terminal.v1": _matrix(
            "terminal.v1",
            retry_safe="终态 Intent 未开始且全部前置 Step 可验证",
            finalize="完整结果检查点允许 Recovery Authority 原子收敛",
            reconcile="终态与 latest、结果快照或 Callback 资格冲突",
            defer="同一 Task 终态事务结果正在只读核验",
            stale="latest 已由合法新 Task 取代且旧终态未提交",
        ),
    }
)


def analysis_recovery_matrix(matrix_ref: str) -> AnalysisRecoveryMatrixDefinition:
    try:
        return ANALYSIS_RECOVERY_MATRICES[matrix_ref]
    except (KeyError, TypeError) as exc:
        raise AnalysisContractError("Analysis recovery matrix 未登记") from exc


class AnalysisTaskRecoveryPolicy(RegistryTaskRecoveryPolicy):
    """Analysis/file Step Registry 的唯一过期 Attempt 分类策略。"""

    def __init__(self) -> None:
        from .execution_steps import resolve_analysis_step

        super().__init__(
            task_type="file",
            policy_version="analysis-task-recovery.v1",
            resolve_step=resolve_analysis_step,
            finalization_step_key="result.snapshot",
            resumable_step=lambda step_key: step_key
            in {"source.download", "document.prepare"},
        )


__all__ = [
    "ANALYSIS_RECOVERY_MATRICES",
    "AnalysisRecoveryMatrixDefinition",
    "AnalysisTaskRecoveryPolicy",
    "analysis_recovery_matrix",
]
