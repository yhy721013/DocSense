"""分类节点变更恢复的观察协作器。

Observer 实际持有本地提交状态探测、远端 workspace/成员关系探测、lease 续租与观察事实写入。
它只使用传入的请求级 Knowledge Port，所有网络调用都在短 Unit of Work 之外执行；它不会创建
Client、线程或后台任务。
"""

from __future__ import annotations

import logging

from app.shared.domain.knowledge_workspace import permanent_architecture_workspace_name
from app.modules.reassign.domain import (
    ReassignmentBindingState,
    ReassignmentDocumentSnapshot,
    ReassignmentStepName,
    architecture_id_storage_value,
    build_step_idempotency_key,
)
from app.modules.reassign.ports import (
    ReassignmentDocumentReference,
    ReassignmentKnowledgePort,
    ReassignmentLease,
    ReassignmentLeaseUpdateResult,
    ReassignmentLocalCommitState,
    ReassignmentMembershipProbeRequest,
    ReassignmentMembershipProbeResult,
    ReassignmentMembershipState,
    ReassignmentOperationRecord,
    ReassignmentRecoveryObservation,
    ReassignmentRecoveryObservationRecord,
    ReassignmentRepositoryPort,
    ReassignmentWorkspacePreparationRequest,
    ReassignmentWorkspaceOwnership,
    ReassignmentWorkspaceProbeResult,
    ReassignmentWorkspaceProbeState,
    ReassignmentWorkspaceReference,
    ReassignmentWorkspaceReferenceProbeRequest,
    ReassignmentWriteOutcome,
)

from .recovery_types import (
    RecoverReassignmentCommand,
    RecoveryLeaseContext,
    RemoteObservation,
)
from .service import ReassignmentExecutionSettings


logger = logging.getLogger(__name__)


