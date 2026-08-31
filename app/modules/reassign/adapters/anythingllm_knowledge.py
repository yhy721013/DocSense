"""分类节点变更的 AnythingLLM Knowledge Port 适配器。

适配器将供应商原子 Client 的异常、``false`` 与协议异常转换为端口定义的四分类结果。它不访问
SQLite、不创建 Operation/Step，也不在数据库事务中执行网络调用；未来 Application 必须先提交
``mutation_started``，再调用本适配器，并把确定结果或未知现场写回 Repository。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from time import monotonic
from typing import Callable, TypeVar

from app.integrations.anythingllm import (
    AnythingLLMConnectionError,
    AnythingLLMDocument,
    AnythingLLMHTTPError,
    AnythingLLMProtocolError,
    AnythingLLMTimeoutError,
    AnythingLLMTransportClosedError,
    AnythingLLMTransportError,
    AnythingLLMWorkspace,
)
from app.integrations.anythingllm.models import normalize_document_location_key
from app.modules.reassign.domain import ReassignmentStepName
from app.modules.reassign.ports import (
    ReassignmentDocumentMutationRequest,
    ReassignmentDocumentMutationResult,
    ReassignmentKnowledgeOutcome,
    ReassignmentKnowledgePort,
    ReassignmentMembershipProbeRequest,
    ReassignmentMembershipProbeResult,
    ReassignmentMembershipState,
    ReassignmentWorkspacePreparationRequest,
    ReassignmentWorkspacePreparationResult,
    ReassignmentWorkspaceOwnership,
    ReassignmentWorkspaceProbeResult,
    ReassignmentWorkspaceProbeState,
    ReassignmentWorkspaceReference,
    ReassignmentWorkspaceReferenceProbeRequest,
)

from .anythingllm_clients import ReassignmentAnythingLLMClientFactoryProtocol
from .infrastructure_config import (
    ReassignmentDeadlineExceededError,
    ReassignmentExecutionDeadline,
    ReassignmentInfrastructureConfig,
)


logger = logging.getLogger(__name__)

_T = TypeVar("_T")


class _ProviderResultError(RuntimeError):
    """供应商返回值不满足原子 Client 协议，但写是否发生无法安全判断。"""


@dataclass(frozen=True)
class _FailureFact:
    """内部异常分类，绝不保存或返回供应商原始正文。"""

    outcome: ReassignmentKnowledgeOutcome
    error_code: str
    error_summary: str
    may_have_external_effect: bool


class AnythingLLMReassignmentKnowledgeAdapter(ReassignmentKnowledgePort):
    """单次同步分类变更请求专用的 Knowledge Port 实现。

    实例持有一次 Operation 的单调 deadline，因此**不可**作为全局单例或跨线程共享。每个原子
    HTTP 调用都通过 Client Factory 获取独立 Transport，以保证慢 I/O 不污染其他文档请求，也让
    每次请求可依据剩余预算裁剪超时。
    """

    def __init__(
        self,
        client_factory: ReassignmentAnythingLLMClientFactoryProtocol,
        infrastructure_config: ReassignmentInfrastructureConfig,
        *,
        monotonic_clock: Callable[[], float] = monotonic,
        elapsed_seconds: float = 0.0,
        user_id: int = 1,
    ) -> None:
        if not callable(getattr(client_factory, "create", None)):
            raise TypeError("client_factory 必须提供 create(timeout_seconds=...) 方法")
        if not isinstance(infrastructure_config, ReassignmentInfrastructureConfig):
            raise TypeError(
                "infrastructure_config 必须是 ReassignmentInfrastructureConfig"
            )
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
            raise ValueError("user_id 必须是正整数")
        self._client_factory = client_factory
        self._deadline = ReassignmentExecutionDeadline(
            infrastructure_config,
            monotonic_clock=monotonic_clock,
            elapsed_seconds=elapsed_seconds,
        )
        # 当前遗留链路的内部 AnythingLLM 用户上下文固定为 1。它不是公开请求参数，未来
        # 需要多租户能力时必须以完整认证边界改造，而不是从本接口透传 userId。
        self._user_id = user_id

    def prepare_target_workspace(
        self,
        request: ReassignmentWorkspacePreparationRequest,
    ) -> ReassignmentWorkspacePreparationResult:
        """按确定性名称复用或创建目标 workspace，并严格校验创建响应。"""

        self._require_workspace_request(request)
        logger.info(
            "开始准备分类节点变更目标 workspace: operation_id=%s workspace_name=%s",
            request.operation_id,
            request.desired_workspace_name,
        )
        try:
            matches = self._find_target_workspaces(request, use_recovery_budget=False)
        except Exception as exc:
            failure = self._classify_exception(
                exc,
                action="查询目标 workspace",
                write_may_have_effect=False,
            )
            logger.warning(
                "分类节点变更无法确认目标 workspace 是否已存在: operation_id=%s "
                "workspace_name=%s error_code=%s",
                request.operation_id,
                request.desired_workspace_name,
                failure.error_code,
            )
            return ReassignmentWorkspacePreparationResult(
                ReassignmentKnowledgeOutcome.OUTCOME_UNKNOWN,
                error_code=failure.error_code,
                error_summary=failure.error_summary,
            )

        if len(matches) == 1:
            logger.info(
                "分类节点变更复用已存在目标 workspace: operation_id=%s "
                "workspace_name=%s",
                request.operation_id,
                request.desired_workspace_name,
            )
            return ReassignmentWorkspacePreparationResult(
                ReassignmentKnowledgeOutcome.ALREADY_IN_DESIRED_STATE,
                workspace=self._workspace_reference(matches[0]),
                ownership=ReassignmentWorkspaceOwnership.PREEXISTING,
            )
        if len(matches) > 1:
            logger.warning(
                "分类节点变更目标 workspace 名称存在多重精确匹配: operation_id=%s "
                "workspace_name=%s count=%d",
                request.operation_id,
                request.desired_workspace_name,
                len(matches),
            )
            return ReassignmentWorkspacePreparationResult(
                ReassignmentKnowledgeOutcome.OUTCOME_UNKNOWN,
                error_code="workspace_identity_ambiguous",
                error_summary="目标 workspace 身份存在多个精确匹配，不能自动选择",
            )

        try:
            created = self._run_forward(
                request.operation_id,
                "创建目标 workspace",
                lambda clients: clients.workspaces.create_workspace(
                    request.desired_workspace_name,
                    user_id=self._user_id,
                ),
            )
        except Exception as exc:
            return self._resolve_uncertain_workspace_creation(request, exc)

        # ``false`` 是供应商显式拒绝，当前调用可以确定没有取得可提交的 workspace 引用。
        if created is False:
            logger.warning(
                "分类节点变更目标 workspace 创建被供应商明确拒绝: operation_id=%s "
                "workspace_name=%s",
                request.operation_id,
                request.desired_workspace_name,
            )
            return ReassignmentWorkspacePreparationResult(
                ReassignmentKnowledgeOutcome.KNOWN_FAILURE,
                error_code="workspace_create_returned_false",
                error_summary="目标 workspace 创建被供应商明确拒绝",
            )

        try:
            created_workspace = self._require_created_workspace(
                created,
                request=request,
            )
        except Exception as exc:
            # 响应缺 slug、类型错误或返回了身份不匹配对象时，无法证明服务端没有创建资源；
            # 必须查回并保守进入未知结果，不能把对象强转为可提交成功。
            return self._resolve_uncertain_workspace_creation(request, exc)

        logger.info(
            "分类节点变更目标 workspace 已创建并取得有效 slug: operation_id=%s "
            "workspace_name=%s",
            request.operation_id,
            request.desired_workspace_name,
        )
        return ReassignmentWorkspacePreparationResult(
            ReassignmentKnowledgeOutcome.APPLIED,
            workspace=self._workspace_reference(created_workspace),
            ownership=ReassignmentWorkspaceOwnership.CREATED_BY_OPERATION,
        )

    def probe_target_workspace(
        self,
        request: ReassignmentWorkspacePreparationRequest,
    ) -> ReassignmentWorkspaceProbeResult:
        """只读查回确定性目标名称，不创建、不更新远端 workspace。"""

        self._require_workspace_request(request)
        try:
            matches = self._find_target_workspaces(request, use_recovery_budget=True)
        except Exception as exc:
            failure = self._classify_exception(
                exc,
                action="查回目标 workspace",
                write_may_have_effect=False,
            )
            logger.warning(
                "分类节点变更目标 workspace 查回失败: operation_id=%s error_code=%s",
                request.operation_id,
                failure.error_code,
            )
            return ReassignmentWorkspaceProbeResult(
                ReassignmentWorkspaceProbeState.OUTCOME_UNKNOWN,
                error_code=failure.error_code,
                error_summary=failure.error_summary,
            )

        if not matches:
            return ReassignmentWorkspaceProbeResult(
                ReassignmentWorkspaceProbeState.ABSENT,
            )
        if len(matches) == 1:
            # 只读列表只能证明唯一资源存在，不能证明创建者。显式 UNKNOWN 既允许后续
            # 使用该 workspace，也能阻止补偿误删可能由其他请求创建的共享资源。
            return ReassignmentWorkspaceProbeResult(
                ReassignmentWorkspaceProbeState.PRESENT,
                workspace=self._workspace_reference(matches[0]),
                ownership=ReassignmentWorkspaceOwnership.UNKNOWN,
            )
        logger.warning(
            "分类节点变更目标 workspace 查回存在多个精确匹配: operation_id=%s count=%d",
            request.operation_id,
            len(matches),
        )
        return ReassignmentWorkspaceProbeResult(
            ReassignmentWorkspaceProbeState.OUTCOME_UNKNOWN,
            error_code="workspace_identity_ambiguous",
            error_summary="目标 workspace 存在多个精确匹配，无法确认唯一身份",
        )

    def probe_workspace_reference(
        self,
        request: ReassignmentWorkspaceReferenceProbeRequest,
    ) -> ReassignmentWorkspaceProbeResult:
        """按本地 mapping 保存的 slug 精确查回，不依赖当前确定性名称规则。"""

        if not isinstance(request, ReassignmentWorkspaceReferenceProbeRequest):
            raise TypeError("request 必须是 ReassignmentWorkspaceReferenceProbeRequest")
        try:
            workspaces = self._run_recovery(
                request.operation_id,
                "按既有 slug 查回目标 workspace",
                lambda clients: clients.workspaces.list_workspaces(user_id=self._user_id),
            )
            if not isinstance(workspaces, list):
                raise _ProviderResultError("AnythingLLM workspace 列表不是 list")
            expected_slug_key = request.workspace.slug.casefold()
            matched_by_slug: dict[str, AnythingLLMWorkspace] = {}
            for workspace in workspaces:
                if not isinstance(workspace, AnythingLLMWorkspace):
                    raise _ProviderResultError(
                        "AnythingLLM workspace 列表包含非法对象"
                    )
                reference = self._workspace_reference(workspace)
                slug_key = reference.slug.casefold()
                if slug_key == expected_slug_key:
                    matched_by_slug.setdefault(slug_key, workspace)
        except Exception as exc:
            failure = self._classify_exception(
                exc,
                action="按既有 slug 查回目标 workspace",
                write_may_have_effect=False,
            )
            logger.warning(
                "分类节点变更既有 workspace slug 查回失败: "
                "operation_id=%s error_code=%s",
                request.operation_id,
                failure.error_code,
            )
            return ReassignmentWorkspaceProbeResult(
                ReassignmentWorkspaceProbeState.OUTCOME_UNKNOWN,
                error_code=failure.error_code,
                error_summary=failure.error_summary,
            )

        if not matched_by_slug:
            return ReassignmentWorkspaceProbeResult(
                ReassignmentWorkspaceProbeState.ABSENT,
            )
        # casefold 后的 slug 是唯一资源身份键；同一资源在供应商列表中重复出现不构成歧义。
        workspace = next(iter(matched_by_slug.values()))
        return ReassignmentWorkspaceProbeResult(
            ReassignmentWorkspaceProbeState.PRESENT,
            workspace=self._workspace_reference(workspace),
            ownership=ReassignmentWorkspaceOwnership.UNKNOWN,
        )

    def probe_document_membership(
        self,
        request: ReassignmentMembershipProbeRequest,
    ) -> ReassignmentMembershipProbeResult:
        """按完整规范化 ``doc_path`` 精确查回成员关系，绝不退回文件名匹配。"""

        return self._probe_document_membership(
            request,
            use_recovery_budget=True,
        )

    def _probe_document_membership(
        self,
        request: ReassignmentMembershipProbeRequest,
        *,
        use_recovery_budget: bool,
    ) -> ReassignmentMembershipProbeResult:
        """按调用目的选择预算执行成员探测。

        对外显式探测属于恢复动作，可使用保留窗口；前向写成功后的常规确认仍使用前向
        预算，只有不确定写和补偿步骤才允许消耗恢复保留，避免正常路径提前挤占补偿能力。
        """

        if not isinstance(request, ReassignmentMembershipProbeRequest):
            raise TypeError("request 必须是 ReassignmentMembershipProbeRequest")
        runner = self._run_recovery if use_recovery_budget else self._run_forward
        try:
            document = runner(
                request.operation_id,
                "探测文档成员关系",
                lambda clients: clients.workspaces.find_document(
                    request.workspace.slug,
                    request.document.doc_path,
                    user_id=self._user_id,
                ),
            )
        except Exception as exc:
            failure = self._classify_exception(
                exc,
                action="探测文档成员关系",
                write_may_have_effect=False,
            )
            logger.warning(
                "分类节点变更文档成员关系探测失败: operation_id=%s error_code=%s",
                request.operation_id,
                failure.error_code,
            )
            return ReassignmentMembershipProbeResult(
                ReassignmentMembershipState.OUTCOME_UNKNOWN,
                error_code=failure.error_code,
                error_summary=failure.error_summary,
            )

        if document is None:
            return ReassignmentMembershipProbeResult(ReassignmentMembershipState.ABSENT)
        if not isinstance(document, AnythingLLMDocument):
            return ReassignmentMembershipProbeResult(
                ReassignmentMembershipState.OUTCOME_UNKNOWN,
                error_code="membership_probe_protocol_error",
                error_summary="成员关系探测返回了无法确认的供应商对象",
            )
        try:
            expected_location = normalize_document_location_key(request.document.doc_path)
            actual_location = normalize_document_location_key(document.location)
        except Exception:
            expected_location = ""
            actual_location = ""
        if not expected_location or actual_location != expected_location:
            logger.warning(
                "分类节点变更成员探测返回文档身份不一致: operation_id=%s",
                request.operation_id,
            )
            return ReassignmentMembershipProbeResult(
                ReassignmentMembershipState.OUTCOME_UNKNOWN,
                error_code="membership_probe_identity_conflict",
                error_summary="成员关系探测返回的文档身份与冻结 doc_path 不一致",
            )
        return ReassignmentMembershipProbeResult(ReassignmentMembershipState.PRESENT)

    def detach_document(
        self,
        request: ReassignmentDocumentMutationRequest,
    ) -> ReassignmentDocumentMutationResult:
        """从 workspace 删除精确文档，并以读后探测确认 ``ABSENT``。"""

        self._require_mutation_step(
            request,
            allowed_steps={
                ReassignmentStepName.DETACH_SOURCE_DOCUMENT,
                ReassignmentStepName.COMPENSATE_TARGET_DOCUMENT,
            },
            action="删除 workspace 文档",
        )
        return self._mutate_document_membership(
            request,
            action=(
                "补偿删除目标 workspace 文档"
                if request.step_name
                is ReassignmentStepName.COMPENSATE_TARGET_DOCUMENT
                else "删除来源 workspace 文档"
            ),
            desired_state=ReassignmentMembershipState.ABSENT,
            use_recovery_budget=(
                request.step_name
                is ReassignmentStepName.COMPENSATE_TARGET_DOCUMENT
            ),
            mutation=lambda clients: clients.workspaces.update_embeddings(
                request.workspace.slug,
                deletes=[request.document.doc_path],
                user_id=self._user_id,
            ),
        )

    def attach_document(
        self,
        request: ReassignmentDocumentMutationRequest,
    ) -> ReassignmentDocumentMutationResult:
        """向 workspace 加入精确文档，并以读后探测确认 ``PRESENT``。"""

        self._require_mutation_step(
            request,
            allowed_steps={
                ReassignmentStepName.ATTACH_TARGET_DOCUMENT,
                ReassignmentStepName.COMPENSATE_SOURCE_DOCUMENT,
            },
            action="加入 workspace 文档",
        )
        return self._mutate_document_membership(
            request,
            action=(
                "补偿恢复来源 workspace 文档"
                if request.step_name
                is ReassignmentStepName.COMPENSATE_SOURCE_DOCUMENT
                else "加入目标 workspace 文档"
            ),
            desired_state=ReassignmentMembershipState.PRESENT,
            use_recovery_budget=(
                request.step_name
                is ReassignmentStepName.COMPENSATE_SOURCE_DOCUMENT
            ),
            mutation=lambda clients: clients.workspaces.update_embeddings(
                request.workspace.slug,
                adds=[request.document.doc_path],
                user_id=self._user_id,
            ),
        )

    def pin_document_best_effort(
        self,
        request: ReassignmentDocumentMutationRequest,
    ) -> ReassignmentDocumentMutationResult:
        """保持旧链路的 best-effort Pin；失败不推翻已确认的成员关系。"""

        self._require_mutation_step(
            request,
            allowed_steps={ReassignmentStepName.ATTACH_TARGET_DOCUMENT},
            action="固定目标 workspace 文档",
        )
        try:
            pin_result = self._run_forward(
                request.operation_id,
                "固定目标 workspace 文档",
                lambda clients: clients.workspaces.update_pin(
                    request.workspace.slug,
                    request.document.doc_path,
                    pinned=True,
                    user_id=self._user_id,
                ),
            )
        except Exception as exc:
            failure = self._classify_exception(
                exc,
                action="固定目标 workspace 文档",
                write_may_have_effect=True,
            )
            logger.warning(
                "分类节点变更 best-effort Pin 未确认: operation_id=%s error_code=%s",
                request.operation_id,
                failure.error_code,
            )
            return ReassignmentDocumentMutationResult(
                failure.outcome,
                error_code=failure.error_code,
                error_summary=failure.error_summary,
            )

        if pin_result is False:
            logger.warning(
                "分类节点变更 best-effort Pin 被供应商明确拒绝: operation_id=%s",
                request.operation_id,
            )
            return ReassignmentDocumentMutationResult(
                ReassignmentKnowledgeOutcome.KNOWN_FAILURE,
                error_code="pin_returned_false",
                error_summary="供应商明确拒绝固定目标文档",
            )
        if pin_result is not None:
            return ReassignmentDocumentMutationResult(
                ReassignmentKnowledgeOutcome.OUTCOME_UNKNOWN,
                error_code="pin_protocol_error",
                error_summary="固定目标文档返回了无法确认的供应商结果",
            )
        logger.debug(
            "分类节点变更 best-effort Pin 已完成: operation_id=%s",
            request.operation_id,
        )
        return ReassignmentDocumentMutationResult(
            ReassignmentKnowledgeOutcome.APPLIED,
            external_reference=request.workspace.slug,
        )

    def _mutate_document_membership(
        self,
        request: ReassignmentDocumentMutationRequest,
        *,
        action: str,
        desired_state: ReassignmentMembershipState,
        use_recovery_budget: bool,
        mutation: Callable[[object], object],
    ) -> ReassignmentDocumentMutationResult:
        """执行一次可写操作后立即探测，统一处理 false/异常/协议不一致。"""

        self._require_mutation_request(request)
        mutation_runner = (
            self._run_recovery if use_recovery_budget else self._run_forward
        )
        provider_confirmed = False
        failure: _FailureFact | None = None
        try:
            response = mutation_runner(
                request.operation_id,
                action,
                mutation,
            )
            if response is False:
                failure = _FailureFact(
                    outcome=ReassignmentKnowledgeOutcome.KNOWN_FAILURE,
                    error_code="workspace_update_returned_false",
                    error_summary="供应商明确拒绝更新 workspace 文档关系",
                    may_have_external_effect=False,
                )
            else:
                self._require_workspace_mutation_response(
                    response,
                    expected_workspace=request.workspace,
                )
                provider_confirmed = True
        except Exception as exc:
            failure = self._classify_exception(
                exc,
                action=action,
                write_may_have_effect=True,
            )

        # 补偿步骤和“写可能已生效”的异常必须使用恢复预算；正常写成功或明确拒绝后的
        # 状态确认继续使用前向预算，防止日常成功路径侵占专门保留的补偿窗口。
        probe_uses_recovery_budget = use_recovery_budget or (
            failure is not None and failure.may_have_external_effect
        )
        probe = self._probe_document_membership(
            ReassignmentMembershipProbeRequest(
                operation_id=request.operation_id,
                workspace=request.workspace,
                document=request.document,
            ),
            use_recovery_budget=probe_uses_recovery_budget,
        )
        if probe.state is desired_state:
            # 外部请求失败但读后状态已成立时，只能收敛为“已处于目标状态”，而不是声称
            # 本次请求一定成功；这对崩溃恢复和审计具有不同含义。
            outcome = (
                ReassignmentKnowledgeOutcome.APPLIED
                if provider_confirmed
                else ReassignmentKnowledgeOutcome.ALREADY_IN_DESIRED_STATE
            )
            logger.info(
                "分类节点变更外部文档关系已由探测确认: operation_id=%s action=%s "
                "outcome=%s",
                request.operation_id,
                action,
                outcome.value,
            )
            return ReassignmentDocumentMutationResult(
                outcome,
                external_reference=request.workspace.slug,
            )
        if probe.state is ReassignmentMembershipState.OUTCOME_UNKNOWN:
            # 读后仍无法确认时，不论最初返回 false 还是异常，都不允许未来 Application
            # 直接重发同一写请求。
            if failure is not None:
                error_code = failure.error_code
                error_summary = failure.error_summary
            elif probe.error_code is not None:
                # 外部写本身有成功响应，但写后读发生超时/协议异常时，保留探测侧的稳定
                # 分类，便于后续恢复服务判断“写后检查点未确认”的真正原因。
                error_code = probe.error_code
                error_summary = probe.error_summary or "外部写后无法确认文档成员关系"
            else:
                error_code = "membership_probe_unknown"
                error_summary = "外部写后无法确认文档成员关系"
            logger.warning(
                "分类节点变更外部文档关系结果未知: operation_id=%s action=%s "
                "error_code=%s",
                request.operation_id,
                action,
                error_code,
            )
            return ReassignmentDocumentMutationResult(
                ReassignmentKnowledgeOutcome.OUTCOME_UNKNOWN,
                error_code=error_code,
                error_summary=error_summary,
            )

        # 探测已明确证明目标状态未形成。无论供应商最初给出 false、HTTP 拒绝还是响应
        # 协议异常，当前调用都能安全归类为已知失败，而不是伪造成功或未知重试。
        error_code = failure.error_code if failure is not None else "membership_not_changed"
        error_summary = (
            failure.error_summary
            if failure is not None
            else "供应商调用完成后文档成员关系未达到目标状态"
        )
        logger.warning(
            "分类节点变更外部文档关系明确未达到目标状态: operation_id=%s action=%s "
            "error_code=%s",
            request.operation_id,
            action,
            error_code,
        )
        return ReassignmentDocumentMutationResult(
            ReassignmentKnowledgeOutcome.KNOWN_FAILURE,
            error_code=error_code,
            error_summary=error_summary,
        )

    def _resolve_uncertain_workspace_creation(
        self,
        request: ReassignmentWorkspacePreparationRequest,
        error: Exception,
    ) -> ReassignmentWorkspacePreparationResult:
        """创建响应无法确认时进行一次只读查回，绝不自动再次创建。"""

        failure = self._classify_exception(
            error,
            action="创建目标 workspace",
            write_may_have_effect=True,
        )
        if not failure.may_have_external_effect:
            return ReassignmentWorkspacePreparationResult(
                ReassignmentKnowledgeOutcome.KNOWN_FAILURE,
                error_code=failure.error_code,
                error_summary=failure.error_summary,
            )

        probe = self.probe_target_workspace(request)
        if probe.state is ReassignmentWorkspaceProbeState.ABSENT:
            # 超时/断连后已经精确查回且确认不存在，可以结束为确定失败；后续 Application
            # 可根据已提交的前置事实决定是否补偿，而不能直接再次 create。
            return ReassignmentWorkspacePreparationResult(
                ReassignmentKnowledgeOutcome.KNOWN_FAILURE,
                error_code=failure.error_code,
                error_summary="创建目标 workspace 后查回确认未形成目标资源",
            )

        if probe.state is ReassignmentWorkspaceProbeState.PRESENT:
            # 已确认唯一目标资源可用，但现有供应商协议没有可验证的创建幂等键/metadata，
            # 因而只能持久化 UNKNOWN 归属。它允许流程继续，却永远不能作为自动删除依据。
            logger.info(
                "分类节点变更目标 workspace 创建后已查回唯一资源: "
                "operation_id=%s workspace_name=%s ownership=%s",
                request.operation_id,
                request.desired_workspace_name,
                ReassignmentWorkspaceOwnership.UNKNOWN.value,
            )
            return ReassignmentWorkspacePreparationResult(
                ReassignmentKnowledgeOutcome.ALREADY_IN_DESIRED_STATE,
                workspace=probe.workspace,
                ownership=ReassignmentWorkspaceOwnership.UNKNOWN,
            )

        logger.warning(
            "分类节点变更目标 workspace 创建结果无法确认: operation_id=%s "
            "workspace_name=%s error_code=%s probe_state=%s",
            request.operation_id,
            request.desired_workspace_name,
            failure.error_code,
            probe.state.value,
        )
        return ReassignmentWorkspacePreparationResult(
            ReassignmentKnowledgeOutcome.OUTCOME_UNKNOWN,
            error_code=failure.error_code,
            error_summary=failure.error_summary,
        )

    def _find_target_workspaces(
        self,
        request: ReassignmentWorkspacePreparationRequest,
        *,
        use_recovery_budget: bool,
    ) -> tuple[AnythingLLMWorkspace, ...]:
        """列出并按确定性名称/slug 精确匹配；不同 slug 的多重匹配绝不任选。"""

        runner = self._run_recovery if use_recovery_budget else self._run_forward
        workspaces = runner(
            request.operation_id,
            "查回目标 workspace" if use_recovery_budget else "查询目标 workspace",
            lambda clients: clients.workspaces.list_workspaces(user_id=self._user_id),
        )
        if not isinstance(workspaces, list):
            raise _ProviderResultError("AnythingLLM workspace 列表不是 list")

        expected_key = self._identity_key(request.desired_workspace_name)
        matched_by_slug: dict[str, AnythingLLMWorkspace] = {}
        for workspace in workspaces:
            if not isinstance(workspace, AnythingLLMWorkspace):
                raise _ProviderResultError("AnythingLLM workspace 列表包含非法对象")
            workspace_key = self._identity_key(workspace.name)
            slug_key = self._identity_key(workspace.slug)
            if expected_key not in {workspace_key, slug_key}:
                continue
            reference = self._workspace_reference(workspace)
            # 同一 slug 在上游列表中重复不构成两份资源；不同 slug 才是无法自动选择的
            # 身份冲突。使用 casefold 保持与现有 Workspace Client 的比较语义一致。
            matched_by_slug.setdefault(reference.slug.casefold(), workspace)
        return tuple(matched_by_slug.values())

    def _run_forward(
        self,
        operation_id: str,
        action: str,
        callback: Callable[[object], _T],
    ) -> _T:
        """在保留补偿窗口的前向预算内执行一次原子 HTTP 调用。"""

        timeout_seconds = self._deadline.forward_http_timeout_seconds()
        return self._run_with_timeout(
            operation_id,
            action,
            timeout_seconds,
            callback,
        )

    def _run_recovery(
        self,
        operation_id: str,
        action: str,
        callback: Callable[[object], _T],
    ) -> _T:
        """在剩余总预算内执行只读探测或后续恢复调用。"""

        timeout_seconds = self._deadline.recovery_http_timeout_seconds()
        return self._run_with_timeout(
            operation_id,
            action,
            timeout_seconds,
            callback,
        )

    def _run_with_timeout(
        self,
        operation_id: str,
        action: str,
        timeout_seconds: float,
        callback: Callable[[object], _T],
    ) -> _T:
        """建立、使用并关闭请求级 Client，日志只记录内部操作摘要。"""

        logger.debug(
            "分类节点变更发起 AnythingLLM 原子调用: operation_id=%s action=%s "
            "timeout_seconds=%.3f",
            operation_id,
            action,
            timeout_seconds,
        )
        with self._client_factory.create(timeout_seconds=timeout_seconds) as clients:
            return callback(clients)

    @staticmethod
    def _require_workspace_request(
        request: ReassignmentWorkspacePreparationRequest,
    ) -> None:
        if not isinstance(request, ReassignmentWorkspacePreparationRequest):
            raise TypeError("request 必须是 ReassignmentWorkspacePreparationRequest")

    @staticmethod
    def _require_mutation_request(
        request: ReassignmentDocumentMutationRequest,
    ) -> None:
        if not isinstance(request, ReassignmentDocumentMutationRequest):
            raise TypeError("request 必须是 ReassignmentDocumentMutationRequest")

    @classmethod
    def _require_mutation_step(
        cls,
        request: ReassignmentDocumentMutationRequest,
        *,
        allowed_steps: set[ReassignmentStepName],
        action: str,
    ) -> None:
        """在任何远端调用前校验步骤与动作的固定映射。

        端口 DTO 只能证明 ``step_name`` 是合法枚举，不能证明调用方把它交给了正确动作。
        生产 Adapter 必须与严格 Fake 使用相同门禁，避免编排错误变成真实删除或加入。
        """

        cls._require_mutation_request(request)
        if request.step_name not in allowed_steps:
            expected = ",".join(sorted(step.value for step in allowed_steps))
            raise ValueError(
                f"{action} 不接受步骤 {request.step_name.value}，允许步骤为 {expected}"
            )

    @staticmethod
    def _identity_key(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            return ""
        return value.strip().casefold()

    def _workspace_reference(
        self,
        workspace: AnythingLLMWorkspace,
    ) -> ReassignmentWorkspaceReference:
        if not isinstance(workspace, AnythingLLMWorkspace):
            raise _ProviderResultError("AnythingLLM workspace 响应类型错误")
        slug = self._identity_key(workspace.slug)
        if not slug:
            raise _ProviderResultError("AnythingLLM workspace 响应缺少有效 slug")
        # ``ReassignmentWorkspaceReference`` 保留原 slug 的大小写；身份比较只在 Adapter
        # 内部使用 casefold，避免意外改变供应商返回的引用文本。
        return ReassignmentWorkspaceReference(workspace.slug)

    def _require_created_workspace(
        self,
        value: object,
        *,
        request: ReassignmentWorkspacePreparationRequest,
    ) -> AnythingLLMWorkspace:
        """验证 create 响应可被当前 Operation 安全登记。"""

        if not isinstance(value, AnythingLLMWorkspace):
            raise _ProviderResultError("AnythingLLM 创建 workspace 返回类型错误")
        self._workspace_reference(value)
        expected_key = self._identity_key(request.desired_workspace_name)
        if expected_key not in {
            self._identity_key(value.name),
            self._identity_key(value.slug),
        }:
            raise _ProviderResultError("AnythingLLM 创建 workspace 返回身份与请求不一致")
        return value

    def _require_workspace_mutation_response(
        self,
        value: object,
        *,
        expected_workspace: ReassignmentWorkspaceReference,
    ) -> None:
        """验证 update-embeddings 成功响应未指向其他 workspace。"""

        if not isinstance(value, AnythingLLMWorkspace):
            raise _ProviderResultError("AnythingLLM 更新 workspace 文档返回类型错误")
        actual_workspace = self._workspace_reference(value)
        if actual_workspace.slug.casefold() != expected_workspace.slug.casefold():
            raise _ProviderResultError("AnythingLLM 更新 workspace 文档返回身份不一致")

    @staticmethod
    def _classify_exception(
        error: Exception,
        *,
        action: str,
        write_may_have_effect: bool,
    ) -> _FailureFact:
        """将供应商异常映射为端口四分类，不使用异常正文做业务判断。"""

        if isinstance(error, ReassignmentDeadlineExceededError):
            return _FailureFact(
                ReassignmentKnowledgeOutcome.KNOWN_FAILURE,
                "reassign_budget_exhausted",
                f"{action} 时内部远端调用预算已耗尽",
                False,
            )
        if isinstance(error, (AnythingLLMTimeoutError, AnythingLLMConnectionError)):
            return _FailureFact(
                ReassignmentKnowledgeOutcome.OUTCOME_UNKNOWN,
                f"anythingllm_{error.code}",
                f"{action} 超时或连接中断，无法确认远端结果",
                write_may_have_effect,
            )
        if isinstance(error, AnythingLLMHTTPError):
            status_code = error.status_code or 0
            # 这些状态不能证明可写请求未被服务端接收：408/425/429 可能由网关产生，
            # 409 对 create 还可能表示并发请求已先创建同名资源。统一进入查回路径。
            if status_code in {408, 409, 425, 429}:
                return _FailureFact(
                    ReassignmentKnowledgeOutcome.OUTCOME_UNKNOWN,
                    "anythingllm_http_ambiguous_error",
                    f"{action} 收到可重试或冲突状态，必须查回确认远端结果",
                    write_may_have_effect,
                )
            if 400 <= status_code < 500:
                return _FailureFact(
                    ReassignmentKnowledgeOutcome.KNOWN_FAILURE,
                    "anythingllm_http_client_error",
                    f"{action} 被供应商明确拒绝",
                    False,
                )
            return _FailureFact(
                ReassignmentKnowledgeOutcome.OUTCOME_UNKNOWN,
                "anythingllm_http_server_error",
                f"{action} 收到供应商异常状态，无法确认远端结果",
                write_may_have_effect,
            )
        if isinstance(error, (AnythingLLMProtocolError, _ProviderResultError)):
            return _FailureFact(
                ReassignmentKnowledgeOutcome.OUTCOME_UNKNOWN,
                "anythingllm_protocol_error",
                f"{action} 返回协议不完整或身份不一致的数据",
                write_may_have_effect,
            )
        if isinstance(error, AnythingLLMTransportClosedError):
            return _FailureFact(
                ReassignmentKnowledgeOutcome.KNOWN_FAILURE,
                "anythingllm_transport_closed",
                f"{action} 未执行，Transport 生命周期已关闭",
                False,
            )
        if isinstance(error, (TypeError, ValueError)):
            return _FailureFact(
                ReassignmentKnowledgeOutcome.KNOWN_FAILURE,
                "anythingllm_client_validation_error",
                f"{action} 在发送前未通过供应商 Client 参数校验",
                False,
            )
        if isinstance(error, AnythingLLMTransportError):
            return _FailureFact(
                ReassignmentKnowledgeOutcome.OUTCOME_UNKNOWN,
                f"anythingllm_{error.code}",
                f"{action} 发生未分类传输异常，无法确认远端结果",
                write_may_have_effect,
            )
        # 关闭 Transport、测试替身或未来 Client 的意外异常不能被当作“没有副作用”。
        # 对可写调用一律保守保留现场；纯查询调用也以未知状态报告，避免把短暂故障伪造为
        # “资源不存在”。
        return _FailureFact(
            ReassignmentKnowledgeOutcome.OUTCOME_UNKNOWN,
            "anythingllm_unexpected_error",
            f"{action} 发生未分类内部异常，无法确认远端结果",
            write_may_have_effect,
        )


class AnythingLLMReassignmentKnowledgeAdapterFactory:
    """按同步请求创建独立 Knowledge Adapter，避免 deadline 跨 Operation 串扰。"""

    def __init__(
        self,
        client_factory: ReassignmentAnythingLLMClientFactoryProtocol,
        infrastructure_config: ReassignmentInfrastructureConfig,
        *,
        monotonic_clock: Callable[[], float] = monotonic,
        user_id: int = 1,
    ) -> None:
        if not callable(getattr(client_factory, "create", None)):
            raise TypeError("client_factory 必须提供 create(timeout_seconds=...) 方法")
        if not isinstance(infrastructure_config, ReassignmentInfrastructureConfig):
            raise TypeError(
                "infrastructure_config 必须是 ReassignmentInfrastructureConfig"
            )
        if not callable(monotonic_clock):
            raise TypeError("monotonic_clock 必须可调用")
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
            raise ValueError("user_id 必须是正整数")
        self._client_factory = client_factory
        self._infrastructure_config = infrastructure_config
        self._monotonic_clock = monotonic_clock
        self._user_id = user_id

    def create(
        self,
        *,
        elapsed_seconds: float = 0.0,
    ) -> AnythingLLMReassignmentKnowledgeAdapter:
        """创建请求级 Adapter，并从远端总预算扣除 Application 前置耗时。"""

        return AnythingLLMReassignmentKnowledgeAdapter(
            self._client_factory,
            self._infrastructure_config,
            monotonic_clock=self._monotonic_clock,
            elapsed_seconds=elapsed_seconds,
            user_id=self._user_id,
        )


__all__ = [
    "AnythingLLMReassignmentKnowledgeAdapter",
    "AnythingLLMReassignmentKnowledgeAdapterFactory",
]
