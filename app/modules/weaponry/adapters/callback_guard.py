"""基于共享 SQLite 任务库的武器谱 Callback Guard 与精确 HTTP 投递适配器。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import logging
import time
from uuid import uuid4

import requests

from app.modules.tasks.http_deadlines import required_http_lease_seconds
from app.modules.weaponry.domain import WeaponryCallbackPayload
from app.modules.weaponry.ports import (
    AcquireWeaponryCallback,
    DeliverWeaponryCallback,
    ReleaseUnknownWeaponryCallback,
    WaitForWeaponryCallbackRelease,
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
)
from app.services.llm_service.task_service import LLMTaskService
from app.infrastructure.observability.callback_history import (
    save_callback_history_payload,
)


logger = logging.getLogger(__name__)

_WEAPONRY_BUSINESS_TYPE = "weaponry"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_token() -> str:
    return uuid4().hex


def _aware_clock_value(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if not isinstance(value, datetime):
        raise TypeError("callback clock 必须返回 datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("callback clock 必须返回带时区 datetime")
    return value.astimezone(timezone.utc)


class SQLiteWeaponryCallbackAdapter:
    """把武器谱 Callback Port 映射到短事务和事务外 HTTP 调用。

    latest 复核、Guard CAS 与 outcome 落库均由共享 Task Service 完成。网络调用前会再次
    校验 task_id、业务键、lease token、fencing token 和截止时间；3xx 不视为成功，
    无法证明甲方是否收到的网络错误冻结为 outcome-unknown，禁止自动重发。
    """

    def __init__(
        self,
        task_service: LLMTaskService,
        *,
        callback_url: str,
        callback_timeout: float,
        lease_seconds: float = 30.0,
        wait_poll_interval: float = 0.05,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        token_factory: Callable[[], str] = _new_token,
        transport: Callable[[dict[str, object]], WeaponryCallbackDeliveryResult]
        | None = None,
    ) -> None:
        if not isinstance(task_service, LLMTaskService):
            raise TypeError("task_service 必须是 LLMTaskService")
        if not isinstance(callback_url, str):
            raise TypeError("callback_url 必须是 str")
        for name, value in (
            ("callback_timeout", callback_timeout),
            ("lease_seconds", lease_seconds),
            ("wait_poll_interval", wait_poll_interval),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} 必须是数字")
            normalized = float(value)
            if (
                normalized != normalized
                or normalized in (float("inf"), float("-inf"))
                or normalized <= 0.0
            ):
                raise ValueError(f"{name} 必须是正有限数字")
        for name, dependency in (
            ("clock", clock),
            ("monotonic", monotonic),
            ("sleeper", sleeper),
            ("token_factory", token_factory),
        ):
            if not callable(dependency):
                raise TypeError(f"{name} 必须可调用")
        if transport is not None and not callable(transport):
            raise TypeError("transport 必须可调用或为 None")

        self._task_service = task_service
        self._callback_url = callback_url.strip()
        self._callback_timeout = float(callback_timeout)
        self._lease_seconds = float(lease_seconds)
        required_lease = (
            self._callback_timeout
            if transport is not None
            else required_http_lease_seconds(self._callback_timeout)
        )
        lease_too_short = (
            self._lease_seconds <= required_lease
            if transport is not None
            else self._lease_seconds < required_lease
        )
        if lease_too_short:
            raise ValueError(
                "lease_seconds 必须覆盖连接、响应头读取和安全余量，"
                f"当前至少需要 {required_lease:.3f} 秒"
            )
        self._wait_poll_interval = float(wait_poll_interval)
        self._clock = clock
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._token_factory = token_factory
        self._transport = transport or self._deliver_http

    def acquire(
        self,
        command: AcquireWeaponryCallback,
    ) -> WeaponryCallbackAcquireResult:
        if not isinstance(command, AcquireWeaponryCallback):
            raise TypeError("command 必须是 AcquireWeaponryCallback")
        token = self._token_factory()
        if not isinstance(token, str) or not token.strip():
            raise ValueError("token_factory 必须返回非空 str")
        raw = self._task_service.acquire_callback_delivery_guard(
            expected_execution_id=command.task_id.value,
            business_type=_WEAPONRY_BUSINESS_TYPE,
            business_key=str(command.architecture_id),
            lease_token=token.strip(),
            lease_seconds=self._lease_seconds,
            acquired_at=_aware_clock_value(self._clock).isoformat(),
            allow_failed_retry=(
                command.reason
                is WeaponryCallbackAcquireReason.EXPLICIT_CHECK_TASK_RECOVERY
            ),
            allow_outcome_unknown_retry=(
                command.reason
                is WeaponryCallbackAcquireReason.EXPLICIT_CHECK_TASK_RECOVERY
            ),
            expected_callback_attempts=command.expected_callback_attempts,
            delivery_trigger=command.reason.value,
            request_trace_id=command.request_trace_id,
        )
        try:
            outcome = WeaponryCallbackAcquireOutcome(raw.get("outcome"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("武器谱 Callback Guard 返回未知 acquire outcome") from exc
        if outcome is not WeaponryCallbackAcquireOutcome.ACQUIRED:
            return WeaponryCallbackAcquireResult(outcome)
        lease = WeaponryCallbackGuardLease(
            task_id=command.task_id,
            architecture_id=command.architecture_id,
            token=str(raw.get("lease_token") or ""),
            fencing_token=raw.get("fencing_token"),  # type: ignore[arg-type]
            deadline_at=str(raw.get("deadline_at") or ""),
        )
        return WeaponryCallbackAcquireResult(outcome, lease)

    def wait_until_released(
        self,
        command: WaitForWeaponryCallbackRelease,
    ) -> WeaponryCallbackWaitResult:
        if not isinstance(command, WaitForWeaponryCallbackRelease):
            raise TypeError("command 必须是 WaitForWeaponryCallbackRelease")
        wait_deadline = self._monotonic() + command.timeout_seconds
        while True:
            observed = self._task_service.observe_callback_delivery_guard(
                business_type=_WEAPONRY_BUSINESS_TYPE,
                business_key=str(command.architecture_id),
                observed_at=_aware_clock_value(self._clock).isoformat(),
            )
            state = observed.get("state")
            if state == "idle":
                return WeaponryCallbackWaitResult(
                    WeaponryCallbackWaitOutcome.RELEASED
                )
            if state == "outcome_unknown":
                return WeaponryCallbackWaitResult(
                    WeaponryCallbackWaitOutcome.OUTCOME_UNKNOWN
                )
            if state != "sending":
                raise RuntimeError("武器谱 Callback Guard 返回未知 state")
            remaining = wait_deadline - self._monotonic()
            if remaining <= 0.0:
                return WeaponryCallbackWaitResult(
                    WeaponryCallbackWaitOutcome.TIMED_OUT
                )
            self._sleeper(min(self._wait_poll_interval, remaining))

    def deliver(
        self,
        command: DeliverWeaponryCallback,
    ) -> WeaponryCallbackDeliveryResult:
        if not isinstance(command, DeliverWeaponryCallback):
            raise TypeError("command 必须是 DeliverWeaponryCallback")
        lease = command.lease
        validation = self._task_service.validate_callback_delivery_guard(
            expected_execution_id=lease.task_id.value,
            business_type=_WEAPONRY_BUSINESS_TYPE,
            business_key=str(lease.architecture_id),
            lease_token=lease.token,
            fencing_token=lease.fencing_token,
            validated_at=_aware_clock_value(self._clock).isoformat(),
        )
        if not bool(validation.get("valid")):
            outcome = str(validation.get("outcome") or "invalid")
            logger.warning(
                "武器谱回调发送前租约复核未通过，跳过网络调用: "
                "task_id=%s architecture_id=%s outcome=%s fencing_token=%s",
                lease.task_id.value,
                lease.architecture_id,
                outcome,
                lease.fencing_token,
            )
            return WeaponryCallbackDeliveryResult(
                WeaponryCallbackDeliveryOutcome.STALE,
                f"guard_validation={outcome}",
            )
        if not self._callback_url:
            logger.info(
                "武器谱未配置回调地址，按 skipped 收敛: task_id=%s architecture_id=%s",
                lease.task_id.value,
                lease.architecture_id,
            )
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
        if not isinstance(payload, WeaponryCallbackPayload):
            raise TypeError("payload 必须是 WeaponryCallbackPayload")
        if payload.architecture_id != lease.architecture_id:
            raise ValueError("Callback payload 与 Guard Lease architecture_id 不一致")
        completed = self._task_service.complete_callback_delivery_guard(
            expected_execution_id=lease.task_id.value,
            business_type=_WEAPONRY_BUSINESS_TYPE,
            business_key=str(lease.architecture_id),
            lease_token=lease.token,
            fencing_token=lease.fencing_token,
            delivery_outcome=result.outcome.value,
            detail=result.detail,
            completed_at=_aware_clock_value(self._clock).isoformat(),
        )
        if not completed:
            logger.error(
                "武器谱回调 Guard 未完成，跳过非权威历史文件: task_id=%s "
                "architecture_id=%s outcome=%s fencing_token=%s",
                lease.task_id.value,
                lease.architecture_id,
                result.outcome.value,
                lease.fencing_token,
            )
            return False
        if result.outcome is WeaponryCallbackDeliveryOutcome.SKIPPED:
            return True
        try:
            save_callback_history_payload(
                payload.to_public_dict(),
                callback_context={
                    "businessType": _WEAPONRY_BUSINESS_TYPE,
                    "architectureId": payload.architecture_id,
                },
            )
        except Exception:
            # 历史文件不参与权威 delivery 判断，保存失败绝不能触发 HTTP 重放。
            logger.warning("保存武器谱回调历史失败", exc_info=True)
        return True

    def freeze_expired(self, *, limit: int) -> WeaponryCallbackGuardSweepResult:
        raw = self._task_service.freeze_expired_callback_delivery_guards(
            business_type=_WEAPONRY_BUSINESS_TYPE,
            limit=limit,
            observed_at=_aware_clock_value(self._clock).isoformat(),
        )
        try:
            return WeaponryCallbackGuardSweepResult(
                scanned_count=raw.get("scanned_count"),  # type: ignore[arg-type]
                frozen_count=raw.get("frozen_count"),  # type: ignore[arg-type]
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("武器谱 Callback Guard sweep 返回值无效") from exc

    def release_unknown(
        self,
        command: ReleaseUnknownWeaponryCallback,
    ) -> WeaponryCallbackReleaseResult:
        if not isinstance(command, ReleaseUnknownWeaponryCallback):
            raise TypeError("command 必须是 ReleaseUnknownWeaponryCallback")
        raw_outcome = self._task_service.release_callback_delivery_guard(
            business_type=_WEAPONRY_BUSINESS_TYPE,
            business_key=str(command.architecture_id),
            released_by=command.released_by,
            release_reason=command.reason,
            worker_stopped_confirmed=command.worker_stopped_confirmed,
            released_at=_aware_clock_value(self._clock).isoformat(),
        )
        try:
            outcome = WeaponryCallbackReleaseOutcome(raw_outcome)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("武器谱 Callback Guard 返回未知 release outcome") from exc
        return WeaponryCallbackReleaseResult(outcome)

    def _deliver_http(
        self,
        payload: dict[str, object],
    ) -> WeaponryCallbackDeliveryResult:
        response = None
        try:
            response = requests.post(
                self._callback_url,
                json=payload,
                timeout=self._callback_timeout,
                # 回调契约只依赖状态码。流式模式在取得响应头后立即返回，避免甲方
                # 意外返回的大响应体继续占用 Callback Guard 租约。
                stream=True,
                # 回调契约只接受接收端对原始 URL 返回的严格 2xx。若自动跟随
                # 3xx，最终 200 会掩盖甲方端点配置错误并被误记为成功。
                allow_redirects=False,
            )
        except (
            requests.exceptions.ConnectTimeout,
            requests.exceptions.InvalidSchema,
            requests.exceptions.InvalidURL,
            requests.exceptions.MissingSchema,
        ) as exc:
            logger.warning(
                "武器谱回调在建立有效连接前失败: error_type=%s",
                type(exc).__name__,
            )
            result = WeaponryCallbackDeliveryResult(
                WeaponryCallbackDeliveryOutcome.DEFINITELY_NOT_SENT,
                type(exc).__name__,
            )
        except requests.exceptions.RequestException as exc:
            logger.warning(
                "武器谱回调发送结果无法确认: error_type=%s",
                type(exc).__name__,
            )
            result = WeaponryCallbackDeliveryResult(
                WeaponryCallbackDeliveryOutcome.DELIVERY_OUTCOME_UNKNOWN,
                type(exc).__name__,
            )
        else:
            result = WeaponryCallbackDeliveryResult(
                (
                    WeaponryCallbackDeliveryOutcome.SUCCESS
                    if 200 <= response.status_code < 300
                    else WeaponryCallbackDeliveryOutcome.REJECTED
                ),
                f"http_status={response.status_code}",
            )
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    logger.warning("关闭武器谱回调响应失败", exc_info=True)
        return result


__all__ = ["SQLiteWeaponryCallbackAdapter"]
