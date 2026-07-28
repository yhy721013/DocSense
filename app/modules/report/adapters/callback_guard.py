"""基于兼容 SQLite 任务库的报告回调 Guard 与精确 HTTP 投递适配器。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import logging
import time
from uuid import uuid4

import requests

from app.modules.tasks.http_deadlines import required_http_lease_seconds
from app.modules.report.domain import ReportCallbackPayload
from app.modules.report.ports import (
    DeliverReportCallback,
    ReportCallbackAcquire,
    ReportCallbackAcquireOutcome,
    ReportCallbackAcquireReason,
    ReportCallbackAcquireResult,
    ReportCallbackDeliveryOutcome,
    ReportCallbackDeliveryResult,
    ReportCallbackGuardLease,
    ReportCallbackGuardSweepResult,
    ReportCallbackReleaseOutcome,
    ReportCallbackReleaseResult,
    ReportCallbackWaitOutcome,
    ReportCallbackWaitResult,
    ReleaseUnknownReportCallback,
    WaitForReportCallbackRelease,
)
from app.services.llm_service.task_service import LLMTaskService
from app.services.utils.callback_client import save_callback_history_payload


logger = logging.getLogger(__name__)

_REPORT_BUSINESS_TYPE = "report"


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


class SQLiteReportCallbackAdapter:
    """把报告 Callback Port 映射到短 SQLite 事务和事务外 HTTP 调用。

    数据库事务只负责 latest 复核、Guard CAS 和结果落库，绝不在持锁期间等待网络。HTTP
    结果采用保守分类：连接建立前的明确配置/连接超时视为 definitely-not-sent；读取超时、
    连接中断等无法证明接收方是否收到请求的异常统一冻结为 outcome-unknown。
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
        transport: Callable[[dict[str, object]], ReportCallbackDeliveryResult]
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
        command: ReportCallbackAcquire,
    ) -> ReportCallbackAcquireResult:
        if not isinstance(command, ReportCallbackAcquire):
            raise TypeError("command 必须是 ReportCallbackAcquire")
        token = self._token_factory()
        if not isinstance(token, str) or not token.strip():
            raise ValueError("token_factory 必须返回非空 str")
        raw = self._task_service.acquire_callback_delivery_guard(
            expected_execution_id=command.task_id.value,
            business_type=_REPORT_BUSINESS_TYPE,
            business_key=command.report_id.business_key,
            lease_token=token.strip(),
            lease_seconds=self._lease_seconds,
            acquired_at=_aware_clock_value(self._clock).isoformat(),
            allow_failed_retry=(
                command.reason
                is ReportCallbackAcquireReason.EXPLICIT_CHECK_TASK_RECOVERY
            ),
            allow_outcome_unknown_retry=(
                command.reason
                is ReportCallbackAcquireReason.EXPLICIT_CHECK_TASK_RECOVERY
            ),
            expected_callback_attempts=command.expected_callback_attempts,
            delivery_trigger=command.reason.value,
            request_trace_id=command.request_trace_id,
        )
        try:
            outcome = ReportCallbackAcquireOutcome(raw.get("outcome"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("callback Guard 返回未知 acquire outcome") from exc
        if outcome is not ReportCallbackAcquireOutcome.ACQUIRED:
            return ReportCallbackAcquireResult(outcome)
        lease = ReportCallbackGuardLease(
            task_id=command.task_id,
            report_id=command.report_id,
            token=str(raw.get("lease_token") or ""),
            fencing_token=raw.get("fencing_token"),  # type: ignore[arg-type]
            deadline_at=str(raw.get("deadline_at") or ""),
        )
        return ReportCallbackAcquireResult(outcome, lease)

    def wait_until_released(
        self,
        command: WaitForReportCallbackRelease,
    ) -> ReportCallbackWaitResult:
        if not isinstance(command, WaitForReportCallbackRelease):
            raise TypeError("command 必须是 WaitForReportCallbackRelease")
        wait_deadline = self._monotonic() + command.timeout_seconds
        while True:
            observed = self._task_service.observe_callback_delivery_guard(
                business_type=_REPORT_BUSINESS_TYPE,
                business_key=command.report_id.business_key,
                observed_at=_aware_clock_value(self._clock).isoformat(),
            )
            state = observed.get("state")
            if state == "idle":
                return ReportCallbackWaitResult(
                    ReportCallbackWaitOutcome.RELEASED
                )
            if state == "outcome_unknown":
                return ReportCallbackWaitResult(
                    ReportCallbackWaitOutcome.OUTCOME_UNKNOWN
                )
            if state != "sending":
                raise RuntimeError("callback Guard 返回未知 state")
            remaining = wait_deadline - self._monotonic()
            if remaining <= 0.0:
                return ReportCallbackWaitResult(
                    ReportCallbackWaitOutcome.TIMED_OUT
                )
            # 每轮 sleep 都有严格上界，且此时没有持有数据库连接或事务。
            self._sleeper(min(self._wait_poll_interval, remaining))

    def deliver(
        self,
        command: DeliverReportCallback,
    ) -> ReportCallbackDeliveryResult:
        if not isinstance(command, DeliverReportCallback):
            raise TypeError("command 必须是 DeliverReportCallback")
        # acquire 与真正 HTTP 调用之间可能发生线程暂停、租约过期、人工解除或新任务提交。
        # 因此网络调用前必须再次在一个短事务中复核 latest owner、租约 token、fencing token
        # 和截止时间。该复核缩小了发送窗口；极端的“复核后线程暂停”由人工解除命令强制要求
        # 旧 Worker 已停止/隔离这一运维前置条件封闭。
        validation = self._task_service.validate_callback_delivery_guard(
            expected_execution_id=command.lease.task_id.value,
            business_type=_REPORT_BUSINESS_TYPE,
            business_key=command.lease.report_id.business_key,
            lease_token=command.lease.token,
            fencing_token=command.lease.fencing_token,
            validated_at=_aware_clock_value(self._clock).isoformat(),
        )
        if not bool(validation.get("valid")):
            outcome = str(validation.get("outcome") or "invalid")
            logger.warning(
                "报告回调发送前租约复核未通过，跳过网络调用: "
                "task_id=%s report_id=%s outcome=%s fencing_token=%s",
                command.lease.task_id,
                command.lease.report_id.public_value,
                outcome,
                command.lease.fencing_token,
            )
            return ReportCallbackDeliveryResult(
                ReportCallbackDeliveryOutcome.STALE,
                f"guard_validation={outcome}",
            )
        if not self._callback_url:
            logger.info(
                "报告未配置回调地址，按 skipped 收敛: task_id=%s report_id=%s",
                command.lease.task_id,
                command.lease.report_id.public_value,
            )
            return ReportCallbackDeliveryResult(
                ReportCallbackDeliveryOutcome.SKIPPED,
                "callback url is empty",
            )
        result = self._transport(command.payload.to_public_dict())
        if not isinstance(result, ReportCallbackDeliveryResult):
            raise TypeError("callback transport 必须返回 ReportCallbackDeliveryResult")
        return result

    def complete(
        self,
        lease: ReportCallbackGuardLease,
        result: ReportCallbackDeliveryResult,
        payload: ReportCallbackPayload,
    ) -> bool:
        if not isinstance(lease, ReportCallbackGuardLease):
            raise TypeError("lease 必须是 ReportCallbackGuardLease")
        if not isinstance(result, ReportCallbackDeliveryResult):
            raise TypeError("result 必须是 ReportCallbackDeliveryResult")
        if not isinstance(payload, ReportCallbackPayload):
            raise TypeError("payload 必须是 ReportCallbackPayload")
        if payload.report_id != lease.report_id:
            raise ValueError("Callback payload 与 Guard Lease report_id 不一致")
        completed = self._task_service.complete_callback_delivery_guard(
            expected_execution_id=lease.task_id.value,
            business_type=_REPORT_BUSINESS_TYPE,
            business_key=lease.report_id.business_key,
            lease_token=lease.token,
            fencing_token=lease.fencing_token,
            delivery_outcome=result.outcome.value,
            detail=result.detail,
            completed_at=_aware_clock_value(self._clock).isoformat(),
        )
        if not completed:
            # HTTP 可能已经送达，但当前调用方已经失去 Guard 完成权。无 outcome/租约字段的
            # 调试历史文件不足以表达这一不确定性，写入反而会被人工误读为已提交事实；
            # 权威 Guard 及错误日志保留现场，维护扫描负责收敛过期 sending。
            logger.error(
                "报告回调 Guard 未完成，跳过非权威历史文件: task_id=%s report_id=%s "
                "outcome=%s fencing_token=%s",
                lease.task_id,
                lease.report_id.public_value,
                result.outcome.value,
                lease.fencing_token,
            )
            return False
        if result.outcome is ReportCallbackDeliveryOutcome.SKIPPED:
            # 未配置 URL 时没有发生 HTTP 投递，保持历史目录只记录实际发送尝试的旧语义。
            return True
        # 历史文件只是一份运维副本，不参与“是否已发送”的权威判断。必须先完成数据库
        # Guard，再执行可能受慢磁盘影响的写盘；保存失败也绝不能回滚或重试 HTTP。
        try:
            save_callback_history_payload(
                payload.to_public_dict(),
                callback_context={
                    "businessType": _REPORT_BUSINESS_TYPE,
                    "reportId": payload.report_id.public_value,
                },
            )
        except Exception:
            logger.warning("保存报告回调历史失败", exc_info=True)
        return completed

    def freeze_expired(self, *, limit: int) -> ReportCallbackGuardSweepResult:
        """有界冻结失联 Worker 留下的过期发送权，不允许自动重发。"""

        raw = self._task_service.freeze_expired_callback_delivery_guards(
            business_type=_REPORT_BUSINESS_TYPE,
            limit=limit,
            observed_at=_aware_clock_value(self._clock).isoformat(),
        )
        try:
            return ReportCallbackGuardSweepResult(
                scanned_count=raw.get("scanned_count"),  # type: ignore[arg-type]
                frozen_count=raw.get("frozen_count"),  # type: ignore[arg-type]
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("callback Guard sweep 返回值无效") from exc

    def release_unknown(
        self,
        command: ReleaseUnknownReportCallback,
    ) -> ReportCallbackReleaseResult:
        """执行内部人工解除；该入口只改变 Guard，不会重发或改写旧回调事实。"""

        if not isinstance(command, ReleaseUnknownReportCallback):
            raise TypeError("command 必须是 ReleaseUnknownReportCallback")
        raw_outcome = self._task_service.release_callback_delivery_guard(
            business_type=_REPORT_BUSINESS_TYPE,
            business_key=command.report_id.business_key,
            released_by=command.released_by,
            release_reason=command.reason,
            worker_stopped_confirmed=command.worker_stopped_confirmed,
            released_at=_aware_clock_value(self._clock).isoformat(),
        )
        try:
            outcome = ReportCallbackReleaseOutcome(raw_outcome)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("callback Guard 返回未知 release outcome") from exc
        return ReportCallbackReleaseResult(outcome)

    def _deliver_http(
        self,
        payload: dict[str, object],
    ) -> ReportCallbackDeliveryResult:
        response = None
        try:
            response = requests.post(
                self._callback_url,
                json=payload,
                timeout=self._callback_timeout,
                # 只消费响应状态行，不把非契约响应体的下载时间算到发送权之外。
                stream=True,
                # 与武器谱保持同一严格回调协议：3xx 本身就是拒绝，不能跟随到
                # 最终 2xx 后伪装成本次原始端点投递成功。
                allow_redirects=False,
            )
        except (
            requests.exceptions.ConnectTimeout,
            requests.exceptions.InvalidSchema,
            requests.exceptions.InvalidURL,
            requests.exceptions.MissingSchema,
        ) as exc:
            logger.warning(
                "报告回调在建立有效连接前失败: error_type=%s",
                type(exc).__name__,
            )
            result = ReportCallbackDeliveryResult(
                ReportCallbackDeliveryOutcome.DEFINITELY_NOT_SENT,
                type(exc).__name__,
            )
        except requests.exceptions.RequestException as exc:
            logger.warning(
                "报告回调发送结果无法确认: error_type=%s",
                type(exc).__name__,
            )
            result = ReportCallbackDeliveryResult(
                ReportCallbackDeliveryOutcome.DELIVERY_OUTCOME_UNKNOWN,
                type(exc).__name__,
            )
        else:
            result = ReportCallbackDeliveryResult(
                (
                    ReportCallbackDeliveryOutcome.SUCCESS
                    if 200 <= response.status_code < 300
                    else ReportCallbackDeliveryOutcome.REJECTED
                ),
                f"http_status={response.status_code}",
            )
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    # 连接清理失败不能覆盖已经完成的 HTTP outcome 分类；通过日志暴露给
                    # 运维，并让 Guard 继续按收到的响应状态完成。
                    logger.warning("关闭报告回调响应失败", exc_info=True)

        return result


__all__ = ["SQLiteReportCallbackAdapter"]
