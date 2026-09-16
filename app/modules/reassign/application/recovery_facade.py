"""分类节点变更恢复 Facade 的受控高层编排。

Facade 保留唯一 Application 入口、命令校验、过期 lease 接管和高层流程选择。观察、检查点写入、
固定顺序补偿、终态提交与隔离均由独立协作器实际执行，避免重新形成 callback-wrapper 或巨型
恢复文件。该模块不创建后台线程、队列、SQLite 连接或 HTTP Client。
"""

from __future__ import annotations

import logging

from app.modules.reassign.domain import (
    ReassignmentBindingState,
    ReassignmentCompensationAction,
    ReassignmentCompensationMode,
    ReassignmentOperationStatus,
    ReassignmentStepName,
    ReassignmentTerminalEvidenceKind,
)
from app.modules.reassign.ports import (
    ReassignmentExpiredLeaseTakeoverRequest,
    ReassignmentKnowledgePort,
    ReassignmentKnowledgePortFactory,
    ReassignmentLease,
    ReassignmentLeaseUpdateResult,
    ReassignmentLocalCommitState,
    ReassignmentOperationRecord,
    ReassignmentRecoveryCursor,
    ReassignmentRepositoryPort,
    ReassignmentWriteOutcome,
)

from .recovery_checkpoints import ReassignmentRecoveryCheckpointReconciler
from .recovery_compensator import ReassignmentRecoveryCompensator
from .recovery_finalizer import ReassignmentRecoveryFinalizer
from .recovery_observer import ReassignmentRecoveryObserver
from .recovery_types import (
    CompensationCheckpointDisposition,
    OperationReadResult,
    RecoverReassignmentCommand,
    ReassignmentRecoveryResult,
    ReassignmentRecoveryResultCategory,
    RecoveryLeaseContext,
    actor_marker,
    required_text,
)
from .service import ReassignmentExecutionSettings


logger = logging.getLogger(__name__)


