"""关闭术语规则辅助时真正零 I/O 的 Adapter。"""

from __future__ import annotations

import logging

from app.modules.weaponry.domain import AUXILIARY_GUIDANCE_NONE
from app.modules.weaponry.ports import (
    AuxiliaryGuidanceOutcome,
    AuxiliaryGuidanceRequest,
    AuxiliaryGuidanceResult,
    WeaponryPortStateError,
)


logger = logging.getLogger(__name__)


class NoAuxiliaryGuidanceAdapter:
    """不读取目录、环境变量，不创建 Client，也不发生 embedding/网络调用。"""

    def load(self, request: AuxiliaryGuidanceRequest) -> AuxiliaryGuidanceResult:
        if not isinstance(request, AuxiliaryGuidanceRequest):
            raise TypeError("request 必须是 AuxiliaryGuidanceRequest")
        if request.policy.policy_id != AUXILIARY_GUIDANCE_NONE:
            raise WeaponryPortStateError(
                "auxiliary_policy_adapter_mismatch",
                "none Adapter 不能执行术语规则策略",
            )
        logger.debug(
            "武器谱辅助语境已关闭，保持零 I/O: task_id=%s call_id=%s",
            request.call.task_id.value,
            request.call.call_id,
        )
        return AuxiliaryGuidanceResult(
            call=request.call,
            guidance=(),
            outcome=AuxiliaryGuidanceOutcome.EMPTY,
        )


__all__ = ["NoAuxiliaryGuidanceAdapter"]
