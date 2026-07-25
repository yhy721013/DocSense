"""分类节点变更恢复流程的内部共享类型。

本文件只定义值对象、命令校验和无 I/O 的小工具。它不创建 Repository、Knowledge Port、线程或
网络客户端，避免四个恢复协作器为了共享内部状态而重新耦合回巨型 Facade。

这里的类型仅服务于 ``application`` 包内部；公开 HTTP 层仍只能看到既有的同步响应，绝不能
序列化 Operation、lease、fencing 或恢复观察事实。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum

from app.modules.reassign.domain import (
    ReassignmentBindingState,
    ReassignmentStepName,
)
from app.modules.reassign.ports import (
    ReassignmentLease,
    ReassignmentOperationRecord,
    ReassignmentWorkspacePreparationClaim,
    ReassignmentWorkspaceOwnership,
    ReassignmentWorkspaceReference,
)


def required_text(value: object, *, name: str, max_length: int | None = None) -> str:
    """校验恢复内部文本，拒绝隐式字符串转换和空白标识。"""

    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    if max_length is not None and len(normalized) > max_length:
        raise ValueError(f"{name} 长度不能超过 {max_length}")
    return normalized


def positive_int(value: object, *, name: str) -> int:
    """校验预期 fencing token 等内部正整数。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} 必须是正整数")
    return value


class ReassignmentRecoveryResultCategory(str, Enum):
    """恢复服务返回给离线运维工具的内部结果分类。

    该分类只能帮助人工脚本和未来可靠队列区分“已收口”与“仍须保留现场”。Presenter 不得把它
    映射到公开接口，因此公开请求/响应契约不会因恢复实现拆分而改变。
    """

    OPERATION_NOT_FOUND = "operation_not_found"
    TAKEOVER_REJECTED = "takeover_rejected"
    RECOVERED_SUCCEEDED = "recovered_succeeded"
    RECOVERED_FAILED_NO_SIDE_EFFECT = "recovered_failed_no_side_effect"
    COMPENSATED = "compensated"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True)
class RecoverReassignmentCommand:
    """一次人工或未来队列驱动的受控恢复命令。

    ``expected_fencing_token`` 必须来自只读诊断得到的精确值。恢复器不会自动改用最新 token，
    因而同一 Operation 的并发恢复者中最多只有一个 Compare-And-Swap 可以接管成功。
    """

    operation_id: str
    expected_fencing_token: int
    actor: str
    reason_code: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            required_text(self.operation_id, name="operation_id", max_length=512),
        )
        object.__setattr__(
            self,
            "expected_fencing_token",
            positive_int(self.expected_fencing_token, name="expected_fencing_token"),
        )
        object.__setattr__(
            self,
            "actor",
            required_text(self.actor, name="actor", max_length=512),
        )
        object.__setattr__(
            self,
            "reason_code",
            required_text(self.reason_code, name="reason_code", max_length=128),
        )


@dataclass(frozen=True)
class ReassignmentRecoveryResult:
    """恢复执行的内部最小结果，不包含文档路径、供应商正文或公开响应字段。"""

    operation_id: str
    category: ReassignmentRecoveryResultCategory

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_id",
            required_text(self.operation_id, name="operation_id", max_length=512),
        )
        if not isinstance(self.category, ReassignmentRecoveryResultCategory):
            raise TypeError("category 必须是 ReassignmentRecoveryResultCategory")


@dataclass(frozen=True)
class RecoveryLeaseContext:
    """恢复过程中的最新 lease 与可安全释放的目标 workspace claim。"""

    lease: ReassignmentLease
    preparation_claim: ReassignmentWorkspacePreparationClaim | None

    def __post_init__(self) -> None:
        if not isinstance(self.lease, ReassignmentLease):
            raise TypeError("lease 必须是 ReassignmentLease")
        if self.preparation_claim is not None and not isinstance(
            self.preparation_claim,
            ReassignmentWorkspacePreparationClaim,
        ):
            raise TypeError(
                "preparation_claim 必须是 ReassignmentWorkspacePreparationClaim 或 None"
            )


