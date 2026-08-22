"""Analysis Callback 业务语义到 Task Control v2 Guard 的唯一适配器。"""

from __future__ import annotations

from collections.abc import Callable
import logging
import time

import requests

from app.infrastructure.observability.callback_history import save_callback_history_payload
from app.modules.analysis.domain.task_inputs import FrozenJsonObject
from app.modules.analysis.ports import (
    AnalysisCallbackAcquireOutcome,
    AnalysisCallbackAcquireResult,
    AnalysisCallbackDelivery,
    AnalysisCallbackDeliveryOutcome,
    AnalysisCallbackDeliveryRequest,
    AnalysisCallbackGuardLease,
    AnalysisCallbackGuardSweepResult,
    AnalysisCallbackRequest,
    AnalysisCallbackWaitOutcome,
    AnalysisCallbackWaitResult,
    AnalysisExecutionRef,
    WaitForAnalysisCallbackRelease,
)
from app.modules.tasks.domain import TaskBusinessRef, add_persisted_utc_seconds
from app.modules.tasks.http_deadlines import required_http_lease_seconds
from app.modules.tasks.ports import (
    CallbackAcquireCommand,
    CallbackAcquireOutcome,
    CallbackCompleteCommand,
    CallbackControlMutationOutcome,
    CallbackDeliveryLease,
    CallbackDeliveryOutcome,
    CallbackDeliveryTrigger,
    CallbackDeliveryUnitOfWorkFactory,
    CallbackGuardState,
    CallbackGuardSweepCommand,
    CallbackValidationCommand,
    CallbackValidationOutcome,
    ClockPort,
)


logger = logging.getLogger(__name__)
_BUSINESS_TYPE = "file"


