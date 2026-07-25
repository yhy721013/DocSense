"""分类节点变更恢复的有序补偿协作器。

Compensator 实际拥有“先目标解绑、后来源恢复”的补偿算法、写前意图、写后四分类检查点和
再次探测。所有远端调用均经 Observer 在短 UoW 外完成 lease 续租，不创建后台线程、队列或
共享网络 Client。
"""

from __future__ import annotations

import logging

from app.modules.reassign.domain import (
    ReassignmentBindingState,
    ReassignmentCompensationAction,
    ReassignmentMutationOutcome,
    ReassignmentOperationStatus,
    ReassignmentStepName,
    ReassignmentStepState,
    build_step_idempotency_key,
)
from app.modules.reassign.ports import (
    ReassignmentDocumentMutationRequest,
    ReassignmentDocumentMutationResult,
    ReassignmentKnowledgeOutcome,
    ReassignmentKnowledgePort,
    ReassignmentLease,
    ReassignmentOperationRecord,
    ReassignmentOperationTransition,
    ReassignmentRepositoryPort,
    ReassignmentStepCompletion,
    ReassignmentStepRecord,
    ReassignmentWorkspaceReference,
)

from .recovery_observer import ReassignmentRecoveryObserver
from .recovery_types import (
    RecoveryLeaseContext,
    RemoteObservation,
)


logger = logging.getLogger(__name__)


