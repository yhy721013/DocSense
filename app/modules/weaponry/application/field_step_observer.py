"""把 Weaponry 字段级外部调用与审计提交映射为 Authority-aware Step。"""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
from typing import TypeVar

from app.modules.tasks.domain import TaskStepCheckpoint
from app.modules.tasks.ports import TaskWorkflowContextPort
from app.modules.weaponry.application.errors import WeaponryScenePreservationError
from app.modules.weaponry.ports import (
    WeaponryAuditReceipt,
    WeaponryCallIdentity,
    WeaponryExternalOperationError,
    WeaponryExternalOutcome,
)

from .step_runtime import ActiveWeaponryStep, WeaponryStepRuntime


T = TypeVar("T")


def _digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class WeaponryFieldStepObserver:
    """字段执行器的窄 Step 桥；不让供应商 Adapter 接触 Authority。"""

    def __init__(
        self,
        *,
        context: TaskWorkflowContextPort,
        runtime: WeaponryStepRuntime,
    ) -> None:
        if not isinstance(context, TaskWorkflowContextPort):
            raise TypeError("context 必须实现 TaskWorkflowContextPort")
        if not isinstance(runtime, WeaponryStepRuntime):
            raise TypeError("runtime 必须是 WeaponryStepRuntime")
        self._context = context
        self._runtime = runtime
        self._audit_steps: dict[str, ActiveWeaponryStep] = {}
        self._deferred_unknown_steps: dict[
            str,
            tuple[ActiveWeaponryStep, str],
        ] = {}

    def execute(
        self,
        *,
        step_key: str,
        idempotency_key: str,
        operation: Callable[[], T],
        checkpoint_code: str,
        result_identity: Callable[[T], object],
        audit_call: WeaponryCallIdentity | None = None,
    ) -> T:
        """在外部调用前落 intent，调用返回后只保存稳定摘要。

        对模型、翻译这类“外部调用被审计包围”的操作，结果未知时不能立刻隔离
        Task。否则后续审计完成条件写会因为 Authority 已失效而必然失败。此处只暂存
        未知结论，待 ``complete_audit`` 先提交审计事实后，再原子隔离 Task。
        """

        if audit_call is not None and not isinstance(
            audit_call,
            WeaponryCallIdentity,
        ):
            raise TypeError("audit_call 必须是 WeaponryCallIdentity 或 None")

        active = self._runtime.begin(
            self._context,
            step_key=step_key,
            idempotency_key=idempotency_key,
        )
        try:
            result = operation()
        except BaseException as exc:
            error_code = self._error_code(exc)
            outcome_unknown = self._outcome_unknown(exc)
            if outcome_unknown and audit_call is not None:
                if audit_call.call_id in self._deferred_unknown_steps:
                    raise RuntimeError("同一 call_id 已存在待提交的外部未知 Step")
                self._deferred_unknown_steps[audit_call.call_id] = (
                    active,
                    error_code,
                )
            else:
                self._runtime.fail(
                    self._context,
                    active,
                    error_code=error_code,
                    outcome_unknown=outcome_unknown,
                )
            raise
        identity = result_identity(result)
        digest = _digest(identity)
        self._runtime.succeed(
            self._context,
            active,
            TaskStepCheckpoint(
                code=checkpoint_code,
                result_ref=f"weaponry-step:v1:{digest}",
                result_digest=digest,
            ),
        )
        return result

    def begin_audit(self, call: WeaponryCallIdentity) -> None:
        if not isinstance(call, WeaponryCallIdentity):
            raise TypeError("call 必须是 WeaponryCallIdentity")
        if call.call_id in self._audit_steps:
            raise RuntimeError("同一 call_id 的 Audit Step 已经开始")
        self._audit_steps[call.call_id] = self._runtime.begin(
            self._context,
            step_key=f"interaction_audit.commit:{call.call_id}",
            idempotency_key=f"weaponry-audit:{call.attempt_key}",
        )

    def complete_audit(
        self,
        call: WeaponryCallIdentity,
        receipt: WeaponryAuditReceipt,
    ) -> None:
        active = self._audit_steps.pop(call.call_id, None)
        if active is None:
            raise RuntimeError("Audit 完成缺少已持久化 Step intent")
        digest = _digest(
            {
                "reservation_id": receipt.reservation_id,
                "attempt_key": receipt.attempt_key,
                "audit_id": receipt.audit_id,
            }
        )
        self._runtime.succeed(
            self._context,
            active,
            TaskStepCheckpoint(
                code="weaponry_interaction_audit_committed_v1",
                result_ref=f"weaponry-audit:{receipt.audit_id}",
                result_digest=digest,
            ),
        )
        deferred = self._deferred_unknown_steps.pop(call.call_id, None)
        if deferred is not None:
            external_step, error_code = deferred
            # 审计成功事实必须先于 Task 隔离提交。隔离完成后禁止再使用当前 Authority
            # 写任何 Step；调用方随后只允许执行不依赖 Task Authority 的现场隔离。
            self._runtime.fail(
                self._context,
                external_step,
                error_code=error_code,
                outcome_unknown=True,
            )

    def fail_audit(self, call: WeaponryCallIdentity, *, error_code: str) -> None:
        active = self._audit_steps.pop(call.call_id, None)
        # 审计本身结果未知已经足以隔离 Task；关联外部 Step 保留 running intent，供
        # Recovery Fact Collector 识别，不能在 Authority 失效后继续补写第二笔未知。
        self._deferred_unknown_steps.pop(call.call_id, None)
        if active is None:
            return
        # SQLite 调用抛错时无法证明 commit 是否到达；保守隔离，禁止模型请求重放。
        self._runtime.fail(
            self._context,
            active,
            error_code=error_code,
            outcome_unknown=True,
        )

    @staticmethod
    def _outcome_unknown(exc: BaseException) -> bool:
        if isinstance(exc, WeaponryScenePreservationError):
            return True
        return bool(
            isinstance(exc, WeaponryExternalOperationError)
            and exc.outcome is WeaponryExternalOutcome.OUTCOME_UNKNOWN
        )

    @staticmethod
    def _error_code(exc: BaseException) -> str:
        code = getattr(exc, "error_code", "") or getattr(exc, "code", "")
        return str(code).strip() or "weaponry_step_unclassified_error"


__all__ = ["WeaponryFieldStepObserver"]
