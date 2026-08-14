"""Weaponry Step Registry 的五类纯恢复矩阵定义。

这里只冻结分类规则，不读取数据库或供应商，也不授予重试权。后续 Recovery Policy 必须
基于持久 Observation 作出决定；在此之前，未知外部效果始终留在隔离态。
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from app.modules.tasks.domain import RecoveryClassification
from app.modules.weaponry.domain import WeaponryDomainValidationError


@dataclass(frozen=True, slots=True)
class WeaponryRecoveryMatrixDefinition:
    matrix_ref: str
    rules: Mapping[RecoveryClassification, str]

    def __post_init__(self) -> None:
        if not isinstance(self.matrix_ref, str) or not self.matrix_ref.strip():
            raise WeaponryDomainValidationError("matrix_ref 不能为空")
        normalized = dict(self.rules)
        if set(normalized) != set(RecoveryClassification):
            raise WeaponryDomainValidationError("恢复矩阵必须覆盖全部五类结论")
        if any(not isinstance(value, str) or not value.strip() for value in normalized.values()):
            raise WeaponryDomainValidationError("恢复矩阵规则说明不能为空")
        object.__setattr__(self, "rules", MappingProxyType(normalized))


def _matrix(matrix_ref: str, *, retry_safe: str, finalize: str, reconcile: str, defer: str, stale: str) -> WeaponryRecoveryMatrixDefinition:
    return WeaponryRecoveryMatrixDefinition(
        matrix_ref,
        {
            RecoveryClassification.RETRY_SAFE: retry_safe,
            RecoveryClassification.FINALIZE_FROM_CHECKPOINT: finalize,
            RecoveryClassification.RECONCILE_REQUIRED: reconcile,
            RecoveryClassification.DEFER: defer,
            RecoveryClassification.MARK_STALE: stale,
        },
    )


WEAPONRY_RECOVERY_MATRICES = MappingProxyType(
    {
        "deterministic.v1": _matrix("deterministic.v1", retry_safe="无成功检查点、冻结输入匹配且 latest 仍指向本 Task", finalize="结果摘要与引用完整且校验通过", reconcile="检查点损坏、输入身份冲突或出现未登记副作用", defer="同一 Recovery Case 的证据尚未收齐", stale="latest 已由更高世代合法 Task 取代"),
        "local_idempotent.v1": _matrix("local_idempotent.v1", retry_safe="意图未提交，或目标不存在且所有权确定", finalize="稳定键事实存在且 owner 与摘要匹配", reconcile="稳定键冲突、所有权未知或提交结果无法核验", defer="本地事实正由有效维护租约核验", stale="latest 已变化且旧事实不会被新 Task 复用"),
        "external_write_reconcile.v1": _matrix("external_write_reconcile.v1", retry_safe="审计明确 definitely_not_sent，且没有外部引用或 Creation Intent", finalize="稳定身份只探测到唯一效果，外部引用、owner 与结果摘要匹配", reconcile="请求可能已发送、结果未知、探测歧义或 owner 不明", defer="probe intent 已提交但 Observation 尚未返回或供应商暂不可用", stale="latest 已变化且旧效果已隔离或完成受控补偿"),
        "optional_degrade.v1": _matrix("optional_degrade.v1", retry_safe="审计明确 definitely_not_sent 且冻结 Profile 匹配", finalize="成功结果已保存，或完整上游检查点支持既有空结果降级", reconcile="外部结果未知且缺少完整上游检查点，或资源所有权不明", defer="可选效果正在探测且尚未超过恢复预算", stale="latest 已变化且可选效果不会被新 Task 复用"),
        "terminal.v1": _matrix("terminal.v1", retry_safe="终态意图未开始且全部前置 Step 可验证", finalize="终态已提交，或完整结果检查点允许新 Authority 单次 CAS 收敛", reconcile="终态与 latest、结果或资源事实冲突", defer="同一 Task 的终态事务结果正在只读核验", stale="latest 已由合法新 Task 取代且旧 Task 未提交终态"),
    }
)


def weaponry_recovery_matrix(matrix_ref: str) -> WeaponryRecoveryMatrixDefinition:
    try:
        return WEAPONRY_RECOVERY_MATRICES[matrix_ref]
    except (KeyError, TypeError) as exc:
        raise WeaponryDomainValidationError("Weaponry recovery matrix 未登记") from exc


__all__ = [
    "WEAPONRY_RECOVERY_MATRICES",
    "WeaponryRecoveryMatrixDefinition",
    "weaponry_recovery_matrix",
]
