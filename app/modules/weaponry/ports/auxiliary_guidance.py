"""可整体替换或删除的武器谱辅助语境端口。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from app.modules.weaponry.domain import (
    AUXILIARY_GUIDANCE_NONE,
    AuxiliaryGuidance,
    AuxiliaryGuidancePolicySnapshot,
    WeaponryFieldSpecification,
)

from .common import WeaponryCallIdentity, WeaponryOperation, optional_text


@dataclass(frozen=True)
class AuxiliaryGuidanceRequest:
    """字段级辅助检索输入；不暴露术语目录、workspace 或环境变量。"""

    call: WeaponryCallIdentity
    field: WeaponryFieldSpecification
    policy: AuxiliaryGuidancePolicySnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.call, WeaponryCallIdentity):
            raise TypeError("call 必须是 WeaponryCallIdentity")
        if self.call.operation is not WeaponryOperation.AUXILIARY_GUIDANCE:
            raise ValueError("辅助语境只能使用 auxiliary_guidance call")
        if not isinstance(self.field, WeaponryFieldSpecification):
            raise TypeError("field 必须是 WeaponryFieldSpecification")
        if not isinstance(self.policy, AuxiliaryGuidancePolicySnapshot):
            raise TypeError("policy 必须是 AuxiliaryGuidancePolicySnapshot")


class AuxiliaryGuidanceOutcome(str, Enum):
    """辅助语境获取结果；降级不会使目标字段失败。"""

    PROVIDED = "provided"
    EMPTY = "empty"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class AuxiliaryGuidanceResult:
    """辅助片段及兼容降级结果。"""

    call: WeaponryCallIdentity
    guidance: tuple[AuxiliaryGuidance, ...]
    outcome: AuxiliaryGuidanceOutcome
    error_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.call, WeaponryCallIdentity):
            raise TypeError("call 必须是 WeaponryCallIdentity")
        if self.call.operation is not WeaponryOperation.AUXILIARY_GUIDANCE:
            raise ValueError("辅助结果只能绑定 auxiliary_guidance call")
        if not isinstance(self.guidance, (tuple, list)) or any(
            not isinstance(item, AuxiliaryGuidance) for item in self.guidance
        ):
            raise TypeError("guidance 只能包含 AuxiliaryGuidance")
        guidance = tuple(self.guidance)
        guidance_ids = tuple(item.guidance_id for item in guidance)
        if len(set(guidance_ids)) != len(guidance_ids):
            raise ValueError("guidance_id 不能重复")
        object.__setattr__(self, "guidance", guidance)
        if not isinstance(self.outcome, AuxiliaryGuidanceOutcome):
            raise TypeError("outcome 必须是 AuxiliaryGuidanceOutcome")
        error_code = optional_text(self.error_code, name="error_code")
        if self.outcome is AuxiliaryGuidanceOutcome.PROVIDED and not guidance:
            raise ValueError("provided 结果必须包含辅助片段")
        if self.outcome is not AuxiliaryGuidanceOutcome.PROVIDED and guidance:
            raise ValueError("empty/degraded 结果不得包含辅助片段")
        if self.outcome is AuxiliaryGuidanceOutcome.DEGRADED and not error_code:
            raise ValueError("degraded 结果必须携带 error_code")
        if self.outcome is not AuxiliaryGuidanceOutcome.DEGRADED and error_code:
            raise ValueError("非 degraded 结果不得携带 error_code")
        object.__setattr__(self, "error_code", error_code)


@runtime_checkable
class AuxiliaryGuidancePort(Protocol):
    """返回通用辅助片段；``none`` 实现必须保持真正零 I/O。"""

    def load(self, request: AuxiliaryGuidanceRequest) -> AuxiliaryGuidanceResult:
        ...


def validate_auxiliary_result_policy(
    request: AuxiliaryGuidanceRequest,
    result: AuxiliaryGuidanceResult,
) -> None:
    """验证结果与任务冻结策略一致，供严格 Fake 和后续 Adapter 共同复用。"""

    if result.call != request.call:
        raise ValueError("辅助结果与请求 call 不一致")
    if request.policy.policy_id == AUXILIARY_GUIDANCE_NONE:
        if result.outcome is not AuxiliaryGuidanceOutcome.EMPTY or result.guidance:
            raise ValueError("none 辅助策略只能返回零 I/O 空结果")


__all__ = [
    "AuxiliaryGuidanceOutcome",
    "AuxiliaryGuidancePort",
    "AuxiliaryGuidanceRequest",
    "AuxiliaryGuidanceResult",
    "validate_auxiliary_result_policy",
]
