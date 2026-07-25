"""阶段 1C-2 报告 Application/Port 的严格可编程内存替身。

这些 Fake 不读写 SQLite、文件系统或网络。每个方法都记录全局调用顺序，并允许在明确
步骤注入异常或返回 stale；替身不会自动吞错、修正错误身份或替 Application 执行补偿。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
import hashlib
from itertools import count

from app.modules.tasks.domain import (
    TaskBusinessRef,
    TaskExecutionSnapshot,
    TaskId,
)
from app.modules.tasks.ports import (
    ExpectedProgressUpdate,
    ExpectedTaskCompletion,
    ProgressPublication,
    TaskClaimOutcome,
    TaskClaimResult,
    TaskSubmissionCommand,
    TaskSubmissionOutcome,
    TaskSubmissionResult,
    TaskQueueSnapshot,
)

from app.modules.report.application import ReportTaskCompletion
from app.modules.report.domain import (
    ReportCallbackPayload,
    ReportInputSnapshot,
    ReportResourceConcurrencyError,
    ReportResourceNotReadyError,
    ReportSubmission,
)
from app.modules.report.ports import (
    AppendReportLifecycleEvents,
    CleanupReportRag,
    DeliverReportCallback,
    PersistReportRagTrace,
    ReportArtifactCategory,
    ReportArtifactCleanupResult,
    ReportArtifactRef,
    ReportArtifactScope,
    ReportAuditReceipt,
    ReportCallbackAcquire,
    ReportCallbackAcquireOutcome,
    ReportCallbackAcquireResult,
    ReportCallbackDeliveryOutcome,
    ReportCallbackDeliveryResult,
    ReportCallbackGuardLease,
    ReportCallbackGuardSweepResult,
    ReportCallbackReleaseOutcome,
    ReportCallbackReleaseResult,
    ReportCallbackWaitOutcome,
    ReportCallbackWaitResult,
    ReportCleanupPartState,
    ReportRagAttempt,
    ReportRagCleanupRef,
    ReportRagLifecycleEvent,
    ReportRagRequest,
    ReportRagResponse,
    ReportRagSource,
    ReportRagTrace,
    ReportResourceRecord,
    ReportResourceState,
    ReportSourceDownload,
    ReportTemplateDownload,
    ReleaseUnknownReportCallback,
    WaitForReportCallbackRelease,
)


class InvocationRecorder:
    """跨 Fake 保存确定性调用顺序。"""

    def __init__(self) -> None:
        self.events: list[str] = []

    def record(self, event: str) -> None:
        if not isinstance(event, str) or not event.strip():
            raise ValueError("event 必须是非空 str")
        self.events.append(event)


class FakeReportTaskCommandPort:
    """表达追加执行、latest 条件写和领取结果的内存 Task Command Fake。"""

    def __init__(self, recorder: InvocationRecorder) -> None:
        self.recorder = recorder
        self._sequence = count(1)
        self.executions: dict[TaskId, TaskExecutionSnapshot[ReportInputSnapshot]] = {}
        self.latest: dict[TaskBusinessRef, TaskId] = {}
        self.submission_outcomes: dict[TaskBusinessRef, TaskSubmissionOutcome] = {}
        self.submission_outcome_sequence: list[TaskSubmissionOutcome] = []
        self.claim_outcomes: dict[TaskId, TaskClaimOutcome] = {}
        self.errors: dict[str, BaseException] = {}
        self.progress_results: list[object] = []
        self.finish_results: list[object] = []
        self.latest_results: list[object] = []
        self.forced_create_result: object | None = None
        self.forced_get_result: object | None = None
        self.forced_claim_result: object | None = None
        self.submission_calls: list[TaskSubmissionCommand[ReportSubmission]] = []
        self.get_calls: list[TaskId] = []
        self.claim_calls: list[TaskId] = []
        self.progress_calls: list[ExpectedProgressUpdate] = []
        self.completion_calls: list[ExpectedTaskCompletion[ReportTaskCompletion]] = []
        self.latest_calls: list[tuple[TaskId, TaskBusinessRef]] = []
        self.list_calls: list[tuple[str, int]] = []
        self.defer_calls: list[tuple[TaskId, str, str]] = []

    def create_if_allowed(
        self,
        command: TaskSubmissionCommand[ReportSubmission],
    ) -> TaskSubmissionResult[ReportInputSnapshot]:
        self.recorder.record("task.create")
        self.submission_calls.append(command)
        self._raise_if_configured("create")
        if self.forced_create_result is not None:
            return self.forced_create_result  # type: ignore[return-value]
        if not isinstance(command, TaskSubmissionCommand):
            raise TypeError("command 必须是 TaskSubmissionCommand")
        if not isinstance(command.submission, ReportSubmission):
            raise TypeError("Fake 只接受 ReportSubmission")
        outcome = (
            self.submission_outcome_sequence.pop(0)
            if self.submission_outcome_sequence
            else self.submission_outcomes.get(
                command.business_ref,
                TaskSubmissionOutcome.ACCEPTED,
            )
        )
        if outcome is not TaskSubmissionOutcome.ACCEPTED:
            return TaskSubmissionResult(outcome)

        number = next(self._sequence)
        task_id = TaskId(f"report-task-{number:04d}")
        accepted_at = f"2026-07-16T00:00:{number:02d}+08:00"
        snapshot = ReportInputSnapshot.from_submission(
            command.submission,
            task_id=task_id.value,
            accepted_at=accepted_at,
            schema_version=command.input_schema_version,
        )
        execution = TaskExecutionSnapshot(
            task_id=task_id,
            task_type=command.task_type,
            business_ref=command.business_ref,
            execution_state="accepted",
            public_status="0",
            progress=0.0,
            message="",
            input_snapshot=snapshot,
            accepted_at=accepted_at,
            trace_id=command.trace_id,
        )
        self.executions[task_id] = execution
        self.latest[command.business_ref] = task_id
        return TaskSubmissionResult(TaskSubmissionOutcome.ACCEPTED, execution)

    def get_execution(
        self,
        task_id: TaskId,
    ) -> TaskExecutionSnapshot[ReportInputSnapshot] | None:
        self.recorder.record("task.get")
        self.get_calls.append(task_id)
        self._raise_if_configured("get")
        if self.forced_get_result is not None:
            return self.forced_get_result  # type: ignore[return-value]
        return self.executions.get(task_id)

    def claim(self, task_id: TaskId) -> TaskClaimResult[ReportInputSnapshot]:
        self.recorder.record("task.claim")
        self.claim_calls.append(task_id)
        self._raise_if_configured("claim")
        if self.forced_claim_result is not None:
            return self.forced_claim_result  # type: ignore[return-value]
        execution = self.executions.get(task_id)
        if execution is None:
            return TaskClaimResult(TaskClaimOutcome.MISSING)
        outcome = self.claim_outcomes.get(task_id, TaskClaimOutcome.CLAIMED)
        if outcome is TaskClaimOutcome.CLAIMED:
            execution = replace(execution, execution_state="running")
            self.executions[task_id] = execution
        return TaskClaimResult(outcome, execution)

    def update_progress_if_current(self, update: ExpectedProgressUpdate) -> bool:
        self.recorder.record(f"task.progress:{update.progress}")
        self.progress_calls.append(update)
        self._raise_if_configured("progress")
        result = self.progress_results.pop(0) if self.progress_results else True
        if not isinstance(result, bool):
            return result  # type: ignore[return-value]
        if not result:
            execution = self.executions.get(update.expected_task_id)
            if execution is not None:
                self.executions[update.expected_task_id] = replace(
                    execution,
                    execution_state="stale",
                )
            return False
        execution = self.executions.get(update.expected_task_id)
        if execution is None or self.latest.get(update.business_ref) != update.expected_task_id:
            return False
        self.executions[update.expected_task_id] = replace(
            execution,
            progress=update.progress,
            message=update.message,
            execution_state=update.execution_state,
            public_status=update.public_status,
        )
        return True

    def finish_if_current(
        self,
        completion: ExpectedTaskCompletion[ReportTaskCompletion],
    ) -> bool:
        self.recorder.record(f"task.finish:{completion.public_status}")
        self.completion_calls.append(completion)
        self._raise_if_configured("finish")
        result = self.finish_results.pop(0) if self.finish_results else True
        if not isinstance(result, bool):
            return result  # type: ignore[return-value]
        if not result:
            execution = self.executions.get(completion.expected_task_id)
            if execution is not None:
                self.executions[completion.expected_task_id] = replace(
                    execution,
                    execution_state="stale",
                )
            return False
        execution = self.executions.get(completion.expected_task_id)
        if (
            execution is None
            or self.latest.get(completion.business_ref) != completion.expected_task_id
        ):
            return False
        self.executions[completion.expected_task_id] = replace(
            execution,
            progress=1.0,
            message=completion.message,
            execution_state=completion.execution_state,
            public_status=completion.public_status,
        )
        return True

    def is_latest(self, task_id: TaskId, business_ref: TaskBusinessRef) -> bool:
        self.recorder.record("task.is_latest")
        self.latest_calls.append((task_id, business_ref))
        self._raise_if_configured("latest")
        if self.latest_results:
            return self.latest_results.pop(0)  # type: ignore[return-value]
        return self.latest.get(business_ref) == task_id

    def list_accepted(self, task_type: str, *, limit: int) -> tuple[TaskId, ...]:
        self.recorder.record("task.list_accepted")
        self.list_calls.append((task_type, limit))
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit 必须是正整数")
        return tuple(
            execution.task_id
            for execution in self.executions.values()
            if execution.task_type == task_type
            and execution.execution_state == "accepted"
        )[:limit]

    def defer_accepted(
        self,
        task_id: TaskId,
        *,
        retry_at: str,
        reason: str,
    ) -> bool:
        self.recorder.record("task.defer_accepted")
        self.defer_calls.append((task_id, retry_at, reason))
        execution = self.executions.get(task_id)
        return execution is not None and execution.execution_state == "accepted"

    def inspect_queue(
        self,
        task_type: str,
        *,
        running_sample_limit: int,
    ) -> TaskQueueSnapshot:
        self.recorder.record("task.inspect_queue")
        matching = tuple(
            execution
            for execution in self.executions.values()
            if execution.task_type == task_type
        )
        accepted = tuple(
            item for item in matching if item.execution_state == "accepted"
        )
        running = tuple(
            item for item in matching if item.execution_state == "running"
        )
        return TaskQueueSnapshot(
            task_type=task_type,
            accepted_count=len(accepted),
            running_count=len(running),
            oldest_accepted_at=(
                min(item.accepted_at for item in accepted) if accepted else None
            ),
            oldest_running_at=(
                min(item.accepted_at for item in running) if running else None
            ),
            running_task_ids=tuple(
                item.task_id for item in running[:running_sample_limit]
            ),
        )

    def _raise_if_configured(self, step: str) -> None:
        error = self.errors.get(step)
        if error is not None:
            raise error


class FakeProgressPublisherPort:
    def __init__(self, recorder: InvocationRecorder) -> None:
        self.recorder = recorder
        self.publications: list[ProgressPublication] = []
        self.error: BaseException | None = None

    def publish(self, publication: ProgressPublication) -> None:
        self.recorder.record(f"progress.publish:{publication.progress}")
        if not isinstance(publication, ProgressPublication):
            raise TypeError("publication 必须是 ProgressPublication")
        if self.error is not None:
            raise self.error
        self.publications.append(publication)


class FakeReportDispatcherPort:
    def __init__(self, recorder: InvocationRecorder) -> None:
        self.recorder = recorder
        self.task_ids: list[TaskId] = []
        self.error: BaseException | None = None
        self.started = False
        self.closed = False
        self.start_count = 0
        self.stop_count = 0
        self.close_count = 0

    def dispatch(self, task_id: TaskId) -> None:
        self.recorder.record("dispatcher.dispatch")
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if self.error is not None:
            raise self.error
        self.task_ids.append(task_id)

    def start(self) -> None:
        if self.closed:
            raise RuntimeError("Fake Dispatcher 已关闭")
        self.start_count += 1
        self.started = True

    def stop(self, *, timeout_seconds: float | None = None) -> bool:
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("timeout_seconds 必须大于 0")
        self.stop_count += 1
        self.started = False
        return True

    def close(self) -> None:
        if self.closed:
            return
        self.close_count += 1
        self.started = False
        self.closed = True


class FakeReportArtifactPort:
    def __init__(self, recorder: InvocationRecorder) -> None:
        self.recorder = recorder
        self.begin_error: BaseException | None = None
        self.persist_error: BaseException | None = None
        self.cleanup_error: BaseException | None = None
        self.forced_scope: object | None = None
        self.forced_report_artifact: object | None = None
        self.cleanup_result: object | None = None
        self.scopes: list[ReportArtifactScope] = []
        self.persisted_html: list[str] = []
        self.cleanup_calls: list[tuple[ReportArtifactScope, tuple[ReportArtifactRef, ...]]] = []

    def begin(self, task_id: TaskId) -> ReportArtifactScope:
        self.recorder.record("artifact.begin")
        if self.begin_error is not None:
            raise self.begin_error
        if self.forced_scope is not None:
            return self.forced_scope  # type: ignore[return-value]
        scope = ReportArtifactScope(task_id, f"runtime/tasks/{task_id.value}")
        self.scopes.append(scope)
        return scope

    def persist_report_html(
        self,
        scope: ReportArtifactScope,
        html_details: str,
    ) -> ReportArtifactRef:
        self.recorder.record("artifact.persist_report")
        if self.persist_error is not None:
            raise self.persist_error
        self.persisted_html.append(html_details)
        if self.forced_report_artifact is not None:
            return self.forced_report_artifact  # type: ignore[return-value]
        return ReportArtifactRef(
            scope.task_id,
            f"{scope.task_id.value}:report.html",
            ReportArtifactCategory.REPORT_HTML,
            size_bytes=len(html_details.encode("utf-8")),
            checksum=hashlib.sha256(html_details.encode("utf-8")).hexdigest(),
        )

    def cleanup_unretained(
        self,
        scope: ReportArtifactScope,
        *,
        retain: tuple[ReportArtifactRef, ...],
    ) -> ReportArtifactCleanupResult:
        self.recorder.record("artifact.cleanup")
        self.cleanup_calls.append((scope, tuple(retain)))
        if self.cleanup_error is not None:
            raise self.cleanup_error
        if self.cleanup_result is not None:
            return self.cleanup_result  # type: ignore[return-value]
        return ReportArtifactCleanupResult()


class FakeReportFilePort:
    def __init__(self, recorder: InvocationRecorder) -> None:
        self.recorder = recorder
        self.errors: dict[str, BaseException] = {}
        self.template_text: object = "Word模板大纲"
        self.source_downloads: list[ReportSourceDownload] = []
        self.normalized: list[ReportArtifactRef] = []
        self.prepared: list[ReportArtifactRef] = []
        self.template_downloads: list[ReportTemplateDownload] = []
        self.template_artifacts: list[ReportArtifactRef] = []
        self.normalize_results: dict[str, object] = {}
        self.prepare_results: dict[str, object] = {}

    def download_source(self, command: ReportSourceDownload) -> ReportArtifactRef:
        self.recorder.record(f"file.download_source:{command.sequence_no}")
        self._raise("download_source")
        self.source_downloads.append(command)
        return ReportArtifactRef(
            command.scope.task_id,
            f"source-{command.sequence_no:04d}",
            ReportArtifactCategory.SOURCE,
            sequence_no=command.sequence_no,
        )

    def normalize_source(self, source: ReportArtifactRef) -> ReportArtifactRef:
        self.recorder.record(f"file.normalize:{source.sequence_no}")
        self._raise("normalize")
        self.normalized.append(source)
        forced = self.normalize_results.get(source.artifact_id)
        if forced is not None:
            return forced  # type: ignore[return-value]
        return ReportArtifactRef(
            source.task_id,
            f"{source.artifact_id}:normalized",
            ReportArtifactCategory.NORMALIZED_SOURCE,
            sequence_no=source.sequence_no,
        )

    def prepare_upload_files(
        self,
        source: ReportArtifactRef,
    ) -> tuple[ReportArtifactRef, ...]:
        self.recorder.record(f"file.prepare:{source.sequence_no}")
        self._raise("prepare")
        self.prepared.append(source)
        forced = self.prepare_results.get(source.artifact_id)
        if forced is not None:
            return forced  # type: ignore[return-value]
        return (
            ReportArtifactRef(
                source.task_id,
                f"{source.artifact_id}:rag",
                ReportArtifactCategory.RAG_INPUT,
                sequence_no=source.sequence_no,
            ),
        )

    def download_template(
        self,
        command: ReportTemplateDownload,
    ) -> ReportArtifactRef:
        self.recorder.record("file.download_template")
        self._raise("download_template")
        self.template_downloads.append(command)
        artifact = ReportArtifactRef(
            command.scope.task_id,
            "template",
            ReportArtifactCategory.TEMPLATE,
        )
        self.template_artifacts.append(artifact)
        return artifact

    def extract_template_text(self, template: ReportArtifactRef) -> str:
        self.recorder.record("file.extract_template")
        self._raise("extract_template")
        return self.template_text  # type: ignore[return-value]

    def _raise(self, step: str) -> None:
        error = self.errors.get(step)
        if error is not None:
            raise error


class FakeReportRagPort:
    def __init__(self, recorder: InvocationRecorder) -> None:
        self.recorder = recorder
        self.raw_content: str | None = "<section>报告内容</section>"
        self.generate_error: BaseException | None = None
        self.cleanup_error: BaseException | None = None
        self.forced_response: object | None = None
        self.requests: list[ReportRagRequest] = []
        self.cleanup_calls: list[ReportRagCleanupRef] = []
        self.cleanup_commands: list[CleanupReportRag] = []
        self.cleanup_results: list[tuple[ReportRagLifecycleEvent, ...]] = []

    def generate(self, request: ReportRagRequest) -> ReportRagResponse:
        self.recorder.record("rag.generate")
        self.requests.append(request)
        if self.generate_error is not None:
            raise self.generate_error
        if self.forced_response is not None:
            return self.forced_response  # type: ignore[return-value]
        return ReportRagResponse(
            raw_content=self.raw_content,
            trace=sample_report_trace(
                request.trace_id,
                prompt=request.prompt,
                context_name=request.context_name,
                raw_response=self.raw_content or "",
            ),
            cleanup_ref=ReportRagCleanupRef(f"cleanup:{request.task_id.value}"),
        )

    def cleanup(
        self,
        command: CleanupReportRag,
    ) -> tuple[ReportRagLifecycleEvent, ...]:
        self.recorder.record("rag.cleanup")
        if not isinstance(command, CleanupReportRag):
            raise TypeError("command 必须是 CleanupReportRag")
        self.cleanup_calls.append(command.cleanup_ref)
        self.cleanup_commands.append(command)
        if self.cleanup_error is not None:
            raise self.cleanup_error
        events = (
            self.cleanup_results.pop(0)
            if self.cleanup_results
            else (
                ReportRagLifecycleEvent(
                    sequence_no=command.sequence_start or 2,
                    operation="context_delete",
                    attempt_no=(
                        dict(command.attempt_baselines).get("context_delete", 0) + 1
                    ),
                    success=True,
                    external_ref=command.cleanup_ref.value,
                ),
            )
        )
        if command.heartbeat is not None:
            command.heartbeat()
        if command.event_checkpoint is not None:
            for event in events:
                command.event_checkpoint(event)
        return events


class FakeReportAuditPort:
    def __init__(self, recorder: InvocationRecorder) -> None:
        self.recorder = recorder
        self.persist_error: BaseException | None = None
        self.append_error: BaseException | None = None
        self.forced_receipt: object | None = None
        self.persist_calls: list[PersistReportRagTrace] = []
        self.append_calls: list[AppendReportLifecycleEvents] = []

    def persist_trace(self, command: PersistReportRagTrace) -> ReportAuditReceipt:
        self.recorder.record("audit.persist")
        self.persist_calls.append(command)
        if self.persist_error is not None:
            raise self.persist_error
        if self.forced_receipt is not None:
            return self.forced_receipt  # type: ignore[return-value]
        return ReportAuditReceipt(
            task_id=command.task_id,
            idempotency_key=command.idempotency_key,
            audit_id=f"audit:{command.task_id.value}",
        )

    def append_lifecycle_events(
        self,
        command: AppendReportLifecycleEvents,
    ) -> None:
        self.recorder.record("audit.append_cleanup")
        self.append_calls.append(command)
        if self.append_error is not None:
            raise self.append_error


class FakeReportCallbackPort:
    def __init__(self, recorder: InvocationRecorder) -> None:
        self.recorder = recorder
        self.acquire_outcome = ReportCallbackAcquireOutcome.ACQUIRED
        self.wait_outcome = ReportCallbackWaitOutcome.RELEASED
        self.delivery_result: object = ReportCallbackDeliveryResult(
            ReportCallbackDeliveryOutcome.SUCCESS
        )
        self.acquire_error: BaseException | None = None
        self.wait_error: BaseException | None = None
        self.delivery_error: BaseException | None = None
        self.complete_error: BaseException | None = None
        self.complete_result: object = True
        self.acquire_calls: list[ReportCallbackAcquire] = []
        self.wait_calls: list[WaitForReportCallbackRelease] = []
        self.delivery_calls: list[DeliverReportCallback] = []
        self.complete_calls: list[
            tuple[
                ReportCallbackGuardLease,
                ReportCallbackDeliveryResult,
                ReportCallbackPayload,
            ]
        ] = []
        self.guard_sweep_result = ReportCallbackGuardSweepResult(0, 0)
        self.guard_sweep_error: BaseException | None = None
        self.guard_sweep_calls: list[int] = []
        self.release_outcome = ReportCallbackReleaseOutcome.RELEASED
        self.release_calls: list[ReleaseUnknownReportCallback] = []

    def acquire(
        self,
        command: ReportCallbackAcquire,
    ) -> ReportCallbackAcquireResult:
        self.recorder.record("callback.acquire")
        self.acquire_calls.append(command)
        if self.acquire_error is not None:
            raise self.acquire_error
        if self.acquire_outcome is ReportCallbackAcquireOutcome.ACQUIRED:
            return ReportCallbackAcquireResult(
                self.acquire_outcome,
                ReportCallbackGuardLease(
                    task_id=command.task_id,
                    report_id=command.report_id,
                    token=f"guard:{command.task_id.value}",
                    fencing_token=1,
                    deadline_at="2026-07-16T00:01:00+00:00",
                ),
            )
        return ReportCallbackAcquireResult(self.acquire_outcome)

    def wait_until_released(
        self,
        command: WaitForReportCallbackRelease,
    ) -> ReportCallbackWaitResult:
        self.recorder.record("callback.wait")
        self.wait_calls.append(command)
        if self.wait_error is not None:
            raise self.wait_error
        return ReportCallbackWaitResult(self.wait_outcome)

    def deliver(
        self,
        command: DeliverReportCallback,
    ) -> ReportCallbackDeliveryResult:
        self.recorder.record("callback.deliver")
        self.delivery_calls.append(command)
        if self.delivery_error is not None:
            raise self.delivery_error
        return self.delivery_result  # type: ignore[return-value]

    def complete(
        self,
        lease: ReportCallbackGuardLease,
        result: ReportCallbackDeliveryResult,
        payload: ReportCallbackPayload,
    ) -> bool:
        self.recorder.record("callback.complete")
        self.complete_calls.append((lease, result, payload))
        if self.complete_error is not None:
            raise self.complete_error
        return self.complete_result  # type: ignore[return-value]

    def freeze_expired(self, *, limit: int) -> ReportCallbackGuardSweepResult:
        self.recorder.record("callback.freeze_expired")
        self.guard_sweep_calls.append(limit)
        if self.guard_sweep_error is not None:
            raise self.guard_sweep_error
        return self.guard_sweep_result

    def release_unknown(
        self,
        command: ReleaseUnknownReportCallback,
    ) -> ReportCallbackReleaseResult:
        self.recorder.record("callback.release_unknown")
        if not isinstance(command, ReleaseUnknownReportCallback):
            raise TypeError("command 必须是 ReleaseUnknownReportCallback")
        self.release_calls.append(command)
        return ReportCallbackReleaseResult(self.release_outcome)


class FakeReportResourceStorePort:
    """以 CAS version 模拟任务级资源事实，不执行清理副作用。"""

    def __init__(
        self,
        execution_loader: Callable[[TaskId], object | None] | None = None,
    ) -> None:
        self._execution_loader = execution_loader
        self.records: dict[TaskId, ReportResourceRecord] = {}
        self.errors: dict[str, BaseException] = {}
        self.defer_calls: list[tuple[TaskId, str, str]] = []

    def create(self, record: ReportResourceRecord) -> ReportResourceRecord:
        self._raise("create")
        existing = self.records.get(record.task_id)
        if existing is not None:
            if (
                existing.business_ref != record.business_ref
                or existing.scope != record.scope
            ):
                raise ValueError("资源记录幂等身份冲突")
            return existing
        created = replace(record, version=1)
        self.records[record.task_id] = created
        return created

    def get(self, task_id: TaskId) -> ReportResourceRecord | None:
        self._raise("get")
        return self.records.get(task_id)

    def save(
        self,
        record: ReportResourceRecord,
        *,
        expected_version: int,
    ) -> ReportResourceRecord:
        self._raise("save")
        current = self.records.get(record.task_id)
        if current is None or current.version != expected_version:
            raise ReportResourceConcurrencyError("资源记录 CAS 未命中")
        saved = replace(record, version=expected_version + 1)
        self.records[record.task_id] = saved
        return saved

    def prepare_cleanup(self, task_id: TaskId) -> ReportResourceRecord:
        self._raise("prepare")
        record = self.records[task_id]
        if record.state is not ReportResourceState.TRACKING:
            return record
        execution = (
            self._execution_loader(task_id)
            if self._execution_loader is not None
            else None
        )
        state = getattr(execution, "execution_state", "failed")
        if state not in {"succeeded", "failed", "stale"}:
            raise ReportResourceNotReadyError("execution 尚未形成终态")
        retained = (
            (record.final_artifact,)
            if state == "succeeded" and record.final_artifact is not None
            else ()
        )
        prepared = replace(
            record,
            state=ReportResourceState.CLEANUP_PENDING,
            external_state=(
                ReportCleanupPartState.PENDING
                if record.cleanup_ref is not None
                else ReportCleanupPartState.NOT_REQUIRED
            ),
            artifact_state=ReportCleanupPartState.PENDING,
            retained=retained,
        )
        return self.save(prepared, expected_version=record.version)

    def list_recoverable(self, *, limit: int) -> tuple[TaskId, ...]:
        self._raise("list")
        return tuple(
            task_id
            for task_id, record in self.records.items()
            if record.state
            in {
                ReportResourceState.TRACKING,
                ReportResourceState.CLEANUP_PENDING,
                ReportResourceState.AUDIT_PENDING,
            }
        )[:limit]

    def defer_recovery(
        self,
        task_id: TaskId,
        *,
        retry_at: str,
        reason: str,
    ) -> bool:
        self._raise("defer")
        self.defer_calls.append((task_id, retry_at, reason))
        record = self.records.get(task_id)
        return record is not None and record.state in {
            ReportResourceState.TRACKING,
            ReportResourceState.CLEANUP_PENDING,
            ReportResourceState.AUDIT_PENDING,
        }

    def _raise(self, operation: str) -> None:
        error = self.errors.get(operation)
        if error is not None:
            raise error


def sample_report_trace(
    trace_id: str,
    *,
    prompt: str = "prompt",
    context_name: str = "context-ref",
    raw_response: str = "报告内容",
) -> ReportRagTrace:
    """构造包含最终 attempt 和初始资源事件的最小完整测试 trace。"""

    return ReportRagTrace(
        trace_id=trace_id,
        context_name=context_name,
        context_ref="context-ref",
        conversation_ref="conversation-ref",
        final_call_id="rag-call-001",
        attempts=(
            ReportRagAttempt(
                sequence_no=1,
                operation="report_query",
                attempt_no=1,
                prompt_kind="report_generation",
                prompt_digest=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                raw_response=raw_response,
                sources=(
                    ReportRagSource(
                        document_ref="document:source-001",
                        text="来源证据",
                    ),
                ),
                call_id="rag-call-001",
            ),
        ),
        lifecycle_events=(
            ReportRagLifecycleEvent(
                sequence_no=1,
                operation="context_create",
                attempt_no=1,
                success=True,
                external_ref="context-ref",
            ),
        ),
        summary="完整测试轨迹",
    )


def sample_failed_report_trace(
    trace_id: str,
    *,
    context_name: str = "context-ref",
    failure_stage: str = "context_create",
) -> ReportRagTrace:
    """构造模型调用前失败的零 attempt 轨迹。"""

    return ReportRagTrace(
        trace_id=trace_id,
        context_name=context_name,
        context_ref=None,
        conversation_ref=None,
        attempts=(),
        lifecycle_events=(
            ReportRagLifecycleEvent(
                sequence_no=1,
                operation="context_create",
                attempt_no=1,
                success=False,
                failure_stage=failure_stage,
                error_message="context create failed",
            ),
        ),
        failure_stage=failure_stage,
        error_message="context create failed",
        summary="模型调用前失败",
    )


__all__ = [
    "FakeProgressPublisherPort",
    "FakeReportArtifactPort",
    "FakeReportAuditPort",
    "FakeReportCallbackPort",
    "FakeReportDispatcherPort",
    "FakeReportFilePort",
    "FakeReportRagPort",
    "FakeReportResourceStorePort",
    "FakeReportTaskCommandPort",
    "InvocationRecorder",
    "sample_report_trace",
    "sample_failed_report_trace",
]
