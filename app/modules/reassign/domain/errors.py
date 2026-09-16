"""分类节点变更领域的稳定错误分类。

错误类型只表达内部业务事实，绝不携带 Flask 状态码、供应商响应、数据库路径或公开
JSON 字段。后续 Application 会把这些类型用于日志、审计和补偿分支；Presenter 仍须依
据已冻结的接口契约独立选择既有公开响应。
"""

from __future__ import annotations


class ReassignmentError(Exception):
    """分类节点变更内部错误的共同基类。"""

    code = "reassignment_error"
    stage = "reassignment"


class ReassignmentDomainValidationError(ReassignmentError, ValueError):
    """领域对象或纯规则的输入不满足不可变业务不变量。"""

    code = "reassignment_domain_validation"
    stage = "domain"


class ReassignmentStateTransitionError(ReassignmentDomainValidationError):
    """Operation 或 Step 尝试执行未获批准的状态转换。"""

    code = "reassignment_invalid_state_transition"
    stage = "state_machine"


class ReassignmentContractError(ReassignmentError):
    """Port、Repository 或 Adapter 返回的内部对象违反约定。

    该错误不代表公开接口参数校验失败。公开参数仍必须由 Web Adapter 按既有校验顺序
    处理，不能因为内部契约更严格而悄悄收紧外部行为。
    """

    code = "reassignment_contract_error"
    stage = "contract"


class ReassignmentConcurrencyError(ReassignmentError):
    """同一文档的 lease、版本或条件更新所有权发生冲突。"""

    code = "reassignment_concurrency_error"
    stage = "concurrency"


class ReassignmentFencingError(ReassignmentConcurrencyError):
    """旧 lease owner 尝试以过期 fencing token 写入新的 Operation 事实。"""

    code = "reassignment_fencing_error"
    stage = "fencing"


class ReassignmentExternalOutcomeUnknownError(ReassignmentError):
    """外部写或探测的结果无法可靠确认，禁止自动盲目重放。"""

    code = "reassignment_external_outcome_unknown"
    stage = "external_outcome"


class ReassignmentCompensationError(ReassignmentError):
    """反向补偿未能恢复可确认的调用前权威状态。"""

    code = "reassignment_compensation_error"
    stage = "compensation"


class ReassignmentRecoveryRequiredError(ReassignmentError):
    """现场必须保留并交由带 fencing 的恢复流程或人工处置。"""

    code = "reassignment_recovery_required"
    stage = "recovery"


__all__ = [
    "ReassignmentCompensationError",
    "ReassignmentConcurrencyError",
    "ReassignmentContractError",
    "ReassignmentDomainValidationError",
    "ReassignmentError",
    "ReassignmentExternalOutcomeUnknownError",
    "ReassignmentFencingError",
    "ReassignmentRecoveryRequiredError",
    "ReassignmentStateTransitionError",
]
