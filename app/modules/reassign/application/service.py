"""分类节点变更同步 Saga 的正常编排服务。

本文件只依赖 Domain、Port 和显式注入的组合值。它不会读取环境变量、不会创建 HTTP Client，
也不会生成 Flask Response。每次本地事实写入都使用一个短 Unit of Work；任何 AnythingLLM
调用都发生在事务关闭之后，避免网络等待扩大 SQLite 写锁范围。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import monotonic
from typing import Callable

from app.modules.reassign.domain import (
    ReassignDocumentCommand,
    ReassignmentBindingState,
    ReassignmentDocumentSnapshot,
    ReassignmentMutationOutcome,
    ReassignmentOperationStatus,
    ReassignmentPublicMessage,
    ReassignmentResult,
    ReassignmentResultCategory,
    ReassignmentStepName,
    ReassignmentStepState,
    ReassignmentTerminalEvidence,
    ReassignmentTerminalEvidenceKind,
    build_step_idempotency_key,
)
from app.modules.reassign.ports import (
    ReassignmentBestEffortPinCompletion,
    ReassignmentDocumentMutationRequest,
    ReassignmentDocumentMutationResult,
    ReassignmentDocumentReference,
    ReassignmentKnowledgeOutcome,
    ReassignmentKnowledgePort,
    ReassignmentKnowledgePortFactory,
    ReassignmentLease,
    ReassignmentLeaseUpdateResult,
    ReassignmentLocalCommitRequest,
    ReassignmentLocalCommitState,
    ReassignmentNoSideEffectFailureRequest,
    ReassignmentOperationRecord,
    ReassignmentOperationTransition,
    ReassignmentReservationOutcome,
    ReassignmentReservationRequest,
    ReassignmentRecoveryFinalizationRequest,
    ReassignmentRecoveryObservation,
    ReassignmentRecoveryObservationRecord,
    ReassignmentRepositoryPort,
    ReassignmentStepCompletion,
    ReassignmentStepRecord,
    ReassignmentWorkspacePreparationClaim,
    ReassignmentWorkspacePreparationClaimOutcome,
    ReassignmentWorkspacePreparationClaimRequest,
    ReassignmentWorkspacePreparationClaimResult,
    ReassignmentWorkspaceMappingRequest,
    ReassignmentWorkspacePreparationFactRequest,
    ReassignmentWorkspaceOwnership,
    ReassignmentWorkspacePreparationRequest,
    ReassignmentWorkspacePreparationResult,
    ReassignmentWorkspaceProbeResult,
    ReassignmentWorkspaceProbeState,
    ReassignmentWorkspaceReference,
    ReassignmentWorkspaceReferenceProbeRequest,
    ReassignmentWriteOutcome,
)


logger = logging.getLogger(__name__)


def _default_utc_clock() -> datetime:
    """返回带时区的 UTC 当前时间；测试可显式注入可控时钟。"""

    return datetime.now(timezone.utc)


def _new_internal_identifier() -> str:
    """生成仅用于 Operation/lease 的不透明内部标识，不会出现在公开响应。"""

    return uuid.uuid4().hex


def _required_text(value: object, *, name: str) -> str:
    """校验 Application 自己生成的内部文本，拒绝静默字符串转换。"""

    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


@dataclass(frozen=True)
class ReassignmentExecutionSettings:
    """同步 Saga 每次执行所需的内部时钟、身份与 lease 策略。

    生产组合根在 1E-6 才负责把实例身份和预算策略注入此对象。此处故意不提供隐藏的环境变量
    默认值，防止未接线代码误以为已经具备多实例部署所需的唯一 owner 与合适 lease 时长。
    """

    lease_owner: str
    lease_duration_seconds: float
    remote_total_timeout_seconds: float
    lease_safety_margin_seconds: float
    clock: Callable[[], datetime] = _default_utc_clock
    operation_id_factory: Callable[[], str] = _new_internal_identifier
    lease_token_factory: Callable[[], str] = _new_internal_identifier
    workspace_claim_token_factory: Callable[[], str] = _new_internal_identifier
    monotonic_clock: Callable[[], float] = monotonic

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "lease_owner",
            _required_text(self.lease_owner, name="lease_owner"),
        )
        normalized_seconds: dict[str, float] = {}
        for field_name in (
            "lease_duration_seconds",
            "remote_total_timeout_seconds",
            "lease_safety_margin_seconds",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{field_name} 必须是有限正数")
            normalized = float(value)
            if (
                normalized != normalized  # NaN 不等于自身。
                or normalized in {float("inf"), float("-inf")}
                or normalized <= 0.0
            ):
                raise ValueError(f"{field_name} 必须是有限正数")
            normalized_seconds[field_name] = normalized
            object.__setattr__(self, field_name, normalized)
        minimum_lease_seconds = (
            normalized_seconds["remote_total_timeout_seconds"]
            + normalized_seconds["lease_safety_margin_seconds"]
        )
        if normalized_seconds["lease_duration_seconds"] < minimum_lease_seconds:
            raise ValueError(
                "lease_duration_seconds 必须不小于 "
                "remote_total_timeout_seconds + lease_safety_margin_seconds"
            )
        for attribute_name in (
            "clock",
            "operation_id_factory",
            "lease_token_factory",
            "workspace_claim_token_factory",
            "monotonic_clock",
        ):
            if not callable(getattr(self, attribute_name)):
                raise TypeError(f"{attribute_name} 必须是可调用对象")

    def execution_started_at(self) -> float:
        """读取同步编排的单调起点，供远端总预算覆盖前置本地事务耗时。"""

        value = self.monotonic_clock()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("monotonic_clock 必须返回有限数字")
        normalized = float(value)
        if normalized != normalized or normalized in {float("inf"), float("-inf")}:
            raise ValueError("monotonic_clock 必须返回有限数字")
        return normalized

    def elapsed_seconds_since(self, started_at: float) -> float:
        """返回不会因错误测试时钟倒退而变成负数的已用编排时间。"""

        if isinstance(started_at, bool) or not isinstance(started_at, (int, float)):
            raise TypeError("started_at 必须是有限数字")
        normalized_started_at = float(started_at)
        if (
            normalized_started_at != normalized_started_at
            or normalized_started_at in {float("inf"), float("-inf")}
        ):
            raise ValueError("started_at 必须是有限数字")
        return max(0.0, self.execution_started_at() - normalized_started_at)

    def lease_expires_at(self) -> str:
        """基于注入时钟计算一次 Operation/目标 claim 共用的 UTC 过期时间。"""

        now = self.clock()
        if not isinstance(now, datetime):
            raise TypeError("clock 必须返回 datetime")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock 必须返回带时区 datetime")
        expiry = now.astimezone(timezone.utc) + timedelta(
            seconds=self.lease_duration_seconds
        )
        return expiry.isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class _WorkspacePreparationExecution:
    """目标准备子流程结果，以及退出时是否必须保留持久化 claim。"""

    value: ReassignmentOperationRecord | ReassignmentResult
    retain_claim_for_recovery: bool


class DocumentReassignmentService:
    """编排分类节点变更的前向成功路径及保守失败收口。

    当前同步路径会对已确认、可安全反向的远端副作用执行有界补偿；只要任一远端动作结果未知、
    本地 CAS 结果无法确认，或补偿事实不完整，服务仍会保留现场并进入 ``recovery_required``，
    交给显式恢复用例继续处理，绝不盲目重放外部写。
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

    def execute(self, command: ReassignDocumentCommand) -> ReassignmentResult:
        """执行一次同步变更，并且只返回已批准的最小 Presenter 结果。"""

        if not isinstance(command, ReassignDocumentCommand):
            raise TypeError("command 必须是 ReassignDocumentCommand")

        # 总远端预算从 Application 接到合法命令时开始计时。这样文档保留、状态推进和 SQLite
        # 锁等待都会消耗后续可用远端窗口，不会在创建 Knowledge Adapter 时重新获得满额预算。
        try:
            execution_started_at = self._settings.execution_started_at()
        except Exception as exc:
            logger.error(
                "分类节点变更无法读取同步编排单调起点: error_type=%s",
                type(exc).__name__,
            )
            return self._recovery_required()

        try:
            reservation = self._reserve(command)
        except Exception as exc:
            # reserve 的事务提交结果也可能处于未知状态，不能把异常直接泄露给 Web 层，
            # 更不能误报为确定的“文档不存在”或普通本地冲突。
            logger.error(
                "分类节点变更无法确认 Operation 保留结果: "
                "document_marker=%s error_type=%s",
                self._command_marker(command),
                type(exc).__name__,
            )
            return self._recovery_required()
        if reservation.outcome is ReassignmentReservationOutcome.DOCUMENT_NOT_FOUND:
            logger.info(
                "分类节点变更未找到本地文档记录: document_marker=%s",
                self._command_marker(command),
            )
            return self._failed(ReassignmentPublicMessage.DOCUMENT_NOT_FOUND)
        if reservation.outcome is ReassignmentReservationOutcome.ACTIVE_OPERATION_EXISTS:
            logger.info(
                "分类节点变更被同文档活动 Operation 拒绝: document_marker=%s",
                self._command_marker(command),
            )
            return self._failed(ReassignmentPublicMessage.CONCURRENT_OPERATION)
        if reservation.record is None:
            # Port DTO 已保证 acquired 携带 record。此分支保留为防御性边界：不要把
            # Repository 契约损坏伪装为成功或公开内部错误。
            logger.error("分类节点变更保留结果缺少 Operation 记录")
            return self._recovery_required()

        record = reservation.record
        lease = record.lease
        logger.info(
            "分类节点变更同步 Saga 开始: operation_id=%s document_marker=%s",
            record.operation.operation_id,
            self._document_marker(record.operation.document),
        )

        if not self._promote_to_running(lease):
            return self._finalize_no_side_effect_failure(
                lease,
                current_step=ReassignmentStepName.RESERVE_DOCUMENT,
                error_code="operation_start_conflict",
                public_message=ReassignmentPublicMessage.LOCAL_STATE_CONFLICT,
            )

        if not record.operation.document.requires_remote_membership_change:
            return self._commit_local_architecture(record)

        # 每个需要远端迁移的 Operation 单独从 Factory 取得 Knowledge Port，避免不同请求
        # 共享 deadline、Transport 或可变状态；local-only 路径不会依赖远端基础设施。
        try:
            knowledge = self._knowledge_factory.create(
                elapsed_seconds=self._settings.elapsed_seconds_since(
                    execution_started_at
                )
            )
        except Exception as exc:
            logger.error(
                "分类节点变更无法创建请求级 Knowledge Port: operation_id=%s error_type=%s",
                record.operation.operation_id,
                type(exc).__name__,
            )
            return self._finalize_no_side_effect_failure(
                lease,
                current_step=ReassignmentStepName.RESERVE_DOCUMENT,
                error_code="knowledge_port_unavailable",
                public_message=ReassignmentPublicMessage.REMOTE_MIGRATION_FAILED,
            )
        if not isinstance(knowledge, ReassignmentKnowledgePort):
            logger.error(
                "分类节点变更 Factory 返回了不符合 Knowledge Port 的对象: operation_id=%s",
                record.operation.operation_id,
            )
            return self._finalize_no_side_effect_failure(
                lease,
                current_step=ReassignmentStepName.RESERVE_DOCUMENT,
                error_code="knowledge_port_contract_error",
                public_message=ReassignmentPublicMessage.REMOTE_MIGRATION_FAILED,
            )

        return self._execute_remote_path(record, knowledge)

    def _reserve(self, command: ReassignDocumentCommand):
        """写入 Operation、固定 Step 和同文档 lease；事务结束后才可能调用网络。"""

        operation_id = _required_text(
            self._settings.operation_id_factory(),
            name="operation_id_factory 返回值",
        )
        lease_token = _required_text(
            self._settings.lease_token_factory(),
            name="lease_token_factory 返回值",
        )
        with self._repository.unit_of_work() as unit_of_work:
            # 必须在取得 Repository 写事务后再计算到期时间，避免 BEGIN IMMEDIATE 的锁等待
            # 提前吞掉 lease 安全余量。多实例阶段仍需迁移为数据库权威时间。
            request = ReassignmentReservationRequest(
                command=command,
                operation_id=operation_id,
                lease_owner=self._settings.lease_owner,
                lease_token=lease_token,
                lease_expires_at=self._settings.lease_expires_at(),
            )
            return unit_of_work.reserve(request)

    def _promote_to_running(self, lease: ReassignmentLease) -> bool:
        """显式记录前向编排开始，不通过泛型状态转换写入终态。"""

        try:
            with self._repository.unit_of_work() as unit_of_work:
                result = unit_of_work.transition_operation(
                    ReassignmentOperationTransition(
                        lease=lease,
                        next_status=ReassignmentOperationStatus.RUNNING,
                        current_step=ReassignmentStepName.RESERVE_DOCUMENT,
                    )
                )
        except Exception as exc:
            logger.error(
                "分类节点变更无法进入运行态: operation_id=%s error_type=%s",
                lease.operation_id,
                type(exc).__name__,
            )
            return False
        return isinstance(result, ReassignmentOperationRecord)

    def _execute_remote_path(
        self,
        record: ReassignmentOperationRecord,
        knowledge: ReassignmentKnowledgePort,
    ) -> ReassignmentResult:
        """执行有 doc_path 的前向路径，并在 finally 中清理尚未原子释放的目标 claim。"""

        lease = record.lease
        preparation_request: ReassignmentWorkspacePreparationRequest
        try:
            preparation_request = self._workspace_preparation_request(record)
        except Exception as exc:
            logger.error(
                "分类节点变更无法构造目标 workspace 请求: operation_id=%s error_type=%s",
                lease.operation_id,
                type(exc).__name__,
            )
            return self._finalize_no_side_effect_failure(
                lease,
                current_step=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                error_code="workspace_request_invalid",
                public_message=ReassignmentPublicMessage.LOCAL_STATE_CONFLICT,
            )

        claim: ReassignmentWorkspacePreparationClaim | None = None
        retain_claim_for_recovery = False
        try:
            try:
                claim_result = self._acquire_workspace_preparation_claim(
                    lease,
                    record.operation.target_architecture_raw,
                )
            except Exception as exc:
                # 此时尚未发起来源解绑或目标创建，异常仅限本地 claim 申请，可以按
                # 无副作用失败释放同文档保护，而不是制造不必要的恢复积压。
                logger.error(
                    "分类节点变更申请目标 workspace 准备权异常: operation_id=%s error_type=%s",
                    lease.operation_id,
                    type(exc).__name__,
                )
                return self._finalize_no_side_effect_failure(
                    lease,
                    current_step=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                    error_code="workspace_claim_exception",
                    public_message=ReassignmentPublicMessage.LOCAL_STATE_CONFLICT,
                )
            if isinstance(claim_result, ReassignmentWriteOutcome):
                return self._finalize_no_side_effect_failure(
                    lease,
                    current_step=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                    error_code="workspace_claim_write_conflict",
                    public_message=ReassignmentPublicMessage.LOCAL_STATE_CONFLICT,
                )
            if (
                claim_result.outcome
                is ReassignmentWorkspacePreparationClaimOutcome.ACTIVE_CLAIM_EXISTS
            ):
                # 目标资源正在由另一份 Operation 准备，但当前文档尚未发起任何远端写，
                # 因此可安全关闭并由调用方稍后重试，而不把内部 claim 泄露到公开响应。
                return self._finalize_no_side_effect_failure(
                    lease,
                    current_step=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                    error_code="workspace_preparation_busy",
                    public_message=ReassignmentPublicMessage.REMOTE_MIGRATION_FAILED,
                )
            if (
                claim_result.outcome
                is ReassignmentWorkspacePreparationClaimOutcome.ACQUIRED
            ):
                claim = claim_result.claim
                if claim is None:
                    logger.error("分类节点变更准备权结果缺少 claim: operation_id=%s", lease.operation_id)
                    return self._finalize_no_side_effect_failure(
                        lease,
                        current_step=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                        error_code="workspace_claim_contract_error",
                        public_message=ReassignmentPublicMessage.LOCAL_STATE_CONFLICT,
                    )

            renewed = self._renew_execution_lease(record, claim)
            if isinstance(renewed, ReassignmentResult):
                return renewed
            record, claim = renewed
            lease = record.lease

            detach_result = self._detach_source_document(record, knowledge)
            if detach_result is not None:
                return detach_result

            renewed = self._renew_execution_lease(record, claim)
            if isinstance(renewed, ReassignmentResult):
                return renewed
            record, claim = renewed
            lease = record.lease

            if (
                claim_result.outcome
                is ReassignmentWorkspacePreparationClaimOutcome.MAPPING_EXISTS
            ):
                return self._reuse_existing_workspace_mapping(
                    record,
                    knowledge,
                    claim_result,
                )

            # 一旦进入目标 prepare 子流程，未分类异常也可能发生在供应商已经创建资源之后；
            # 默认保留 claim，只有子流程明确证明可以释放或 mapping 原子成功时才覆盖。
            retain_claim_for_recovery = True
            preparation = self._prepare_new_workspace_mapping(
                record,
                knowledge,
                preparation_request,
                claim,
            )
            retain_claim_for_recovery = preparation.retain_claim_for_recovery
            prepared = preparation.value
            if isinstance(prepared, ReassignmentResult):
                return prepared
            # 成功登记 mapping 时 Repository 已在同一事务释放 claim。避免 finally 用旧
            # owner 再次释放，确保后续 Operation 取得的更大 fencing 不受影响。
            claim = None
            return self._attach_and_commit(record, knowledge, prepared)
        except Exception as exc:
            # 此处只覆盖远端路径中未被单步处理的异常。来源解绑可能已经成功，不能把
            # 未分类的编排错误收口为普通失败；必须隔离给后续恢复服务。
            logger.error(
                "分类节点变更远端编排出现未分类异常: operation_id=%s error_type=%s",
                lease.operation_id,
                type(exc).__name__,
            )
            return self._mark_recovery_required(
                lease,
                current_step=ReassignmentStepName.FINALIZE_OPERATION,
                error_code="remote_orchestration_exception",
            )
        finally:
            if claim is not None and not retain_claim_for_recovery:
                self._release_workspace_preparation_claim(claim)

    def _detach_source_document(
        self,
        record: ReassignmentOperationRecord,
        knowledge: ReassignmentKnowledgePort,
    ) -> ReassignmentResult | None:
        """按既有顺序先解绑来源；没有来源 mapping 时保留兼容的跳过行为。"""

        source_workspace_slug = record.source_workspace_slug
        if source_workspace_slug is None:
            logger.warning(
                "分类节点变更缺少来源 workspace 映射，按兼容语义跳过远端解绑: operation_id=%s",
                record.operation.operation_id,
            )
            return None
        lease = record.lease
        if not self._begin_step_mutation(
            lease,
            ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
        ):
            return self._finalize_no_side_effect_failure(
                lease,
                current_step=ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
                error_code="detach_intent_conflict",
                public_message=ReassignmentPublicMessage.LOCAL_STATE_CONFLICT,
            )
        request = ReassignmentDocumentMutationRequest(
            operation_id=record.operation.operation_id,
            step_name=ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
            workspace=ReassignmentWorkspaceReference(source_workspace_slug),
            document=ReassignmentDocumentReference.from_snapshot(record.operation.document),
            architecture_raw=record.operation.source_architecture_raw,
            idempotency_key=build_step_idempotency_key(
                record.operation,
                ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
            ),
        )
        try:
            response = knowledge.detach_document(request)
        except Exception as exc:
            logger.error(
                "分类节点变更来源解绑调用异常: operation_id=%s error_type=%s",
                lease.operation_id,
                type(exc).__name__,
            )
            self._complete_unknown_step(
                lease,
                ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
                error_code="detach_exception",
            )
            return self._mark_recovery_required(
                lease,
                current_step=ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
                error_code="detach_exception",
            )
        if not isinstance(response, ReassignmentDocumentMutationResult):
            logger.error("分类节点变更来源解绑返回类型错误: operation_id=%s", lease.operation_id)
            self._complete_unknown_step(
                lease,
                ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
                error_code="detach_result_invalid",
            )
            return self._mark_recovery_required(
                lease,
                current_step=ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
                error_code="detach_result_invalid",
            )
        if response.outcome in {
            ReassignmentKnowledgeOutcome.APPLIED,
            ReassignmentKnowledgeOutcome.ALREADY_IN_DESIRED_STATE,
        }:
            mutation_outcome = (
                ReassignmentMutationOutcome.CONFIRMED_EFFECT
                if response.outcome is ReassignmentKnowledgeOutcome.APPLIED
                else ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT
            )
            if self._complete_step(
                lease,
                ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
                ReassignmentStepState.SUCCEEDED,
                mutation_outcome,
                external_reference=response.external_reference,
            ):
                return None
            return self._mark_recovery_required(
                lease,
                current_step=ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
                error_code="detach_checkpoint_conflict",
            )
        if response.outcome is ReassignmentKnowledgeOutcome.KNOWN_FAILURE:
            if not self._complete_step(
                lease,
                ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
                ReassignmentStepState.KNOWN_FAILED,
                ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT,
                error_code="detach_known_failure",
            ):
                return self._mark_recovery_required(
                    lease,
                    current_step=ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
                    error_code="detach_checkpoint_conflict",
                )
            return self._finalize_no_side_effect_failure(
                lease,
                current_step=ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
                error_code="detach_known_failure",
                public_message=ReassignmentPublicMessage.REMOTE_MIGRATION_FAILED,
            )
        self._complete_unknown_step(
            lease,
            ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
            error_code="detach_outcome_unknown",
        )
        return self._mark_recovery_required(
            lease,
            current_step=ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
            error_code="detach_outcome_unknown",
        )

    def _reuse_existing_workspace_mapping(
        self,
        record: ReassignmentOperationRecord,
        knowledge: ReassignmentKnowledgePort,
        claim_result: ReassignmentWorkspacePreparationClaimResult,
    ) -> ReassignmentResult:
        """查回既有本地 mapping 对应的远端资源，再把 prepare Step 写为无副作用成功。"""

        workspace_slug = claim_result.workspace_slug
        if workspace_slug is None:
            logger.error("分类节点变更既有 mapping 结果缺少 slug: operation_id=%s", record.operation.operation_id)
            return self._mark_recovery_required(
                record.lease,
                current_step=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                error_code="workspace_mapping_contract_error",
            )
        # 固定 Step 状态机要求 ``pending -> mutation_started -> succeeded``。既有 mapping
        # 分支虽然只做远端查回、不创建 workspace，也先记录 prepare 检查点；若查回失败会
        # 明确收口为“无副作用已知失败”，不会把纯读取误记成未知外部写。
        if not self._begin_step_mutation(
            record.lease,
            ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
        ):
            return self._mark_recovery_required(
                record.lease,
                current_step=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                error_code="workspace_probe_intent_conflict",
            )
        try:
            probe = knowledge.probe_workspace_reference(
                ReassignmentWorkspaceReferenceProbeRequest(
                    operation_id=record.operation.operation_id,
                    workspace=ReassignmentWorkspaceReference(workspace_slug),
                )
            )
        except Exception as exc:
            logger.error(
                "分类节点变更目标 workspace 查回异常: operation_id=%s error_type=%s",
                record.operation.operation_id,
                type(exc).__name__,
            )
            self._complete_step(
                record.lease,
                ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                ReassignmentStepState.KNOWN_FAILED,
                ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT,
                error_code="workspace_probe_exception",
            )
            return self._mark_recovery_required(
                record.lease,
                current_step=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                error_code="workspace_probe_exception",
            )
        if (
            not isinstance(probe, ReassignmentWorkspaceProbeResult)
            or probe.state is not ReassignmentWorkspaceProbeState.PRESENT
            or probe.workspace is None
            or probe.workspace.slug.casefold() != workspace_slug.casefold()
        ):
            logger.warning(
                "分类节点变更既有目标 mapping 未能被远端唯一确认: operation_id=%s probe_state=%s",
                record.operation.operation_id,
                getattr(probe, "state", None),
            )
            self._complete_step(
                record.lease,
                ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                ReassignmentStepState.KNOWN_FAILED,
                ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT,
                error_code="workspace_mapping_not_confirmed",
            )
            return self._mark_recovery_required(
                record.lease,
                current_step=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                error_code="workspace_mapping_not_confirmed",
            )
        mapped = self._record_workspace_mapping(
            record.lease,
            record.operation.target_architecture_raw,
            workspace_slug,
            ReassignmentWorkspaceOwnership.PREEXISTING,
        )
        if isinstance(mapped, ReassignmentResult):
            return mapped
        return self._attach_and_commit(record, knowledge, mapped)

    def _prepare_new_workspace_mapping(
        self,
        record: ReassignmentOperationRecord,
        knowledge: ReassignmentKnowledgePort,
        request: ReassignmentWorkspacePreparationRequest,
        claim: ReassignmentWorkspacePreparationClaim | None,
    ) -> _WorkspacePreparationExecution:
        """持有目标 claim 后创建/复用 workspace，并原子登记 mapping 与释放 claim。"""

        lease = record.lease
        if claim is None:
            logger.error("分类节点变更新目标 mapping 缺少持久化准备权: operation_id=%s", lease.operation_id)
            return _WorkspacePreparationExecution(
                self._mark_recovery_required(
                    lease,
                    current_step=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                    error_code="workspace_claim_missing",
                ),
                False,
            )
        if not self._begin_step_mutation(
            lease,
            ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
        ):
            return _WorkspacePreparationExecution(
                self._mark_recovery_required(
                    lease,
                    current_step=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                    error_code="workspace_prepare_intent_conflict",
                ),
                False,
            )
        try:
            response = knowledge.prepare_target_workspace(request)
        except Exception as exc:
            logger.error(
                "分类节点变更目标 workspace 准备调用异常: operation_id=%s error_type=%s",
                lease.operation_id,
                type(exc).__name__,
            )
            self._complete_unknown_step(
                lease,
                ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                error_code="workspace_prepare_exception",
            )
            return _WorkspacePreparationExecution(
                self._mark_recovery_required(
                    lease,
                    current_step=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                    error_code="workspace_prepare_exception",
                ),
                True,
            )
        if not isinstance(response, ReassignmentWorkspacePreparationResult):
            # 供应商 Adapter 违反 Port 返回契约时，调用已经离开本进程，不能假设没有
            # 创建 workspace；写入未知检查点并交由恢复流程查回。
            logger.error(
                "分类节点变更目标 workspace 准备返回类型错误: operation_id=%s",
                lease.operation_id,
            )
            self._complete_unknown_step(
                lease,
                ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                error_code="workspace_prepare_result_invalid",
            )
            return _WorkspacePreparationExecution(
                self._mark_recovery_required(
                    lease,
                    current_step=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                    error_code="workspace_prepare_result_invalid",
                ),
                True,
            )
        if response.outcome in {
            ReassignmentKnowledgeOutcome.APPLIED,
            ReassignmentKnowledgeOutcome.ALREADY_IN_DESIRED_STATE,
        } and response.workspace is not None and response.ownership is not None:
            mapped = self._record_workspace_mapping(
                lease,
                record.operation.target_architecture_raw,
                response.workspace.slug,
                response.ownership,
                claim,
                preserve_preparation_fact_on_failure=True,
            )
            return _WorkspacePreparationExecution(
                mapped,
                isinstance(mapped, ReassignmentResult),
            )
        if response.outcome is ReassignmentKnowledgeOutcome.KNOWN_FAILURE:
            if self._complete_step(
                lease,
                ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                ReassignmentStepState.KNOWN_FAILED,
                ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT,
                error_code="workspace_prepare_known_failure",
            ):
                return _WorkspacePreparationExecution(
                    self._compensate_confirmed_forward_effects(
                        record,
                        knowledge,
                        public_message=ReassignmentPublicMessage.REMOTE_MIGRATION_FAILED,
                        detach_target=False,
                        preparation_claim=claim,
                    ),
                    # claim 已由补偿终态原子释放，或被保留给恢复流程；finally 均不能
                    # 再用旧快照释放它。
                    True,
                )
            return _WorkspacePreparationExecution(
                self._mark_recovery_required(
                    lease,
                    current_step=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                    error_code="workspace_prepare_checkpoint_conflict",
                ),
                False,
            )
        self._complete_unknown_step(
            lease,
            ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
            error_code="workspace_prepare_outcome_unknown",
        )
        return _WorkspacePreparationExecution(
            self._mark_recovery_required(
                lease,
                current_step=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                error_code="workspace_prepare_outcome_unknown",
            ),
            True,
        )

    def _attach_and_commit(
        self,
        record: ReassignmentOperationRecord,
        knowledge: ReassignmentKnowledgePort,
        prepared: ReassignmentOperationRecord,
    ) -> ReassignmentResult:
        """加入目标成员、执行非阻断 Pin，然后把本地分类与成功终态原子提交。"""

        renewed = self._renew_execution_lease(prepared)
        if isinstance(renewed, ReassignmentResult):
            return renewed
        active_record, _ = renewed
        lease = active_record.lease
        target_workspace_slug = active_record.target_workspace_slug
        if target_workspace_slug is None:
            logger.error("分类节点变更 prepare 成功后缺少目标 slug: operation_id=%s", lease.operation_id)
            return self._mark_recovery_required(
                lease,
                current_step=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                error_code="workspace_mapping_missing_after_prepare",
            )
        if not self._begin_step_mutation(
            lease,
            ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
        ):
            return self._mark_recovery_required(
                lease,
                current_step=ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
                error_code="attach_intent_conflict",
            )
        request = ReassignmentDocumentMutationRequest(
            operation_id=record.operation.operation_id,
            step_name=ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
            workspace=ReassignmentWorkspaceReference(target_workspace_slug),
            document=ReassignmentDocumentReference.from_snapshot(
                active_record.operation.document
            ),
            architecture_raw=active_record.operation.target_architecture_raw,
            idempotency_key=build_step_idempotency_key(
                active_record.operation,
                ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
            ),
        )
        try:
            response = knowledge.attach_document(request)
        except Exception as exc:
            logger.error(
                "分类节点变更目标成员加入调用异常: operation_id=%s error_type=%s",
                lease.operation_id,
                type(exc).__name__,
            )
            self._complete_unknown_step(
                lease,
                ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
                error_code="attach_exception",
            )
            return self._mark_recovery_required(
                lease,
                current_step=ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
                error_code="attach_exception",
            )
        if not isinstance(response, ReassignmentDocumentMutationResult):
            logger.error("分类节点变更目标成员加入返回类型错误: operation_id=%s", lease.operation_id)
            self._complete_unknown_step(
                lease,
                ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
                error_code="attach_result_invalid",
            )
            return self._mark_recovery_required(
                lease,
                current_step=ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
                error_code="attach_result_invalid",
            )
        if response.outcome in {
            ReassignmentKnowledgeOutcome.APPLIED,
            ReassignmentKnowledgeOutcome.ALREADY_IN_DESIRED_STATE,
        }:
            mutation_outcome = (
                ReassignmentMutationOutcome.CONFIRMED_EFFECT
                if response.outcome is ReassignmentKnowledgeOutcome.APPLIED
                else ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT
            )
            if not self._complete_step(
                lease,
                ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
                ReassignmentStepState.SUCCEEDED,
                mutation_outcome,
                external_reference=response.external_reference,
            ):
                return self._mark_recovery_required(
                    lease,
                    current_step=ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
                    error_code="attach_checkpoint_conflict",
                )
            renewed = self._renew_execution_lease(active_record)
            if isinstance(renewed, ReassignmentResult):
                return renewed
            active_record, _ = renewed
            self._pin_target_document_best_effort(
                active_record.lease,
                knowledge,
                request,
            )
            renewed = self._renew_execution_lease(active_record)
            if isinstance(renewed, ReassignmentResult):
                return renewed
            active_record, _ = renewed
            return self._commit_local_architecture(active_record, knowledge=knowledge)
        if response.outcome is ReassignmentKnowledgeOutcome.KNOWN_FAILURE:
            if self._complete_step(
                lease,
                ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
                ReassignmentStepState.KNOWN_FAILED,
                ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT,
                error_code="attach_known_failure",
            ):
                return self._compensate_confirmed_forward_effects(
                    active_record,
                    knowledge,
                    public_message=ReassignmentPublicMessage.REMOTE_MIGRATION_FAILED,
                    detach_target=False,
                )
            return self._mark_recovery_required(
                lease,
                current_step=ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
                error_code="attach_checkpoint_conflict",
            )
        self._complete_unknown_step(
            lease,
            ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
            error_code="attach_outcome_unknown",
        )
        return self._mark_recovery_required(
            lease,
            current_step=ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
            error_code="attach_outcome_unknown",
        )

    def _pin_target_document_best_effort(
        self,
        lease: ReassignmentLease,
        knowledge: ReassignmentKnowledgePort,
        request: ReassignmentDocumentMutationRequest,
    ) -> None:
        """审计 Pin 意图和结果；失败仍不影响已确认成员关系和本地 CAS。"""

        if not self._begin_best_effort_pin_audit(lease):
            logger.warning(
                "分类节点变更无法记录 Pin 写意图，已跳过 best-effort Pin: "
                "operation_id=%s",
                request.operation_id,
            )
            return
        try:
            result = knowledge.pin_document_best_effort(request)
        except Exception as exc:
            logger.warning(
                "分类节点变更目标文档 Pin 异常，按 best-effort 忽略: operation_id=%s error_type=%s",
                request.operation_id,
                type(exc).__name__,
            )
            self._complete_best_effort_pin_audit(
                lease,
                ReassignmentMutationOutcome.OUTCOME_UNKNOWN,
                error_code="pin_exception",
            )
            return
        if not isinstance(result, ReassignmentDocumentMutationResult):
            logger.warning(
                "分类节点变更目标文档 Pin 返回类型错误，按 best-effort 忽略: operation_id=%s",
                request.operation_id,
            )
            self._complete_best_effort_pin_audit(
                lease,
                ReassignmentMutationOutcome.OUTCOME_UNKNOWN,
                error_code="pin_result_invalid",
            )
            return
        mutation_outcome = {
            ReassignmentKnowledgeOutcome.APPLIED: (
                ReassignmentMutationOutcome.CONFIRMED_EFFECT
            ),
            ReassignmentKnowledgeOutcome.ALREADY_IN_DESIRED_STATE: (
                ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT
            ),
            ReassignmentKnowledgeOutcome.KNOWN_FAILURE: (
                ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT
            ),
            ReassignmentKnowledgeOutcome.OUTCOME_UNKNOWN: (
                ReassignmentMutationOutcome.OUTCOME_UNKNOWN
            ),
        }[result.outcome]
        self._complete_best_effort_pin_audit(
            lease,
            mutation_outcome,
            error_code=(
                None
                if result.outcome
                in {
                    ReassignmentKnowledgeOutcome.APPLIED,
                    ReassignmentKnowledgeOutcome.ALREADY_IN_DESIRED_STATE,
                }
                else "pin_not_confirmed"
            ),
        )
        if result.outcome not in {
            ReassignmentKnowledgeOutcome.APPLIED,
            ReassignmentKnowledgeOutcome.ALREADY_IN_DESIRED_STATE,
        }:
            logger.warning(
                "分类节点变更目标文档 Pin 未确认成功，按 best-effort 忽略: operation_id=%s outcome=%s",
                request.operation_id,
                result.outcome.value,
            )

    def _begin_best_effort_pin_audit(self, lease: ReassignmentLease) -> bool:
        """先持久化 Pin 尝试意图；审计失败时宁可跳过非关键外部写。"""

        try:
            with self._repository.unit_of_work() as unit_of_work:
                outcome = unit_of_work.begin_best_effort_pin(lease=lease)
        except Exception as exc:
            logger.warning(
                "分类节点变更无法记录 Pin 尝试意图: "
                "operation_id=%s error_type=%s",
                lease.operation_id,
                type(exc).__name__,
            )
            return False
        return outcome is ReassignmentWriteOutcome.APPLIED

    def _complete_best_effort_pin_audit(
        self,
        lease: ReassignmentLease,
        mutation_outcome: ReassignmentMutationOutcome,
        *,
        error_code: str | None,
    ) -> None:
        """尽力记录 Pin 结果；不让非关键审计完成失败推翻核心迁移结果。"""

        try:
            with self._repository.unit_of_work() as unit_of_work:
                outcome = unit_of_work.complete_best_effort_pin(
                    ReassignmentBestEffortPinCompletion(
                        lease=lease,
                        mutation_outcome=mutation_outcome,
                        error_code=error_code,
                    )
                )
        except Exception as exc:
            logger.warning(
                "分类节点变更无法记录 Pin 完成事实: "
                "operation_id=%s error_type=%s",
                lease.operation_id,
                type(exc).__name__,
            )
            return
        if outcome is not ReassignmentWriteOutcome.APPLIED:
            logger.warning(
                "分类节点变更 Pin 完成事实写入被拒绝: "
                "operation_id=%s outcome=%s",
                lease.operation_id,
                outcome.value,
            )

    def _commit_local_architecture(
        self,
        record: ReassignmentOperationRecord,
        *,
        knowledge: ReassignmentKnowledgePort | None = None,
    ) -> ReassignmentResult:
        """把条件更新、commit Step 和成功 Operation 放入 Repository 的同一短事务。"""

        lease = record.lease
        if not self._begin_step_mutation(
            lease,
            ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
        ):
            if knowledge is not None:
                return self._compensate_confirmed_forward_effects(
                    record,
                    knowledge,
                    public_message=ReassignmentPublicMessage.LOCAL_STATE_CONFLICT,
                    detach_target=True,
                )
            return self._finalize_no_side_effect_failure(
                lease,
                current_step=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
                error_code="local_commit_intent_conflict",
                public_message=ReassignmentPublicMessage.LOCAL_STATE_CONFLICT,
            )
        try:
            with self._repository.unit_of_work() as unit_of_work:
                committed = unit_of_work.commit_local_architecture(
                    ReassignmentLocalCommitRequest(
                        lease=lease,
                        expected_document=record.operation.document,
                        target_architecture_raw=record.operation.target_architecture_raw,
                        terminal_evidence=ReassignmentTerminalEvidence(
                            ReassignmentTerminalEvidenceKind.FORWARD_SUCCESS_CONFIRMED
                        ),
                    )
                )
        except Exception as exc:
            logger.error(
                "分类节点变更本地 CAS 提交异常: operation_id=%s error_type=%s",
                lease.operation_id,
                type(exc).__name__,
            )
            reconciled = self._reconcile_committed_success(record)
            if reconciled is not None:
                return reconciled
            if knowledge is not None:
                return self._compensate_confirmed_forward_effects(
                    record,
                    knowledge,
                    public_message=ReassignmentPublicMessage.LOCAL_STATE_CONFLICT,
                    detach_target=True,
                )
            return self._mark_recovery_required(
                lease,
                current_step=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
                error_code="local_commit_exception",
            )
        if isinstance(committed, ReassignmentOperationRecord):
            logger.info("分类节点变更同步 Saga 成功: operation_id=%s", lease.operation_id)
            return ReassignmentResult(
                ReassignmentResultCategory.SUCCEEDED,
                ReassignmentPublicMessage.SUCCEEDED,
            )
        # Port 返回值异常或条件结果与调用方预期不一致时，先按持久化终态对账。数据库可能
        # 已经原子提交成功，只是在事务确认、连接关闭或 Adapter 返回阶段丢失了确认。
        reconciled = self._reconcile_committed_success(record)
        if reconciled is not None:
            return reconciled
        if committed is ReassignmentWriteOutcome.CONFLICT:
            if knowledge is not None:
                return self._compensate_confirmed_forward_effects(
                    record,
                    knowledge,
                    public_message=ReassignmentPublicMessage.LOCAL_STATE_CONFLICT,
                    detach_target=True,
                )
            # 本地-only 路径没有远端副作用，可由专用收口释放保护。
            return self._finalize_no_side_effect_failure(
                lease,
                current_step=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
                error_code="local_commit_conflict",
                public_message=ReassignmentPublicMessage.LOCAL_STATE_CONFLICT,
            )
        if knowledge is not None:
            return self._compensate_confirmed_forward_effects(
                record,
                knowledge,
                public_message=ReassignmentPublicMessage.LOCAL_STATE_CONFLICT,
                detach_target=True,
            )
        return self._mark_recovery_required(
            lease,
            current_step=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
            error_code="local_commit_write_outcome",
        )

    def _reconcile_committed_success(
        self,
        record: ReassignmentOperationRecord,
    ) -> ReassignmentResult | None:
        """提交确认不确定时，以原子持久化终态对账，避免成功后误报失败。

        ``commit_local_architecture`` 会在同一事务中更新文档、Commit Step 和 Operation；因此
        持久化 ``succeeded`` 本身就是完整成功的权威证据。终态后文档可能按正常流程被删除，
        这里不能再依赖当前 documents 行是否仍存在来否定已经完成的历史事实。
        """

        try:
            with self._repository.unit_of_work(read_only=True) as unit_of_work:
                current = unit_of_work.get_operation(record.operation.operation_id)
        except Exception as exc:
            logger.error(
                "分类节点变更提交结果对账读取失败: operation_id=%s error_type=%s",
                record.operation.operation_id,
                type(exc).__name__,
            )
            return None
        if (
            isinstance(current, ReassignmentOperationRecord)
            and current.operation.status is ReassignmentOperationStatus.SUCCEEDED
        ):
            logger.warning(
                "分类节点变更提交确认异常但持久化终态已成功: operation_id=%s",
                record.operation.operation_id,
            )
            return ReassignmentResult(
                ReassignmentResultCategory.SUCCEEDED,
                ReassignmentPublicMessage.SUCCEEDED,
            )
        return None

    def _compensate_confirmed_forward_effects(
        self,
        record: ReassignmentOperationRecord,
        knowledge: ReassignmentKnowledgePort,
        *,
        public_message: ReassignmentPublicMessage,
        detach_target: bool,
        preparation_claim: ReassignmentWorkspacePreparationClaim | None = None,
    ) -> ReassignmentResult:
        """在当前同步请求内撤销已确认的远端副作用并安全释放文档保护。

        本入口只处理前向事实已经明确的场景：目标加入明确失败时目标成员已知不存在；
        本地 CAS 冲突发生在目标加入明确成功之后，因此必须先解绑目标。任何本地探测、
        写意图、远端返回或检查点无法确认时都立即停止，不会盲目重放补偿写。
        """

        operation_id = record.operation.operation_id
        current = self._read_current_operation(operation_id)
        if current is None:
            logger.error("分类节点变更同步补偿无法读取 Operation: operation_id=%s", operation_id)
            return self._recovery_required()
        lease = current.lease

        local_state = self._probe_local_commit_state(operation_id)
        if local_state is not ReassignmentLocalCommitState.SOURCE_UNCHANGED:
            logger.warning(
                "分类节点变更同步补偿前无法确认本地仍指向来源: operation_id=%s local_state=%s",
                operation_id,
                getattr(local_state, "value", "probe_failed"),
            )
            return self._mark_recovery_required(
                lease,
                current_step=ReassignmentStepName.COMMIT_LOCAL_ARCHITECTURE,
                error_code="synchronous_compensation_local_state_unknown",
            )

        try:
            with self._repository.unit_of_work() as unit_of_work:
                transitioned = unit_of_work.transition_operation(
                    ReassignmentOperationTransition(
                        lease=lease,
                        next_status=ReassignmentOperationStatus.COMPENSATING,
                        current_step=(
                            ReassignmentStepName.COMPENSATE_TARGET_DOCUMENT
                            if detach_target
                            else ReassignmentStepName.COMPENSATE_SOURCE_DOCUMENT
                        ),
                    )
                )
        except Exception as exc:
            logger.error(
                "分类节点变更同步补偿无法进入补偿态: operation_id=%s error_type=%s",
                operation_id,
                type(exc).__name__,
            )
            return self._mark_recovery_required(
                lease,
                current_step=ReassignmentStepName.FINALIZE_OPERATION,
                error_code="synchronous_compensating_transition_exception",
            )
        if not isinstance(transitioned, ReassignmentOperationRecord):
            return self._mark_recovery_required(
                lease,
                current_step=ReassignmentStepName.FINALIZE_OPERATION,
                error_code="synchronous_compensating_transition_conflict",
            )
        current = transitioned

        actions: list[
            tuple[
                ReassignmentStepName,
                ReassignmentWorkspaceReference,
                object,
                bool,
            ]
        ] = []
        current_claim = preparation_claim
        if detach_target:
            if current.target_workspace_slug is None:
                return self._mark_recovery_required(
                    current.lease,
                    current_step=ReassignmentStepName.COMPENSATE_TARGET_DOCUMENT,
                    error_code="synchronous_compensation_target_workspace_missing",
                )
            actions.append(
                (
                    ReassignmentStepName.COMPENSATE_TARGET_DOCUMENT,
                    ReassignmentWorkspaceReference(current.target_workspace_slug),
                    current.operation.target_architecture_raw,
                    True,
                )
            )
        if current.source_workspace_slug is not None:
            actions.append(
                (
                    ReassignmentStepName.COMPENSATE_SOURCE_DOCUMENT,
                    ReassignmentWorkspaceReference(current.source_workspace_slug),
                    current.operation.source_architecture_raw,
                    False,
                )
            )

        terminal_step = ReassignmentStepName.FINALIZE_OPERATION
        for step_name, workspace, architecture_raw, is_detach in actions:
            terminal_step = step_name
            renewed = self._renew_execution_lease(current, current_claim)
            if isinstance(renewed, ReassignmentResult):
                return renewed
            current, current_claim = renewed
            outcome = self._execute_synchronous_compensation_step(
                current,
                knowledge,
                step_name=step_name,
                workspace=workspace,
                architecture_raw=architecture_raw,
                detach=is_detach,
            )
            if outcome is ReassignmentKnowledgeOutcome.KNOWN_FAILURE:
                self._mark_recovery_required(
                    current.lease,
                    current_step=step_name,
                    error_code="synchronous_compensation_known_failure",
                )
                return ReassignmentResult(
                    ReassignmentResultCategory.RECOVERY_REQUIRED,
                    ReassignmentPublicMessage.COMPENSATION_FAILED,
                )
            if outcome not in {
                ReassignmentKnowledgeOutcome.APPLIED,
                ReassignmentKnowledgeOutcome.ALREADY_IN_DESIRED_STATE,
            }:
                return self._mark_recovery_required(
                    current.lease,
                    current_step=step_name,
                    error_code="synchronous_compensation_outcome_unknown",
                )
            refreshed = self._read_current_operation(operation_id)
            if refreshed is None:
                return self._recovery_required()
            current = refreshed

        # 补偿远端返回只证明成员关系，最终释放保护前仍需再次读取本地权威行，并把
        # 双侧结论持久化为终态门禁使用的审计事实。
        local_state = self._probe_local_commit_state(operation_id)
        if local_state is not ReassignmentLocalCommitState.SOURCE_UNCHANGED:
            return self._mark_recovery_required(
                current.lease,
                current_step=terminal_step,
                error_code="synchronous_compensation_local_recheck_failed",
            )
        observation = self._record_synchronous_compensation_observation(
            current,
            local_state=local_state,
        )
        if observation is None:
            return self._mark_recovery_required(
                current.lease,
                current_step=terminal_step,
                error_code="synchronous_compensation_observation_failed",
            )
        try:
            with self._repository.unit_of_work() as unit_of_work:
                finalized = unit_of_work.finalize_recovery_operation(
                    ReassignmentRecoveryFinalizationRequest(
                        lease=current.lease,
                        observation=observation,
                        next_status=ReassignmentOperationStatus.COMPENSATED,
                        current_step=terminal_step,
                        terminal_evidence=ReassignmentTerminalEvidence(
                            ReassignmentTerminalEvidenceKind.COMPENSATION_CONFIRMED
                        ),
                        preparation_claim=current_claim,
                        error_code="synchronous_compensation_confirmed",
                    )
                )
        except Exception as exc:
            logger.error(
                "分类节点变更同步补偿终态提交异常: operation_id=%s error_type=%s",
                operation_id,
                type(exc).__name__,
            )
            return self._mark_recovery_required(
                current.lease,
                current_step=terminal_step,
                error_code="synchronous_compensation_finalize_exception",
            )
        if not isinstance(finalized, ReassignmentOperationRecord):
            return self._mark_recovery_required(
                current.lease,
                current_step=terminal_step,
                error_code="synchronous_compensation_finalize_conflict",
            )
        logger.info(
            "分类节点变更同步补偿已确认完成: operation_id=%s trigger_message=%s",
            operation_id,
            public_message.value,
        )
        return ReassignmentResult(
            ReassignmentResultCategory.COMPENSATED,
            public_message,
        )

    def _execute_synchronous_compensation_step(
        self,
        record: ReassignmentOperationRecord,
        knowledge: ReassignmentKnowledgePort,
        *,
        step_name: ReassignmentStepName,
        workspace: ReassignmentWorkspaceReference,
        architecture_raw: object,
        detach: bool,
    ) -> ReassignmentKnowledgeOutcome | None:
        """执行一笔带持久意图的同步补偿写，并返回可供公开文案分类的四态结果。"""

        lease = record.lease
        if not self._begin_step_mutation(lease, step_name):
            return None
        request = ReassignmentDocumentMutationRequest(
            operation_id=record.operation.operation_id,
            step_name=step_name,
            workspace=workspace,
            document=ReassignmentDocumentReference.from_snapshot(
                record.operation.document
            ),
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
                "分类节点变更同步补偿远端调用异常: operation_id=%s step=%s error_type=%s",
                lease.operation_id,
                step_name.value,
                type(exc).__name__,
            )
            self._complete_unknown_step(
                lease,
                step_name,
                error_code="synchronous_compensation_exception",
            )
            return ReassignmentKnowledgeOutcome.OUTCOME_UNKNOWN
        if not isinstance(result, ReassignmentDocumentMutationResult):
            self._complete_unknown_step(
                lease,
                step_name,
                error_code="synchronous_compensation_result_invalid",
            )
            return ReassignmentKnowledgeOutcome.OUTCOME_UNKNOWN

        if result.outcome is ReassignmentKnowledgeOutcome.APPLIED:
            state = ReassignmentStepState.SUCCEEDED
            mutation = ReassignmentMutationOutcome.CONFIRMED_EFFECT
        elif result.outcome is ReassignmentKnowledgeOutcome.ALREADY_IN_DESIRED_STATE:
            state = ReassignmentStepState.SUCCEEDED
            mutation = ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT
        elif result.outcome is ReassignmentKnowledgeOutcome.KNOWN_FAILURE:
            state = ReassignmentStepState.KNOWN_FAILED
            mutation = ReassignmentMutationOutcome.CONFIRMED_NO_EFFECT
        else:
            state = ReassignmentStepState.OUTCOME_UNKNOWN
            mutation = ReassignmentMutationOutcome.OUTCOME_UNKNOWN
        if not self._complete_step(
            lease,
            step_name,
            state,
            mutation,
            external_reference=result.external_reference,
            error_code=result.error_code,
        ):
            # 远端结果已经返回但检查点未确认时，禁止依据返回值继续下一笔补偿。
            return None
        return result.outcome

    def _read_current_operation(
        self,
        operation_id: str,
    ) -> ReassignmentOperationRecord | None:
        """以只读短事务取得当前 Operation；异常不伪装成不存在。"""

        try:
            with self._repository.unit_of_work(read_only=True) as unit_of_work:
                record = unit_of_work.get_operation(operation_id)
        except Exception as exc:
            logger.error(
                "分类节点变更读取当前 Operation 异常: operation_id=%s error_type=%s",
                operation_id,
                type(exc).__name__,
            )
            return None
        return record if isinstance(record, ReassignmentOperationRecord) else None

    def _probe_local_commit_state(
        self,
        operation_id: str,
    ) -> ReassignmentLocalCommitState | None:
        """读取本地权威分类状态；失败时同步补偿不得发起任何远端写。"""

        try:
            with self._repository.unit_of_work(read_only=True) as unit_of_work:
                state = unit_of_work.probe_local_commit_state(operation_id)
        except Exception as exc:
            logger.error(
                "分类节点变更同步补偿本地探测异常: operation_id=%s error_type=%s",
                operation_id,
                type(exc).__name__,
            )
            return None
        return state if isinstance(state, ReassignmentLocalCommitState) else None

    def _record_synchronous_compensation_observation(
        self,
        record: ReassignmentOperationRecord,
        *,
        local_state: ReassignmentLocalCommitState,
    ) -> ReassignmentRecoveryObservationRecord | None:
        """记录同步补偿后的最小双侧事实，不保存文件路径或供应商响应正文。"""

        try:
            with self._repository.unit_of_work() as unit_of_work:
                observation = unit_of_work.record_recovery_observation(
                    ReassignmentRecoveryObservation(
                        lease=record.lease,
                        local_commit_state=local_state,
                        source_binding_state=(
                            ReassignmentBindingState.CONFIRMED_PRESENT
                            if record.source_workspace_slug is not None
                            else ReassignmentBindingState.NOT_APPLICABLE
                        ),
                        target_binding_state=ReassignmentBindingState.CONFIRMED_ABSENT,
                        remote_membership_required=True,
                        actor="synchronous-reassignment",
                        reason_code="forward_failure_compensation",
                    )
                )
        except Exception as exc:
            logger.error(
                "分类节点变更同步补偿观测写入异常: operation_id=%s error_type=%s",
                record.operation.operation_id,
                type(exc).__name__,
            )
            return None
        return (
            observation
            if isinstance(observation, ReassignmentRecoveryObservationRecord)
            else None
        )

    def _renew_execution_lease(
        self,
        record: ReassignmentOperationRecord,
        claim: ReassignmentWorkspacePreparationClaim | None = None,
    ) -> (
        tuple[
            ReassignmentOperationRecord,
            ReassignmentWorkspacePreparationClaim | None,
        ]
        | ReassignmentResult
    ):
        """在远端步骤边界续租，并同步延长当前 Operation 持有的目标准备权。"""

        lease = record.lease
        try:
            with self._repository.unit_of_work() as unit_of_work:
                # 和初始保留相同，续租到期时间从取得写事务后开始计算。
                lease_expires_at = self._settings.lease_expires_at()
                result = unit_of_work.renew_lease(
                    lease=lease,
                    lease_expires_at=lease_expires_at,
                )
                refreshed = unit_of_work.get_operation(lease.operation_id)
        except Exception as exc:
            logger.error(
                "分类节点变更步骤边界续租异常: operation_id=%s error_type=%s",
                lease.operation_id,
                type(exc).__name__,
            )
            return self._mark_recovery_required(
                lease,
                current_step=record.operation.current_step
                or ReassignmentStepName.RESERVE_DOCUMENT,
                error_code="lease_renewal_exception",
            )
        if (
            not isinstance(result, ReassignmentLeaseUpdateResult)
            or result.outcome is not ReassignmentWriteOutcome.APPLIED
            or result.lease is None
            or not isinstance(refreshed, ReassignmentOperationRecord)
            or refreshed.lease != result.lease
        ):
            logger.warning(
                "分类节点变更步骤边界续租被拒绝或返回契约错误: "
                "operation_id=%s outcome=%s",
                lease.operation_id,
                getattr(getattr(result, "outcome", None), "value", "invalid_result"),
            )
            return self._mark_recovery_required(
                lease,
                current_step=record.operation.current_step
                or ReassignmentStepName.RESERVE_DOCUMENT,
                error_code="lease_renewal_conflict",
            )

        renewed_claim = result.workspace_preparation_claim
        if claim is None and renewed_claim is not None:
            logger.error(
                "分类节点变更续租返回了调用方未持有的目标准备权: operation_id=%s",
                lease.operation_id,
            )
            return self._mark_recovery_required(
                result.lease,
                current_step=refreshed.operation.current_step
                or ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                error_code="lease_renewal_claim_contract_error",
            )
        if claim is not None:
            if (
                renewed_claim is None
                or renewed_claim.operation_id != claim.operation_id
                or renewed_claim.owner != claim.owner
                or renewed_claim.token != claim.token
                or renewed_claim.fencing_token != claim.fencing_token
                or renewed_claim.target_architecture_raw.canonical_json()
                != claim.target_architecture_raw.canonical_json()
            ):
                logger.warning(
                    "分类节点变更续租未能保持目标准备权: operation_id=%s",
                    lease.operation_id,
                )
                return self._mark_recovery_required(
                    result.lease,
                    current_step=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                    error_code="workspace_claim_renewal_conflict",
                )
        return refreshed, renewed_claim

    def _workspace_preparation_request(
        self,
        record: ReassignmentOperationRecord,
    ) -> ReassignmentWorkspacePreparationRequest:
        """按遗留 f-string 规则构造 workspace 名称，不擅自规范化公开新分类原始值。"""

        raw_target_value = record.operation.target_architecture_raw.to_python()
        return ReassignmentWorkspacePreparationRequest(
            operation_id=record.operation.operation_id,
            target_architecture_raw=record.operation.target_architecture_raw,
            desired_workspace_name=f"architectureId-{raw_target_value}",
            idempotency_key=build_step_idempotency_key(
                record.operation,
                ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
            ),
        )

    def _acquire_workspace_preparation_claim(
        self,
        lease: ReassignmentLease,
        target_architecture_raw: object,
    ) -> ReassignmentWorkspacePreparationClaimResult | ReassignmentWriteOutcome:
        """申请目标分类唯一准备权；整个申请不包含网络 I/O。"""

        claim_token = _required_text(
            self._settings.workspace_claim_token_factory(),
            name="workspace_claim_token_factory 返回值",
        )
        request = ReassignmentWorkspacePreparationClaimRequest(
            operation_lease=lease,
            target_architecture_raw=target_architecture_raw,
            claim_token=claim_token,
            claim_expires_at=lease.expires_at,
        )
        with self._repository.unit_of_work() as unit_of_work:
            return unit_of_work.acquire_workspace_preparation_claim(request)

    def _release_workspace_preparation_claim(
        self,
        claim: ReassignmentWorkspacePreparationClaim,
    ) -> None:
        """尽力释放尚未随 mapping 原子释放的 claim；失败后由过期接管兜底。"""

        try:
            with self._repository.unit_of_work() as unit_of_work:
                outcome = unit_of_work.release_workspace_preparation_claim(claim)
        except Exception as exc:
            logger.warning(
                "分类节点变更释放目标 workspace 准备权异常，等待过期接管: operation_id=%s error_type=%s",
                claim.operation_id,
                type(exc).__name__,
            )
            return
        if outcome is not ReassignmentWriteOutcome.APPLIED:
            logger.warning(
                "分类节点变更目标 workspace 准备权未立即释放，等待过期接管: operation_id=%s outcome=%s",
                claim.operation_id,
                outcome.value,
            )

    def _begin_step_mutation(
        self,
        lease: ReassignmentLease,
        step_name: ReassignmentStepName,
    ) -> bool:
        """外部写或本地 CAS 前持久化写意图；失败时调用者不得继续执行动作。"""

        try:
            with self._repository.unit_of_work() as unit_of_work:
                result = unit_of_work.begin_step_mutation(
                    lease=lease,
                    step_name=step_name,
                )
        except Exception as exc:
            logger.error(
                "分类节点变更无法记录步骤写意图: operation_id=%s step=%s error_type=%s",
                lease.operation_id,
                step_name.value,
                type(exc).__name__,
            )
            return False
        if not isinstance(result, ReassignmentStepRecord):
            logger.error(
                "分类节点变更步骤写意图返回契约错误: "
                "operation_id=%s step=%s result_type=%s",
                lease.operation_id,
                step_name.value,
                type(result).__name__,
            )
            return False
        return True

    def _complete_step(
        self,
        lease: ReassignmentLease,
        step_name: ReassignmentStepName,
        next_state: ReassignmentStepState,
        probe_outcome: ReassignmentMutationOutcome,
        *,
        external_reference: str | None = None,
        error_code: str | None = None,
    ) -> bool:
        """持久化外部动作的四分类结果，不把供应商正文写入内部事实。"""

        try:
            with self._repository.unit_of_work() as unit_of_work:
                result = unit_of_work.complete_step(
                    ReassignmentStepCompletion(
                        lease=lease,
                        step_name=step_name,
                        next_state=next_state,
                        external_reference=external_reference,
                        error_code=error_code,
                        probe_outcome=probe_outcome,
                    )
                )
        except Exception as exc:
            logger.error(
                "分类节点变更无法保存步骤结果: operation_id=%s step=%s error_type=%s",
                lease.operation_id,
                step_name.value,
                type(exc).__name__,
            )
            return False
        if not isinstance(result, ReassignmentStepRecord):
            logger.error(
                "分类节点变更步骤结果返回契约错误: "
                "operation_id=%s step=%s result_type=%s",
                lease.operation_id,
                step_name.value,
                type(result).__name__,
            )
            return False
        return True

    def _complete_unknown_step(
        self,
        lease: ReassignmentLease,
        step_name: ReassignmentStepName,
        *,
        error_code: str,
    ) -> None:
        """尽力写入未知结果检查点；即使检查点写失败，调用方仍会隔离 Operation。"""

        completed = self._complete_step(
            lease,
            step_name,
            ReassignmentStepState.OUTCOME_UNKNOWN,
            ReassignmentMutationOutcome.OUTCOME_UNKNOWN,
            error_code=error_code,
        )
        if not completed:
            logger.error(
                "分类节点变更未知结果检查点未写入: operation_id=%s step=%s",
                lease.operation_id,
                step_name.value,
            )

    def _record_workspace_mapping(
        self,
        lease: ReassignmentLease,
        target_architecture_raw: object,
        workspace_slug: str,
        ownership: ReassignmentWorkspaceOwnership,
        claim: ReassignmentWorkspacePreparationClaim | None = None,
        *,
        preserve_preparation_fact_on_failure: bool = False,
    ) -> ReassignmentOperationRecord | ReassignmentResult:
        """以同一短事务保存 workspace mapping、prepare 成功事实及可选 claim release。"""

        try:
            with self._repository.unit_of_work() as unit_of_work:
                result = unit_of_work.record_workspace_mapping(
                    ReassignmentWorkspaceMappingRequest(
                        lease=lease,
                        target_architecture_raw=target_architecture_raw,
                        workspace_slug=workspace_slug,
                        ownership=ownership,
                        preparation_claim=claim,
                    )
                )
        except Exception as exc:
            logger.error(
                "分类节点变更无法保存目标 workspace mapping: operation_id=%s error_type=%s",
                lease.operation_id,
                type(exc).__name__,
            )
            if preserve_preparation_fact_on_failure:
                self._record_workspace_preparation_fact(
                    lease,
                    workspace_slug,
                    ownership,
                    error_code="workspace_mapping_exception",
                )
            return self._mark_recovery_required(
                lease,
                current_step=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
                error_code="workspace_mapping_exception",
            )
        if isinstance(result, ReassignmentOperationRecord):
            return result
        if preserve_preparation_fact_on_failure:
            self._record_workspace_preparation_fact(
                lease,
                workspace_slug,
                ownership,
                error_code="workspace_mapping_conflict",
            )
        return self._mark_recovery_required(
            lease,
            current_step=ReassignmentStepName.PREPARE_TARGET_WORKSPACE,
            error_code="workspace_mapping_conflict",
        )

    def _record_workspace_preparation_fact(
        self,
        lease: ReassignmentLease,
        workspace_slug: str,
        ownership: ReassignmentWorkspaceOwnership,
        *,
        error_code: str,
    ) -> None:
        """尽力保存远端成功但 mapping 未提交的准确恢复现场。"""

        try:
            with self._repository.unit_of_work() as unit_of_work:
                result = unit_of_work.record_workspace_preparation_fact(
                    ReassignmentWorkspacePreparationFactRequest(
                        lease=lease,
                        workspace_slug=workspace_slug,
                        ownership=ownership,
                        error_code=error_code,
                    )
                )
        except Exception as exc:
            logger.error(
                "分类节点变更无法保存待恢复 workspace 准备事实: "
                "operation_id=%s error_type=%s",
                lease.operation_id,
                type(exc).__name__,
            )
            return
        if not isinstance(result, ReassignmentOperationRecord):
            logger.error(
                "分类节点变更待恢复 workspace 准备事实写入被拒绝: "
                "operation_id=%s outcome=%s",
                lease.operation_id,
                getattr(result, "value", "invalid_result"),
            )

    def _finalize_no_side_effect_failure(
        self,
        lease: ReassignmentLease,
        *,
        current_step: ReassignmentStepName,
        error_code: str,
        public_message: ReassignmentPublicMessage,
    ) -> ReassignmentResult:
        """尝试安全失败收口；Repository 发现任何副作用时自动退回恢复隔离。"""

        try:
            with self._repository.unit_of_work() as unit_of_work:
                result = unit_of_work.finalize_no_side_effect_failure(
                    ReassignmentNoSideEffectFailureRequest(
                        lease=lease,
                        current_step=current_step,
                        error_code=error_code,
                        error_summary=None,
                        terminal_evidence=ReassignmentTerminalEvidence(
                            ReassignmentTerminalEvidenceKind.NO_SIDE_EFFECT_FAILURE_CONFIRMED
                        ),
                    )
                )
        except Exception as exc:
            logger.warning(
                "分类节点变更无法按无副作用失败收口，转入恢复隔离: operation_id=%s error_type=%s",
                lease.operation_id,
                type(exc).__name__,
            )
            return self._mark_recovery_required(
                lease,
                current_step=current_step,
                error_code=f"{error_code}_recovery_required",
            )
        if isinstance(result, ReassignmentOperationRecord):
            logger.info(
                "分类节点变更已安全失败收口: operation_id=%s error_code=%s",
                lease.operation_id,
                error_code,
            )
            return self._failed(public_message)
        return self._mark_recovery_required(
            lease,
            current_step=current_step,
            error_code=f"{error_code}_write_outcome",
        )

    def _mark_recovery_required(
        self,
        lease: ReassignmentLease,
        *,
        current_step: ReassignmentStepName,
        error_code: str,
    ) -> ReassignmentResult:
        """将可能存在跨系统不一致的现场隔离，禁止本次请求继续尝试补偿或重放。"""

        try:
            with self._repository.unit_of_work() as unit_of_work:
                result = unit_of_work.transition_operation(
                    ReassignmentOperationTransition(
                        lease=lease,
                        next_status=ReassignmentOperationStatus.RECOVERY_REQUIRED,
                        current_step=current_step,
                        error_code=error_code,
                    )
                )
        except Exception as exc:
            logger.error(
                "分类节点变更无法记录恢复隔离: operation_id=%s error_type=%s",
                lease.operation_id,
                type(exc).__name__,
            )
        else:
            if isinstance(result, ReassignmentOperationRecord):
                logger.warning(
                    "分类节点变更已进入恢复隔离: operation_id=%s step=%s error_code=%s",
                    lease.operation_id,
                    current_step.value,
                    error_code,
                )
            else:
                logger.error(
                    "分类节点变更恢复隔离写入被拒绝: operation_id=%s outcome=%s",
                    lease.operation_id,
                    result.value,
                )
        return self._recovery_required()

    @staticmethod
    def _failed(message: ReassignmentPublicMessage) -> ReassignmentResult:
        """构造不携带 Operation、lease、fencing 的稳定公开失败结果。"""

        return ReassignmentResult(ReassignmentResultCategory.FAILED, message)

    @staticmethod
    def _recovery_required() -> ReassignmentResult:
        """构造“现场待恢复”的稳定公开结果。"""

        return ReassignmentResult(
            ReassignmentResultCategory.RECOVERY_REQUIRED,
            ReassignmentPublicMessage.RECOVERY_PENDING,
        )

    @staticmethod
    def _document_marker(document: ReassignmentDocumentSnapshot) -> str:
        """为日志生成不可逆文档标记，避免记录文件名和路径。"""

        value = f"{document.document_row_id}:{document.file_name}".encode("utf-8")
        return hashlib.sha256(value).hexdigest()[:16]

    @staticmethod
    def _command_marker(command: ReassignDocumentCommand) -> str:
        """文档不存在等未生成快照的路径也使用同样的脱敏日志关联标记。"""

        value = f"{command.file_name}:{command.old_architecture_id_query_value}".encode(
            "utf-8"
        )
        return hashlib.sha256(value).hexdigest()[:16]


__all__ = ["DocumentReassignmentService", "ReassignmentExecutionSettings"]