@dataclass(frozen=True)
class OperationReadResult:
    """区分“明确不存在”与“读取失败”的内部读取结果。"""

    record: ReassignmentOperationRecord | None
    read_failed: bool = False

    def __post_init__(self) -> None:
        if self.record is not None and not isinstance(
            self.record,
            ReassignmentOperationRecord,
        ):
            raise TypeError("record 必须是 ReassignmentOperationRecord 或 None")
        if not isinstance(self.read_failed, bool):
            raise TypeError("read_failed 必须是 bool")
        if self.record is not None and self.read_failed:
            raise ValueError("读取成功时不能同时标记 read_failed")


@dataclass(frozen=True)
class RemoteObservation:
    """一次无副作用远端探测的结果及本次可用目标 workspace 引用。"""

    source_binding_state: ReassignmentBindingState
    target_binding_state: ReassignmentBindingState
    target_workspace: ReassignmentWorkspaceReference | None
    target_workspace_ownership: ReassignmentWorkspaceOwnership | None

    def __post_init__(self) -> None:
        if not isinstance(self.source_binding_state, ReassignmentBindingState):
            raise TypeError("source_binding_state 必须是 ReassignmentBindingState")
        if not isinstance(self.target_binding_state, ReassignmentBindingState):
            raise TypeError("target_binding_state 必须是 ReassignmentBindingState")
        if self.target_workspace is not None and not isinstance(
            self.target_workspace,
            ReassignmentWorkspaceReference,
        ):
            raise TypeError("target_workspace 必须是 ReassignmentWorkspaceReference 或 None")
        if self.target_workspace_ownership is not None and not isinstance(
            self.target_workspace_ownership,
            ReassignmentWorkspaceOwnership,
        ):
            raise TypeError(
                "target_workspace_ownership 必须是 ReassignmentWorkspaceOwnership 或 None"
            )
        if (self.target_workspace is None) != (
            self.target_workspace_ownership is None
        ):
            raise ValueError("目标 workspace 引用与创建归属必须同时存在或同时为空")


class CompensationCheckpointDisposition(str, Enum):
    """恢复时对既有补偿写检查点的判定结果。"""

    CONTINUE = "continue"
    TERMINAL_READY = "terminal_ready"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class CompensationCheckpointReconciliation:
    """既有补偿写的探测收敛结果，不携带供应商正文或外部引用。"""

    disposition: CompensationCheckpointDisposition
    terminal_step: ReassignmentStepName | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, CompensationCheckpointDisposition):
            raise TypeError("disposition 必须是 CompensationCheckpointDisposition")
        if self.terminal_step is not None and not isinstance(
            self.terminal_step,
            ReassignmentStepName,
        ):
            raise TypeError("terminal_step 必须是 ReassignmentStepName 或 None")
        if (
            self.disposition is CompensationCheckpointDisposition.TERMINAL_READY
            and self.terminal_step is None
        ):
            raise ValueError("terminal_ready 必须携带终态补偿步骤")
        if (
            self.disposition is not CompensationCheckpointDisposition.TERMINAL_READY
            and self.terminal_step is not None
        ):
            raise ValueError("非 terminal_ready 不能携带终态补偿步骤")


def actor_marker(actor: str) -> str:
    """日志中仅保留操作者的不可逆短摘要。"""

    return hashlib.sha256(actor.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "CompensationCheckpointDisposition",
    "CompensationCheckpointReconciliation",
    "OperationReadResult",
    "RecoverReassignmentCommand",
    "ReassignmentRecoveryResult",
    "ReassignmentRecoveryResultCategory",
    "RecoveryLeaseContext",
    "RemoteObservation",
    "actor_marker",
    "positive_int",
    "required_text",
]
