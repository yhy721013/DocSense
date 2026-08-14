"""Weaponry Callback 业务语义到 Task Control v2 Guard 的唯一生产适配器。"""

from __future__ import annotations

from collections.abc import Callable
import logging
import time

import requests

from app.infrastructure.observability.callback_history import save_callback_history_payload
from app.modules.weaponry.domain import WeaponryCallbackPayload
from app.modules.weaponry.ports import (
    DeliverWeaponryCallback,
    ReleaseUnknownWeaponryCallback,
    AcquireWeaponryCallback,
    WeaponryCallbackAcquireOutcome,
    WeaponryCallbackAcquireReason,
    WeaponryCallbackAcquireResult,
    WeaponryCallbackDeliveryOutcome,
    WeaponryCallbackDeliveryResult,
    WeaponryCallbackGuardLease,
    WeaponryCallbackGuardSweepResult,
    WeaponryCallbackReleaseOutcome,
    WeaponryCallbackReleaseResult,
    WeaponryCallbackWaitOutcome,
    WeaponryCallbackWaitResult,
    WaitForWeaponryCallbackRelease,
)
from app.modules.tasks.domain import (
    TaskBusinessRef,
    add_persisted_utc_seconds,
)
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
    CallbackReleaseOutcome,
    CallbackReleaseUnknownCommand,
    CallbackValidationCommand,
    CallbackValidationOutcome,
    ClockPort,
)


logger = logging.getLogger(__name__)
_WEAPONRY_BUSINESS_TYPE = "weaponry"


