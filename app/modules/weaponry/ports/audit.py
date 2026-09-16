"""武器谱外部交互的 reserve/complete 原子审计端口。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.weaponry.domain import normalize_architecture_id_value

from .common import (
    WeaponryCallIdentity,
    WeaponryOperation,
    non_negative_int,
    optional_text,
    required_text,
    sha256_digest,
    text_tuple,
)


def _validate_business_ref(value: object) -> TaskBusinessRef:
    if not isinstance(value, TaskBusinessRef):
        raise TypeError("business_ref 必须是 TaskBusinessRef")
    if value.business_type != "weaponry":
        raise ValueError("武器谱审计 business_type 必须是 weaponry")
    # 端口层只核对规范业务键，不做公开请求的宽松兼容转换。
    architecture_id = normalize_architecture_id_value(value.business_key)
    if str(architecture_id) != value.business_key:
        raise ValueError("武器谱审计 business_key 必须是规范十进制字符串")
    return value


@dataclass(frozen=True)
class ReserveWeaponryInteraction:
    """外部调用前原子预留 pending 审计事实。"""

    business_ref: TaskBusinessRef
    call: WeaponryCallIdentity
    input_digest: str
    input_chars: int
    allowed_document_keys: tuple[str, ...] = ()
    source_marker_digests: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _validate_business_ref(self.business_ref)
        if not isinstance(self.call, WeaponryCallIdentity):
            raise TypeError("call 必须是 WeaponryCallIdentity")
        object.__setattr__(
            self,
            "input_digest",
            sha256_digest(self.input_digest, name="input_digest"),
        )
        non_negative_int(self.input_chars, name="input_chars")
        object.__setattr__(
            self,
            "allowed_document_keys",
            text_tuple(
                self.allowed_document_keys,
                name="allowed_document_keys",
            ),
        )
        if not isinstance(self.source_marker_digests, (tuple, list)):
            raise TypeError("source_marker_digests 必须是摘要序列")
        marker_digests = tuple(
            sha256_digest(item, name="source_marker_digest")
            for item in self.source_marker_digests
        )
        if len(set(marker_digests)) != len(marker_digests):
            raise ValueError("source_marker_digests 不能重复")
        object.__setattr__(self, "source_marker_digests", marker_digests)


@dataclass(frozen=True)
class WeaponryAuditReservation:
    """尚未完成的原子审计预留凭据。"""

    reservation_id: str
    business_ref: TaskBusinessRef
    call: WeaponryCallIdentity

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reservation_id",
            required_text(self.reservation_id, name="reservation_id"),
        )
        _validate_business_ref(self.business_ref)
        if not isinstance(self.call, WeaponryCallIdentity):
            raise TypeError("call 必须是 WeaponryCallIdentity")


class WeaponryAuditReserveOutcome(str, Enum):
    """同一 ``attempt_key`` 在持久审计中的原子预留分类。

    ``RESERVED`` 是唯一允许 Application 继续执行外部 I/O 的结果。``PENDING`` 表示
    先前 Worker 可能已经发出外部请求但尚未提交结果，``COMPLETED`` 表示该调用已经留下
    完整审计终态；后二者都必须交由恢复/对账流程处理，不能被解释为新的调用授权。
    """

    RESERVED = "reserved"
    PENDING = "pending"
    COMPLETED = "completed"


@dataclass(frozen=True)
class WeaponryAuditReserveResult:
    """携带预留凭据和原子分类，消除 pending 重放歧义。"""

    outcome: WeaponryAuditReserveOutcome
    reservation: WeaponryAuditReservation

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, WeaponryAuditReserveOutcome):
            raise TypeError("outcome 必须是 WeaponryAuditReserveOutcome")
        if not isinstance(self.reservation, WeaponryAuditReservation):
            raise TypeError("reservation 必须是 WeaponryAuditReservation")


class WeaponryAuditOutcome(str, Enum):
    """一次交互的最终审计分类。"""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"


@dataclass(frozen=True)
class CompleteWeaponryInteraction:
    """原子完成一条 pending 审计，不保存业务正文。"""

    reservation: WeaponryAuditReservation
    outcome: WeaponryAuditOutcome
    output_digest: str = ""
    output_chars: int = 0
    candidate_count: int = 0
    selected_count: int = 0
    source_count: int = 0
    verified_source_count: int = 0
    missing_source_count: int = 0
    mismatched_source_count: int = 0
    rejection_reasons: tuple[str, ...] = ()
    error_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.reservation, WeaponryAuditReservation):
            raise TypeError("reservation 必须是 WeaponryAuditReservation")
        if not isinstance(self.outcome, WeaponryAuditOutcome):
            raise TypeError("outcome 必须是 WeaponryAuditOutcome")
        output_digest = optional_text(self.output_digest, name="output_digest")
        if output_digest:
            output_digest = sha256_digest(output_digest, name="output_digest")
        if self.outcome is WeaponryAuditOutcome.SUCCEEDED and not output_digest:
            raise ValueError("成功审计必须携带 output_digest")
        object.__setattr__(self, "output_digest", output_digest)
        for name in (
            "output_chars",
            "candidate_count",
            "selected_count",
            "source_count",
            "verified_source_count",
            "missing_source_count",
            "mismatched_source_count",
        ):
            non_negative_int(getattr(self, name), name=name)
        if (
            self.reservation.call.operation is WeaponryOperation.TARGET_RETRIEVAL
            and self.selected_count > self.candidate_count
        ):
            raise ValueError("目标检索 selected_count 不得超过 candidate_count")
        if (
            self.verified_source_count
            + self.missing_source_count
            + self.mismatched_source_count
            != self.source_count
        ):
            raise ValueError("来源校验分类数量之和必须等于 source_count")
        if not isinstance(self.rejection_reasons, (tuple, list)):
            raise TypeError("rejection_reasons 必须是有序文本序列")
        rejection_reasons = tuple(
            required_text(item, name="rejection_reason")
            for item in self.rejection_reasons
        )
        if self.reservation.call.operation is WeaponryOperation.TARGET_RETRIEVAL:
            if self.selected_count + len(rejection_reasons) != self.candidate_count:
                raise ValueError(
                    "目标检索 selected_count 与 rejection_reasons 必须完整覆盖 candidate_count"
                )
        elif rejection_reasons:
            raise ValueError("只有目标检索审计可以携带 rejection_reasons")
        object.__setattr__(self, "rejection_reasons", rejection_reasons)
        error_code = optional_text(self.error_code, name="error_code")
        if self.outcome is WeaponryAuditOutcome.SUCCEEDED and error_code:
            raise ValueError("成功审计不得携带 error_code")
        if self.outcome is not WeaponryAuditOutcome.SUCCEEDED and not error_code:
            raise ValueError("失败或拒绝审计必须携带 error_code")
        object.__setattr__(self, "error_code", error_code)


@dataclass(frozen=True)
class WeaponryAuditReceipt:
    """审计完成已经持久提交的凭据。"""

    audit_id: str
    reservation_id: str
    task_id: TaskId
    attempt_key: str

    def __post_init__(self) -> None:
        for name in ("audit_id", "reservation_id", "attempt_key"):
            object.__setattr__(
                self,
                name,
                required_text(getattr(self, name), name=name),
            )
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")


@runtime_checkable
class WeaponryInteractionAuditPort(Protocol):
    """关键外部调用必须 reserve 成功后执行，并在提交业务结果前 complete。"""

    def reserve(
        self,
        command: ReserveWeaponryInteraction,
    ) -> WeaponryAuditReserveResult:
        ...

    def complete(
        self,
        command: CompleteWeaponryInteraction,
    ) -> WeaponryAuditReceipt:
        ...

    def list_pending(
        self,
        task_id: TaskId,
        *,
        limit: int,
    ) -> tuple[WeaponryAuditReservation, ...]:
        """有界列出崩溃遗留 pending；结果不得被解释为成功。"""
        ...


__all__ = [
    "CompleteWeaponryInteraction",
    "ReserveWeaponryInteraction",
    "WeaponryAuditOutcome",
    "WeaponryAuditReceipt",
    "WeaponryAuditReservation",
    "WeaponryAuditReserveOutcome",
    "WeaponryAuditReserveResult",
    "WeaponryInteractionAuditPort",
]