class ReassignmentRecoveryObserver:
    """执行恢复所需的只读探测与可审计观察写入。

    Observer 的依赖仅限于 Repository 和执行设置。具体远端实现由调用者传入的请求级
    ``ReassignmentKnowledgePort`` 提供，因此既不共享连接，也不把网络 I/O 带入事务。
    """

    def __init__(
        self,
        repository: ReassignmentRepositoryPort,
        settings: ReassignmentExecutionSettings,
    ) -> None:
        if not isinstance(repository, ReassignmentRepositoryPort):
            raise TypeError("repository 必须实现 ReassignmentRepositoryPort")
        if not isinstance(settings, ReassignmentExecutionSettings):
            raise TypeError("settings 必须是 ReassignmentExecutionSettings")
        self._repository = repository
        self._settings = settings

    def probe_local_commit_state(
        self,
        operation_id: str,
    ) -> ReassignmentLocalCommitState | None:
        """读取当前权威 documents 行；读取失败时不允许继续远端操作。"""

        try:
            with self._repository.unit_of_work(read_only=True) as unit_of_work:
                return unit_of_work.probe_local_commit_state(operation_id)
        except Exception as exc:
            logger.error(
                "分类节点变更恢复本地 CAS 探测失败: operation_id=%s error_type=%s",
                operation_id,
                type(exc).__name__,
            )
            return None

    def renew_before_remote(
        self,
        context: RecoveryLeaseContext,
        *,
        operation_id: str,
    ) -> RecoveryLeaseContext | None:
        """在每个远端原子调用前续租，并复核 Operation 与 preparation claim 身份。

        续租、重读 Operation 与 claim 复核都在同一短 UoW 内完成。返回后才允许调用远端 Port，
        这样慢 I/O 不会占用数据库写事务，也不会让陈旧 fencing 继续执行外部副作用。
        """

        try:
            with self._repository.unit_of_work() as unit_of_work:
                # 续租到期时间从取得写事务后开始计算，防止 SQLite 锁等待吞掉安全余量。
                lease_expires_at = self._settings.lease_expires_at()
                result = unit_of_work.renew_lease(
                    lease=context.lease,
                    lease_expires_at=lease_expires_at,
                )
                refreshed_record = unit_of_work.get_operation(operation_id)
        except Exception as exc:
            logger.error(
                "分类节点变更恢复远端调用前续租异常: operation_id=%s error_type=%s",
                operation_id,
                type(exc).__name__,
            )
            return None

        expected_lease = ReassignmentLease(
            operation_id=context.lease.operation_id,
            owner=context.lease.owner,
            token=context.lease.token,
            fencing_token=context.lease.fencing_token,
            expires_at=lease_expires_at,
        )
        if (
            not isinstance(result, ReassignmentLeaseUpdateResult)
            or result.outcome is not ReassignmentWriteOutcome.APPLIED
            or result.lease != expected_lease
            or not isinstance(refreshed_record, ReassignmentOperationRecord)
            or refreshed_record.lease != expected_lease
        ):
            logger.warning(
                "分类节点变更恢复远端调用前续租被拒绝或返回契约错误: "
                "operation_id=%s outcome=%s",
                operation_id,
                getattr(getattr(result, "outcome", None), "value", "invalid_result"),
            )
            return None

        renewed_claim = result.workspace_preparation_claim
        if context.preparation_claim is None and renewed_claim is not None:
            logger.error(
                "分类节点变更恢复续租返回了调用方未持有的目标准备权: operation_id=%s",
                operation_id,
            )
            return None
        if context.preparation_claim is not None and (
            renewed_claim is None
            or renewed_claim.operation_id != context.preparation_claim.operation_id
            or renewed_claim.owner != context.preparation_claim.owner
            or renewed_claim.token != context.preparation_claim.token
            or renewed_claim.fencing_token != context.preparation_claim.fencing_token
            or renewed_claim.expires_at != expected_lease.expires_at
            or renewed_claim.target_architecture_raw.canonical_json()
            != context.preparation_claim.target_architecture_raw.canonical_json()
        ):
            logger.error(
                "分类节点变更恢复续租未能保持已接管的目标准备权: operation_id=%s",
                operation_id,
            )
            return None
        return RecoveryLeaseContext(
            lease=expected_lease,
            preparation_claim=renewed_claim,
        )

    def observe_remote(
        self,
        context: RecoveryLeaseContext,
        record: ReassignmentOperationRecord,
        knowledge: ReassignmentKnowledgePort,
    ) -> tuple[RecoveryLeaseContext, RemoteObservation] | None:
        """只读探测来源和目标绑定；恢复期间绝不创建新的 workspace。"""

        document = self.document_reference(record.operation.document)
        if document is None:
            return None
        target_workspace_result = self._probe_target_workspace(
            context,
            record,
            knowledge,
        )
        if target_workspace_result is None:
            return None
        (
            context,
            target_workspace,
            target_workspace_ownership,
            target_workspace_state,
        ) = target_workspace_result

        if record.source_workspace_slug is None:
            source_binding = ReassignmentBindingState.NOT_APPLICABLE
        else:
            source_workspace = ReassignmentWorkspaceReference(record.source_workspace_slug)
            source_probe = self._probe_membership(
                context,
                record,
                knowledge,
                workspace=source_workspace,
                document=document,
            )
            if source_probe is None:
                return None
            context, source_binding = source_probe

        if target_workspace_state is ReassignmentWorkspaceProbeState.ABSENT:
            target_binding = ReassignmentBindingState.CONFIRMED_ABSENT
        elif target_workspace_state is ReassignmentWorkspaceProbeState.OUTCOME_UNKNOWN:
            target_binding = ReassignmentBindingState.OUTCOME_UNKNOWN
        elif target_workspace is None:
            logger.error(
                "分类节点变更恢复目标 workspace 探测协议不完整: operation_id=%s",
                record.operation.operation_id,
            )
            return None
        else:
            target_probe = self._probe_membership(
                context,
                record,
                knowledge,
                workspace=target_workspace,
                document=document,
            )
            if target_probe is None:
                return None
            context, target_binding = target_probe

        return context, RemoteObservation(
            source_binding_state=source_binding,
            target_binding_state=target_binding,
            target_workspace=target_workspace,
            target_workspace_ownership=target_workspace_ownership,
        )

    def record_observation(
        self,
        context: RecoveryLeaseContext,
        *,
        local_state: ReassignmentLocalCommitState,
        source_binding_state: ReassignmentBindingState,
        target_binding_state: ReassignmentBindingState,
        remote_membership_required: bool,
        command: RecoverReassignmentCommand,
    ) -> ReassignmentRecoveryObservationRecord | None:
        """在短事务内追加可供终态校验的恢复观察事实。"""

        try:
            with self._repository.unit_of_work() as unit_of_work:
                result = unit_of_work.record_recovery_observation(
                    ReassignmentRecoveryObservation(
                        lease=context.lease,
                        local_commit_state=local_state,
                        source_binding_state=source_binding_state,
                        target_binding_state=target_binding_state,
                        remote_membership_required=remote_membership_required,
                        actor=command.actor,
                        reason_code=command.reason_code,
                    )
                )
        except Exception as exc:
            logger.error(
                "分类节点变更恢复保存观测异常: operation_id=%s error_type=%s",
                context.lease.operation_id,
                type(exc).__name__,
            )
            return None
        if not isinstance(result, ReassignmentRecoveryObservationRecord):
            logger.error(
                "分类节点变更恢复保存观测被拒绝: operation_id=%s outcome=%s",
                context.lease.operation_id,
                getattr(result, "value", "invalid_result"),
            )
            return None
        return result

    @staticmethod
    def document_reference(
        document: ReassignmentDocumentSnapshot,
    ) -> ReassignmentDocumentReference | None:
        """把冻结快照转换为精确远端文档引用，拒绝模糊路径回退。"""

        try:
            return ReassignmentDocumentReference.from_snapshot(document)
        except Exception:
            return None

    @staticmethod
    def workspace_preparation_request(
        record: ReassignmentOperationRecord,
    ) -> ReassignmentWorkspacePreparationRequest:
        """复用前向服务的规范化名称规则，仅用于恢复时的只读查回。"""

        target_architecture_id = architecture_id_storage_value(
            record.operation.target_architecture_raw,
            name="target_architecture_raw",
        )
        workspace_name = permanent_architecture_workspace_name(target_architecture_id)
        return ReassignmentWorkspacePreparationRequest(
            operation_id=record.operation.operation_id,
            target_architecture_raw=record.operation.target_architecture_raw,
            desired_workspace_name=workspace_name,
            idempotency_key=build_step_idempotency_key(
                record.operation,
                # 创建步骤的幂等键只用于供应商只读查回的精确资源身份，不触发创建。
                ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
            ),
        )

    def _probe_target_workspace(
        self,
        context: RecoveryLeaseContext,
        record: ReassignmentOperationRecord,
        knowledge: ReassignmentKnowledgePort,
    ) -> tuple[
        RecoveryLeaseContext,
        ReassignmentWorkspaceReference | None,
        ReassignmentWorkspaceOwnership | None,
        ReassignmentWorkspaceProbeState,
    ] | None:
        """按持久化 slug、当前 mapping 或确定性名称只读查回目标 workspace。"""

        persisted_slug = record.target_workspace_slug
        if persisted_slug is None:
            try:
                with self._repository.unit_of_work(read_only=True) as unit_of_work:
                    persisted_slug = unit_of_work.get_workspace_slug(
                        record.operation.target_architecture_raw
                    )
            except Exception as exc:
                logger.error(
                    "分类节点变更恢复读取目标 workspace mapping 失败: "
                    "operation_id=%s error_type=%s",
                    record.operation.operation_id,
                    type(exc).__name__,
                )
                return None

        refreshed = self.renew_before_remote(
            context,
            operation_id=record.operation.operation_id,
        )
        if refreshed is None:
            return None
        try:
            if persisted_slug is not None:
                result = knowledge.probe_workspace_reference(
                    ReassignmentWorkspaceReferenceProbeRequest(
                        operation_id=record.operation.operation_id,
                        workspace=ReassignmentWorkspaceReference(persisted_slug),
                    )
                )
            else:
                result = knowledge.probe_target_workspace(
                    self.workspace_preparation_request(record)
                )
        except Exception as exc:
            logger.warning(
                "分类节点变更恢复目标 workspace 探测异常: operation_id=%s error_type=%s",
                record.operation.operation_id,
                type(exc).__name__,
            )
            return None
        if not isinstance(result, ReassignmentWorkspaceProbeResult):
            logger.error(
                "分类节点变更恢复目标 workspace 探测返回契约错误: "
                "operation_id=%s result_type=%s",
                record.operation.operation_id,
                type(result).__name__,
            )
            return None
        if (
            persisted_slug is not None
            and result.state is ReassignmentWorkspaceProbeState.PRESENT
            and (
                result.workspace is None
                or result.workspace.slug.casefold() != persisted_slug.casefold()
            )
        ):
            logger.error(
                "分类节点变更恢复目标 workspace 引用不一致: operation_id=%s",
                record.operation.operation_id,
            )
            return None
        return refreshed, result.workspace, result.ownership, result.state

    def _probe_membership(
        self,
        context: RecoveryLeaseContext,
        record: ReassignmentOperationRecord,
        knowledge: ReassignmentKnowledgePort,
        *,
        workspace: ReassignmentWorkspaceReference,
        document: ReassignmentDocumentReference,
    ) -> tuple[RecoveryLeaseContext, ReassignmentBindingState] | None:
        """执行精确 ``doc_path`` 成员关系查回，并映射为领域绑定状态。"""

        refreshed = self.renew_before_remote(
            context,
            operation_id=record.operation.operation_id,
        )
        if refreshed is None:
            return None
        try:
            result = knowledge.probe_document_membership(
                ReassignmentMembershipProbeRequest(
                    operation_id=record.operation.operation_id,
                    workspace=workspace,
                    document=document,
                )
            )
        except Exception as exc:
            logger.warning(
                "分类节点变更恢复成员关系探测异常: operation_id=%s error_type=%s",
                record.operation.operation_id,
                type(exc).__name__,
            )
            return None
        if not isinstance(result, ReassignmentMembershipProbeResult):
            logger.error(
                "分类节点变更恢复成员关系探测返回契约错误: "
                "operation_id=%s result_type=%s",
                record.operation.operation_id,
                type(result).__name__,
            )
            return None
        state_by_result = {
            ReassignmentMembershipState.PRESENT: ReassignmentBindingState.CONFIRMED_PRESENT,
            ReassignmentMembershipState.ABSENT: ReassignmentBindingState.CONFIRMED_ABSENT,
            ReassignmentMembershipState.OUTCOME_UNKNOWN: ReassignmentBindingState.OUTCOME_UNKNOWN,
        }
        return refreshed, state_by_result[result.state]


__all__ = ["ReassignmentRecoveryObserver"]