class TaskControlWeaponryCallbackAdapter:
    """短事务控制发送权，事务外执行 HTTP 与诊断历史写入。

    acquire/validate/observe 会在发现过期 lease 时原子冻结 unknown，因此无论返回哪类
    竞争结果都必须提交该次短事务。任何日志都禁止包含 lease token。
    """

    def __init__(
        self,
        uow_factory: CallbackDeliveryUnitOfWorkFactory,
        *,
        clock: ClockPort,
        callback_url: str,
        callback_timeout: float,
        token_factory: Callable[[], str],
        lease_seconds: float = 30.0,
        wait_poll_interval: float = 0.05,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        transport: Callable[[dict[str, object]], WeaponryCallbackDeliveryResult]
        | None = None,
    ) -> None:
        if not callable(uow_factory):
            raise TypeError("uow_factory 必须可调用")
        if not isinstance(clock, ClockPort):
            raise TypeError("clock 必须实现 ClockPort")
        if not isinstance(callback_url, str):
            raise TypeError("callback_url 必须是 str")
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
        self._callback_url = callback_url.strip()
        self._callback_timeout = float(callback_timeout)
        self._lease_seconds = float(lease_seconds)
        self._wait_poll_interval = float(wait_poll_interval)
        self._token_factory = token_factory
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._transport = transport or self._deliver_http

    def acquire(self, command: AcquireWeaponryCallback) -> WeaponryCallbackAcquireResult:
        if not isinstance(command, AcquireWeaponryCallback):
            raise TypeError("command 必须是 AcquireWeaponryCallback")
        now = self._clock.now_utc()
        token = self._token_factory()
        if not isinstance(token, str) or not token.strip():
            raise ValueError("token_factory 必须返回非空 str")
        trigger = (
            CallbackDeliveryTrigger.EXPLICIT_CHECK_TASK_RECOVERY
            if command.reason is WeaponryCallbackAcquireReason.EXPLICIT_CHECK_TASK_RECOVERY
            else CallbackDeliveryTrigger.INITIAL_DELIVERY
        )
        with self._uow_factory() as unit_of_work:
            result = unit_of_work.callback_delivery.acquire(
                CallbackAcquireCommand(
                    task_id=command.task_id,
                    business_ref=TaskBusinessRef(
                        _WEAPONRY_BUSINESS_TYPE,
                        str(command.architecture_id),
                    ),
                    trigger=trigger,
                    lease_token=token.strip(),
                    acquired_at=now,
                    lease_expires_at=add_persisted_utc_seconds(
                        now,
                        seconds=self._lease_seconds,
                    ),
                    expected_callback_attempts=command.expected_callback_attempts,
                    request_trace_id=command.request_trace_id,
                )
            )
            unit_of_work.commit()
        outcome_map = {
            CallbackAcquireOutcome.ACQUIRED: WeaponryCallbackAcquireOutcome.ACQUIRED,
            CallbackAcquireOutcome.STALE: WeaponryCallbackAcquireOutcome.STALE,
            CallbackAcquireOutcome.BUSY: WeaponryCallbackAcquireOutcome.BUSY,
            CallbackAcquireOutcome.OUTCOME_UNKNOWN: WeaponryCallbackAcquireOutcome.OUTCOME_UNKNOWN,
            CallbackAcquireOutcome.ALREADY_COMPLETED: WeaponryCallbackAcquireOutcome.ALREADY_COMPLETED,
            # 非法内部状态按 fail-closed unknown 暴露给业务用例，禁止通过异常重试 HTTP。
            CallbackAcquireOutcome.INVALID_STATE: WeaponryCallbackAcquireOutcome.OUTCOME_UNKNOWN,
        }
        business_outcome = outcome_map[result.outcome]
        if result.lease is None:
            return WeaponryCallbackAcquireResult(business_outcome)
        return WeaponryCallbackAcquireResult(
            business_outcome,
            self._to_weaponry_lease(result.lease, command.architecture_id),
        )

    def wait_until_released(
        self,
        command: WaitForWeaponryCallbackRelease,
    ) -> WeaponryCallbackWaitResult:
        if not isinstance(command, WaitForWeaponryCallbackRelease):
            raise TypeError("command 必须是 WaitForWeaponryCallbackRelease")
        deadline = self._monotonic() + command.timeout_seconds
        business_ref = TaskBusinessRef(_WEAPONRY_BUSINESS_TYPE, str(command.architecture_id))
        while True:
            with self._uow_factory() as unit_of_work:
                observed = unit_of_work.callback_delivery.observe(
                    business_ref,
                    observed_at=self._clock.now_utc(),
                )
                unit_of_work.commit()
            if observed.state is CallbackGuardState.IDLE:
                return WeaponryCallbackWaitResult(WeaponryCallbackWaitOutcome.RELEASED)
            if observed.state is CallbackGuardState.OUTCOME_UNKNOWN:
                return WeaponryCallbackWaitResult(WeaponryCallbackWaitOutcome.OUTCOME_UNKNOWN)
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return WeaponryCallbackWaitResult(WeaponryCallbackWaitOutcome.TIMED_OUT)
            self._sleeper(min(self._wait_poll_interval, remaining))

    def deliver(self, command: DeliverWeaponryCallback) -> WeaponryCallbackDeliveryResult:
        if not isinstance(command, DeliverWeaponryCallback):
            raise TypeError("command 必须是 DeliverWeaponryCallback")
        lease = self._to_control_lease(command.lease)
        with self._uow_factory() as unit_of_work:
            validation = unit_of_work.callback_delivery.validate(
                CallbackValidationCommand(lease, self._clock.now_utc())
            )
            unit_of_work.commit()
        if validation is not CallbackValidationOutcome.VALID:
            logger.warning(
                "Weaponry Callback 发送前已失权，跳过 HTTP: task_id=%s architecture_id=%s "
                "fencing_token=%d outcome=%s",
                command.lease.task_id,
                command.lease.architecture_id,
                command.lease.fencing_token,
                validation.value,
            )
            return WeaponryCallbackDeliveryResult(
                WeaponryCallbackDeliveryOutcome.STALE,
                f"guard_validation={validation.value}",
            )
        if not self._callback_url:
            return WeaponryCallbackDeliveryResult(
                WeaponryCallbackDeliveryOutcome.SKIPPED,
                "callback url is empty",
            )
        result = self._transport(command.payload.to_public_dict())
        if not isinstance(result, WeaponryCallbackDeliveryResult):
            raise TypeError("callback transport 必须返回 WeaponryCallbackDeliveryResult")
        return result

    def complete(
        self,
        lease: WeaponryCallbackGuardLease,
        result: WeaponryCallbackDeliveryResult,
        payload: WeaponryCallbackPayload,
    ) -> bool:
        if not isinstance(lease, WeaponryCallbackGuardLease):
            raise TypeError("lease 必须是 WeaponryCallbackGuardLease")
        if not isinstance(result, WeaponryCallbackDeliveryResult):
            raise TypeError("result 必须是 WeaponryCallbackDeliveryResult")
        if (
            not isinstance(payload, WeaponryCallbackPayload)
            or payload.architecture_id != lease.architecture_id
        ):
            raise ValueError("Callback payload 与 lease 不一致")
        outcome = CallbackDeliveryOutcome(result.outcome.value)
        with self._uow_factory() as unit_of_work:
            mutation = unit_of_work.callback_delivery.complete(
                CallbackCompleteCommand(
                    lease=self._to_control_lease(lease),
                    outcome=outcome,
                    detail=result.detail,
                    completed_at=self._clock.now_utc(),
                )
            )
            unit_of_work.commit()
        if mutation is not CallbackControlMutationOutcome.APPLIED:
            logger.error(
                "Weaponry Callback Guard 完成权丢失: task_id=%s architecture_id=%s "
                "fencing_token=%d outcome=%s mutation=%s",
                lease.task_id,
                lease.architecture_id,
                lease.fencing_token,
                result.outcome.value,
                mutation.value,
            )
            return False
        if result.outcome is not WeaponryCallbackDeliveryOutcome.SKIPPED:
            try:
                save_callback_history_payload(
                    payload.to_public_dict(),
                    callback_context={
                        "businessType": _WEAPONRY_BUSINESS_TYPE,
                        "architectureId": payload.architecture_id,
                    },
                )
            except Exception:
                logger.warning("保存 Weaponry Callback 诊断历史失败", exc_info=True)
        return True

    def freeze_expired(self, *, limit: int) -> WeaponryCallbackGuardSweepResult:
        with self._uow_factory() as unit_of_work:
            result = unit_of_work.callback_delivery.freeze_expired(
                CallbackGuardSweepCommand(
                    business_type=_WEAPONRY_BUSINESS_TYPE,
                    observed_at=self._clock.now_utc(),
                    limit=limit,
                )
            )
            unit_of_work.commit()
        return WeaponryCallbackGuardSweepResult(result.scanned_count, result.frozen_count)

    def release_unknown(
        self,
        command: ReleaseUnknownWeaponryCallback,
    ) -> WeaponryCallbackReleaseResult:
        if not isinstance(command, ReleaseUnknownWeaponryCallback):
            raise TypeError("command 必须是 ReleaseUnknownWeaponryCallback")
        with self._uow_factory() as unit_of_work:
            outcome = unit_of_work.callback_delivery.release_unknown(
                CallbackReleaseUnknownCommand(
                    business_ref=TaskBusinessRef(
                        _WEAPONRY_BUSINESS_TYPE,
                        str(command.architecture_id),
                    ),
                    released_by=command.released_by,
                    reason=command.reason,
                    worker_stopped_confirmed=command.worker_stopped_confirmed,
                    released_at=self._clock.now_utc(),
                )
            )
            unit_of_work.commit()
        return WeaponryCallbackReleaseResult(WeaponryCallbackReleaseOutcome(outcome.value))

    @staticmethod
    def _to_weaponry_lease(
        control: CallbackDeliveryLease,
        architecture_id: int,
    ) -> WeaponryCallbackGuardLease:
        return WeaponryCallbackGuardLease(
            task_id=control.task_id,
            architecture_id=architecture_id,
            token=control.lease_token,
            fencing_token=control.fencing_token,
            deadline_at=control.lease_expires_at,
        )

    @staticmethod
    def _to_control_lease(weaponry: WeaponryCallbackGuardLease) -> CallbackDeliveryLease:
        return CallbackDeliveryLease(
            task_id=weaponry.task_id,
            business_ref=TaskBusinessRef(
                _WEAPONRY_BUSINESS_TYPE,
                str(weaponry.architecture_id),
            ),
            lease_token=weaponry.token,
            fencing_token=weaponry.fencing_token,
            lease_expires_at=weaponry.deadline_at,
        )

    def _deliver_http(self, payload: dict[str, object]) -> WeaponryCallbackDeliveryResult:
        response = None
        try:
            response = requests.post(
                self._callback_url,
                json=payload,
                timeout=self._callback_timeout,
                stream=True,
                allow_redirects=False,
            )
        except (
            requests.exceptions.ConnectTimeout,
            requests.exceptions.InvalidSchema,
            requests.exceptions.InvalidURL,
            requests.exceptions.MissingSchema,
        ) as exc:
            logger.warning("Weaponry Callback 建连前失败: error_type=%s", type(exc).__name__)
            return WeaponryCallbackDeliveryResult(
                WeaponryCallbackDeliveryOutcome.DEFINITELY_NOT_SENT,
                type(exc).__name__,
            )
        except requests.exceptions.RequestException as exc:
            logger.warning("Weaponry Callback 发送结果未知: error_type=%s", type(exc).__name__)
            return WeaponryCallbackDeliveryResult(
                WeaponryCallbackDeliveryOutcome.DELIVERY_OUTCOME_UNKNOWN,
                type(exc).__name__,
            )
        else:
            return WeaponryCallbackDeliveryResult(
                WeaponryCallbackDeliveryOutcome.SUCCESS
                if 200 <= response.status_code < 300
                else WeaponryCallbackDeliveryOutcome.REJECTED,
                f"http_status={response.status_code}",
            )
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    logger.warning("关闭 Weaponry Callback 响应失败", exc_info=True)


__all__ = ["TaskControlWeaponryCallbackAdapter"]

