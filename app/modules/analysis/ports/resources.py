"""任务资源事实的推进 Port；未知外部结果必须隔离而非自动补偿。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from app.modules.analysis.domain.task_inputs import FrozenJsonObject

from .common import AnalysisExecutionRef


class AnalysisResourceState(str, Enum):
    TRACKING = "tracking"
    AUDIT_PENDING = "audit_pending"
    CLEANUP_PENDING = "cleanup_pending"
    CLEANED = "cleaned"
    QUARANTINED = "quarantined"


# 状态机同时由 Port DTO 与 SQLite Service 强制执行。这里首先阻止 Application、
# Dispatcher 或测试替身构造非法命令；持久化层仍会独立复核，避免其他内部调用者绕过
# Port 后复活不可逆终态。
_ALLOWED_RESOURCE_TRANSITIONS = {
    AnalysisResourceState.TRACKING: frozenset(
        {
            AnalysisResourceState.TRACKING,
            AnalysisResourceState.AUDIT_PENDING,
            AnalysisResourceState.CLEANUP_PENDING,
            AnalysisResourceState.QUARANTINED,
        }
    ),
    AnalysisResourceState.CLEANUP_PENDING: frozenset(
        {
            AnalysisResourceState.CLEANUP_PENDING,
            AnalysisResourceState.AUDIT_PENDING,
            AnalysisResourceState.CLEANED,
            AnalysisResourceState.QUARANTINED,
        }
    ),
    AnalysisResourceState.AUDIT_PENDING: frozenset(
        {
            AnalysisResourceState.AUDIT_PENDING,
            AnalysisResourceState.CLEANED,
            AnalysisResourceState.QUARANTINED,
        }
    ),
    AnalysisResourceState.CLEANED: frozenset(),
    AnalysisResourceState.QUARANTINED: frozenset(),
}


@dataclass(frozen=True)
class AnalysisResourceCommand:
    execution: AnalysisExecutionRef
    expected_state: AnalysisResourceState | None
    expected_version: int | None
    target_state: AnalysisResourceState
    record_payload: FrozenJsonObject

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        if self.expected_state is not None and not isinstance(
            self.expected_state,
            AnalysisResourceState,
        ):
            raise TypeError("expected_state 必须是 AnalysisResourceState 或 None")
        if not isinstance(self.target_state, AnalysisResourceState):
            raise TypeError("target_state 必须是 AnalysisResourceState")
        if self.expected_version is not None and (
            isinstance(self.expected_version, bool)
            or not isinstance(self.expected_version, int)
            or self.expected_version < 0
        ):
            raise ValueError("expected_version 必须是非负整数或 None")
        if (self.expected_state is None) != (self.expected_version is None):
            raise ValueError("expected_state 与 expected_version 必须同时存在或同时为空")
        if self.expected_state is None and self.target_state is not AnalysisResourceState.TRACKING:
            raise ValueError("资源创建只能进入 tracking")
        if (
            self.expected_state is not None
            and self.target_state
            not in _ALLOWED_RESOURCE_TRANSITIONS[self.expected_state]
        ):
            raise ValueError(
                "非法资源状态迁移: "
                f"{self.expected_state.value} -> {self.target_state.value}"
            )
        # 资源引用往往在同一个恢复阶段内分批出现：例如先取得 Context，再取得
        # Conversation，首个模型调用后才取得 Document。每个引用都必须立即落库，
        # 因而允许 ``target_state`` 与 ``expected_state`` 相同的 CAS 载荷更新；版本
        # 仍会递增，不能把它误认为无条件覆盖。
        if not isinstance(self.record_payload, FrozenJsonObject):
            raise TypeError("record_payload 必须是 FrozenJsonObject")


@dataclass(frozen=True)
class AnalysisResourceRecord:
    execution: AnalysisExecutionRef
    state: AnalysisResourceState
    version: int
    record_payload: FrozenJsonObject
    recovery_deferral_count: int = 0
    next_recovery_at: str | None = None
    last_recovery_reason: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        if not isinstance(self.state, AnalysisResourceState):
            raise TypeError("state 必须是 AnalysisResourceState")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 0:
            raise ValueError("version 必须是非负整数")
        if not isinstance(self.record_payload, FrozenJsonObject):
            raise TypeError("record_payload 必须是 FrozenJsonObject")
        if (
            isinstance(self.recovery_deferral_count, bool)
            or not isinstance(self.recovery_deferral_count, int)
            or self.recovery_deferral_count < 0
        ):
            raise ValueError("recovery_deferral_count 必须是非负整数")
        if self.next_recovery_at is not None and (
            not isinstance(self.next_recovery_at, str)
            or not self.next_recovery_at.strip()
        ):
            raise ValueError("next_recovery_at 必须是非空 str 或 None")
        if not isinstance(self.last_recovery_reason, str):
            raise TypeError("last_recovery_reason 必须是 str")
        object.__setattr__(
            self,
            "last_recovery_reason",
            self.last_recovery_reason.strip(),
        )
        if self.recovery_deferral_count == 0 and (
            self.next_recovery_at is not None or self.last_recovery_reason
        ):
            raise ValueError("未延期记录不得携带恢复时间或原因")
        if self.recovery_deferral_count > 0 and (
            self.next_recovery_at is None or not self.last_recovery_reason
        ):
            raise ValueError("已延期记录必须携带恢复时间和原因")


@dataclass(frozen=True)
class AnalysisResourceScanBatch:
    """一次有界扫描及 Adapter 在解码阶段形成的收敛指标。

    毒记录可能在转换为 ``AnalysisResourceRecord`` 之前就被隔离，因此不能只返回成功
    解码的记录元组，否则 Application 会把实际扫描量和隔离量系统性低估。
    """

    records: tuple[AnalysisResourceRecord, ...]
    quarantined_count: int = 0
    pending_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.records, tuple) or any(
            not isinstance(item, AnalysisResourceRecord) for item in self.records
        ):
            raise TypeError("records 必须是 AnalysisResourceRecord 元组")
        for name in ("quarantined_count", "pending_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} 必须是非负整数")


@runtime_checkable
class AnalysisResourcePort(Protocol):
    """资源事实存储；所有推进必须使用 state+version CAS。"""

    def create(self, command: AnalysisResourceCommand) -> AnalysisResourceRecord:
        ...

    def get(
        self,
        execution: AnalysisExecutionRef,
    ) -> AnalysisResourceRecord | None:
        ...

    def advance(self, command: AnalysisResourceCommand) -> AnalysisResourceRecord:
        ...

    def list_recoverable(self, *, limit: int) -> AnalysisResourceScanBatch:
        ...

    def defer_recovery(
        self,
        execution: AnalysisExecutionRef,
        *,
        expected_version: int,
        retry_at: str,
        reason: str,
    ) -> AnalysisResourceRecord:
        ...

    def quarantine_recovery_record(
        self,
        execution: AnalysisExecutionRef,
        *,
        expected_state: AnalysisResourceState,
        expected_version: int,
        reason: str,
    ) -> bool:
        """不依赖业务 payload 解码，条件隔离不可恢复或已毒化的资源记录。"""

        ...


__all__ = (
    "AnalysisResourceCommand",
    "AnalysisResourcePort",
    "AnalysisResourceRecord",
    "AnalysisResourceScanBatch",
    "AnalysisResourceState",
)
