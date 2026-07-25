"""分类节点变更恢复的检查点收敛协作器。

本协作器拥有前向步骤探测事实收敛、workspace 准备检查点收敛、补偿检查点收敛及纯领域补偿
决策。所有写操作都带当前 lease/fencing，并分别在短 Unit of Work 中提交；它不依赖
Knowledge Port 或 Client Factory，因此不会在事务内发起网络 I/O。
"""

from __future__ import annotations

import logging

from app.modules.reassign.domain import (
    ReassignmentBindingState,
    ReassignmentCompensationDecision,
    ReassignmentCompensationFacts,
    ReassignmentMutationOutcome,
    ReassignmentStepName,
    ReassignmentStepState,
    decide_compensation,
)
from app.modules.reassign.ports import (
    ReassignmentLocalCommitState,
    ReassignmentOperationRecord,
    ReassignmentRepositoryPort,
    ReassignmentStepCompletion,
    ReassignmentStepRecord,
    ReassignmentWorkspaceOwnership,
    ReassignmentWorkspacePreparationFactRequest,
)

from .recovery_types import (
    CompensationCheckpointDisposition,
    CompensationCheckpointReconciliation,
    RecoveryLeaseContext,
    RemoteObservation,
)


logger = logging.getLogger(__name__)


class ReassignmentRecoveryCheckpointReconciler:
    """以探测优先原则收敛恢复检查点与补偿决策。

    该对象只依赖 Repository Port。它不会创建远端客户端，也不会根据推测写入“成功”或重新播放
    外部副作用；所有持久化更新都由当前 lease 的 fencing 校验保护。
    """

    def __init__(self, repository: ReassignmentRepositoryPort) -> None:
        if not isinstance(repository, ReassignmentRepositoryPort):
            raise TypeError("repository 必须实现 ReassignmentRepositoryPort")
        self._repository = repository

    def resolve_local_commit_step(
        self,
        context: RecoveryLeaseContext,
        record: ReassignmentOperationRecord,
        local_state: ReassignmentLocalCommitState,
    ) -> bool:
        """仅收敛已启动或未知的本地 CAS Step，保留 pending 的未尝试事实。"""

        steps = self.read_steps(record.operation.operation_id)
        if steps is None:
            return False
        by_name = {step.step.step_name: step for step in steps}
        commit_step = by_name.get(ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE)
        if commit_step is None:
            logger.error(
                "分类节点变更恢复缺少本地提交步骤: operation_id=%s",
                record.operation.operation_id,
            )
            return False
        outcome = self._forward_outcome_from_local_state(commit_step, local_state)
        if outcome is ReassignmentMutationOutcome.OUTCOME_UNKNOWN:
            return False
        return self._resolve_step_from_outcome(context, commit_step, outcome)

    def resolve_forward_steps(
        self,
        context: RecoveryLeaseContext,
        record: ReassignmentOperationRecord,
        steps: dict[ReassignmentStepName, ReassignmentStepRecord],
        local_state: ReassignmentLocalCommitState,
        remote: RemoteObservation,
    ) -> bool:
        """把写后探测可证明的前向效果收敛为步骤事实，不重放未知外部写。"""

        assignments = (
            (
                ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
                self._forward_outcome_from_binding(
                    steps[ReassignmentStepName.DETACH_SOURCE_DOCUMENT],
                    remote.source_binding_state,
                    expected_present_after_effect=False,
                ),
            ),
            (
                ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
                self._forward_outcome_from_binding(
                    steps[ReassignmentStepName.ATTACH_TARGET_DOCUMENT],
                    remote.target_binding_state,
                    expected_present_after_effect=True,
                ),
            ),
            (
                ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
                self._forward_outcome_from_local_state(
                    steps[ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE],
                    local_state,
                ),
            ),
        )
        for step_name, outcome in assignments:
            if outcome is ReassignmentMutationOutcome.OUTCOME_UNKNOWN:
                logger.warning(
                    "分类节点变更恢复前向步骤结果仍未知: operation_id=%s step=%s",
                    record.operation.operation_id,
                    step_name.value,
                )
                return False
            if not self._resolve_step_from_outcome(
                context,
                steps[step_name],
                outcome,
            ):
                return False
        return True

    def compensation_decision(
        self,
        *,
        record: ReassignmentOperationRecord,
        steps: dict[ReassignmentStepName, ReassignmentStepRecord],
        local_state: ReassignmentLocalCommitState,
        remote: RemoteObservation,
    ) -> ReassignmentCompensationDecision:
        """将持久化写意图和最新探测投影为纯领域补偿决策。"""

        return decide_compensation(
            ReassignmentCompensationFacts(
                source_detach_outcome=self._forward_outcome_from_binding(
                    steps[ReassignmentStepName.DETACH_SOURCE_DOCUMENT],
                    remote.source_binding_state,
                    expected_present_after_effect=False,
                ),
                target_attach_outcome=self._forward_outcome_from_binding(
                    steps[ReassignmentStepName.ATTACH_TARGET_DOCUMENT],
                    remote.target_binding_state,
                    expected_present_after_effect=True,
                ),
                local_commit_outcome=self._forward_outcome_from_local_state(
                    steps[ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE],
                    local_state,
                ),
                remote_membership_required=(
                    record.operation.document.requires_remote_membership_change
                ),
                source_binding_state=remote.source_binding_state,
                target_binding_state=remote.target_binding_state,
            )
        )

    def reconcile_compensation_checkpoints(
        self,
        *,
        context: RecoveryLeaseContext,
        record: ReassignmentOperationRecord,
        steps: dict[ReassignmentStepName, ReassignmentStepRecord],
        remote: RemoteObservation,
    ) -> CompensationCheckpointReconciliation:
        """用最新双侧探测收敛此前崩溃留下的补偿写窗口。"""

        target_step = steps[ReassignmentStepName.COMPENSATE_TARGET_DOCUMENT]
        source_step = steps[ReassignmentStepName.COMPENSATE_SOURCE_DOCUMENT]
        target_incomplete = target_step.step.state in {
            ReassignmentStepState.MUTATION_STARTED,
            ReassignmentStepState.OUTCOME_UNKNOWN,
        }
        source_incomplete = source_step.step.state in {
            ReassignmentStepState.MUTATION_STARTED,
            ReassignmentStepState.OUTCOME_UNKNOWN,
        }

        if target_incomplete:
            if remote.target_binding_state is ReassignmentBindingState.CONFIRMED_ABSENT:
                if not self._complete_compensation_step_from_probe(
                    context,
                    target_step,
                ):
                    return CompensationCheckpointReconciliation(
                        CompensationCheckpointDisposition.UNRESOLVED
                    )
            else:
                self._preserve_unknown_compensation_checkpoint(context, target_step)
                return CompensationCheckpointReconciliation(
                    CompensationCheckpointDisposition.UNRESOLVED
                )

        if source_incomplete:
            if (
                remote.target_binding_state
                is ReassignmentBindingState.CONFIRMED_ABSENT
                and remote.source_binding_state
                is ReassignmentBindingState.CONFIRMED_PRESENT
            ):
                if not self._complete_compensation_step_from_probe(
                    context,
                    source_step,
                ):
                    return CompensationCheckpointReconciliation(
                        CompensationCheckpointDisposition.UNRESOLVED
                    )
            else:
                self._preserve_unknown_compensation_checkpoint(context, source_step)
                return CompensationCheckpointReconciliation(
                    CompensationCheckpointDisposition.UNRESOLVED
                )

        has_compensation_history = any(
            step.step.state is not ReassignmentStepState.PENDING
            or step.attempt_count > 0
            for step in (target_step, source_step)
        )
        if has_compensation_history and self.remote_state_is_compensated(remote):
            terminal_step = (
                ReassignmentStepName.COMPENSATE_SOURCE_DOCUMENT
                if source_step.step.state is not ReassignmentStepState.PENDING
                else ReassignmentStepName.COMPENSATE_TARGET_DOCUMENT
            )
            return CompensationCheckpointReconciliation(
                CompensationCheckpointDisposition.TERMINAL_READY,
                terminal_step=terminal_step,
            )
        return CompensationCheckpointReconciliation(
            CompensationCheckpointDisposition.CONTINUE
        )

    def reconcile_workspace_preparation_checkpoint(
        self,
        *,
        context: RecoveryLeaseContext,
        record: ReassignmentOperationRecord,
        prepare_step: ReassignmentStepRecord,
        remote: RemoteObservation,
    ) -> bool:
        """为 workspace 创建后丢失的检查点补记最小、可验证的远端事实。"""

        if prepare_step.step.state not in {
            ReassignmentStepState.MUTATION_STARTED,
            ReassignmentStepState.OUTCOME_UNKNOWN,
        }:
            return True
        if not record.operation.document.requires_remote_membership_change:
            return False
        if record.target_workspace_slug is not None:
            # 已有受 fencing 保护的 slug 事实时，不能用本次探测覆盖尚未完成的 mapping 现场。
            return True
        if remote.target_workspace is None:
            if remote.target_binding_state is not ReassignmentBindingState.CONFIRMED_ABSENT:
                return False
            return self._complete_workspace_prepare_no_effect(context, prepare_step)
        if remote.target_workspace_ownership is None:
            return False
        try:
            with self._repository.unit_of_work() as unit_of_work:
                persisted = unit_of_work.record_workspace_preparation_fact(
                    ReassignmentWorkspacePreparationFactRequest(
                        lease=context.lease,
                        workspace_slug=remote.target_workspace.slug,
                        ownership=remote.target_workspace_ownership,
                        error_code="recovery_workspace_preparation_fact",
                        recovery_authorized=True,
                    )
                )
        except Exception as exc:
            logger.error(
                "分类节点变更恢复无法保存 workspace 准备事实: "
                "operation_id=%s error_type=%s",
                record.operation.operation_id,
                type(exc).__name__,
            )
            return False
        if not isinstance(persisted, ReassignmentOperationRecord):
            logger.warning(
                "分类节点变更恢复 workspace 准备事实被拒绝: operation_id=%s outcome=%s",
                record.operation.operation_id,
                getattr(persisted, "value", "invalid_result"),
            )
            return False
        logger.info(
            "分类节点变更恢复已保存 workspace 准备事实: operation_id=%s ownership=%s",
            record.operation.operation_id,
            remote.target_workspace_ownership.value,
        )
        return True

    def remote_state_is_compensated(self, remote: RemoteObservation) -> bool:
        """判断最新探测是否明确恢复了目标缺失与来源存在（或来源不适用）。"""

        return (
            remote.target_binding_state is ReassignmentBindingState.CONFIRMED_ABSENT
            and remote.source_binding_state
            in {
                ReassignmentBindingState.CONFIRMED_PRESENT,
                ReassignmentBindingState.NOT_APPLICABLE,
            }
        )

    def has_recovery_side_effect_history(
        self,
        record: ReassignmentOperationRecord,
        steps: dict[ReassignmentStepName, ReassignmentStepRecord],
    ) -> bool:
        """判断是否已有已确认或无法排除的外部副作用历史。"""

        if record.target_workspace_ownership in {
            ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION,
            ReassignmentWorkspaceOwnership.UNKNOWN,
        }:
            return True
        return any(
            step.step.state
            in {
                ReassignmentStepState.MUTATION_STARTED,
                ReassignmentStepState.OUTCOME_UNKNOWN,
            }
            or step.probe_outcome
            in {
                ReassignmentMutationOutcome.CONFIRMED_EFFECT,
                ReassignmentMutationOutcome.OUTCOME_UNKNOWN,
            }
            for step in steps.values()
            if step.step.step_name
            in {
                ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
                ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
                ReassignmentStepName.COMPENSATE_TARGET_DOCUMENT,
                ReassignmentStepName.COMPENSATE_SOURCE_DOCUMENT,
            }
        )

    def read_steps(
        self,
        operation_id: str,
    ) -> tuple[ReassignmentStepRecord, ...] | None:
        """读取固定步骤快照；读取异常时禁止接下来写入补偿事实。"""

        try:
            with self._repository.unit_of_work(read_only=True) as unit_of_work:
                return unit_of_work.list_steps(operation_id)
        except Exception as exc:
            logger.error(
                "分类节点变更恢复读取步骤失败: operation_id=%s error_type=%s",
                operation_id,
                type(exc).__name__,
            )
            return None

    @staticmethod
    def _forward_outcome_from_binding(
        step: ReassignmentStepRecord,
        binding_state: ReassignmentBindingState,
        *,
        expected_present_after_effect: bool,
    ) -> ReassignmentMutationOutcome:
        """将绑定状态与持久化写意图组合为保守的前向副作用结论。"""

        if binding_state is ReassignmentBindingState.NOT_APPLICABLE:
            return ReassignmentMutationOutcome.NOT_STARTED
        if binding_state is ReassignmentBindingState.OUTCOME_UNKNOWN:
            return ReassignmentMutationOutcome.OUTCOME_UNKNOWN
        present = binding_state is ReassignmentBindingState.CONFIRMED_PRESENT
        effect = present is expected_present_after_effect
        if step.step.state is ReassignmentStepState.PENDING:
            return (
                ReassignmentMutationOutcome.NOT_STARTED
                if not effect
                else ReassignmentMutationOutcome.OUTCOME_UNKNOWN
            )
        if step.step.state is ReassignmentStepState.KNOWN_FAILED and effect:
            return ReassignmentMutationOutcome.OUTCOME_UNKNOWN
        return (
            ReassignmentMutationOutcome.CONFIRMED_EFFECT
            if effect
            else ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT
        )

    @staticmethod
    def _forward_outcome_from_local_state(
        step: ReassignmentStepRecord,
        local_state: ReassignmentLocalCommitState,
    ) -> ReassignmentMutationOutcome:
        """基于权威本地行与 commit Step 判断本地 CAS 是否已产生效果。"""

        if local_state is ReassignmentLocalCommitState.CONFLICT:
            return ReassignmentMutationOutcome.OUTCOME_UNKNOWN
        effect = local_state is ReassignmentLocalCommitState.TARGET_COMMITTED
        if step.step.state is ReassignmentStepState.PENDING:
            return (
                ReassignmentMutationOutcome.NOT_STARTED
                if not effect
                else ReassignmentMutationOutcome.OUTCOME_UNKNOWN
            )
        if step.step.state is ReassignmentStepState.KNOWN_FAILED and effect:
            return ReassignmentMutationOutcome.OUTCOME_UNKNOWN
        return (
            ReassignmentMutationOutcome.CONFIRMED_EFFECT
            if effect
            else ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT
        )

    def _resolve_step_from_outcome(
        self,
        context: RecoveryLeaseContext,
        step: ReassignmentStepRecord,
        outcome: ReassignmentMutationOutcome,
    ) -> bool:
        """用探测结论收敛已写意图步骤，绝不对未知外部写进行重放。"""

        if outcome is ReassignmentMutationOutcome.NOT_STARTED:
            return step.step.state is ReassignmentStepState.PENDING
        if step.step.state in {
            ReassignmentStepState.PENDING,
            ReassignmentStepState.SUCCEEDED,
            ReassignmentStepState.KNOWN_FAILED,
        }:
            return True
        next_state = (
            ReassignmentStepState.SUCCEEDED
            if outcome is ReassignmentMutationOutcome.CONFIRMED_EFFECT
            else ReassignmentStepState.KNOWN_FAILED
        )
        try:
            with self._repository.unit_of_work() as unit_of_work:
                result = unit_of_work.complete_step(
                    ReassignmentStepCompletion(
                        lease=context.lease,
                        step_name=step.step.step_name,
                        next_state=next_state,
                        probe_outcome=outcome,
                        recovery_authorized=True,
                    )
                )
        except Exception as exc:
            logger.error(
                "分类节点变更恢复无法收敛步骤探测事实: "
                "operation_id=%s step=%s error_type=%s",
                context.lease.operation_id,
                step.step.step_name.value,
                type(exc).__name__,
            )
            return False
        return isinstance(result, ReassignmentStepRecord)

    def _complete_workspace_prepare_no_effect(
        self,
        context: RecoveryLeaseContext,
        prepare_step: ReassignmentStepRecord,
    ) -> bool:
        """确认目标 workspace 不存在时，安全结束此前未完成的创建意图。"""

        try:
            with self._repository.unit_of_work() as unit_of_work:
                completed = unit_of_work.complete_step(
                    ReassignmentStepCompletion(
                        lease=context.lease,
                        step_name=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                        next_state=ReassignmentStepState.KNOWN_FAILED,
                        error_code="recovery_workspace_not_found_after_prepare_intent",
                        probe_outcome=ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT,
                        recovery_authorized=True,
                    )
                )
        except Exception as exc:
            logger.error(
                "分类节点变更恢复无法收敛 workspace 创建检查点: "
                "operation_id=%s error_type=%s",
                context.lease.operation_id,
                type(exc).__name__,
            )
            return False
        if not isinstance(completed, ReassignmentStepRecord):
            logger.warning(
                "分类节点变更恢复 workspace 创建检查点收敛被拒绝: "
                "operation_id=%s outcome=%s",
                context.lease.operation_id,
                getattr(completed, "value", "invalid_result"),
            )
            return False
        logger.info(
            "分类节点变更恢复确认目标 workspace 创建未生效: operation_id=%s step=%s",
            context.lease.operation_id,
            prepare_step.step.step_name.value,
        )
        return True

    def _complete_compensation_step_from_probe(
        self,
        context: RecoveryLeaseContext,
        step: ReassignmentStepRecord,
    ) -> bool:
        """把已记录意图且已满足目标状态的补偿 Step 原子收敛为成功。"""

        try:
            with self._repository.unit_of_work() as unit_of_work:
                completed = unit_of_work.complete_step(
                    ReassignmentStepCompletion(
                        lease=context.lease,
                        step_name=step.step.step_name,
                        next_state=ReassignmentStepState.SUCCEEDED,
                        probe_outcome=ReassignmentMutationOutcome.CONFIRMED_EFFECT,
                        recovery_authorized=True,
                    )
                )
        except Exception as exc:
            logger.error(
                "分类节点变更恢复无法收敛补偿检查点: "
                "operation_id=%s step=%s error_type=%s",
                context.lease.operation_id,
                step.step.step_name.value,
                type(exc).__name__,
            )
            return False
        if not isinstance(completed, ReassignmentStepRecord):
            logger.warning(
                "分类节点变更恢复补偿检查点收敛被拒绝: "
                "operation_id=%s step=%s outcome=%s",
                context.lease.operation_id,
                step.step.step_name.value,
                getattr(completed, "value", "invalid_result"),
            )
            return False
        logger.info(
            "分类节点变更恢复已通过探测收敛补偿检查点: operation_id=%s step=%s",
            context.lease.operation_id,
            step.step.step_name.value,
        )
        return True

    def _preserve_unknown_compensation_checkpoint(
        self,
        context: RecoveryLeaseContext,
        step: ReassignmentStepRecord,
    ) -> None:
        """尽力标记未满足目标状态的补偿写为未知，禁止下一次恢复盲重试。"""

        if step.step.state is ReassignmentStepState.OUTCOME_UNKNOWN:
            return
        self._complete_unknown_step(context, step.step.step_name)

    def _complete_unknown_step(
        self,
        context: RecoveryLeaseContext,
        step_name: ReassignmentStepName,
    ) -> None:
        """尽力留下结果未知检查点；失败仍由调用方保留 Operation 的恢复隔离。"""

        try:
            with self._repository.unit_of_work() as unit_of_work:
                unit_of_work.complete_step(
                    ReassignmentStepCompletion(
                        lease=context.lease,
                        step_name=step_name,
                        next_state=ReassignmentStepState.OUTCOME_UNKNOWN,
                        error_code="compensation_result_unknown",
                        probe_outcome=ReassignmentMutationOutcome.OUTCOME_UNKNOWN,
                        recovery_authorized=True,
                    )
                )
        except Exception as exc:
            logger.error(
                "分类节点变更恢复未知补偿检查点写入失败: "
                "operation_id=%s step=%s error_type=%s",
                context.lease.operation_id,
                step_name.value,
                type(exc).__name__,
            )


__all__ = ["ReassignmentRecoveryCheckpointReconciler"]
