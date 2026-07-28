"""Analysis 召回决策与完整模型交互的强类型审计 Port。

审计数据可以包含必须持久化的 Prompt、模型原始响应和来源证据，但 Adapter 与 Application
日志禁止输出这些正文。召回审计采用 reserve/finalize 两步，保证远端 Session 创建前已有
本地事实；交互审计则原子保存全部 attempt，成功 Receipt 是永久入库的硬前置。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from app.modules.analysis.domain.task_inputs import FrozenJsonObject

from .common import AnalysisExecutionRef
from .rag import (
    AnalysisRagLifecycleEvent,
    AnalysisRagOperation,
    AnalysisRagSessionRef,
    AnalysisRagSource,
)


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空 str")
    return value.strip()


def _non_negative_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} 必须是非负整数")
    return value


class AnalysisAuditOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class AnalysisRecallAuditRecord:
    """在创建任何远端 RAG 资源前写入的确定性召回事实。"""

    execution: AnalysisExecutionRef
    idempotency_key: str
    payload: FrozenJsonObject

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        object.__setattr__(
            self,
            "idempotency_key",
            _required_text(self.idempotency_key, name="idempotency_key"),
        )
        if not isinstance(self.payload, FrozenJsonObject):
            raise TypeError("payload 必须是 FrozenJsonObject")


@dataclass(frozen=True)
class AnalysisRecallAuditReceipt:
    """召回审计的持久化身份和 CAS 版本。"""

    execution: AnalysisExecutionRef
    idempotency_key: str
    audit_id: str
    version: int
    finalized: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        for name in ("idempotency_key", "audit_id"):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name=name),
            )
        _non_negative_int(self.version, name="version")
        if not isinstance(self.finalized, bool):
            raise TypeError("finalized 必须是 bool")


@dataclass(frozen=True)
class FinalizeAnalysisRecallAudit:
    """按 Receipt 版本终结召回审计，重复或过期写入必须由 Adapter 拒绝。"""

    receipt: AnalysisRecallAuditReceipt
    expected_version: int
    outcome: AnalysisAuditOutcome
    payload: FrozenJsonObject
    error_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, AnalysisRecallAuditReceipt):
            raise TypeError("receipt 必须是 AnalysisRecallAuditReceipt")
        expected_version = _non_negative_int(
            self.expected_version,
            name="expected_version",
        )
        if self.receipt.version != expected_version:
            raise ValueError("receipt.version 与 expected_version 不一致")
        if self.receipt.finalized:
            raise ValueError("已 finalized 的召回审计不得再次终结")
        if not isinstance(self.outcome, AnalysisAuditOutcome):
            raise TypeError("outcome 必须是 AnalysisAuditOutcome")
        if not isinstance(self.payload, FrozenJsonObject):
            raise TypeError("payload 必须是 FrozenJsonObject")
        if not isinstance(self.error_code, str):
            raise TypeError("error_code 必须是 str")
        if self.outcome is AnalysisAuditOutcome.SUCCEEDED and self.error_code:
            raise ValueError("成功召回审计不得携带 error_code")
        if self.outcome is AnalysisAuditOutcome.FAILED and not self.error_code.strip():
            raise ValueError("失败召回审计必须携带 error_code")


@dataclass(frozen=True)
class AnalysisInteractionAttempt:
    """一次模型调用的完整审计快照，正文只允许进入审计存储而非日志。"""

    operation: AnalysisRagOperation
    attempt_number: int
    prompt_digest: str
    raw_response: str | None
    sources: tuple[AnalysisRagSource, ...] = ()
    error_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.operation, AnalysisRagOperation):
            raise TypeError("operation 必须是 AnalysisRagOperation")
        if (
            isinstance(self.attempt_number, bool)
            or not isinstance(self.attempt_number, int)
            or self.attempt_number < 1
        ):
            raise ValueError("attempt_number 必须是正整数")
        digest = _required_text(self.prompt_digest, name="prompt_digest").lower()
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError("prompt_digest 必须是 SHA-256 小写十六进制摘要")
        object.__setattr__(self, "prompt_digest", digest)
        if self.raw_response is not None and not isinstance(self.raw_response, str):
            raise TypeError("raw_response 必须是 str 或 None")
        sources = tuple(self.sources)
        if any(not isinstance(item, AnalysisRagSource) for item in sources):
            raise TypeError("sources 只能包含 AnalysisRagSource")
        object.__setattr__(self, "sources", sources)
        if not isinstance(self.error_code, str):
            raise TypeError("error_code 必须是 str")
        if not self.error_code and self.raw_response is None:
            raise ValueError("成功 attempt 必须明确携带 raw_response")


@dataclass(frozen=True)
class AnalysisInteractionAuditRecord:
    """原子持久化 Prompt、全部 attempts 与最终业务结果。

    打开 Context 后创建 Conversation 可能失败，此时不存在可合法构造的完整 SessionRef，
    但 lifecycle 中已经包含必须持久化的部分资源引用。因此失败审计允许 ``session=None``；
    成功审计仍必须携带完整 Session，并继续执行来源归属校验。
    """

    execution: AnalysisExecutionRef
    idempotency_key: str
    session: AnalysisRagSessionRef | None
    context_name: str
    trace_id: str
    prompt: str
    attempts: tuple[AnalysisInteractionAttempt, ...]
    lifecycle_events: tuple[AnalysisRagLifecycleEvent, ...]
    outcome: AnalysisAuditOutcome
    error_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        object.__setattr__(
            self,
            "idempotency_key",
            _required_text(self.idempotency_key, name="idempotency_key"),
        )
        if self.session is not None:
            if not isinstance(self.session, AnalysisRagSessionRef):
                raise TypeError("session 必须是 AnalysisRagSessionRef 或 None")
            if self.session.execution != self.execution:
                raise ValueError("session 必须属于当前 execution")
        for field_name in ("context_name", "trace_id"):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), name=field_name),
            )
        if not isinstance(self.prompt, str) or not self.prompt:
            raise ValueError("prompt 必须是非空 str")
        if not isinstance(self.outcome, AnalysisAuditOutcome):
            raise TypeError("outcome 必须是 AnalysisAuditOutcome")
        attempts = tuple(self.attempts)
        if any(not isinstance(item, AnalysisInteractionAttempt) for item in attempts):
            raise TypeError("attempts 只能包含 AnalysisInteractionAttempt")
        # Context/Conversation 创建本身也可能失败。此时尚未发生模型请求，不能为了满足
        # DTO 形状伪造一条不存在的 attempt；但成功交互仍必须至少保存一条真实模型调用。
        if self.outcome is AnalysisAuditOutcome.SUCCEEDED and not attempts:
            raise ValueError("成功交互审计必须包含 AnalysisInteractionAttempt")
        if self.outcome is AnalysisAuditOutcome.SUCCEEDED and self.session is None:
            raise ValueError("成功交互审计必须携带完整 RAG SessionRef")
        attempt_keys = tuple(
            (item.operation, item.attempt_number) for item in attempts
        )
        if len(set(attempt_keys)) != len(attempt_keys):
            raise ValueError("同一 operation 的 attempt_number 不得重复")
        for operation in {item.operation for item in attempts}:
            numbers = tuple(
                item.attempt_number for item in attempts if item.operation is operation
            )
            if numbers != tuple(range(1, len(numbers) + 1)):
                raise ValueError("同一 operation 的 attempt_number 必须从 1 连续递增")
        object.__setattr__(self, "attempts", attempts)
        if self.session is not None and self.session.document_bound:
            for attempt in attempts:
                if any(
                    source.document_ref != self.session.document_ref
                    for source in attempt.sources
                ):
                    raise ValueError("attempt sources 必须全部属于当前 session.document_ref")
        lifecycle_events = tuple(self.lifecycle_events)
        if not lifecycle_events or any(
            not isinstance(item, AnalysisRagLifecycleEvent)
            for item in lifecycle_events
        ):
            raise ValueError("lifecycle_events 必须包含 AnalysisRagLifecycleEvent")
        sequences = tuple(item.sequence_no for item in lifecycle_events)
        if sequences != tuple(sorted(set(sequences))):
            raise ValueError("lifecycle_events sequence_no 必须严格递增且不重复")
        if sequences != tuple(range(1, len(lifecycle_events) + 1)):
            raise ValueError("初始 lifecycle_events sequence_no 必须从 1 连续递增")
        object.__setattr__(self, "lifecycle_events", lifecycle_events)
        if not isinstance(self.error_code, str):
            raise TypeError("error_code 必须是 str")
        if self.outcome is AnalysisAuditOutcome.SUCCEEDED:
            if self.error_code or attempts[-1].error_code:
                raise ValueError("成功交互审计不得携带失败信息")
        elif not self.error_code.strip():
            raise ValueError("失败交互审计必须携带 error_code")


@dataclass(frozen=True)
class AnalysisInteractionAuditReceipt:
    """完整交互事务已经提交的凭据。"""

    execution: AnalysisExecutionRef
    idempotency_key: str
    audit_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        for name in ("idempotency_key", "audit_id"):
            object.__setattr__(
                self,
                name,
                _required_text(getattr(self, name), name=name),
            )


@dataclass(frozen=True)
class AppendAnalysisLifecycleEvents:
    """交互审计成功后幂等追加 close/cleanup 事件，禁止覆盖已有序号。"""

    receipt: AnalysisInteractionAuditReceipt
    events: tuple[AnalysisRagLifecycleEvent, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.receipt, AnalysisInteractionAuditReceipt):
            raise TypeError("receipt 必须是 AnalysisInteractionAuditReceipt")
        events = tuple(self.events)
        if not events or any(
            not isinstance(item, AnalysisRagLifecycleEvent) for item in events
        ):
            raise ValueError("events 必须包含 AnalysisRagLifecycleEvent")
        sequences = tuple(item.sequence_no for item in events)
        if sequences != tuple(sorted(set(sequences))):
            raise ValueError("events sequence_no 必须严格递增且不重复")
        object.__setattr__(self, "events", events)


@dataclass(frozen=True)
class LoadAnalysisInteraction:
    """按 execution 与幂等键联合读取，禁止仅凭全局字符串串读其他任务。"""

    execution: AnalysisExecutionRef
    idempotency_key: str

    def __post_init__(self) -> None:
        if not isinstance(self.execution, AnalysisExecutionRef):
            raise TypeError("execution 必须是 AnalysisExecutionRef")
        object.__setattr__(
            self,
            "idempotency_key",
            _required_text(self.idempotency_key, name="idempotency_key"),
        )


@runtime_checkable
class AnalysisAuditPort(Protocol):
    """召回与交互审计的唯一持久化边界。"""

    def reserve_recall(
        self,
        record: AnalysisRecallAuditRecord,
    ) -> AnalysisRecallAuditReceipt:
        ...

    def finalize_recall(
        self,
        command: FinalizeAnalysisRecallAudit,
    ) -> AnalysisRecallAuditReceipt:
        ...

    def persist_interaction(
        self,
        record: AnalysisInteractionAuditRecord,
    ) -> AnalysisInteractionAuditReceipt:
        ...

    def load_interaction(
        self,
        query: LoadAnalysisInteraction,
    ) -> AnalysisInteractionAuditReceipt | None:
        ...

    def append_lifecycle_events(
        self,
        command: AppendAnalysisLifecycleEvents,
    ) -> None:
        ...


__all__ = (
    "AnalysisAuditOutcome",
    "AnalysisAuditPort",
    "AppendAnalysisLifecycleEvents",
    "AnalysisInteractionAttempt",
    "AnalysisInteractionAuditReceipt",
    "AnalysisInteractionAuditRecord",
    "AnalysisRecallAuditReceipt",
    "AnalysisRecallAuditRecord",
    "FinalizeAnalysisRecallAudit",
    "LoadAnalysisInteraction",
)