class TaskControlAnalysisCallbackAdapter:
    """短事务取得、复核并完成发送权；HTTP 与诊断历史始终在事务外。"""

    def __init__(
        self,
        uow_factory: CallbackDeliveryUnitOfWorkFactory,
        *,
        clock: ClockPort,
        callback_timeout: float,
        token_factory: Callable[[], str],
        lease_seconds: float = 30.0,
        wait_poll_interval: float = 0.05,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        transport: Callable[[AnalysisCallbackDeliveryRequest], AnalysisCallbackDelivery]
        | None = None,
    ) -> None:
        if not callable(uow_factory):
            raise TypeError("uow_factory 必须可调用")
        if not isinstance(clock, ClockPort):
            raise TypeError("clock 必须实现 ClockPort")
        for name, value in (
            ("callback_timeout", callback_timeout),
            ("lease_seconds", lease_seconds),
            ("wait_poll_interval", wait_poll_interval),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} 必须是数字")
            if float(value) <= 0 or float(value) != float(value):
                raise ValueError(f"{name} 必须是正有限数字")
        for name, dependency in (
            ("token_factory", token_factory),
            ("monotonic", monotonic),
            ("sleeper", sleeper),
        ):
            if not callable(dependency):
                raise TypeError(f"{name} 必须可调用")
        required_lease = (
            callback_timeout
            if transport is not None
            else required_http_lease_seconds(callback_timeout)
        )
        if float(lease_seconds) < float(required_lease):
            raise ValueError("lease_seconds 必须覆盖 Callback HTTP deadline 与安全余量")
        self._uow_factory = uow_factory
        self._clock = clock
        self._callback_timeout = float(callback_timeout)
        self._lease_seconds = float(lease_seconds)
        self._wait_poll_interval = float(wait_poll_interval)
        self._token_factory = token_factory
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._transport = transport

    def acquire(self, request: AnalysisCallbackRequest) -> AnalysisCallbackAcquireResult:
        if not isinstance(request, AnalysisCallbackRequest):
            raise TypeError("request 必须是 AnalysisCallbackRequest")
        now = self._clock.now_utc()
        token = self._token_factory()
        if not isinstance(token, str) or not token.strip():
            raise ValueError("token_factory 必须返回非空 str")
        trigger = (
            CallbackDeliveryTrigger.EXPLICIT_CHECK_TASK_RECOVERY
            if request.allow_failed_retry
            else CallbackDeliveryTrigger.INITIAL_DELIVERY
        )
        business_ref = TaskBusinessRef(_BUSINESS_TYPE, request.execution.file_name)
        with self._uow_factory() as unit_of_work:
            result = unit_of_work.callback_delivery.acquire(
                CallbackAcquireCommand(
                    task_id=request.execution.task_id,
                    business_ref=business_ref,
                    trigger=trigger,
                    lease_token=token.strip(),
                    acquired_at=now,
                    lease_expires_at=add_persisted_utc_seconds(now, seconds=self._lease_seconds),
                    expected_callback_attempts=request.expected_callback_attempts,
                    request_trace_id=request.request_trace_id,
                )
            )
            unit_of_work.commit()
        outcome = {
            CallbackAcquireOutcome.ACQUIRED: AnalysisCallbackAcquireOutcome.ACQUIRED,
            CallbackAcquireOutcome.STALE: AnalysisCallbackAcquireOutcome.STALE,
            CallbackAcquireOutcome.BUSY: AnalysisCallbackAcquireOutcome.WAIT_FOR_OWNER,
            CallbackAcquireOutcome.OUTCOME_UNKNOWN: AnalysisCallbackAcquireOutcome.FROZEN,
            CallbackAcquireOutcome.ALREADY_COMPLETED: AnalysisCallbackAcquireOutcome.SKIPPED,
            CallbackAcquireOutcome.INVALID_STATE: AnalysisCallbackAcquireOutcome.FROZEN,
        }[result.outcome]
        lease = self._to_analysis_lease(result.lease, request.execution) if result.lease else None
        return AnalysisCallbackAcquireResult(request.execution, outcome, lease)

    def wait_until_released(
        self,
        request: WaitForAnalysisCallbackRelease,
    ) -> AnalysisCallbackWaitResult:
        if not isinstance(request, WaitForAnalysisCallbackRelease):
            raise TypeError("request 必须是 WaitForAnalysisCallbackRelease")
        deadline = self._monotonic() + request.timeout_seconds
        business_ref = TaskBusinessRef(_BUSINESS_TYPE, request.execution.file_name)
        while True:
            with self._uow_factory() as unit_of_work:
                observed = unit_of_work.callback_delivery.observe(
                    business_ref,
                    observed_at=self._clock.now_utc(),
                )
                unit_of_work.commit()
            if observed.state is CallbackGuardState.IDLE:
                return AnalysisCallbackWaitResult(request.execution, AnalysisCallbackWaitOutcome.RELEASED)
            if observed.state is CallbackGuardState.OUTCOME_UNKNOWN:
                return AnalysisCallbackWaitResult(request.execution, AnalysisCallbackWaitOutcome.FROZEN)
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return AnalysisCallbackWaitResult(request.execution, AnalysisCallbackWaitOutcome.TIMED_OUT)
            self._sleeper(min(self._wait_poll_interval, remaining))

    def deliver(self, request: AnalysisCallbackDeliveryRequest) -> AnalysisCallbackDelivery:
        if not isinstance(request, AnalysisCallbackDeliveryRequest):
            raise TypeError("request 必须是 AnalysisCallbackDeliveryRequest")
        with self._uow_factory() as unit_of_work:
            validation = unit_of_work.callback_delivery.validate(
                CallbackValidationCommand(
                    self._to_control_lease(request.lease),
                    self._clock.now_utc(),
                )
            )
            unit_of_work.commit()
        if validation is not CallbackValidationOutcome.VALID:
            logger.warning(
                "Analysis Callback 发送前已失权，跳过 HTTP: task_id=%s file_name=%s "
                "fencing_token=%d outcome=%s",
                request.lease.execution.task_id,
                request.lease.execution.file_name,
                request.lease.lease_version,
                validation.value,
            )
            return self._delivery(
                request.lease,
                AnalysisCallbackDeliveryOutcome.STALE,
                f"guard_validation_{validation.value}",
            )
        if not request.callback_url.strip():
            return self._delivery(
                request.lease,
                AnalysisCallbackDeliveryOutcome.SKIPPED,
                "callback_url_empty",
            )
        if self._transport is None:
            return self._deliver_http(request)
        # 测试或专用部署注入的 Transport 同样必须收到完整 Guard Lease。
        # 若这里只传 URL/payload，Transport 只能从闭包补造 task_id、lease_token 与
        # fencing_token，既不利于轮换 Authority，也会掩盖错误的 Callback 归属。
        result = self._transport(request)
        if not isinstance(result, AnalysisCallbackDelivery):
            raise TypeError("callback transport 必须返回 AnalysisCallbackDelivery")
        if (
            result.execution != request.lease.execution
            or result.lease_token != request.lease.lease_token
            or result.lease_version != request.lease.lease_version
        ):
            raise ValueError("callback transport 结果与 Lease 身份不一致")
        return result

    def complete(
        self,
        lease: AnalysisCallbackGuardLease,
        delivery: AnalysisCallbackDelivery,
        payload: FrozenJsonObject,
    ) -> bool:
        if not isinstance(lease, AnalysisCallbackGuardLease):
            raise TypeError("lease 必须是 AnalysisCallbackGuardLease")
        if not isinstance(delivery, AnalysisCallbackDelivery):
            raise TypeError("delivery 必须是 AnalysisCallbackDelivery")
        if not isinstance(payload, FrozenJsonObject):
            raise TypeError("payload 必须是 FrozenJsonObject")
        outcome = {
            AnalysisCallbackDeliveryOutcome.DELIVERED: CallbackDeliveryOutcome.SUCCESS,
            AnalysisCallbackDeliveryOutcome.DEFINITELY_NOT_SENT: CallbackDeliveryOutcome.DEFINITELY_NOT_SENT,
            AnalysisCallbackDeliveryOutcome.REJECTED: CallbackDeliveryOutcome.REJECTED,
            AnalysisCallbackDeliveryOutcome.OUTCOME_UNKNOWN: CallbackDeliveryOutcome.DELIVERY_OUTCOME_UNKNOWN,
            AnalysisCallbackDeliveryOutcome.SKIPPED: CallbackDeliveryOutcome.SKIPPED,
        }.get(delivery.outcome)
        if outcome is None:
            raise ValueError("stale Callback 结果不得完成 Lease")
        with self._uow_factory() as unit_of_work:
            mutation = unit_of_work.callback_delivery.complete(
                CallbackCompleteCommand(
                    lease=self._to_control_lease(lease),
                    outcome=outcome,
                    detail=delivery.detail_code,
                    completed_at=self._clock.now_utc(),
                )
            )
            unit_of_work.commit()
        if mutation is not CallbackControlMutationOutcome.APPLIED:
            logger.error(
                "Analysis Callback Guard 完成权丢失: task_id=%s file_name=%s "
                "fencing_token=%d outcome=%s mutation=%s",
                lease.execution.task_id,
                lease.execution.file_name,
                lease.lease_version,
                delivery.outcome.value,
                mutation.value,
            )
            return False
        if delivery.outcome is not AnalysisCallbackDeliveryOutcome.SKIPPED:
            try:
                save_callback_history_payload(
                    payload.to_dict(),
                    callback_context={"businessType": _BUSINESS_TYPE, "fileName": lease.execution.file_name},
                )
            except Exception:
                logger.warning("保存 Analysis Callback 诊断历史失败", exc_info=True)
        return True

    def freeze_expired(self, *, limit: int) -> AnalysisCallbackGuardSweepResult:
        with self._uow_factory() as unit_of_work:
            result = unit_of_work.callback_delivery.freeze_expired(
                CallbackGuardSweepCommand(_BUSINESS_TYPE, self._clock.now_utc(), limit)
            )
            unit_of_work.commit()
        return AnalysisCallbackGuardSweepResult(result.scanned_count, result.frozen_count)

    @staticmethod
    def _to_analysis_lease(
        lease: CallbackDeliveryLease,
        execution: AnalysisExecutionRef,
    ) -> AnalysisCallbackGuardLease:
        if (
            lease.task_id != execution.task_id
            or lease.business_ref != TaskBusinessRef(_BUSINESS_TYPE, execution.file_name)
        ):
            raise ValueError("Task Control Callback Lease 与 Analysis execution 不一致")
        return AnalysisCallbackGuardLease(
            execution,
            lease.lease_token,
            lease.fencing_token,
            lease.lease_expires_at,
        )

    @staticmethod
    def _to_control_lease(lease: AnalysisCallbackGuardLease) -> CallbackDeliveryLease:
        return CallbackDeliveryLease(
            task_id=lease.execution.task_id,
            business_ref=TaskBusinessRef(_BUSINESS_TYPE, lease.execution.file_name),
            lease_token=lease.lease_token,
            fencing_token=lease.lease_version,
            lease_expires_at=lease.expires_at,
        )

    @staticmethod
    def _delivery(
        lease: AnalysisCallbackGuardLease,
        outcome: AnalysisCallbackDeliveryOutcome,
        detail: str = "",
    ) -> AnalysisCallbackDelivery:
        return AnalysisCallbackDelivery(
            lease.execution,
            lease.lease_token,
            lease.lease_version,
            outcome,
            detail,
        )

    def _deliver_http(self, request: AnalysisCallbackDeliveryRequest) -> AnalysisCallbackDelivery:
        response = None
        detail = ""
        try:
            response = requests.post(
                request.callback_url,
                json=request.payload.to_dict(),
                timeout=self._callback_timeout,
                stream=True,
                allow_redirects=False,
            )
        except (
            requests.exceptions.ConnectTimeout,
            requests.exceptions.InvalidSchema,
            requests.exceptions.InvalidURL,
            requests.exceptions.MissingSchema,
        ) as error:
            outcome = AnalysisCallbackDeliveryOutcome.DEFINITELY_NOT_SENT
            detail = type(error).__name__
        except requests.exceptions.RequestException as error:
            outcome = AnalysisCallbackDeliveryOutcome.OUTCOME_UNKNOWN
            detail = type(error).__name__
        else:
            if 200 <= response.status_code < 300:
                outcome = AnalysisCallbackDeliveryOutcome.DELIVERED
            else:
                outcome = AnalysisCallbackDeliveryOutcome.REJECTED
                detail = f"http_status_{response.status_code}"
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    logger.warning("关闭 Analysis Callback 响应失败", exc_info=True)
        return self._delivery(request.lease, outcome, detail)


__all__ = ["TaskControlAnalysisCallbackAdapter"]
