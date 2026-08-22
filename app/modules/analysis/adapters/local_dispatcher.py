"""文件分析对通用持久 Dispatcher 的业务薄适配器。

本模块不保存任务队列，也不拥有模型、RAG 或 Callback HTTP 客户端。它只把 Analysis 的
毒快照收敛、事务级指数退避，以及资源/Callback Guard 维护语义注入通用 Dispatcher；
真正的任务事实始终保存在 SQLite，后续替换可靠队列或分布式 Worker 时可替换此 Adapter。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import threading
import time
from typing import Any

from app.modules.analysis.application import (
    AnalysisResourceSweepResult,
    RunAnalysisResult,
)
from app.modules.analysis.adapters.task_commands import (
    AnalysisTaskSnapshotCorruptedError,
)
from app.modules.analysis.ports import (
    AnalysisBoundedMaintenancePort,
    AnalysisCallbackGuardSweepResult,
    AnalysisDispatchFailureBackoffPort,
    AnalysisExecutionRef,
    AnalysisPoisonTaskCommandPort,
)
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
from app.modules.analysis.adapters.runtime_config import AnalysisInfrastructureConfig


logger = logging.getLogger(__name__)

_ANALYSIS_TASK_TYPE = "file"
_ANALYSIS_MAINTENANCE_NAME = "analysis-resource-and-callback-guard"
_POISON_SNAPSHOT_REASON = "analysis_task_snapshot_corrupted"


def _utc_now() -> datetime:
    """返回带时区 UTC 时间，供通用 Dispatcher 计算观测用任务年龄。"""

    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class LocalAnalysisDispatcherSnapshot:
    """文件分析 Dispatcher 的只读运行快照，不投影到公开 HTTP 接口。"""

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
    resource_sweep_count: int
    resource_sweep_failure_count: int
    callback_guard_sweep_count: int
    callback_guard_sweep_failure_count: int
    callback_guard_frozen_count: int
    poisoned_snapshot_count: int
    poisoned_snapshot_failure_count: int
    ready: bool
    fatal_error: str


class LocalAnalysisTaskDispatcher:
    """一条文件分析 Worker 与一条资源/Guard 维护线程。

    每个批次只向持久扫描器发送一次无身份唤醒；Worker 仍按 ``dispatch_sequence`` 从
    SQLite 读取新批次 execution。维护线程先冻结过期 Callback Guard，再补齐可证明的
    审计事实，绝不重放外部 RAG 清理，也绝不把 ``running`` 自动退回 ``accepted``。
    """

    def __init__(
        self,
        *,
        task_commands: TaskCommandPort[Any, Any, Any],
        queue_inspector: TaskQueueInspectionPort,
        poison_commands: AnalysisPoisonTaskCommandPort,
        dispatch_failure_backoff: AnalysisDispatchFailureBackoffPort,
        execute: Callable[[TaskId], RunAnalysisResult],
        resource_maintenance: AnalysisBoundedMaintenancePort,
        callback_guard_maintenance: AnalysisBoundedMaintenancePort,
        poison_callback_recovery: Callable[[str], bool],
        config: AnalysisInfrastructureConfig,
        execution_limiter: TaskExecutionPermitPort | None = None,
        process_guard: ProcessSingletonGuardPort | None = None,
        fatal_error_handler: Callable[[str], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not isinstance(task_commands, TaskCommandPort):
            raise TypeError("task_commands 必须实现 TaskCommandPort")
        if not isinstance(queue_inspector, TaskQueueInspectionPort):
            raise TypeError("queue_inspector 必须实现 TaskQueueInspectionPort")
        if not isinstance(poison_commands, AnalysisPoisonTaskCommandPort):
            raise TypeError("poison_commands 必须实现 AnalysisPoisonTaskCommandPort")
        if not isinstance(
            dispatch_failure_backoff,
            AnalysisDispatchFailureBackoffPort,
        ):
            raise TypeError(
                "dispatch_failure_backoff 必须实现 AnalysisDispatchFailureBackoffPort"
            )
        if not callable(execute):
            raise TypeError("execute 必须可调用")
        if not isinstance(resource_maintenance, AnalysisBoundedMaintenancePort):
            raise TypeError(
                "resource_maintenance 必须实现 AnalysisBoundedMaintenancePort"
            )
        if not isinstance(
            callback_guard_maintenance,
            AnalysisBoundedMaintenancePort,
        ):
            raise TypeError(
                "callback_guard_maintenance 必须实现 AnalysisBoundedMaintenancePort"
            )
        if not callable(poison_callback_recovery):
            raise TypeError("poison_callback_recovery 必须可调用")
        if not isinstance(config, AnalysisInfrastructureConfig):
            raise TypeError("config 必须是 AnalysisInfrastructureConfig")
        if config.runtime_mode != "single_instance":
            raise RuntimeError("本地文件分析 Dispatcher 只支持 single_instance")

        self._poison_commands = poison_commands
        self._dispatch_failure_backoff = dispatch_failure_backoff
        self._execute = execute
        self._resource_maintenance = resource_maintenance
        self._callback_guard_maintenance = callback_guard_maintenance
        self._poison_callback_recovery = poison_callback_recovery
        self._config = config
        self._business_state_lock = threading.RLock()
        self._resource_sweep_count = 0
        self._resource_sweep_failure_count = 0
        self._callback_guard_sweep_count = 0
        self._callback_guard_sweep_failure_count = 0
        self._callback_guard_frozen_count = 0
        self._poisoned_snapshot_count = 0
        self._poisoned_snapshot_failure_count = 0

        self._kernel = LocalPersistentTaskDispatcher(
            task_commands=task_commands,
            queue_inspector=queue_inspector,
            execute=self._execute_once,
            settings=LocalPersistentDispatcherSettings(
                task_type=_ANALYSIS_TASK_TYPE,
                business_label="文件分析",
                thread_name_prefix="docsense-analysis",
                scan_interval_seconds=config.scan_interval_seconds,
                accepted_batch_size=config.accepted_batch_size,
                # 实际 retry_at 由下面的事务级 handler 计算；保留 base 以维持通用
                # Dispatcher 的配置快照和无自定义 handler 时的安全默认语义。
                dispatch_failure_retry_seconds=(
                    config.dispatch_retry_base_seconds
                ),
                running_sample_limit=config.resource_sweep_batch_size,
                stop_timeout_seconds=config.stop_timeout_seconds,
                queue_inspection_interval_seconds=config.running_alert_seconds,
                running_warning_interval_seconds=config.running_alert_seconds,
            ),
            maintenance_tasks=(
                LocalPersistentMaintenanceTask(
                    name=_ANALYSIS_MAINTENANCE_NAME,
                    thread_name="docsense-analysis-resource-sweeper",
                    interval_seconds=config.resource_sweep_interval_seconds,
                    execute=self._run_analysis_maintenance_once,
                ),
            ),
            execution_limiter=execution_limiter,
            process_guard=process_guard,
            accepted_deferral_handler=self._defer_failed_accepted_with_backoff,
            fatal_error_handler=fatal_error_handler,
            event_logger=logger,
            monotonic=monotonic,
            wall_clock=wall_clock,
        )

    @property
    def has_process_guard(self) -> bool:
        """供容器在生产装配时验证跨进程单实例锁。"""

        return self._kernel.has_process_guard

    @property
    def task_commands(self) -> TaskCommandPort[Any, Any, Any]:
        """暴露只读依赖身份，便于组合根证明 Submit/Run/Worker 同源。"""

        return self._kernel.task_commands

    @property
    def execution_limiter(self) -> TaskExecutionPermitPort | None:
        """返回共享重型任务 limiter；容器据此验证 Report/Weaponry/Analysis 同一实例。"""

        return self._kernel.execution_limiter

    def wake_up(self) -> None:
        """通知持久扫描器尽快复扫，不把一批 TaskId 复制进内存。"""

        self._kernel.wake_up()

    def start(self) -> None:
        """显式启动本地线程；构造和导入本身绝不偷偷启动后台服务。"""

        self._kernel.start()
        logger.info(
            "文件分析 Dispatcher 配置确认: runtime_mode=%s accepted_batch_size=%d "
            "dispatch_retry_base_seconds=%.3f dispatch_retry_max_seconds=%.3f "
            "resource_sweep_batch_size=%d "
            "resource_close_running_grace_seconds=%.3f "
            "callback_http_timeout_seconds=%.3f "
            "callback_lease_seconds=%.3f",
            self._config.runtime_mode,
            self._config.accepted_batch_size,
            self._config.dispatch_retry_base_seconds,
            self._config.dispatch_retry_max_seconds,
            self._config.resource_sweep_batch_size,
            self._config.resource_close_running_grace_seconds,
            self._config.callback_http_timeout_seconds,
            self._config.callback_lease_seconds,
        )

    def stop(self, *, timeout_seconds: float | None = None) -> bool:
        """停止领取新任务并在有限总超时内等待本模块线程退出。"""

        return self._kernel.stop(timeout_seconds=timeout_seconds)

    def close(self) -> None:
        """幂等关闭；若执行线程未退出则保留 stopping，不伪造 closed。"""

        self._kernel.close()

    def snapshot(self) -> LocalAnalysisDispatcherSnapshot:
        """返回只读诊断快照；不查询模型、不访问网络、不改变任务状态。"""

        common = self._kernel.snapshot()
        maintenance = common.maintenance_by_name(_ANALYSIS_MAINTENANCE_NAME)
        with self._business_state_lock:
            return LocalAnalysisDispatcherSnapshot(
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
                resource_sweep_count=self._resource_sweep_count,
                resource_sweep_failure_count=self._resource_sweep_failure_count,
                callback_guard_sweep_count=self._callback_guard_sweep_count,
                callback_guard_sweep_failure_count=(
                    self._callback_guard_sweep_failure_count
                ),
                callback_guard_frozen_count=self._callback_guard_frozen_count,
                poisoned_snapshot_count=self._poisoned_snapshot_count,
                poisoned_snapshot_failure_count=(
                    self._poisoned_snapshot_failure_count
                ),
                ready=common.ready,
                fatal_error=common.fatal_error,
            )

    def _execute_once(self, task_id: TaskId) -> RunAnalysisResult | None:
        """执行一次任务，并把确定性坏快照收敛为稳定失败而非无限退避。"""

        try:
            result = self._execute(task_id)
        except AnalysisTaskSnapshotCorruptedError:
            # 只对 Codec/快照确定性损坏走专门终态。正常基础设施失败仍抛给通用内核，
            # 由其写入持久退避；不能把数据库暂时不可用误判为输入毒化。
            self._converge_poisoned_snapshot(task_id)
            return None
        if not isinstance(result, RunAnalysisResult):
            raise TypeError("Analysis runner 必须返回 RunAnalysisResult")
        if result.task_id != task_id:
            raise RuntimeError("Analysis runner 返回了其他 TaskId 的结果")
        logger.info(
            "文件分析 Worker 执行完成: task_id=%s outcome=%s stage=%s error_code=%s",
            task_id,
            result.outcome.value,
            result.stage or "-",
            result.error_code or "-",
        )
        return result

    def _converge_poisoned_snapshot(self, task_id: TaskId) -> None:
        """不解码坏 payload，按 latest owner 条件收敛新 accepted execution。"""

        try:
            execution = self._poison_commands.fail_poisoned_accepted(
                task_id,
                reason=_POISON_SNAPSHOT_REASON,
            )
            if execution is not None and not isinstance(
                execution,
                AnalysisExecutionRef,
            ):
                raise TypeError(
                    "fail_poisoned_accepted 必须返回 AnalysisExecutionRef 或 None"
                )
        except Exception:
            with self._business_state_lock:
                self._poisoned_snapshot_failure_count += 1
            logger.exception(
                "文件分析毒快照条件收敛失败，将由持久退避后再次尝试: task_id=%s",
                task_id,
            )
            # 交由通用内核写 accepted 冷却；若此时 SQLite 本身不可用，内核会记录
            # 冷却写失败并在下一扫描周期继续尝试，不能让 Worker 线程整体退出。
            raise

        if execution is not None:
            with self._business_state_lock:
                self._poisoned_snapshot_count += 1
            logger.error(
                "文件分析毒快照已收敛为稳定失败: task_id=%s reason=%s",
                task_id,
                _POISON_SNAPSHOT_REASON,
            )
            try:
                # 毒快照虽不能解码原始输入，但 Adapter 已按固定公开合同提交失败
                # payload。立即进入正常 Callback Guard 恢复链，避免 callback 长期
                # 停留 pending 并阻塞同名文件后续受理。
                replayed = self._poison_callback_recovery(execution.file_name)
                if not isinstance(replayed, bool):
                    raise TypeError("poison_callback_recovery 必须返回 bool")
                logger.info(
                    "文件分析毒快照终态已进入回调恢复链: task_id=%s "
                    "file_name=%s delivered=%s",
                    task_id,
                    execution.file_name,
                    replayed,
                )
            except Exception:
                # 任务终态已经提交，不能再次进入 accepted 退避。恢复链保留的
                # pending/failed/outcome_unknown 事实将由后续 check-task 处理。
                logger.exception(
                    "文件分析毒快照终态回调恢复失败，保留回调事实等待显式恢复: "
                    "task_id=%s file_name=%s",
                    task_id,
                    execution.file_name,
                )
        else:
            # ``None`` 是 expected CAS 结果：可能已被新 execution 替换、被其他 owner
            # 领取，或已经终态。此处只记录观测，绝不写回 accepted。
            logger.info(
                "文件分析毒快照已不具备收敛条件，跳过: task_id=%s",
                task_id,
            )

    def _defer_failed_accepted_with_backoff(
        self,
        task_id: TaskId,
        reason: str,
    ) -> bool:
        """委托 Adapter 在同一持久化事务中计算指数退避。"""

        deferred = self._dispatch_failure_backoff.defer_accepted_with_backoff(
            task_id,
            retry_base_seconds=self._config.dispatch_retry_base_seconds,
            retry_max_seconds=self._config.dispatch_retry_max_seconds,
            reason=reason,
        )
        if not isinstance(deferred, bool):
            raise TypeError("Analysis指数退避端口必须返回 bool")
        return deferred

    def _run_analysis_maintenance_once(self) -> None:
        """固定顺序执行 Guard 冻结和资源审计；其中一项失败不能阻断另一项。"""

        self._sweep_callback_guards()
        self._sweep_resources()

    def _sweep_callback_guards(self) -> None:
        """冻结过期发送权，未知 HTTP 结果保持人工恢复边界。"""

        try:
            result = self._callback_guard_maintenance.run_once(
                limit=self._config.resource_sweep_batch_size,
            )
            if not isinstance(result, AnalysisCallbackGuardSweepResult):
                raise TypeError(
                    "Analysis Callback Guard维护必须返回AnalysisCallbackGuardSweepResult"
                )
            with self._business_state_lock:
                self._callback_guard_sweep_count += 1
                self._callback_guard_frozen_count += result.frozen_count
            logger.log(
                logging.WARNING if result.frozen_count else logging.DEBUG,
                "文件分析 Callback Guard维护完成: scanned=%d frozen=%d",
                result.scanned_count,
                result.frozen_count,
            )
        except Exception:
            with self._business_state_lock:
                self._callback_guard_sweep_failure_count += 1
            logger.exception(
                "文件分析 Callback Guard维护失败，将按周期继续；禁止据此重放HTTP回调"
            )

    def _sweep_resources(self) -> None:
        """只补齐可证明幂等的审计事实，未知外部副作用继续保留现场。"""

        try:
            result = self._resource_maintenance.run_once(
                limit=self._config.resource_sweep_batch_size,
            )
            if not isinstance(result, AnalysisResourceSweepResult):
                raise TypeError(
                    "Analysis资源维护必须返回AnalysisResourceSweepResult"
                )
            with self._business_state_lock:
                self._resource_sweep_count += 1
            logger.debug(
                "文件分析资源维护完成: scanned=%d cleaned=%d deferred=%d "
                "quarantined=%d pending=%d",
                result.scanned_count,
                result.cleaned_count,
                result.deferred_count,
                result.quarantined_count,
                result.pending_count,
            )
        except Exception:
            with self._business_state_lock:
                self._resource_sweep_failure_count += 1
            logger.exception(
                "文件分析资源维护失败，将按周期继续；禁止自动重放外部RAG清理"
            )


__all__ = (
    "LocalAnalysisDispatcherSnapshot",
    "LocalAnalysisTaskDispatcher",
)
