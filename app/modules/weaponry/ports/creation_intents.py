"""武器谱外部资源创建意图的持久化端口。

AnythingLLM 的 create 请求可能已经在远端成功，但调用方因超时或断连拿不到资源标识。
因此，任何 workspace/thread 创建都必须先落下一条确定性意图。后续调用只能查回并核验该
意图对应的远端资源，不能把结果未知误解为“肯定没有创建”而再次发送 create。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from app.modules.tasks.domain import TaskId

from .common import non_negative_int, optional_text, required_text, sha256_digest


class WeaponryCreationIntentKind(str, Enum):
    """当前需要跨崩溃窗口保护的 AnythingLLM 创建操作。"""

    RETRIEVAL_WORKSPACE = "retrieval_workspace"
    EXTRACTION_WORKSPACE = "extraction_workspace"
    SOURCE_THREAD = "source_thread"


class WeaponryCreationIntentState(str, Enum):
    """创建意图只能从 pending 进入 resolved 或 quarantined 终态。"""

    PENDING = "pending"
    RESOLVED = "resolved"
    QUARANTINED = "quarantined"


@dataclass(frozen=True)
class WeaponryCreationIntent:
    """一条不包含业务正文、URL 或 Token 的确定性创建事实。"""

    task_id: TaskId
    intent_id: str
    kind: WeaponryCreationIntentKind
    expected_name: str
    identity_digest: str
    parent_external_ref: str = ""
    document_key: str = ""
    call_id: str = ""
    state: WeaponryCreationIntentState = WeaponryCreationIntentState.PENDING
    external_ref: str = ""
    error_code: str = ""
    version: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        object.__setattr__(self, "intent_id", required_text(self.intent_id, name="intent_id"))
        if not isinstance(self.kind, WeaponryCreationIntentKind):
            raise TypeError("kind 必须是 WeaponryCreationIntentKind")
        object.__setattr__(
            self,
            "expected_name",
            required_text(self.expected_name, name="expected_name"),
        )
        object.__setattr__(
            self,
            "identity_digest",
            sha256_digest(self.identity_digest, name="identity_digest"),
        )
        for name in (
            "parent_external_ref",
            "document_key",
            "call_id",
            "external_ref",
            "error_code",
        ):
            object.__setattr__(self, name, optional_text(getattr(self, name), name=name))
        if not isinstance(self.state, WeaponryCreationIntentState):
            raise TypeError("state 必须是 WeaponryCreationIntentState")
        non_negative_int(self.version, name="version")
        if self.kind is WeaponryCreationIntentKind.SOURCE_THREAD:
            if not self.parent_external_ref or not self.call_id:
                raise ValueError("source_thread 意图必须携带父 workspace 与 call_id")
        elif self.parent_external_ref:
            raise ValueError("workspace 创建意图不得携带 parent_external_ref")
        if self.state is WeaponryCreationIntentState.PENDING:
            if self.external_ref or self.error_code:
                raise ValueError("pending 创建意图不得携带 external_ref 或 error_code")
        elif self.state is WeaponryCreationIntentState.RESOLVED:
            if not self.external_ref or self.error_code:
                raise ValueError("resolved 创建意图必须且只能携带 external_ref")
        elif not self.error_code or self.external_ref:
            raise ValueError("quarantined 创建意图必须且只能携带 error_code")


@dataclass(frozen=True)
class WeaponryCreationIntentReserveResult:
    """``created`` 用于区分首次授权和历史 pending，禁止二者都执行 create。"""

    created: bool
    intent: WeaponryCreationIntent

    def __post_init__(self) -> None:
        if not isinstance(self.created, bool):
            raise TypeError("created 必须是 bool")
        if not isinstance(self.intent, WeaponryCreationIntent):
            raise TypeError("intent 必须是 WeaponryCreationIntent")
        if self.created and (
            self.intent.state is not WeaponryCreationIntentState.PENDING
            or self.intent.version != 0
        ):
            raise ValueError("首次创建结果必须是 pending/version=0")


@dataclass(frozen=True)
class ResolveWeaponryCreationIntent:
    task_id: TaskId
    intent_id: str
    expected_version: int
    external_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        object.__setattr__(self, "intent_id", required_text(self.intent_id, name="intent_id"))
        non_negative_int(self.expected_version, name="expected_version")
        object.__setattr__(
            self,
            "external_ref",
            required_text(self.external_ref, name="external_ref"),
        )


@dataclass(frozen=True)
class QuarantineWeaponryCreationIntent:
    task_id: TaskId
    intent_id: str
    expected_version: int
    error_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        object.__setattr__(self, "intent_id", required_text(self.intent_id, name="intent_id"))
        non_negative_int(self.expected_version, name="expected_version")
        object.__setattr__(
            self,
            "error_code",
            required_text(self.error_code, name="error_code"),
        )


@runtime_checkable
class WeaponryCreationIntentStorePort(Protocol):
    """创建意图 Store；实现只能执行短事务，不得在事务中访问供应商。"""

    def reserve(
        self, intent: WeaponryCreationIntent
    ) -> WeaponryCreationIntentReserveResult:
        ...

    def get(self, task_id: TaskId, intent_id: str) -> WeaponryCreationIntent | None:
        ...

    def resolve(
        self, command: ResolveWeaponryCreationIntent
    ) -> WeaponryCreationIntent:
        ...

    def quarantine(
        self, command: QuarantineWeaponryCreationIntent
    ) -> WeaponryCreationIntent:
        ...

    def list_pending(self, *, limit: int) -> tuple[WeaponryCreationIntent, ...]:
        ...


__all__ = [
    "QuarantineWeaponryCreationIntent",
    "ResolveWeaponryCreationIntent",
    "WeaponryCreationIntent",
    "WeaponryCreationIntentKind",
    "WeaponryCreationIntentReserveResult",
    "WeaponryCreationIntentState",
    "WeaponryCreationIntentStorePort",
]
