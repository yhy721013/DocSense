"""武器谱对通用本地持久任务 Dispatcher 的业务薄适配器。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import threading
import time
from typing import Any, Callable

from app.modules.tasks.adapters.local_persistent_dispatcher import (
    LocalPersistentDispatcherSettings,
    LocalPersistentMaintenanceTask,
    LocalPersistentTaskDispatcher,
)
from app.modules.tasks.domain import TaskId
from app.modules.tasks.ports import (
    ProcessSingletonGuardPort,
    TaskCommandPort,
    TaskExecutionPermitPort,
    TaskQueueInspectionPort,
)
from app.modules.weaponry.application import (
    RunWeaponryOutcome,
    RunWeaponryResult,
    WeaponryResourceRecoverySweepResult,
)
from app.modules.weaponry.ports import (
    WeaponryBoundedMaintenancePort,
    WeaponryResourceMaintenancePort,
    WeaponryTaskRunnerPort,
)

from .infrastructure_config import WeaponryInfrastructureConfig


logger = logging.getLogger(__name__)

_WEAPONRY_TASK_TYPE = "weaponry"
_RESOURCE_MAINTENANCE_NAME = "weaponry-resource-recovery"
_CALLBACK_GUARD_MAINTENANCE_NAME = "weaponry-callback-guard"
_RESOURCE_MAINTENANCE_WAKE_STATES = frozenset(
    {"cleanup_pending", "port_error", "cas_exhausted"}
)

_PROVIDER_CAPACITY_MARKERS = (
    "capacity",
    "payload_too_large",
    "rate_limit",
    "rate_limited",
)
_INPUT_CONTRACT_MARKERS = (
    "contract",
    "mismatch",
    "invalid",
    "unsupported",
    "not_installed",
    "schema",
    "profile",
    "document_scope_empty",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LocalWeaponryDispatcherSnapshot:
    """武器谱调度、维护和稳定失败分类的只读快照。"""

    lifecycle_state: str
    worker_thread_count: int
    maintenance_thread_count: int
    buffered_task_count: int
    waiting_task_id: TaskId | None
    current_task_id: TaskId | None
    dispatch_count: int
    merged_wakeup_count: int
    scan_count: int
    execution_count: int
    execution_failure_count: int
    accepted_deferral_count: int
    accepted_deferral_failure_count: int
    queue_inspection_count: int
    queue_inspection_failure_count: int
    resource_maintenance_count: int
    resource_maintenance_failure_count: int
    callback_guard_maintenance_count: int
    callback_guard_maintenance_failure_count: int
    succeeded_result_count: int
    provider_capacity_error_count: int
    business_zero_result_count: int
    input_contract_error_count: int
    other_failed_result_count: int
    ready: bool
    fatal_error: str


class LocalWeaponryTaskDispatcher:
    """一条 Weaponry Worker + 两条业务维护线程 + 一条只读诊断线程。

    资源恢复与 Callback Guard 分开注入和执行，因此长模型调用不会阻塞维护，两类维护
    也不会因其中一个变慢而互相延迟。离线组合必须显式注入严格 Fake，生产组合则注入
    1D-6 的真实维护用例；禁止用隐式 no-op 假装能力已经具备。
    """

    def __init__(
        self,
        *,
        task_commands: TaskCommandPort[Any, Any, Any],
        queue_inspector: TaskQueueInspectionPort,
        runner: WeaponryTaskRunnerPort,
        resource_maintenance: WeaponryResourceMaintenancePort,
        callback_guard_maintenance: WeaponryBoundedMaintenancePort,
        config: WeaponryInfrastructureConfig,
        execution_limiter: TaskExecutionPermitPort | None = None,
        process_guard: ProcessSingletonGuardPort | None = None,
        startup_gate: Callable[[], None] | None = None,
        monotonic=time.monotonic,
        wall_clock=_utc_now,
    ) -> None:
        if not isinstance(runner, WeaponryTaskRunnerPort):
            raise TypeError("runner 必须实现 WeaponryTaskRunnerPort")
        if not isinstance(resource_maintenance, WeaponryResourceMaintenancePort):
            raise TypeError(
                "resource_maintenance 必须实现 WeaponryResourceMaintenancePort"
            )
        if not isinstance(
            callback_guard_maintenance,
            WeaponryBoundedMaintenancePort,
        ):
            raise TypeError(
                "callback_guard_maintenance 必须实现 WeaponryBoundedMaintenancePort"
            )
        if not isinstance(config, WeaponryInfrastructureConfig):
            raise TypeError("config 必须是 WeaponryInfrastructureConfig")
        if config.runtime_mode != "single_instance":
            raise RuntimeError("本地武器谱 Dispatcher 只支持 single_instance")

        self._runner = runner
        self._resource_maintenance = resource_maintenance
        self._callback_guard_maintenance = callback_guard_maintenance
        self._config = config
        self._result_lock = threading.RLock()
        self._succeeded_result_count = 0
        self._provider_capacity_error_count = 0
        self._business_zero_result_count = 0
        self._input_contract_error_count = 0
        self._other_failed_result_count = 0

        self._kernel = LocalPersistentTaskDispatcher(
            task_commands=task_commands,
            queue_inspector=queue_inspector,
            execute=self._execute_once,
            settings=LocalPersistentDispatcherSettings(
                task_type=_WEAPONRY_TASK_TYPE,
                business_label="武器谱",
                thread_name_prefix="docsense-weaponry",
                scan_interval_seconds=config.scan_interval_seconds,
                accepted_batch_size=config.accepted_batch_size,
                dispatch_failure_retry_seconds=(
                    config.dispatch_failure_retry_seconds
                ),
                running_sample_limit=config.running_sample_limit,
                stop_timeout_seconds=config.stop_timeout_seconds,
            ),
            maintenance_tasks=(
                LocalPersistentMaintenanceTask(
                    name=_RESOURCE_MAINTENANCE_NAME,
                    thread_name="docsense-weaponry-resource-recovery",
                    interval_seconds=config.maintenance_interval_seconds,
                    execute=self._run_resource_maintenance,
                ),
                LocalPersistentMaintenanceTask(
                    name=_CALLBACK_GUARD_MAINTENANCE_NAME,
                    thread_name="docsense-weaponry-callback-guard",
                    interval_seconds=config.maintenance_interval_seconds,
                    execute=self._run_callback_guard_maintenance,
                ),
            ),
            execution_limiter=execution_limiter,
            process_guard=process_guard,
            startup_gate=startup_gate,
            event_logger=logger,
            monotonic=monotonic,
            wall_clock=wall_clock,
        )

    @property
    def has_process_guard(self) -> bool:
        return self._kernel.has_process_guard

    @property
    def task_commands(self) -> TaskCommandPort[Any, Any, Any]:
        return self._kernel.task_commands

    @property
    def execution_limiter(self) -> TaskExecutionPermitPort | None:
        return self._kernel.execution_limiter

    @property
    def runner(self) -> WeaponryTaskRunnerPort:
        return self._runner

    @property
    def resource_maintenance(self) -> WeaponryResourceMaintenancePort:
        return self._resource_maintenance

    @property
    def callback_guard_maintenance(self) -> WeaponryBoundedMaintenancePort:
        return self._callback_guard_maintenance

    def dispatch(self, task_id: TaskId) -> None:
        self._kernel.dispatch(task_id)

    def start(self) -> None:
        self._kernel.start()
        logger.info(
            "武器谱 Dispatcher 固定策略已装配: runtime_mode=%s "
            "accepted_batch_size=%d maintenance_limit=%d provider_fingerprint=%s "
            "embedding_fingerprint=%s document_processing_fingerprint=%s "
            "score_protocol=%s extraction_context_strategy=%s",
            self._config.runtime_mode,
            self._config.accepted_batch_size,
            self._config.maintenance_limit,
            self._config.provider_fingerprint,
            self._config.embedding_fingerprint,
            self._config.document_processing_fingerprint,
            self._config.score_protocol,
            self._config.extraction_context_strategy,
        )

    def stop(self, *, timeout_seconds: float | None = None) -> bool:
        return self._kernel.stop(timeout_seconds=timeout_seconds)

    def close(self) -> None:
        self._kernel.close()

    def snapshot(self) -> LocalWeaponryDispatcherSnapshot:
        common = self._kernel.snapshot()
        resource = common.maintenance_by_name(_RESOURCE_MAINTENANCE_NAME)
        callback = common.maintenance_by_name(
            _CALLBACK_GUARD_MAINTENANCE_NAME
        )
        with self._result_lock:
            return LocalWeaponryDispatcherSnapshot(
                lifecycle_state=common.lifecycle_state,
                worker_thread_count=common.worker_thread_count,
                maintenance_thread_count=common.maintenance_thread_count,
                buffered_task_count=common.buffered_task_count,
                waiting_task_id=common.waiting_task_id,
                current_task_id=common.current_task_id,
                dispatch_count=common.dispatch_count,
                merged_wakeup_count=common.merged_wakeup_count,
                scan_count=common.scan_count,
                execution_count=common.execution_count,
                execution_failure_count=common.execution_failure_count,
                accepted_deferral_count=common.accepted_deferral_count,
                accepted_deferral_failure_count=(
                    common.accepted_deferral_failure_count
                ),
                queue_inspection_count=common.queue_inspection_count,
                queue_inspection_failure_count=(
                    common.queue_inspection_failure_count
                ),
                resource_maintenance_count=resource.success_count,
                resource_maintenance_failure_count=resource.failure_count,
                callback_guard_maintenance_count=callback.success_count,
                callback_guard_maintenance_failure_count=callback.failure_count,
                succeeded_result_count=self._succeeded_result_count,
                provider_capacity_error_count=(
                    self._provider_capacity_error_count
                ),
                business_zero_result_count=self._business_zero_result_count,
                input_contract_error_count=self._input_contract_error_count,
                other_failed_result_count=self._other_failed_result_count,
                ready=common.ready,
                fatal_error=common.fatal_error,
            )

    def _execute_once(self, task_id: TaskId) -> RunWeaponryResult:
        result = self._runner.execute(task_id)
        if not isinstance(result, RunWeaponryResult):
            raise TypeError("Weaponry runner 必须返回 RunWeaponryResult")
        if result.task_id != task_id:
            raise RuntimeError("Weaponry runner 返回了其他 TaskId 的结果")
        self._wake_resource_maintenance(result)
        self._observe_result(result)
        return result

    def _wake_resource_maintenance(self, result: RunWeaponryResult) -> None:
        """业务终态提交后立即提示清理线程；提示失败不得回滚任务结果。"""

        if result.cleanup_state not in _RESOURCE_MAINTENANCE_WAKE_STATES:
            return
        woken = self._kernel.wake_maintenance(_RESOURCE_MAINTENANCE_NAME)
        logger.info(
            "武器谱终态资源清理已提示: task_id=%s cleanup_state=%s woken=%s",
            result.task_id.value,
            result.cleanup_state,
            woken,
        )

    def _observe_result(self, result: RunWeaponryResult) -> None:
        """把供应商容量、业务零结果和输入契约错误分开记录。"""

        diagnostic_codes = tuple(
            code.strip().lower()
            for code in result.diagnostic_error_codes
        )
        if result.error_code.strip():
            diagnostic_codes += (result.error_code.strip().lower(),)
        provider_codes = tuple(
            code
            for code in diagnostic_codes
            if any(marker in code for marker in _PROVIDER_CAPACITY_MARKERS)
        )
        input_contract_codes = tuple(
            code
            for code in diagnostic_codes
            if any(marker in code for marker in _INPUT_CONTRACT_MARKERS)
        )

        if result.outcome is RunWeaponryOutcome.SUCCEEDED:
            # 字段级故障按既有契约可以降级为空并成功回调，但不能因此丢失容量/协议
            # 事实，更不能把故障导致的空结果计成正常业务零结果。
            pure_business_zero = (
                result.selected_evidence_count == 0
                and not diagnostic_codes
            )
            with self._result_lock:
                self._succeeded_result_count += 1
                if provider_codes:
                    self._provider_capacity_error_count += 1
                if input_contract_codes:
                    self._input_contract_error_count += 1
                if pure_business_zero:
                    self._business_zero_result_count += 1
            if provider_codes:
                logger.warning(
                    "武器谱任务按成功契约降级，但发生供应商容量错误: "
                    "category=provider_capacity task_id=%s error_codes=%s",
                    result.task_id.value,
                    ",".join(provider_codes),
                )
            if input_contract_codes:
                logger.error(
                    "武器谱任务按成功契约降级，但发生内部输入/策略契约错误: "
                    "category=input_contract task_id=%s error_codes=%s",
                    result.task_id.value,
                    ",".join(input_contract_codes),
                )
            if pure_business_zero:
                logger.info(
                    "武器谱任务产生合法业务零结果: category=business_zero_result "
                    "task_id=%s model_call_count=%d",
                    result.task_id.value,
                    result.model_call_count,
                )
            elif diagnostic_codes and not (
                provider_codes or input_contract_codes
            ):
                logger.warning(
                    "武器谱任务按成功契约降级: category=degraded_result "
                    "task_id=%s error_codes=%s",
                    result.task_id.value,
                    ",".join(diagnostic_codes),
                )
            return
        if result.outcome is not RunWeaponryOutcome.FAILED:
            return

        if provider_codes:
            with self._result_lock:
                self._provider_capacity_error_count += 1
            logger.warning(
                "武器谱供应商容量错误: category=provider_capacity task_id=%s "
                "error_codes=%s",
                result.task_id.value,
                ",".join(provider_codes),
            )
        if input_contract_codes:
            with self._result_lock:
                self._input_contract_error_count += 1
            logger.error(
                "武器谱内部输入/策略契约错误: category=input_contract task_id=%s "
                "error_codes=%s",
                result.task_id.value,
                ",".join(input_contract_codes),
            )
        if provider_codes or input_contract_codes:
            return
        with self._result_lock:
            self._other_failed_result_count += 1
        logger.warning(
            "武器谱任务失败: category=business_or_infrastructure task_id=%s "
            "error_code=%s",
            result.task_id.value,
            result.error_code or "unknown",
        )

    def _run_resource_maintenance(self) -> object:
        result = self._resource_maintenance.run_once(
            limit=self._config.maintenance_limit,
            stop_requested=self._kernel.stop_requested,
        )
        if (
            isinstance(result, WeaponryResourceRecoverySweepResult)
            and result.cleaned_resource_count > 0
            and (
                result.pending_count > 0
                or result.scanned_count >= result.requested_limit
            )
        ):
            # 当前批次已产生可证明的清理进展且仍可能存在积压。再次设置同一常量空间
            # Event，让维护线程在批次边界检查 stop 后立即续扫；资源身份仍只在 SQLite。
            self._kernel.wake_maintenance(_RESOURCE_MAINTENANCE_NAME)
        logger.debug(
            "武器谱资源维护批次完成: limit=%d result_type=%s",
            self._config.maintenance_limit,
            type(result).__name__,
        )
        return result

    def _run_callback_guard_maintenance(self) -> object:
        result = self._callback_guard_maintenance.run_once(
            limit=self._config.maintenance_limit
        )
        logger.debug(
            "武器谱 Callback Guard 维护批次完成: limit=%d",
            self._config.maintenance_limit,
        )
        return result


__all__ = [
    "LocalWeaponryDispatcherSnapshot",
    "LocalWeaponryTaskDispatcher",
]
