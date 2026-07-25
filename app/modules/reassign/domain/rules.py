"""分类节点变更 Saga 的无 I/O 状态机、幂等键与补偿决策规则。

所有函数只读取不可变领域对象并返回新对象或确定性决策，不记录日志、不访问时钟、不生成
UUID，也不调用网络。后续 Application/Adapter 必须在每次外部写前后持久化事实并记录结构化
日志；领域规则本身保持可复现，便于恢复器在不同进程或实例中作出相同判定。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from .errors import (
    ReassignmentDomainValidationError,
    ReassignmentStateTransitionError,
)
from .models import (
    ReassignmentCompensationAction,
    ReassignmentBindingState,
    ReassignmentCompensationDecision,
    ReassignmentCompensationFacts,
    ReassignmentCompensationMode,
    ReassignmentMutationOutcome,
    ReassignmentOperation,
    ReassignmentOperationStatus,
    ReassignmentStep,
    ReassignmentStepName,
    ReassignmentStepState,
    ReassignmentTerminalEvidence,
    ReassignmentTerminalEvidenceKind,
)


# 正常请求不能从 recovery_required 继续写入。恢复服务必须显式带上
# ``recovery_authorized=True``，并在后续阶段使用 Repository 的 fencing 条件更新取得所有权。
_NORMAL_OPERATION_TRANSITIONS: dict[
    ReassignmentOperationStatus,
    frozenset[ReassignmentOperationStatus],
] = {
    ReassignmentOperationStatus.RESERVED: frozenset(
        {
            ReassignmentOperationStatus.RUNNING,
            ReassignmentOperationStatus.FAILED,
            ReassignmentOperationStatus.RECOVERY_REQUIRED,
        }
    ),
    ReassignmentOperationStatus.RUNNING: frozenset(
        {
            ReassignmentOperationStatus.SUCCEEDED,
            ReassignmentOperationStatus.FAILED,
            ReassignmentOperationStatus.COMPENSATING,
            ReassignmentOperationStatus.RECOVERY_REQUIRED,
        }
    ),
    ReassignmentOperationStatus.COMPENSATING: frozenset(
        {
            ReassignmentOperationStatus.COMPENSATED,
            ReassignmentOperationStatus.RECOVERY_REQUIRED,
        }
    ),
    ReassignmentOperationStatus.COMPENSATED: frozenset(),
    ReassignmentOperationStatus.FAILED: frozenset(),
    ReassignmentOperationStatus.RECOVERY_REQUIRED: frozenset(),
    ReassignmentOperationStatus.SUCCEEDED: frozenset(),
}

_AUTHORIZED_RECOVERY_OPERATION_TRANSITIONS = frozenset(
    {
        ReassignmentOperationStatus.RUNNING,
        ReassignmentOperationStatus.COMPENSATING,
        ReassignmentOperationStatus.SUCCEEDED,
        ReassignmentOperationStatus.COMPENSATED,
        ReassignmentOperationStatus.FAILED,
    }
)

# 会释放文档保护的终态必须携带与目标状态严格匹配的证据类型。Repository 仍须另外验证
# lease/fencing；这里解决的是“只传一个枚举就能安全关闭 Operation”的领域漏洞。
_TERMINAL_EVIDENCE_BY_STATUS = {
    ReassignmentOperationStatus.SUCCEEDED: (
        ReassignmentTerminalEvidenceKind.FORWARD_SUCCESS_CONFIRMED
    ),
    ReassignmentOperationStatus.FAILED: (
        ReassignmentTerminalEvidenceKind.NO_SIDE_EFFECT_FAILURE_CONFIRMED
    ),
    ReassignmentOperationStatus.COMPENSATED: (
        ReassignmentTerminalEvidenceKind.COMPENSATION_CONFIRMED
    ),
}

# 前向和补偿外部写都使用独立 Step，统一走
# ``pending -> mutation_started -> succeeded/known_failed/outcome_unknown``。
# 已知失败或未知结果只能由带 fencing 的恢复服务重新判定；不在前向 Step 上维护第二套
# ``compensating/compensated`` 状态。
_NORMAL_STEP_TRANSITIONS: dict[
    ReassignmentStepState,
    frozenset[ReassignmentStepState],
] = {
    ReassignmentStepState.PENDING: frozenset(
        {ReassignmentStepState.MUTATION_STARTED}
    ),
    ReassignmentStepState.MUTATION_STARTED: frozenset(
        {
            ReassignmentStepState.SUCCEEDED,
            ReassignmentStepState.KNOWN_FAILED,
            ReassignmentStepState.OUTCOME_UNKNOWN,
        }
    ),
    ReassignmentStepState.SUCCEEDED: frozenset(),
    ReassignmentStepState.KNOWN_FAILED: frozenset(),
    ReassignmentStepState.OUTCOME_UNKNOWN: frozenset(),
}

_AUTHORIZED_RECOVERY_STEP_TRANSITIONS: dict[
    ReassignmentStepState,
    frozenset[ReassignmentStepState],
] = {
    ReassignmentStepState.OUTCOME_UNKNOWN: frozenset(
        {
            ReassignmentStepState.SUCCEEDED,
            ReassignmentStepState.KNOWN_FAILED,
        }
    ),
    # 明确失败可以在恢复服务完成重新探测并取得新 fencing 后，使用同一步骤和新的
    # attempt 再次发送；未知结果必须先收敛为 succeeded/known_failed，不能直接重放。
    ReassignmentStepState.KNOWN_FAILED: frozenset(
        {ReassignmentStepState.MUTATION_STARTED}
    ),
}

_PROTECTING_OPERATION_STATUSES = frozenset(
    {
        ReassignmentOperationStatus.RESERVED,
        ReassignmentOperationStatus.RUNNING,
        ReassignmentOperationStatus.COMPENSATING,
        ReassignmentOperationStatus.RECOVERY_REQUIRED,
    }
)


def _require_operation_status(value: object, *, name: str) -> ReassignmentOperationStatus:
    """拒绝字符串等未解析状态，避免 Adapter 绕过持久化 Codec 的枚举校验。"""

    if not isinstance(value, ReassignmentOperationStatus):
        raise ReassignmentDomainValidationError(
            f"{name} 必须是 ReassignmentOperationStatus"
        )
    return value


def _require_step_state(value: object, *, name: str) -> ReassignmentStepState:
    """拒绝字符串等未解析步骤状态。"""

    if not isinstance(value, ReassignmentStepState):
        raise ReassignmentDomainValidationError(
            f"{name} 必须是 ReassignmentStepState"
        )
    return value


def allowed_operation_transitions(
    current_status: ReassignmentOperationStatus,
    *,
    recovery_authorized: bool = False,
) -> frozenset[ReassignmentOperationStatus]:
    """返回当前 Operation 在指定权限下允许进入的下一状态集合。

    ``recovery_required`` 默认没有出边，因此普通请求不能覆盖未知现场。只有恢复服务在
    Repository 的 lease/fencing 校验通过后，才可请求受控恢复转换。
    """

    current_status = _require_operation_status(
        current_status,
        name="current_status",
    )
    if type(recovery_authorized) is not bool:
        raise ReassignmentDomainValidationError(
            "recovery_authorized 必须是 bool"
        )
    if (
        recovery_authorized
        and current_status is ReassignmentOperationStatus.RECOVERY_REQUIRED
    ):
        return _AUTHORIZED_RECOVERY_OPERATION_TRANSITIONS
    return _NORMAL_OPERATION_TRANSITIONS[current_status]


def transition_operation_status(
    operation: ReassignmentOperation,
    next_status: ReassignmentOperationStatus,
    *,
    recovery_authorized: bool = False,
    terminal_evidence: ReassignmentTerminalEvidence | None = None,
) -> ReassignmentOperation:
    """按照状态机生成新的 Operation 快照，并校验释放保护所需的终态证据。"""

    if not isinstance(operation, ReassignmentOperation):
        raise ReassignmentDomainValidationError(
            "operation 必须是 ReassignmentOperation"
        )
    next_status = _require_operation_status(next_status, name="next_status")
    allowed = allowed_operation_transitions(
        operation.status,
        recovery_authorized=recovery_authorized,
    )
    if next_status not in allowed:
        raise ReassignmentStateTransitionError(
            "不允许 Operation 状态转换: "
            f"{operation.status.value} -> {next_status.value}"
        )
    expected_evidence = _TERMINAL_EVIDENCE_BY_STATUS.get(next_status)
    if expected_evidence is None:
        if terminal_evidence is not None:
            raise ReassignmentStateTransitionError(
                "非终态转换不能携带 terminal_evidence"
            )
    elif not isinstance(terminal_evidence, ReassignmentTerminalEvidence):
        raise ReassignmentStateTransitionError(
            f"进入 {next_status.value} 必须提供终态证据"
        )
    elif terminal_evidence.kind is not expected_evidence:
        raise ReassignmentStateTransitionError(
            "终态证据与目标状态不匹配: "
            f"{terminal_evidence.kind.value} -> {next_status.value}"
        )
    return replace(operation, status=next_status)


def operation_holds_document_protection(
    status: ReassignmentOperationStatus,
) -> bool:
    """判断该状态是否仍阻止同一文档启动新的 Saga。"""

    status = _require_operation_status(status, name="status")
    return status in _PROTECTING_OPERATION_STATUSES


def operation_releases_document_protection(
    status: ReassignmentOperationStatus,
) -> bool:
    """判断该状态是否已确认可释放同文档唯一保护。"""

    return not operation_holds_document_protection(status)


def allowed_step_transitions(
    current_state: ReassignmentStepState,
    *,
    recovery_authorized: bool = False,
) -> frozenset[ReassignmentStepState]:
    """返回指定 Step 状态的合法下一状态集合。"""

    current_state = _require_step_state(current_state, name="current_state")
    if type(recovery_authorized) is not bool:
        raise ReassignmentDomainValidationError(
            "recovery_authorized 必须是 bool"
        )
    if recovery_authorized and current_state in _AUTHORIZED_RECOVERY_STEP_TRANSITIONS:
        return _AUTHORIZED_RECOVERY_STEP_TRANSITIONS[current_state]
    return _NORMAL_STEP_TRANSITIONS[current_state]


def record_step_write_intent(step: ReassignmentStep) -> ReassignmentStep:
    """将 pending Step 标记为已持久化写意图。

    调用方必须先把返回快照成功写入 Repository，之后才允许调用外部系统。重复读取到已经
    标记的 pending Step 时返回同一事实，以支持崩溃恢复而不产生第二份写意图。
    """

    if not isinstance(step, ReassignmentStep):
        raise ReassignmentDomainValidationError("step 必须是 ReassignmentStep")
    if step.state is not ReassignmentStepState.PENDING:
        raise ReassignmentStateTransitionError(
            "只有 pending Step 可以记录写意图"
        )
    if step.write_intent_recorded:
        return step
    return replace(step, write_intent_recorded=True)


def transition_step_state(
    step: ReassignmentStep,
    next_state: ReassignmentStepState,
    *,
    recovery_authorized: bool = False,
) -> ReassignmentStep:
    """按照步骤状态机生成新快照，并阻止未持久化意图就启动外部写。"""

    if not isinstance(step, ReassignmentStep):
        raise ReassignmentDomainValidationError("step 必须是 ReassignmentStep")
    next_state = _require_step_state(next_state, name="next_state")
    if (
        next_state is ReassignmentStepState.MUTATION_STARTED
        and not step.write_intent_recorded
    ):
        raise ReassignmentStateTransitionError(
            "启动外部写前必须先持久化 write_intent_recorded"
        )
    allowed = allowed_step_transitions(
        step.state,
        recovery_authorized=recovery_authorized,
    )
    if next_state not in allowed:
        raise ReassignmentStateTransitionError(
            "不允许 Step 状态转换: "
            f"{step.state.value} -> {next_state.value}"
        )
    return replace(step, state=next_state)


def build_step_idempotency_key(
    operation: ReassignmentOperation,
    step_name: ReassignmentStepName,
) -> str:
    """计算供应商无关、可重复构造的步骤幂等键。

    键覆盖 Operation、步骤、稳定本地文档行、AnythingLLM 文档位置以及新旧分类原始事实；
    因此文件展示名既不是唯一输入，也不会被误当作供应商支持天然幂等的证据。原始 ID 以
    ``canonical_json`` 编码，明确区分 ``1``、``"1"`` 和 ``false``。
    """

    if not isinstance(operation, ReassignmentOperation):
        raise ReassignmentDomainValidationError(
            "operation 必须是 ReassignmentOperation"
        )
    if not isinstance(step_name, ReassignmentStepName):
        raise ReassignmentDomainValidationError(
            "step_name 必须是 ReassignmentStepName"
        )
    identity = {
        "version": 1,
        "operation_id": operation.operation_id,
        "step_name": step_name.value,
        "document": {
            "row_id": operation.document.document_row_id,
            "anything_doc_id": operation.document.anything_doc_id,
            "doc_path": operation.document.doc_path,
        },
        "source_category": {
            "internal_id": operation.source_architecture_id,
            "raw_json": operation.source_architecture_raw.canonical_json(),
        },
        "target_category_raw_json": operation.target_architecture_raw.canonical_json(),
    }
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"reassign-v1:{digest}"


def decide_compensation(
    facts: ReassignmentCompensationFacts,
) -> ReassignmentCompensationDecision:
    """根据已确认副作用选择补偿，未知结果永远优先进入人工恢复保护。

    该函数不发送删除/加入请求。它只固定执行顺序：若目标绑定已确认存在，必须先删除目标；
    若来源绑定已确认删除，再恢复来源。已确认本地 CAS 成功时，普通异常路径绝不自动反向
    回滚本地分类。
    """

    if not isinstance(facts, ReassignmentCompensationFacts):
        raise ReassignmentDomainValidationError(
            "facts 必须是 ReassignmentCompensationFacts"
        )
    outcomes = (
        facts.source_detach_outcome,
        facts.target_attach_outcome,
        facts.local_commit_outcome,
    )
    if ReassignmentMutationOutcome.OUTCOME_UNKNOWN in outcomes:
        return ReassignmentCompensationDecision(
            mode=ReassignmentCompensationMode.RECOVERY_REQUIRED
        )
    if (
        facts.local_commit_outcome
        is ReassignmentMutationOutcome.CONFIRMED_EFFECT
    ):
        # 本地已经指向目标分类时，不能仅凭“CAS 生效”宣称可保留。需要远端迁移的文档
        # 必须同时确认目标成员存在；目标缺失或探测未知都表示跨系统状态不一致，只能
        # 保留现场进入恢复。空 doc_path 的本地-only路径则以 NOT_APPLICABLE 显式说明。
        if facts.remote_membership_required:
            if (
                facts.target_binding_state
                is not ReassignmentBindingState.CONFIRMED_PRESENT
                or facts.source_binding_state
                not in {
                    ReassignmentBindingState.CONFIRMED_ABSENT,
                    ReassignmentBindingState.NOT_APPLICABLE,
                }
            ):
                return ReassignmentCompensationDecision(
                    mode=ReassignmentCompensationMode.RECOVERY_REQUIRED
                )
        else:
            remote_outcomes = (
                facts.source_detach_outcome,
                facts.target_attach_outcome,
            )
            if (
                facts.source_binding_state
                is not ReassignmentBindingState.NOT_APPLICABLE
                or facts.target_binding_state
                is not ReassignmentBindingState.NOT_APPLICABLE
                or any(
                    outcome is not ReassignmentMutationOutcome.NOT_STARTED
                    for outcome in remote_outcomes
                )
            ):
                return ReassignmentCompensationDecision(
                    mode=ReassignmentCompensationMode.RECOVERY_REQUIRED
                )
        return ReassignmentCompensationDecision(
            mode=ReassignmentCompensationMode.PRESERVE_CONFIRMED_LOCAL_COMMIT
        )

    actions: list[ReassignmentCompensationAction] = []
    if (
        facts.target_attach_outcome
        is ReassignmentMutationOutcome.CONFIRMED_EFFECT
    ):
        actions.append(ReassignmentCompensationAction.DETACH_TARGET_DOCUMENT)
    if (
        facts.source_detach_outcome
        is ReassignmentMutationOutcome.CONFIRMED_EFFECT
    ):
        actions.append(ReassignmentCompensationAction.RESTORE_SOURCE_DOCUMENT)
    if actions:
        return ReassignmentCompensationDecision(
            mode=ReassignmentCompensationMode.COMPENSATE,
            actions=tuple(actions),
        )
    return ReassignmentCompensationDecision(
        mode=ReassignmentCompensationMode.NO_COMPENSATION_NEEDED
    )


__all__ = [
    "allowed_operation_transitions",
    "allowed_step_transitions",
    "build_step_idempotency_key",
    "decide_compensation",
    "operation_holds_document_protection",
    "operation_releases_document_protection",
    "record_step_write_intent",
    "transition_operation_status",
    "transition_step_state",
]