class RecoverReassignmentOperation:
    """通过探测优先、fencing 接管和有序补偿收敛过期分类变更 Operation。

    调用方必须先通过 :meth:`list_expired_operations` 取得只读候选项，再携带精确的预期 token
    调用 :meth:`recover`。任何无法证明的状态、端口异常或补偿失败都会保持为
    ``recovery_required``；Facade 从不盲目重放外部写。
    """

    def __init__(
        self,
        repository: ReassignmentRepositoryPort,
        knowledge_factory: ReassignmentKnowledgePortFactory,
        settings: ReassignmentExecutionSettings,
    ) -> None:
        if not isinstance(repository, ReassignmentRepositoryPort):
            raise TypeError("repository 必须实现 ReassignmentRepositoryPort")
        if not isinstance(knowledge_factory, ReassignmentKnowledgePortFactory):
            raise TypeError("knowledge_factory 必须实现 ReassignmentKnowledgePortFactory")
        if not isinstance(settings, ReassignmentExecutionSettings):
            raise TypeError("settings 必须是 ReassignmentExecutionSettings")
        self._repository = repository
        self._knowledge_factory = knowledge_factory
        self._settings = settings

        # 四个协作器均直接接收最小端口依赖，不保存 callback 或 Facade 绑定方法。每次 recover
        # 只把当前 Operation 的不可变记录、lease 和请求级 Knowledge Port 作为参数传递。
        self._observer = ReassignmentRecoveryObserver(repository, settings)
        self._checkpoints = ReassignmentRecoveryCheckpointReconciler(repository)
        self._compensator = ReassignmentRecoveryCompensator(
            repository,
            self._observer,
        )
        self._finalizer = ReassignmentRecoveryFinalizer(repository)

    def list_expired_operations(
        self,
        *,
        limit: int,
        cursor: ReassignmentRecoveryCursor | None = None,
    ) -> tuple[ReassignmentOperationRecord, ...]:
        """只读、有界列出可接管的过期 Operation，不创建客户端或发起网络调用。"""

        with self._repository.unit_of_work(read_only=True) as unit_of_work:
            records = unit_of_work.list_recoverable_operations(
                limit=limit,
                cursor=cursor,
            )
        logger.debug("分类节点变更恢复只读扫描完成: count=%s", len(records))
        return records

    def recover(self, command: RecoverReassignmentCommand) -> ReassignmentRecoveryResult:
        """接管一条过期 Operation，并选择 local-only 或远端恢复流程。"""

        if not isinstance(command, RecoverReassignmentCommand):
            raise TypeError("command 必须是 RecoverReassignmentCommand")

        try:
            execution_started_at = self._settings.execution_started_at()
        except Exception as exc:
            logger.error(
                "分类节点变更恢复无法读取编排单调起点: operation_id=%s error_type=%s",
                command.operation_id,
                type(exc).__name__,
            )
            return self._finalizer.result(
                command,
                ReassignmentRecoveryResultCategory.RECOVERY_REQUIRED,
            )

        initial_read = self._read_operation(command.operation_id)
        if initial_read.read_failed:
            # 临时数据库异常不能伪装成永久不存在，否则未来可靠队列会错误停止重试。
            return self._finalizer.result(
                command,
                ReassignmentRecoveryResultCategory.RECOVERY_REQUIRED,
            )
        record = initial_read.record
        if record is None:
            logger.info(
                "分类节点变更恢复未找到 Operation: operation_id=%s actor_marker=%s",
                command.operation_id,
                actor_marker(command.actor),
            )
            return self._finalizer.result(
                command,
                ReassignmentRecoveryResultCategory.OPERATION_NOT_FOUND,
            )

        takeover = self._take_over(record, command)
        if isinstance(takeover, ReassignmentRecoveryResult):
            return takeover
        context = takeover

        post_takeover_read = self._read_operation(command.operation_id)
        record = post_takeover_read.record
        if (
            post_takeover_read.read_failed
            or record is None
            or record.lease != context.lease
        ):
            # 接管已提交却无法重新读取时，不能猜测本地状态或调用外部系统。
            logger.error(
                "分类节点变更恢复接管后无法确认 Operation lease: operation_id=%s",
                command.operation_id,
            )
            return self._finalizer.result(
                command,
                ReassignmentRecoveryResultCategory.RECOVERY_REQUIRED,
            )

        local_state = self._observer.probe_local_commit_state(command.operation_id)
        if local_state is None:
            return self._finalizer.isolate(
                context,
                record,
                command,
                current_step=(
                    record.operation.current_step
                    or ReassignmentStepName.RESERVE_DOCUMENT
                ),
                error_code="recovery_local_probe_failed",
            )
        if local_state is ReassignmentLocalCommitState.CONFLICT:
            observation = self._observer.record_observation(
                context,
                local_state=local_state,
                source_binding_state=(
                    ReassignmentBindingState.NOT_APPLICABLE
                    if not record.operation.document.requires_remote_membership_change
                    else ReassignmentBindingState.OUTCOME_UNKNOWN
                ),
                target_binding_state=(
                    ReassignmentBindingState.NOT_APPLICABLE
                    if not record.operation.document.requires_remote_membership_change
                    else ReassignmentBindingState.OUTCOME_UNKNOWN
                ),
                remote_membership_required=(
                    record.operation.document.requires_remote_membership_change
                ),
                command=command,
            )
            if observation is None:
                logger.error(
                    "分类节点变更恢复无法保存本地冲突观测: operation_id=%s",
                    command.operation_id,
                )
            return self._finalizer.isolate(
                context,
                record,
                command,
                current_step=(
                    record.operation.current_step
                    or ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE
                ),
                error_code="recovery_local_state_conflict",
            )

        if not record.operation.document.requires_remote_membership_change:
            return self._recover_local_only(
                context=context,
                record=record,
                local_state=local_state,
                command=command,
            )
        return self._recover_remote(
            context=context,
            record=record,
            local_state=local_state,
            command=command,
            execution_started_at=execution_started_at,
        )

    def _read_operation(self, operation_id: str) -> OperationReadResult:
        """以短只读事务区分“明确不存在”与“读取失败”。"""

        try:
            with self._repository.unit_of_work(read_only=True) as unit_of_work:
                record = unit_of_work.get_operation(operation_id)
        except Exception as exc:
            logger.error(
                "分类节点变更恢复读取 Operation 失败: operation_id=%s error_type=%s",
                operation_id,
                type(exc).__name__,
            )
            return OperationReadResult(record=None, read_failed=True)
        if record is not None and not isinstance(record, ReassignmentOperationRecord):
            logger.error(
                "分类节点变更恢复读取 Operation 返回契约错误: "
                "operation_id=%s result_type=%s",
                operation_id,
                type(record).__name__,
            )
            return OperationReadResult(record=None, read_failed=True)
        return OperationReadResult(record=record)

    def _take_over(
        self,
        record: ReassignmentOperationRecord,
        command: RecoverReassignmentCommand,
    ) -> RecoveryLeaseContext | ReassignmentRecoveryResult:
        """以调用者给出的精确 token 原子接管过期 lease 和过期目标 claim。"""

        try:
            lease_token = required_text(
                self._settings.lease_token_factory(),
                name="lease_token_factory 返回值",
                max_length=512,
            )
            claim_token = required_text(
                self._settings.workspace_claim_token_factory(),
                name="workspace_claim_token_factory 返回值",
                max_length=512,
            )
            with self._repository.unit_of_work() as unit_of_work:
                # 接管到期时间必须在取得写事务后计算，避免锁等待消耗新 lease 的安全余量。
                lease_expires_at = self._settings.lease_expires_at()
                result = unit_of_work.take_over_expired_lease(
                    ReassignmentExpiredLeaseTakeoverRequest(
                        operation_id=command.operation_id,
                        expected_fencing_token=command.expected_fencing_token,
                        lease_owner=self._settings.lease_owner,
                        lease_token=lease_token,
                        lease_expires_at=lease_expires_at,
                        reason_code=command.reason_code,
                        actor=command.actor,
                        workspace_claim_token=claim_token,
                    )
                )
        except Exception as exc:
            logger.error(
                "分类节点变更恢复接管 lease 异常: operation_id=%s error_type=%s",
                command.operation_id,
                type(exc).__name__,
            )
            return self._finalizer.result(
                command,
                ReassignmentRecoveryResultCategory.RECOVERY_REQUIRED,
            )
        if not isinstance(result, ReassignmentLeaseUpdateResult):
            logger.error(
                "分类节点变更恢复接管 lease 返回契约错误: "
                "operation_id=%s result_type=%s",
                command.operation_id,
                type(result).__name__,
            )
            return self._finalizer.result(
                command,
                ReassignmentRecoveryResultCategory.RECOVERY_REQUIRED,
            )
        if result.outcome is not ReassignmentWriteOutcome.APPLIED or result.lease is None:
            logger.info(
                "分类节点变更恢复未接管 lease: operation_id=%s outcome=%s",
                command.operation_id,
                result.outcome.value,
            )
            return self._finalizer.result(
                command,
                ReassignmentRecoveryResultCategory.TAKEOVER_REJECTED,
            )

        expected_lease = ReassignmentLease(
            operation_id=command.operation_id,
            owner=self._settings.lease_owner,
            token=lease_token,
            fencing_token=command.expected_fencing_token + 1,
            expires_at=lease_expires_at,
        )
        if result.lease != expected_lease:
            logger.error(
                "分类节点变更恢复接管 lease 身份不一致: operation_id=%s",
                command.operation_id,
            )
            return self._finalizer.result(
                command,
                ReassignmentRecoveryResultCategory.RECOVERY_REQUIRED,
            )
        recovered_claim = result.workspace_preparation_claim
        if recovered_claim is not None and (
            recovered_claim.operation_id != command.operation_id
            or recovered_claim.owner != expected_lease.owner
            or recovered_claim.token != claim_token
            or recovered_claim.expires_at != expected_lease.expires_at
            or recovered_claim.target_architecture_raw.canonical_json()
            != record.operation.target_architecture_raw.canonical_json()
        ):
            logger.error(
                "分类节点变更恢复接管返回的目标准备权身份不一致: operation_id=%s",
                command.operation_id,
            )
            return self._finalizer.result(
                command,
                ReassignmentRecoveryResultCategory.RECOVERY_REQUIRED,
            )
        logger.warning(
            "分类节点变更恢复已接管过期 lease: operation_id=%s fencing_token=%s actor_marker=%s",
            command.operation_id,
            result.lease.fencing_token,
            actor_marker(command.actor),
        )
        return RecoveryLeaseContext(
            lease=result.lease,
            preparation_claim=recovered_claim,
        )

    def _recover_local_only(
        self,
        *,
        context: RecoveryLeaseContext,
        record: ReassignmentOperationRecord,
        local_state: ReassignmentLocalCommitState,
        command: RecoverReassignmentCommand,
    ) -> ReassignmentRecoveryResult:
        """选择空 ``doc_path`` 的 local-only 收敛流程，绝不创建 Knowledge Port。"""

        if not self._checkpoints.resolve_local_commit_step(
            context,
            record,
            local_state,
        ):
            return self._finalizer.isolate(
                context,
                record,
                command,
                current_step=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
                error_code="recovery_local_commit_step_conflict",
            )
        observation = self._observer.record_observation(
            context,
            local_state=local_state,
            source_binding_state=ReassignmentBindingState.NOT_APPLICABLE,
            target_binding_state=ReassignmentBindingState.NOT_APPLICABLE,
            remote_membership_required=False,
            command=command,
        )
        if observation is None:
            return self._finalizer.isolate(
                context,
                record,
                command,
                current_step=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
                error_code="recovery_observation_write_failed",
            )
        if local_state is ReassignmentLocalCommitState.TARGET_COMMITTED:
            return self._finalizer.finalize(
                context,
                record,
                command,
                observation,
                next_status=ReassignmentOperationStatus.SUCCEEDED,
                current_step=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
                evidence_kind=ReassignmentTerminalEvidenceKind.FORWARD_SUCCESS_CONFIRMED,
                category=ReassignmentRecoveryResultCategory.RECOVERED_SUCCEEDED,
            )
        return self._finalizer.finalize(
            context,
            record,
            command,
            observation,
            next_status=ReassignmentOperationStatus.FAILED,
            current_step=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
            evidence_kind=(
                ReassignmentTerminalEvidenceKind.NO_SIDE_EFFECT_FAILURE_CONFIRMED
            ),
            category=ReassignmentRecoveryResultCategory.RECOVERED_FAILED_NO_SIDE_EFFECT,
            error_code="recovery_no_side_effect_confirmed",
        )

    def _recover_remote(
        self,
        *,
        context: RecoveryLeaseContext,
        record: ReassignmentOperationRecord,
        local_state: ReassignmentLocalCommitState,
        command: RecoverReassignmentCommand,
        execution_started_at: float,
    ) -> ReassignmentRecoveryResult:
        """选择远端探测、检查点收敛和固定顺序补偿的恢复流程。"""

        try:
            knowledge = self._knowledge_factory.create(
                elapsed_seconds=self._settings.elapsed_seconds_since(
                    execution_started_at
                )
            )
        except Exception as exc:
            logger.error(
                "分类节点变更恢复无法创建 Knowledge Port: operation_id=%s error_type=%s",
                record.operation.operation_id,
                type(exc).__name__,
            )
            return self._isolate_remote_failure(
                context,
                record,
                command,
                error_code="recovery_knowledge_port_unavailable",
            )
        if not isinstance(knowledge, ReassignmentKnowledgePort):
            logger.error(
                "分类节点变更恢复 Factory 返回无效 Knowledge Port: operation_id=%s",
                record.operation.operation_id,
            )
            return self._isolate_remote_failure(
                context,
                record,
                command,
                error_code="recovery_knowledge_port_contract_error",
            )

        observed = self._observer.observe_remote(context, record, knowledge)
        if observed is None:
            return self._isolate_remote_failure(
                context,
                record,
                command,
                error_code="recovery_remote_probe_failed",
            )
        context, remote = observed
        observation = self._observer.record_observation(
            context,
            local_state=local_state,
            source_binding_state=remote.source_binding_state,
            target_binding_state=remote.target_binding_state,
            remote_membership_required=(
                record.operation.document.requires_remote_membership_change
            ),
            command=command,
        )
        if observation is None:
            return self._isolate_remote_failure(
                context,
                record,
                command,
                error_code="recovery_observation_write_failed",
            )

        if local_state is ReassignmentLocalCommitState.TARGET_COMMITTED:
            if not self._checkpoints.resolve_local_commit_step(
                context,
                record,
                local_state,
            ):
                return self._finalizer.isolate(
                    context,
                    record,
                    command,
                    current_step=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
                    error_code="recovery_local_commit_step_conflict",
                )
            return self._finalizer.finalize(
                context,
                record,
                command,
                observation,
                next_status=ReassignmentOperationStatus.SUCCEEDED,
                current_step=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
                evidence_kind=ReassignmentTerminalEvidenceKind.FORWARD_SUCCESS_CONFIRMED,
                category=ReassignmentRecoveryResultCategory.RECOVERED_SUCCEEDED,
            )

        steps = self._checkpoints.read_steps(record.operation.operation_id)
        if steps is None:
            return self._isolate_remote_failure(
                context,
                record,
                command,
                error_code="recovery_step_read_failed",
            )
        step_by_name = {step.step.step_name: step for step in steps}
        if set(step_by_name) != set(ReassignmentStepName):
            logger.error(
                "分类节点变更恢复发现固定步骤缺失或重复: operation_id=%s",
                record.operation.operation_id,
            )
            return self._isolate_remote_failure(
                context,
                record,
                command,
                error_code="recovery_step_set_invalid",
            )
        if not self._checkpoints.reconcile_workspace_preparation_checkpoint(
            context=context,
            record=record,
            prepare_step=step_by_name[ReassignmentStepName.PREPARE_TARGET_WORKSPACE],
            remote=remote,
        ):
            return self._finalizer.isolate(
                context,
                record,
                command,
                current_step=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                error_code="recovery_workspace_preparation_checkpoint_unknown",
            )
        compensation_checkpoint = self._checkpoints.reconcile_compensation_checkpoints(
            context=context,
            record=record,
            steps=step_by_name,
            remote=remote,
        )
        if (
            compensation_checkpoint.disposition
            is CompensationCheckpointDisposition.UNRESOLVED
        ):
            return self._isolate_remote_failure(
                context,
                record,
                command,
                error_code="recovery_compensation_checkpoint_unknown",
                fallback_step=ReassignmentStepName.COMPENSATE_TARGET_DOCUMENT,
            )
        if (
            compensation_checkpoint.disposition
            is CompensationCheckpointDisposition.TERMINAL_READY
        ):
            if not self._compensator.enter(context, record):
                return self._finalizer.isolate(
                    context,
                    record,
                    command,
                    current_step=compensation_checkpoint.terminal_step,
                    error_code="recovery_compensating_transition_failed",
                )
            return self._finalizer.finalize(
                context,
                record,
                command,
                observation,
                next_status=ReassignmentOperationStatus.COMPENSATED,
                current_step=compensation_checkpoint.terminal_step,
                evidence_kind=ReassignmentTerminalEvidenceKind.COMPENSATION_CONFIRMED,
                category=ReassignmentRecoveryResultCategory.COMPENSATED,
            )

        if not self._checkpoints.resolve_forward_steps(
            context,
            record,
            step_by_name,
            local_state,
            remote,
        ):
            return self._isolate_remote_failure(
                context,
                record,
                command,
                error_code="recovery_forward_fact_conflict",
            )

        # 检查点收敛可能在多个短事务内完成；补偿决策必须读取最新持久事实，不能复用旧快照。
        resolved_steps = self._checkpoints.read_steps(record.operation.operation_id)
        if resolved_steps is None:
            return self._isolate_remote_failure(
                context,
                record,
                command,
                error_code="recovery_resolved_step_read_failed",
            )
        step_by_name = {step.step.step_name: step for step in resolved_steps}
        if set(step_by_name) != set(ReassignmentStepName):
            return self._isolate_remote_failure(
                context,
                record,
                command,
                error_code="recovery_resolved_step_set_invalid",
            )

        decision = self._checkpoints.compensation_decision(
            record=record,
            steps=step_by_name,
            local_state=local_state,
            remote=remote,
        )
        if decision.mode is ReassignmentCompensationMode.RECOVERY_REQUIRED:
            return self._isolate_remote_failure(
                context,
                record,
                command,
                error_code="recovery_compensation_decision_unknown",
            )
        if decision.mode is ReassignmentCompensationMode.PRESERVE_CONFIRMED_LOCAL_COMMIT:
            return self._finalizer.isolate(
                context,
                record,
                command,
                current_step=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
                error_code="recovery_local_state_decision_conflict",
            )

        if decision.mode is ReassignmentCompensationMode.COMPENSATE:
            compensated = self._compensator.run(
                context=context,
                record=record,
                knowledge=knowledge,
                remote=remote,
                actions=decision.actions,
            )
            if compensated is None:
                return self._finalizer.isolate(
                    context,
                    record,
                    command,
                    current_step=ReassignmentStepName.COMPENSATE_TARGET_DOCUMENT,
                    error_code="recovery_compensation_failed",
                )
            context, remote = compensated
            observation = self._observer.record_observation(
                context,
                local_state=local_state,
                source_binding_state=remote.source_binding_state,
                target_binding_state=remote.target_binding_state,
                remote_membership_required=(
                    record.operation.document.requires_remote_membership_change
                ),
                command=command,
            )
            if observation is None:
                return self._finalizer.isolate(
                    context,
                    record,
                    command,
                    current_step=ReassignmentStepName.COMPENSATE_SOURCE_DOCUMENT,
                    error_code="recovery_compensation_observation_write_failed",
                )
            return self._finalizer.finalize(
                context,
                record,
                command,
                observation,
                next_status=ReassignmentOperationStatus.COMPENSATED,
                current_step=(
                    ReassignmentStepName.COMPENSATE_SOURCE_DOCUMENT
                    if ReassignmentCompensationAction.RESTORE_SOURCE_DOCUMENT
                    in decision.actions
                    else ReassignmentStepName.COMPENSATE_TARGET_DOCUMENT
                ),
                evidence_kind=ReassignmentTerminalEvidenceKind.COMPENSATION_CONFIRMED,
                category=ReassignmentRecoveryResultCategory.COMPENSATED,
            )

        if self._checkpoints.has_recovery_side_effect_history(record, step_by_name):
            if not self._compensator.enter(context, record):
                return self._finalizer.isolate(
                    context,
                    record,
                    command,
                    current_step=ReassignmentStepName.FINALIZE_OPERATION,
                    error_code="recovery_compensating_transition_failed",
                )
            return self._finalizer.finalize(
                context,
                record,
                command,
                observation,
                next_status=ReassignmentOperationStatus.COMPENSATED,
                current_step=ReassignmentStepName.FINALIZE_OPERATION,
                evidence_kind=ReassignmentTerminalEvidenceKind.COMPENSATION_CONFIRMED,
                category=ReassignmentRecoveryResultCategory.COMPENSATED,
            )
        return self._finalizer.finalize(
            context,
            record,
            command,
            observation,
            next_status=ReassignmentOperationStatus.FAILED,
            current_step=ReassignmentStepName.FINALIZE_OPERATION,
            evidence_kind=ReassignmentTerminalEvidenceKind.NO_SIDE_EFFECT_FAILURE_CONFIRMED,
            category=ReassignmentRecoveryResultCategory.RECOVERED_FAILED_NO_SIDE_EFFECT,
            error_code="recovery_no_side_effect_confirmed",
        )

    def _isolate_remote_failure(
        self,
        context: RecoveryLeaseContext,
        record: ReassignmentOperationRecord,
        command: RecoverReassignmentCommand,
        *,
        error_code: str,
        fallback_step: ReassignmentStepName = ReassignmentStepName.RESERVE_DOCUMENT,
    ) -> ReassignmentRecoveryResult:
        """按当前步骤（或指定回退步骤）统一隔离远端恢复失败。"""

        return self._finalizer.isolate(
            context,
            record,
            command,
            current_step=record.operation.current_step or fallback_step,
            error_code=error_code,
        )


__all__ = [
    "RecoverReassignmentCommand",
    "RecoverReassignmentOperation",
    "ReassignmentRecoveryResult",
    "ReassignmentRecoveryResultCategory",
]
