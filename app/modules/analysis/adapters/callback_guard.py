"""Analysis 回调 Guard 与严格 HTTP 投递适配器。

该适配器复用全局 ``callback_delivery_guards`` 的 latest-wins、租约和 fencing 事实，
不再让文件分析单独维护进程内回调锁。SQLite 事务只授权/完成投递；实际 HTTP 始终在事务
外执行，发送结果不确定时冻结业务键而不是自动重试。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import logging
import time
from uuid import uuid4

import requests

from app.modules.analysis.domain.task_inputs import FrozenJsonObject
from app.modules.analysis.ports import (
    AnalysisCallbackAcquireOutcome,
    AnalysisCallbackAcquireResult,
    AnalysisCallbackDelivery,
    AnalysisCallbackDeliveryOutcome,
    AnalysisCallbackDeliveryRequest,
    AnalysisCallbackGuardLease,
    AnalysisCallbackGuardSweepResult,
    AnalysisCallbackPort,
    AnalysisCallbackRequest,
    AnalysisCallbackWaitOutcome,
    AnalysisCallbackWaitResult,
    WaitForAnalysisCallbackRelease,
)
from app.modules.tasks.http_deadlines import required_http_lease_seconds
from app.services.llm_service.task_service import LLMTaskService
from app.services.utils.callback_client import save_callback_history_payload


logger = logging.getLogger(__name__)

_ANALYSIS_BUSINESS_TYPE = "file"


def _utc_now() -> datetime:
    """返回带时区 UTC 时间，防止 Guard deadline 混入本地无时区值。"""

    return datetime.now(timezone.utc)


def _new_token() -> str:
    """生成一次性租约 token；它仅用于内部条件完成，不会进入回调正文。"""

    return uuid4().hex


def _aware_clock_value(clock: Callable[[], datetime]) -> datetime:
    """校验注入时钟，避免测试或生产配置把无时区时间写入 Guard。"""

    value = clock()
    if not isinstance(value, datetime):
        raise TypeError("callback clock 必须返回 datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("callback clock 必须返回带时区 datetime")
    return value.astimezone(timezone.utc)


class SQLiteAnalysisCallbackAdapter(AnalysisCallbackPort):
    """把 Analysis Callback Port 映射到 SQLite Guard 与事务外 HTTP。

    连接尚未建立即可明确判定的配置/连接错误属于 ``definitely_not_sent``；其余请求异常
    （尤其是响应读取超时、连接中断）无法证明接收端是否已收到请求，统一进入
    ``outcome_unknown`` 并冻结对应 fileName。
    """

    def __init__(
        self,
        task_service: LLMTaskService,
        *,
        callback_timeout: float,
        lease_seconds: float = 30.0,
        wait_poll_interval: float = 0.05,
        clock: Callable[[], datetime] = _utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        token_factory: Callable[[], str] = _new_token,
        transport: Callable[[AnalysisCallbackDeliveryRequest], AnalysisCallbackDelivery]
        | None = None,
        history_writer: Callable[..., None] = save_callback_history_payload,
    ) -> None:
        if not isinstance(task_service, LLMTaskService):
            raise TypeError("task_service 必须是 LLMTaskService")
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
            ("history_writer", history_writer),
        ):
            if not callable(dependency):
                raise TypeError(f"{name} 必须可调用")
        if transport is not None and not callable(transport):
            raise TypeError("transport 必须可调用或为 None")

        self._task_service = task_service
        self._callback_timeout = float(callback_timeout)
        self._lease_seconds = float(lease_seconds)
        required_lease = (
            self._callback_timeout
            if transport is not None
            else required_http_lease_seconds(self._callback_timeout)
        )
        if self._lease_seconds < required_lease:
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
        # 回调历史只是便于人工排查的非权威副本。显式注入点让离线测试不触碰运行环境
        # 目录，也保证历史写入失败不会影响 Guard 已完成的唯一发送事实。
        self._history_writer = history_writer

    def acquire(
        self,
        request: AnalysisCallbackRequest,
    ) -> AnalysisCallbackAcquireResult:
        """在短写事务中复核 latest owner 并获取唯一发送权。"""

        if not isinstance(request, AnalysisCallbackRequest):
            raise TypeError("request 必须是 AnalysisCallbackRequest")
        token = self._token_factory()
        if not isinstance(token, str) or not token.strip():
            raise ValueError("token_factory 必须返回非空 str")
        raw = self._task_service.acquire_callback_delivery_guard(
            expected_execution_id=request.execution.task_id.value,
            business_type=_ANALYSIS_BUSINESS_TYPE,
            business_key=request.execution.file_name,
            lease_token=token.strip(),
            lease_seconds=self._lease_seconds,
            acquired_at=_aware_clock_value(self._clock).isoformat(),
            allow_failed_retry=request.allow_failed_retry,
            allow_outcome_unknown_retry=(
                request.allow_outcome_unknown_retry
            ),
            expected_callback_attempts=request.expected_callback_attempts,
        )
        outcome = self._acquire_outcome(raw.get("outcome"))
        if outcome is not AnalysisCallbackAcquireOutcome.ACQUIRED:
            return AnalysisCallbackAcquireResult(request.execution, outcome)
        lease = AnalysisCallbackGuardLease(
            execution=request.execution,
            lease_token=str(raw.get("lease_token") or ""),
            lease_version=raw.get("fencing_token"),  # type: ignore[arg-type]
            expires_at=str(raw.get("deadline_at") or ""),
        )
        return AnalysisCallbackAcquireResult(
            request.execution,
            AnalysisCallbackAcquireOutcome.ACQUIRED,
            lease,
        )

    def wait_until_released(
        self,
        request: WaitForAnalysisCallbackRelease,
    ) -> AnalysisCallbackWaitResult:
        """在数据库事务外有限等待已有发送者，过期租约会被底层安全冻结。"""

        if not isinstance(request, WaitForAnalysisCallbackRelease):
            raise TypeError("request 必须是 WaitForAnalysisCallbackRelease")
        deadline = self._monotonic() + request.timeout_seconds
        while True:
            observed = self._task_service.observe_callback_delivery_guard(
                business_type=_ANALYSIS_BUSINESS_TYPE,
                business_key=request.execution.file_name,
                observed_at=_aware_clock_value(self._clock).isoformat(),
            )
            state = str(observed.get("state") or "")
            if state == "idle":
                return AnalysisCallbackWaitResult(
                    request.execution,
                    AnalysisCallbackWaitOutcome.RELEASED,
                )
            if state == "outcome_unknown":
                return AnalysisCallbackWaitResult(
                    request.execution,
                    AnalysisCallbackWaitOutcome.FROZEN,
                )
            if state != "sending":
                raise RuntimeError("callback Guard 返回未知等待状态")
            remaining = deadline - self._monotonic()
            if remaining <= 0.0:
                return AnalysisCallbackWaitResult(
                    request.execution,
                    AnalysisCallbackWaitOutcome.TIMED_OUT,
                )
            # 等待期间没有持有 SQLite 连接或写事务，50 个并发 check-task 不会把网络
            # 等待扩大为数据库锁竞争。
            self._sleeper(min(request.poll_seconds, remaining, self._wait_poll_interval))

    def deliver(
        self,
        request: AnalysisCallbackDeliveryRequest,
    ) -> AnalysisCallbackDelivery:
        """发送前再校验 latest/lease/fencing，校验失败时绝不触网。"""

        if not isinstance(request, AnalysisCallbackDeliveryRequest):
            raise TypeError("request 必须是 AnalysisCallbackDeliveryRequest")
        lease = request.lease
        validation = self._task_service.validate_callback_delivery_guard(
            expected_execution_id=lease.execution.task_id.value,
            business_type=_ANALYSIS_BUSINESS_TYPE,
            business_key=lease.execution.file_name,
            lease_token=lease.lease_token,
            fencing_token=lease.lease_version,
            validated_at=_aware_clock_value(self._clock).isoformat(),
        )
        if not bool(validation.get("valid")):
            outcome = str(validation.get("outcome") or "invalid")
            logger.info(
                "文件分析回调发送前Guard复核未通过，跳过网络调用: "
                "task_id=%s file_name=%s outcome=%s lease_version=%s",
                lease.execution.task_id,
                lease.execution.file_name,
                outcome,
                lease.lease_version,
            )
            return AnalysisCallbackDelivery(
                execution=lease.execution,
                lease_token=lease.lease_token,
                lease_version=lease.lease_version,
                outcome=AnalysisCallbackDeliveryOutcome.STALE,
                detail_code=f"guard_validation_{outcome}"[:256],
            )
        if not request.callback_url.strip():
            logger.info(
                "文件分析未配置回调地址，按skipped收敛: task_id=%s file_name=%s",
                lease.execution.task_id,
                lease.execution.file_name,
            )
            return AnalysisCallbackDelivery(
                execution=lease.execution,
                lease_token=lease.lease_token,
                lease_version=lease.lease_version,
                outcome=AnalysisCallbackDeliveryOutcome.SKIPPED,
                detail_code="callback_url_empty",
            )
        result = self._transport(request)
        if not isinstance(result, AnalysisCallbackDelivery):
            raise TypeError("callback transport 必须返回 AnalysisCallbackDelivery")
        if (
            result.execution != lease.execution
            or result.lease_token != lease.lease_token
            or result.lease_version != lease.lease_version
        ):
            raise RuntimeError("callback transport 返回了其他 Guard Lease 的结果")
        return result

    def complete(
        self,
        lease: AnalysisCallbackGuardLease,
        delivery: AnalysisCallbackDelivery,
        payload: FrozenJsonObject,
    ) -> bool:
        """以 token/version CAS 落库回调事实，之后才尽力保存非权威历史副本。"""

        if not isinstance(lease, AnalysisCallbackGuardLease):
            raise TypeError("lease 必须是 AnalysisCallbackGuardLease")
        if not isinstance(delivery, AnalysisCallbackDelivery):
            raise TypeError("delivery 必须是 AnalysisCallbackDelivery")
        if not isinstance(payload, FrozenJsonObject):
            raise TypeError("payload 必须是 FrozenJsonObject")
        if (
            delivery.execution != lease.execution
            or delivery.lease_token != lease.lease_token
            or delivery.lease_version != lease.lease_version
        ):
            raise ValueError("delivery 与 Guard Lease 不一致")
        if delivery.outcome is AnalysisCallbackDeliveryOutcome.STALE:
            # 发送前 Guard 已失效，不能用旧 lease 完成、也不能把它改写成普通失败；真实
            # owner 或过期维护扫描会负责后续状态收敛。
            return False
        outcome = self._storage_outcome(delivery.outcome)
        completed = self._task_service.complete_callback_delivery_guard(
            expected_execution_id=lease.execution.task_id.value,
            business_type=_ANALYSIS_BUSINESS_TYPE,
            business_key=lease.execution.file_name,
            lease_token=lease.lease_token,
            fencing_token=lease.lease_version,
            delivery_outcome=outcome,
            detail=delivery.detail_code[:256],
            completed_at=_aware_clock_value(self._clock).isoformat(),
        )
        if not completed:
            logger.error(
                "文件分析回调Guard完成CAS未命中，保留可能已发生的外部结果: "
                "task_id=%s file_name=%s outcome=%s lease_version=%s",
                lease.execution.task_id,
                lease.execution.file_name,
                delivery.outcome.value,
                lease.lease_version,
            )
            return False
        if delivery.outcome is AnalysisCallbackDeliveryOutcome.SKIPPED:
            return True
        try:
            # 该历史文件不是“是否已发送”的权威来源；必须先完成 Guard，再允许慢磁盘
            # 写入失败。保存失败绝不回滚 Guard 或触发第二次 HTTP。
            self._history_writer(
                payload.to_dict(),
                callback_context={
                    "businessType": _ANALYSIS_BUSINESS_TYPE,
                    "fileName": lease.execution.file_name,
                },
            )
        except Exception:
            logger.warning(
                "保存文件分析回调历史失败，Guard结果保持不变: task_id=%s",
                lease.execution.task_id,
                exc_info=True,
            )
        return True

    def freeze_expired(self, *, limit: int) -> AnalysisCallbackGuardSweepResult:
        """有界冻结过期 sending Guard，绝不重抢或重新发送未知请求。"""

        raw = self._task_service.freeze_expired_callback_delivery_guards(
            business_type=_ANALYSIS_BUSINESS_TYPE,
            limit=limit,
            observed_at=_aware_clock_value(self._clock).isoformat(),
        )
        try:
            return AnalysisCallbackGuardSweepResult(
                scanned_count=raw.get("scanned_count"),  # type: ignore[arg-type]
                frozen_count=raw.get("frozen_count"),  # type: ignore[arg-type]
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Analysis callback Guard扫描结果无效") from exc

    @staticmethod
    def _acquire_outcome(value: object) -> AnalysisCallbackAcquireOutcome:
        mapping = {
            "acquired": AnalysisCallbackAcquireOutcome.ACQUIRED,
            "busy": AnalysisCallbackAcquireOutcome.WAIT_FOR_OWNER,
            "already_completed": AnalysisCallbackAcquireOutcome.SKIPPED,
            "stale": AnalysisCallbackAcquireOutcome.STALE,
            "outcome_unknown": AnalysisCallbackAcquireOutcome.FROZEN,
        }
        try:
            return mapping[str(value)]
        except KeyError as exc:
            raise RuntimeError("callback Guard返回未知acquire outcome") from exc

    @staticmethod
    def _storage_outcome(outcome: AnalysisCallbackDeliveryOutcome) -> str:
        mapping = {
            AnalysisCallbackDeliveryOutcome.DELIVERED: "success",
            AnalysisCallbackDeliveryOutcome.DEFINITELY_NOT_SENT: "definitely_not_sent",
            AnalysisCallbackDeliveryOutcome.REJECTED: "rejected",
            AnalysisCallbackDeliveryOutcome.OUTCOME_UNKNOWN: "delivery_outcome_unknown",
            AnalysisCallbackDeliveryOutcome.SKIPPED: "skipped",
        }
        try:
            return mapping[outcome]
        except KeyError as exc:  # ``STALE`` 必须由 complete 前的早退处理。
            raise ValueError("stale结果不得进入callback Guard完成") from exc

    def _deliver_http(
        self,
        request: AnalysisCallbackDeliveryRequest,
    ) -> AnalysisCallbackDelivery:
        """执行一次禁止重定向的 POST，并保守归类不可判定网络结果。"""

        response = None
        lease = request.lease
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
        ) as exc:
            logger.warning(
                "文件分析回调在建立有效连接前失败: task_id=%s error_type=%s",
                lease.execution.task_id,
                type(exc).__name__,
            )
            outcome = AnalysisCallbackDeliveryOutcome.DEFINITELY_NOT_SENT
            detail = type(exc).__name__
        except requests.exceptions.RequestException as exc:
            logger.warning(
                "文件分析回调发送结果无法确认，冻结业务键: task_id=%s error_type=%s",
                lease.execution.task_id,
                type(exc).__name__,
            )
            outcome = AnalysisCallbackDeliveryOutcome.OUTCOME_UNKNOWN
            detail = type(exc).__name__
        else:
            outcome = (
                AnalysisCallbackDeliveryOutcome.DELIVERED
                if 200 <= response.status_code < 300
                else AnalysisCallbackDeliveryOutcome.REJECTED
            )
            detail = "" if outcome is AnalysisCallbackDeliveryOutcome.DELIVERED else (
                f"http_status={response.status_code}"
            )
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    logger.warning(
                        "关闭文件分析回调响应失败，HTTP结果保持不变: task_id=%s",
                        lease.execution.task_id,
                        exc_info=True,
                    )
        return AnalysisCallbackDelivery(
            execution=lease.execution,
            lease_token=lease.lease_token,
            lease_version=lease.lease_version,
            outcome=outcome,
            detail_code=detail,
        )


__all__ = ("SQLiteAnalysisCallbackAdapter",)