class ReassignmentRecoveryCompensator:
    """以固定顺序执行有 fencing 保护的恢复补偿。"""

    def __init__(
        self,
        repository: ReassignmentRepositoryPort,
        observer: ReassignmentRecoveryObserver,
    ) -> None:
        if not isinstance(repository, ReassignmentRepositoryPort):
            raise TypeError("repository 必须实现 ReassignmentRepositoryPort")
        if not isinstance(observer, ReassignmentRecoveryObserver):
            raise TypeError("observer 必须是 ReassignmentRecoveryObserver")
        self._repository = repository
        self._observer = observer

    def run(
        self,
        *,
        context: RecoveryLeaseContext,
        record: ReassignmentOperationRecord,
        knowledge: ReassignmentKnowledgePort,
        remote: RemoteObservation,
        actions: tuple[ReassignmentCompensationAction, ...],
    ) -> tuple[RecoveryLeaseContext, RemoteObservation] | None:
        """按“目标解绑 → 来源恢复”执行，每一步后重新探测完整远端状态。"""

        # ``compensated`` 只能由 ``compensating``（或此前已隔离的受控恢复状态）收口。
        # 在第一笔外部写前提交该状态，可让进程崩溃后的下一次恢复识别明确的补偿现场。
        if not self.enter(context, record):
            return None

        current_context = context
        current_remote = remote
        for action in actions:
            if action is ReassignmentCompensationAction.DETACH_TARGET_DOCUMENT:
                if current_remote.target_workspace is None:
                    logger.error(
                        "分类节点变更恢复补偿缺少目标 workspace: operation_id=%s",
                        record.operation.operation_id,
                    )
                    return None
                completed = self._execute_compensation_mutation(
                    context=current_context,
                    record=record,
                    knowledge=knowledge,
                    step_name=ReassignmentStepName.COMPENSATE_TARGET_DOCUMENT,
                    workspace=current_remote.target_workspace,
                    architecture_raw=record.operation.target_architecture_raw,
                    detach=True,
                )
            elif action is ReassignmentCompensationAction.RESTORE_SOURCE_DOCUMENT:
                if record.source_workspace_slug is None:
                    logger.error(
                        "分类节点变更恢复补偿缺少来源 workspace: operation_id=%s",
                        record.operation.operation_id,
                    )
                    return None
                completed = self._execute_compensation_mutation(
                    context=current_context,
                    record=record,
                    knowledge=knowledge,
                    step_name=ReassignmentStepName.COMPENSATE_SOURCE_DOCUMENT,
                    workspace=ReassignmentWorkspaceReference(record.source_workspace_slug),
                    architecture_raw=record.operation.source_architecture_raw,
                    detach=False,
                )
            else:
                logger.error(
                    "分类节点变更恢复收到未知补偿动作: operation_id=%s action=%s",
                    record.operation.operation_id,
                    action,
                )
                return None
            if completed is None:
                return None

            current_context = completed
            re_observed = self._observer.observe_remote(
                current_context,
                record,
                knowledge,
            )
            if re_observed is None:
                return None
            current_context, current_remote = re_observed
            if (
                action is ReassignmentCompensationAction.DETACH_TARGET_DOCUMENT
                and current_remote.target_binding_state
                is not ReassignmentBindingState.CONFIRMED_ABSENT
            ):
                logger.warning(
                    "分类节点变更恢复补偿后目标绑定未确认移除: operation_id=%s",
                    record.operation.operation_id,
                )
                return None
            if (
                action is ReassignmentCompensationAction.RESTORE_SOURCE_DOCUMENT
                and current_remote.source_binding_state
                is not ReassignmentBindingState.CONFIRMED_PRESENT
            ):
                logger.warning(
                    "分类节点变更恢复补偿后来源绑定未确认恢复: operation_id=%s",
                    record.operation.operation_id,
                )
                return None
        return current_context, current_remote

    def enter(
        self,
        context: RecoveryLeaseContext,
        record: ReassignmentOperationRecord,
    ) -> bool:
        """在第一笔补偿写前持久化 ``compensating`` 阶段，保持状态机与 fencing 门禁。"""

        current_status = record.operation.status
        if current_status is ReassignmentOperationStatus.COMPENSATING:
            return True
        if current_status not in {
            ReassignmentOperationStatus.RUNNING,
            ReassignmentOperationStatus.RECOVERY_REQUIRED,
        }:
            logger.error(
                "分类节点变更恢复不能进入补偿阶段: operation_id=%s status=%s",
                record.operation.operation_id,
                current_status.value,
            )
            return False
        try:
            with self._repository.unit_of_work() as unit_of_work:
                transitioned = unit_of_work.transition_operation(
                    ReassignmentOperationTransition(
                        lease=context.lease,
                        next_status=ReassignmentOperationStatus.COMPENSATING,
                        current_step=ReassignmentStepName.COMPENSATE_TARGET_DOCUMENT,
                        recovery_authorized=True,
                    )
                )
        except Exception as exc:
            logger.error(
                "分类节点变更恢复进入补偿阶段异常: operation_id=%s error_type=%s",
                record.operation.operation_id,
                type(exc).__name__,
            )
            return False
        if not isinstance(transitioned, ReassignmentOperationRecord):
            logger.warning(
                "分类节点变更恢复进入补偿阶段被拒绝: operation_id=%s outcome=%s",
                record.operation.operation_id,
                getattr(transitioned, "value", "invalid_result"),
            )
            return False
        logger.info(
            "分类节点变更恢复进入补偿阶段: operation_id=%s fencing_token=%s",
            record.operation.operation_id,
            context.lease.fencing_token,
        )
        return True

    def _execute_compensation_mutation(
        self,
        *,
        context: RecoveryLeaseContext,
        record: ReassignmentOperationRecord,
        knowledge: ReassignmentKnowledgePort,
        step_name: ReassignmentStepName,
        workspace: ReassignmentWorkspaceReference,
        architecture_raw: object,
        detach: bool,
    ) -> RecoveryLeaseContext | None:
        """保存写意图，执行一次远端补偿，再持久化严格四分类结果。"""

        try:
            with self._repository.unit_of_work() as unit_of_work:
                started = unit_of_work.begin_step_mutation(
                    lease=context.lease,
                    step_name=step_name,
                    recovery_authorized=True,
                )
        except Exception as exc:
            logger.error(
                "分类节点变更恢复无法记录补偿写意图: "
                "operation_id=%s step=%s error_type=%s",
                record.operation.operation_id,
                step_name.value,
                type(exc).__name__,
            )
            return None
        if not isinstance(started, ReassignmentStepRecord):
            logger.warning(
                "分类节点变更恢复补偿写意图被拒绝: operation_id=%s step=%s outcome=%s",
                record.operation.operation_id,
                step_name.value,
                getattr(started, "value", "invalid_result"),
            )
            return None

        refreshed = self._observer.renew_before_remote(
            context,
            operation_id=record.operation.operation_id,
        )
        if refreshed is None:
            return None
        document = self._observer.document_reference(record.operation.document)
        if document is None:
            logger.error(
                "分类节点变更恢复无法构造精确文档引用: operation_id=%s",
                record.operation.operation_id,
            )
            return None

        request = ReassignmentDocumentMutationRequest(
            operation_id=record.operation.operation_id,
            step_name=step_name,
            workspace=workspace,
            document=document,
            architecture_raw=architecture_raw,
            idempotency_key=build_step_idempotency_key(record.operation, step_name),
        )
        try:
            result = (
                knowledge.detach_document(request)
                if detach
                else knowledge.attach_document(request)
            )
        except Exception as exc:
            logger.error(
                "分类节点变更恢复补偿远端调用异常: "
                "operation_id=%s step=%s error_type=%s",
                record.operation.operation_id,
                step_name.value,
                type(exc).__name__,
            )
            self._complete_unknown_step(refreshed, step_name)
            return None
        if not isinstance(result, ReassignmentDocumentMutationResult):
            logger.error(
                "分类节点变更恢复补偿返回契约错误: "
                "operation_id=%s step=%s result_type=%s",
                record.operation.operation_id,
                step_name.value,
                type(result).__name__,
            )
            self._complete_unknown_step(refreshed, step_name)
            return None

        completion = self._completion_from_mutation_result(
            refreshed.lease,
            step_name,
            result,
        )
        try:
            with self._repository.unit_of_work() as unit_of_work:
                persisted = unit_of_work.complete_step(completion)
        except Exception as exc:
            logger.error(
                "分类节点变更恢复补偿结果检查点写入异常: "
                "operation_id=%s step=%s error_type=%s",
                record.operation.operation_id,
                step_name.value,
                type(exc).__name__,
            )
            return None
        if not isinstance(persisted, ReassignmentStepRecord):
            logger.error(
                "分类节点变更恢复补偿结果检查点被拒绝: operation_id=%s step=%s",
                record.operation.operation_id,
                step_name.value,
            )
            return None
        if completion.next_state is not ReassignmentStepState.SUCCEEDED:
            logger.warning(
                "分类节点变更恢复补偿未确认成功: operation_id=%s step=%s state=%s",
                record.operation.operation_id,
                step_name.value,
                completion.next_state.value,
            )
            return None
        return refreshed

    @staticmethod
    def _completion_from_mutation_result(
        lease: ReassignmentLease,
        step_name: ReassignmentStepName,
        result: ReassignmentDocumentMutationResult,
    ) -> ReassignmentStepCompletion:
        """将 Knowledge Port 四分类严格映射为可持久化的补偿 Step 终态。"""

        if result.outcome is ReassignmentKnowledgeOutcome.APPLIED:
            return ReassignmentStepCompletion(
                lease=lease,
                step_name=step_name,
                next_state=ReassignmentStepState.SUCCEEDED,
                external_reference=result.external_reference,
                probe_outcome=ReassignmentMutationOutcome.CONFIRMED_EFFECT,
                recovery_authorized=True,
            )
        if result.outcome is ReassignmentKnowledgeOutcome.ALREADY_IN_DESIRED_STATE:
            return ReassignmentStepCompletion(
                lease=lease,
                step_name=step_name,
                next_state=ReassignmentStepState.SUCCEEDED,
                external_reference=result.external_reference,
                probe_outcome=ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT,
                recovery_authorized=True,
            )
        if result.outcome is ReassignmentKnowledgeOutcome.KNOWN_FAILURE:
            return ReassignmentStepCompletion(
                lease=lease,
                step_name=step_name,
                next_state=ReassignmentStepState.KNOWN_FAILED,
                error_code=result.error_code or "compensation_known_failure",
                probe_outcome=ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT,
                recovery_authorized=True,
            )
        return ReassignmentStepCompletion(
            lease=lease,
            step_name=step_name,
            next_state=ReassignmentStepState.OUTCOME_UNKNOWN,
            error_code=result.error_code or "compensation_outcome_unknown",
            probe_outcome=ReassignmentMutationOutcome.OUTCOME_UNKNOWN,
            recovery_authorized=True,
        )

    def _complete_unknown_step(
        self,
        context: RecoveryLeaseContext,
        step_name: ReassignmentStepName,
    ) -> None:
        """尽力持久化未知结果；失败由上层保留为 ``recovery_required``。"""

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


__all__ = ["ReassignmentRecoveryCompensator"]
