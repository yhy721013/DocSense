"""分类节点变更恢复的终态收口与隔离协作器。

Finalizer 是恢复流程唯一可写入终态或 ``recovery_required`` 隔离状态的协作器。它只依赖
Repository Port；终态提交会携带最新观察、lease、fencing 与 preparation claim，任何异常或
契约拒绝都保留现场，而不伪造成功。
"""

from __future__ import annotations

import logging

from app.modules.reassign.domain import (
    ReassignmentOperationStatus,
    ReassignmentStepName,
    ReassignmentTerminalEvidence,
    ReassignmentTerminalEvidenceKind,
)
from app.modules.reassign.ports import (
    ReassignmentOperationRecord,
    ReassignmentOperationTransition,
    ReassignmentRecoveryFinalizationRequest,
    ReassignmentRecoveryObservationRecord,
    ReassignmentRepositoryPort,
)

from .recovery_types import (
    RecoverReassignmentCommand,
    ReassignmentRecoveryResult,
    ReassignmentRecoveryResultCategory,
    RecoveryLeaseContext,
    actor_marker,
)


logger = logging.getLogger(__name__)


class ReassignmentRecoveryFinalizer:
    """执行恢复终态提交、隔离与最小结果构造。"""

    def __init__(self, repository: ReassignmentRepositoryPort) -> None:
        if not isinstance(repository, ReassignmentRepositoryPort):
            raise TypeError("repository 必须实现 ReassignmentRepositoryPort")
        self._repository = repository

    def result(
        self,
        command: RecoverReassignmentCommand,
        category: ReassignmentRecoveryResultCategory,
    ) -> ReassignmentRecoveryResult:
        """构造内部结果，明确禁止向公开层泄漏 lease、fencing 或文档信息。"""

        return ReassignmentRecoveryResult(
            operation_id=command.operation_id,
            category=category,
        )

    def finalize(
        self,
        context: RecoveryLeaseContext,
        record: ReassignmentOperationRecord,
        command: RecoverReassignmentCommand,
        observation: ReassignmentRecoveryObservationRecord,
        *,
        next_status: ReassignmentOperationStatus,
        current_step: ReassignmentStepName,
        evidence_kind: ReassignmentTerminalEvidenceKind,
        category: ReassignmentRecoveryResultCategory,
        error_code: str | None = None,
    ) -> ReassignmentRecoveryResult:
        """调用带观察事实门禁的专用终态入口；失败时转入恢复隔离。"""

        try:
            with self._repository.unit_of_work() as unit_of_work:
                result = unit_of_work.finalize_recovery_operation(
                    ReassignmentRecoveryFinalizationRequest(
                        lease=context.lease,
                        observation=observation,
                        next_status=next_status,
                        current_step=current_step,
                        terminal_evidence=ReassignmentTerminalEvidence(evidence_kind),
                        preparation_claim=context.preparation_claim,
                        error_code=error_code,
                        error_summary=None,
                    )
                )
        except Exception as exc:
            logger.error(
                "分类节点变更恢复终态提交异常: operation_id=%s status=%s error_type=%s",
                record.operation.operation_id,
                next_status.value,
                type(exc).__name__,
            )
            reconciled = self._reconcile_expected_terminal(
                command,
                expected_status=next_status,
                category=category,
            )
            if reconciled is not None:
                return reconciled
            return self.isolate(
                context,
                record,
                command,
                current_step=current_step,
                error_code="recovery_terminal_write_failed",
            )
        if isinstance(result, ReassignmentOperationRecord):
            logger.info(
                "分类节点变更恢复已收口: operation_id=%s status=%s actor_marker=%s",
                record.operation.operation_id,
                result.operation.status.value,
                actor_marker(command.actor),
            )
            return self.result(command, category)
        reconciled = self._reconcile_expected_terminal(
            command,
            expected_status=next_status,
            category=category,
        )
        if reconciled is not None:
            return reconciled
        return self.isolate(
            context,
            record,
            command,
            current_step=current_step,
            error_code="recovery_terminal_write_rejected",
        )

    def _reconcile_expected_terminal(
        self,
        command: RecoverReassignmentCommand,
        *,
        expected_status: ReassignmentOperationStatus,
        category: ReassignmentRecoveryResultCategory,
    ) -> ReassignmentRecoveryResult | None:
        """终态确认不确定时重读权威事实，识别已经完成的原子提交。"""

        current = self._read_current_operation(command.operation_id)
        if (
            current is not None
            and current.operation.status is expected_status
        ):
            logger.warning(
                "分类节点变更恢复提交确认异常但持久化终态已收口: "
                "operation_id=%s status=%s actor_marker=%s",
                command.operation_id,
                expected_status.value,
                actor_marker(command.actor),
            )
            return self.result(command, category)
        return None

    def isolate(
        self,
        context: RecoveryLeaseContext,
        record: ReassignmentOperationRecord,
        command: RecoverReassignmentCommand,
        *,
        current_step: ReassignmentStepName,
        error_code: str,
    ) -> ReassignmentRecoveryResult:
        """保留无法证明一致的现场；已经隔离时不覆盖原始诊断状态。"""

        current = self._read_current_operation(record.operation.operation_id)
        if (
            current is not None
            and current.operation.status
            is not ReassignmentOperationStatus.RECOVERY_REQUIRED
        ):
            try:
                with self._repository.unit_of_work() as unit_of_work:
                    result = unit_of_work.transition_operation(
                        ReassignmentOperationTransition(
                            lease=context.lease,
                            next_status=ReassignmentOperationStatus.RECOVERY_REQUIRED,
                            current_step=current_step,
                            error_code=error_code,
                            recovery_authorized=True,
                        )
                    )
            except Exception as exc:
                logger.error(
                    "分类节点变更恢复无法写入隔离状态: operation_id=%s error_type=%s",
                    record.operation.operation_id,
                    type(exc).__name__,
                )
            else:
                if not isinstance(result, ReassignmentOperationRecord):
                    logger.error(
                        "分类节点变更恢复隔离状态被拒绝: operation_id=%s outcome=%s",
                        record.operation.operation_id,
                        getattr(result, "value", "invalid_result"),
                    )
        logger.warning(
            "分类节点变更恢复保留待人工处理现场: operation_id=%s step=%s error_code=%s "
            "actor_marker=%s",
            record.operation.operation_id,
            current_step.value,
            error_code,
            actor_marker(command.actor),
        )
        return self.result(command, ReassignmentRecoveryResultCategory.RECOVERY_REQUIRED)

    def _read_current_operation(
        self,
        operation_id: str,
    ) -> ReassignmentOperationRecord | None:
        """隔离前短事务重读 Operation；读取失败时保持原现场而非猜测状态。"""

        try:
            with self._repository.unit_of_work(read_only=True) as unit_of_work:
                current = unit_of_work.get_operation(operation_id)
        except Exception as exc:
            logger.error(
                "分类节点变更恢复隔离前读取 Operation 失败: "
                "operation_id=%s error_type=%s",
                operation_id,
                type(exc).__name__,
            )
            return None
        if current is not None and not isinstance(current, ReassignmentOperationRecord):
            logger.error(
                "分类节点变更恢复隔离前读取 Operation 返回契约错误: "
                "operation_id=%s result_type=%s",
                operation_id,
                type(current).__name__,
            )
            return None
        return current


__all__ = ["ReassignmentRecoveryFinalizer"]
