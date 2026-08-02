"""阶段 1F-7A：共享 Application 实例的 50 任务隔离验收。"""

from __future__ import annotations

import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
import hashlib
import json
from pathlib import Path
from threading import Barrier, RLock
from typing import Iterable
import unittest

from app.modules.analysis.application import RunAnalysisOutcome, RunAnalysisTask
from app.modules.analysis.domain.task_inputs import (
    AnalysisPolicySnapshot,
    AnalysisSubmissionSnapshot,
    AnalysisTaskInputV1,
)
from app.modules.analysis.ports import (
    AnalysisCallbackAcquireOutcome,
    AnalysisCallbackAcquireResult,
    AnalysisCallbackDelivery,
    AnalysisCallbackDeliveryOutcome,
    AnalysisCallbackGuardLease,
    AnalysisExecutionRef,
    AnalysisInteractionAuditReceipt,
    AnalysisKnowledgeWriteOutcome,
    AnalysisKnowledgeWriteResult,
    AnalysisRagCloseOutcome,
    AnalysisRagCloseResult,
    AnalysisRagLifecycleEvent,
    AnalysisRagLifecycleOutcome,
    AnalysisRagOperation,
    AnalysisRagResult,
    AnalysisRagSessionOpenResult,
    AnalysisRagSessionRef,
    AnalysisRecallAuditReceipt,
    AnalysisResourceCommand,
    AnalysisResourceRecord,
    AnalysisResourceScanBatch,
    AnalysisResourceState,
    AnalysisTaskWorkspace,
    AnalysisTranslationKind,
    AnalysisTranslationOutcome,
    AnalysisTranslationResult,
    PreparedAnalysisDocument,
)
from app.modules.tasks.domain import TaskBusinessRef, TaskExecutionSnapshot, TaskId
from app.modules.tasks.ports import TaskClaimOutcome, TaskClaimResult
from tests.fakes.analysis import (
    StrictAnalysisFakeScript,
    StrictAnalysisGuardedProgressFake,
    StrictAnalysisPortFake,
    StrictAnalysisRagFactoryFake,
    StrictAnalysisTaskCommandFake,
    StrictAnalysisTaskWorkspaceFake,
)


_TASK_COUNT = 50
_ANALYSIS_MODULE_ROOT = Path(__file__).resolve().parents[1] / "app" / "modules" / "analysis"


@dataclass(frozen=True)
class _ParallelAnalysisCase:
    """一个并发样本的全部任务级身份，禁止在测试中使用共享默认值。"""

    index: int
    task_input: AnalysisTaskInputV1
    task_execution: TaskExecutionSnapshot[AnalysisTaskInputV1]
    execution: AnalysisExecutionRef
    workspace: AnalysisTaskWorkspace
    prepared: PreparedAnalysisDocument
    bound_session: AnalysisRagSessionRef


class _ThreadSafeAnalysisResourceStore:
    """供 50 并发验收使用的内存 CAS Store，不触发真实数据库或远端清理。

    Store 自身按 ``TaskId`` 保存不可变记录，并严格校验 ``state + version``。若
    ``RunAnalysisTask`` 意外复用了另一任务的资源局部状态，此处会立即报错，而不是将
    错误静默覆盖为“最后一个任务”的资源记录。
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._records: dict[str, AnalysisResourceRecord] = {}
        self.operation_count_by_task: dict[str, int] = {}

    @property
    def records(self) -> dict[str, AnalysisResourceRecord]:
        """返回浅拷贝，避免断言侧意外修改 Store 的内部索引。"""

        with self._lock:
            return dict(self._records)

    def create(self, command: AnalysisResourceCommand) -> AnalysisResourceRecord:
        """以仅允许首次 ``tracking`` 的语义模拟资源事实登记。"""

        task_key = str(command.execution.task_id)
        with self._lock:
            if (
                command.expected_state is not None
                or command.expected_version is not None
                or command.target_state is not AnalysisResourceState.TRACKING
                or task_key in self._records
            ):
                raise AssertionError("并发资源登记未遵守 execution 专属首次 CAS 语义")
            record = AnalysisResourceRecord(
                execution=command.execution,
                state=command.target_state,
                version=0,
                record_payload=command.record_payload,
            )
            self._records[task_key] = record
            self.operation_count_by_task[task_key] = 1
            return record

    def get(self, execution: AnalysisExecutionRef) -> AnalysisResourceRecord | None:
        """按 execution 身份读取，绝不按文件名或全局最近记录回退。"""

        with self._lock:
            return self._records.get(str(execution.task_id))

    def advance(self, command: AnalysisResourceCommand) -> AnalysisResourceRecord:
        """模拟原子 CAS 推进，状态或版本不匹配即拒绝写入。"""

        task_key = str(command.execution.task_id)
        with self._lock:
            current = self._records.get(task_key)
            if (
                current is None
                or current.execution != command.execution
                or current.state is not command.expected_state
                or current.version != command.expected_version
            ):
                raise AssertionError("并发资源推进发生跨任务覆盖或过期 CAS")
            record = AnalysisResourceRecord(
                execution=command.execution,
                state=command.target_state,
                version=current.version + 1,
                record_payload=command.record_payload,
            )
            self._records[task_key] = record
            self.operation_count_by_task[task_key] += 1
            return record

    def list_recoverable(self, *, limit: int) -> AnalysisResourceScanBatch:
        """本验收不启动恢复器；返回空集合避免把正常并发路径伪装成恢复测试。"""

        if not isinstance(limit, int) or limit < 1:
            raise ValueError("limit 必须是正整数")
        return AnalysisResourceScanBatch(())

    def defer_recovery(
        self,
        execution: AnalysisExecutionRef,
        *,
        expected_version: int,
        retry_at: str,
        reason: str,
    ) -> AnalysisResourceRecord:
        """正常成功路径不应触发恢复延迟；发生时直接暴露为测试失败。"""

        raise AssertionError(
            "50 任务正常隔离验收不应触发资源恢复延迟: "
            f"task_id={execution.task_id} version={expected_version}"
        )

    def quarantine_recovery_record(
        self,
        execution: AnalysisExecutionRef,
        *,
        expected_state: AnalysisResourceState,
        expected_version: int,
        reason: str,
    ) -> bool:
        """正常成功路径不应触发毒记录隔离。"""

        raise AssertionError(
            "50 任务正常隔离验收不应触发资源隔离: "
            f"task_id={execution.task_id} state={expected_state.value} "
            f"version={expected_version} reason={reason}"
        )


def _case(index: int) -> _ParallelAnalysisCase:
    """构造一条数据、目录、RAG 引用和翻译结果均唯一的任务样本。"""

    task_id = TaskId(f"analysis-stage1f7a-{index:02d}")
    file_name = f"stage1f7a-{index:02d}.txt"
    raw_params = {
        "fileName": file_name,
        "filePath": f"https://example.invalid/stage1f7a/{index:02d}.txt",
        "architectureList": [
            {
                "id": 103,
                "name": "装备型号",
                "parentId": None,
                "path": "103",
                "pathName": "装备型号",
                "remark": "并发隔离验收专用节点",
            }
        ],
    }
    submission = AnalysisSubmissionSnapshot.from_request_params(
        raw_params,
        policy_snapshot=AnalysisPolicySnapshot.default(),
    )
    task_input = AnalysisTaskInputV1.from_submission(
        submission,
        task_id=task_id.value,
        batch_id=f"{index:032x}",
        batch_sequence=1,
        accepted_at="2026-07-27T09:00:00+08:00",
        trace_id=f"analysis-stage1f7a-trace-{index:02d}",
    )
    task_execution = TaskExecutionSnapshot(
        task_id=task_id,
        task_type="file",
        business_ref=TaskBusinessRef("file", file_name),
        execution_state="accepted",
        public_status="0",
        progress=0.0,
        message="",
        input_snapshot=task_input,
        accepted_at=task_input.accepted_at,
        trace_id=task_input.trace_id,
    )
    execution = AnalysisExecutionRef(
        task_id=task_id,
        file_name=file_name,
        batch_id=task_input.batch_id,
        batch_sequence=task_input.batch_sequence,
    )
    task_root = f"C:/analysis-stage1f7a/{task_id.value}"
    prepared = PreparedAnalysisDocument(
        execution=execution,
        source_path=f"{task_root}/source-{index:02d}.txt",
        upload_path=f"{task_root}/upload-{index:02d}.txt",
        original_text=f"阶段 1F-7A 并发任务 {index:02d} 的独立文档正文",
    )
    pending = AnalysisRagSessionRef(
        execution=execution,
        session_ref=f"context:{task_id.value}::conversation:{task_id.value}",
        context_ref=f"context:{task_id.value}",
        conversation_ref=f"conversation:{task_id.value}",
    )
    bound_session = pending.with_bound_document(
        document_ref=f"document:{task_id.value}",
        document_location=f"location:{task_id.value}",
        content_sha256=hashlib.sha256(task_id.value.encode("utf-8")).hexdigest(),
        ingested_file_name=f"{task_id.value}.txt",
        structured_source_key=(
            "docsense_ref:"
            + hashlib.sha256(task_id.value.encode("utf-8")).hexdigest()[:32]
        ),
    )
    return _ParallelAnalysisCase(
        index=index,
        task_input=task_input,
        task_execution=task_execution,
        execution=execution,
        workspace=AnalysisTaskWorkspace(execution, task_root),
        prepared=prepared,
        bound_session=bound_session,
    )


def _expect_progress(script: StrictAnalysisFakeScript, task_key: str) -> None:
    """注册一次“条件写 -> Guarded Progress -> latest 复核”的任务级顺序。"""

    script.expect_for(task_key, "task.progress", True)
    script.expect_for(task_key, "progress.publish", True)
    script.expect_for(task_key, "task.is_latest", True)


def _expect_happy_path(
    script: StrictAnalysisFakeScript,
    case: _ParallelAnalysisCase,
) -> None:
    """为一条样本配置完整成功路径，所有期望均按 TaskId 隔离。"""

    task_key = str(case.execution.task_id)
    running = replace(case.task_execution, execution_state="running")
    pending = AnalysisRagSessionRef(
        execution=case.execution,
        session_ref=(
            f"context:{case.execution.task_id}::conversation:{case.execution.task_id}"
        ),
        context_ref=f"context:{case.execution.task_id}",
        conversation_ref=f"conversation:{case.execution.task_id}",
    )
    opened = AnalysisRagSessionOpenResult(
        session=pending,
        lifecycle_events=(
            AnalysisRagLifecycleEvent(
                sequence_no=1,
                operation="context_create",
                attempt_number=1,
                outcome=AnalysisRagLifecycleOutcome.SUCCEEDED,
                external_ref=pending.context_ref,
            ),
            AnalysisRagLifecycleEvent(
                sequence_no=2,
                operation="conversation_create",
                attempt_number=1,
                outcome=AnalysisRagLifecycleOutcome.SUCCEEDED,
                external_ref=pending.conversation_ref,
            ),
        ),
    )
    rag_result = AnalysisRagResult(
        execution=case.execution,
        session=case.bound_session,
        operation=AnalysisRagOperation.EXTRACTION,
        attempt_number=1,
        answer=json.dumps(
            {
                "architectureId": 103,
                "fileDataItem": {
                    "summary": f"任务摘要-{case.index:02d}",
                    "keyword": f"任务关键词-{case.index:02d}",
                },
            },
            ensure_ascii=False,
        ),
        lifecycle_events=(
            AnalysisRagLifecycleEvent(
                sequence_no=3,
                operation="document_upload",
                attempt_number=1,
                outcome=AnalysisRagLifecycleOutcome.SUCCEEDED,
                external_ref=case.bound_session.document_location,
            ),
            AnalysisRagLifecycleEvent(
                sequence_no=4,
                operation="document_bind",
                attempt_number=1,
                outcome=AnalysisRagLifecycleOutcome.SUCCEEDED,
                external_ref=case.bound_session.document_ref,
            ),
        ),
    )
    recall_receipt = AnalysisRecallAuditReceipt(
        execution=case.execution,
        idempotency_key=f"analysis-recall:{case.execution.task_id}",
        audit_id=f"recall:{case.execution.task_id}",
        version=0,
    )
    interaction_receipt = AnalysisInteractionAuditReceipt(
        execution=case.execution,
        idempotency_key=f"analysis-rag:{case.execution.task_id}",
        audit_id=f"interaction:{case.execution.task_id}",
    )
    knowledge_key = RunAnalysisTask._knowledge_idempotency_key(
        file_name=case.task_input.file_name,
        architecture_id=103,
        content_sha256=case.bound_session.content_sha256,
    )
    callback_lease = AnalysisCallbackGuardLease(
        execution=case.execution,
        lease_token=f"callback-lease:{case.execution.task_id}",
        lease_version=1,
        expires_at="2030-01-01T00:00:30+00:00",
    )
    close = AnalysisRagCloseResult(
        execution=case.execution,
        session=case.bound_session,
        outcome=AnalysisRagCloseOutcome.CONFIRMED,
        lifecycle_events=(
            AnalysisRagLifecycleEvent(
                sequence_no=5,
                operation="context_delete",
                attempt_number=1,
                outcome=AnalysisRagLifecycleOutcome.SUCCEEDED,
                external_ref=case.bound_session.context_ref,
            ),
        ),
    )

    script.expect_for(task_key, "task.get", case.task_execution)
    script.expect_for(
        task_key,
        "task.claim",
        TaskClaimResult(TaskClaimOutcome.CLAIMED, running),
    )
    _expect_progress(script, task_key)
    script.expect_for(task_key, "workspace.create", case.workspace)
    script.expect_for(task_key, "file.prepare", case.prepared)
    _expect_progress(script, task_key)
    script.expect_for(task_key, "audit.reserve_recall", recall_receipt)
    script.expect_for(task_key, "task.is_latest", True)
    script.expect_for(task_key, "rag.factory.create", None)
    script.expect_for(task_key, "rag.open_session", opened)
    script.expect_for(task_key, "rag.execute", rag_result)
    script.expect_for(
        task_key,
        "audit.finalize_recall",
        AnalysisRecallAuditReceipt(
            execution=case.execution,
            idempotency_key=recall_receipt.idempotency_key,
            audit_id=recall_receipt.audit_id,
            version=1,
            finalized=True,
        ),
    )
    script.expect_for(task_key, "audit.persist_interaction", interaction_receipt)
    script.expect_for(task_key, "task.is_latest", True)
    script.expect_for(
        task_key,
        "knowledge.persist",
        AnalysisKnowledgeWriteResult(
            execution=case.execution,
            idempotency_key=knowledge_key,
            outcome=AnalysisKnowledgeWriteOutcome.COMMITTED,
            external_ref=f"knowledge:{case.execution.task_id}",
        ),
    )
    _expect_progress(script, task_key)
    script.expect_for(
        task_key,
        "translation.translate",
        AnalysisTranslationResult(
            execution=case.execution,
            kind=AnalysisTranslationKind.DOCUMENT,
            outcome=AnalysisTranslationOutcome.SUCCEEDED,
            document_translation_one=f"单语翻译-{case.index:02d}",
            document_translation_two=f"双语翻译-{case.index:02d}",
        ),
    )
    _expect_progress(script, task_key)
    script.expect_for(task_key, "task.finish", True)
    script.expect_for(task_key, "progress.publish", True)
    script.expect_for(task_key, "task.is_latest", True)
    script.expect_for(
        task_key,
        "callback.acquire",
        AnalysisCallbackAcquireResult(
            execution=case.execution,
            outcome=AnalysisCallbackAcquireOutcome.ACQUIRED,
            lease=callback_lease,
        ),
    )
    script.expect_for(
        task_key,
        "callback.deliver",
        AnalysisCallbackDelivery(
            execution=case.execution,
            lease_token=callback_lease.lease_token,
            lease_version=callback_lease.lease_version,
            outcome=AnalysisCallbackDeliveryOutcome.DELIVERED,
        ),
    )
    script.expect_for(task_key, "callback.complete", True)
    script.expect_for(task_key, "rag.close_session", close)
    script.expect_for(task_key, "audit.append_lifecycle_events", None)


class AnalysisStage1F7AIsolationTests(unittest.TestCase):
    """验证共享用例实例也不会让任务级状态跨线程、跨任务泄漏。"""

    def _build_application(
        self,
        script: StrictAnalysisFakeScript,
        resources: _ThreadSafeAnalysisResourceStore,
    ) -> RunAnalysisTask:
        """复用同一组严格 Fake，故意让跨任务串值无处隐藏。"""

        ports = StrictAnalysisPortFake(script)
        return RunAnalysisTask(
            task_commands=StrictAnalysisTaskCommandFake(script),
            progress_publisher=StrictAnalysisGuardedProgressFake(script),
            workspaces=StrictAnalysisTaskWorkspaceFake(script),
            files=ports,
            rag_factory=StrictAnalysisRagFactoryFake(script, ports),
            knowledge=ports,
            audit=ports,
            translation=ports,
            resources=resources,
            callbacks=ports,
            callback_url="https://callback.invalid/analysis-stage1f7a",
        )

    def test_shared_application_isolates_fifty_task_inputs_resources_and_callbacks(self) -> None:
        """50 个不同文件同时执行时，输入、目录、Progress、RAG、翻译、回调和资源均不串值。"""

        cases = tuple(_case(index) for index in range(1, _TASK_COUNT + 1))
        script = StrictAnalysisFakeScript()
        for case in cases:
            _expect_happy_path(script, case)
        resources = _ThreadSafeAnalysisResourceStore()
        application = self._build_application(script, resources)
        start_barrier = Barrier(_TASK_COUNT)

        def run_case(case: _ParallelAnalysisCase):
            """使全部工作线程先就绪，再竞争同一个无状态 Application 实例。"""

            start_barrier.wait(timeout=15.0)
            return application.execute(case.execution.task_id)

        with ThreadPoolExecutor(max_workers=_TASK_COUNT) as executor:
            futures = [executor.submit(run_case, case) for case in cases]
            results = [future.result(timeout=45.0) for future in futures]

        self.assertEqual(
            {RunAnalysisOutcome.SUCCEEDED},
            {result.outcome for result in results},
        )
        self.assertEqual(
            {case.execution.task_id for case in cases},
            {result.task_id for result in results},
        )
        script.assert_exhausted()

        # 目录和文件准备请求必须一一对应：同名“最近任务目录”或全局上传路径会在此暴露。
        workspace_calls = [
            argument
            for operation, argument in script.calls
            if operation == "workspace.create"
        ]
        file_calls = [
            argument
            for operation, argument in script.calls
            if operation == "file.prepare"
        ]
        self.assertEqual(_TASK_COUNT, len(workspace_calls))
        self.assertEqual(_TASK_COUNT, len(file_calls))
        self.assertEqual(
            {case.execution.task_id for case in cases},
            {execution.task_id for execution in workspace_calls},
        )
        self.assertEqual(
            {case.workspace.root_path for case in cases},
            {request.task_root for request in file_calls},
        )

        # 严格脚本已经按 TaskId 消费了 RAG、翻译和 Callback 结果；以下再从真实调用
        # 参数确认每类副作用覆盖 50 个不同 execution，且没有任何任务遗漏。
        def execution_ids(operation: str) -> set[TaskId]:
            return {
                argument.execution.task_id
                for called_operation, argument in script.calls
                if called_operation == operation
            }

        expected_task_ids = {case.execution.task_id for case in cases}
        self.assertEqual(expected_task_ids, execution_ids("rag.open_session"))
        self.assertEqual(expected_task_ids, execution_ids("rag.execute"))
        self.assertEqual(expected_task_ids, execution_ids("translation.translate"))
        self.assertEqual(expected_task_ids, execution_ids("callback.acquire"))
        self.assertEqual(expected_task_ids, execution_ids("rag.close_session"))
        progress_calls = [
            argument
            for operation, argument in script.calls
            if operation == "progress.publish"
        ]
        self.assertEqual(_TASK_COUNT * 5, len(progress_calls))
        self.assertEqual(
            expected_task_ids,
            {publication.expected_task_id for publication in progress_calls},
        )

        records = resources.records
        self.assertEqual(expected_task_ids, {record.execution.task_id for record in records.values()})
        self.assertEqual(_TASK_COUNT, len(records))
        self.assertTrue(all(count >= 8 for count in resources.operation_count_by_task.values()))
        for case in cases:
            record = records[str(case.execution.task_id)]
            payload = record.record_payload.to_dict()
            self.assertEqual(case.execution, record.execution)
            self.assertIs(AnalysisResourceState.CLEANED, record.state)
            self.assertEqual(case.workspace.root_path, payload["local"]["task_root"])
            self.assertEqual(case.bound_session.context_ref, payload["rag"]["context_ref"])
            self.assertEqual("permanent", payload["ownership"]["document"])
            self.assertEqual("confirmed", payload["cleanup"]["session_close"]["state"])
            self.assertEqual("confirmed", payload["cleanup"]["audit_append"]["state"])

    def test_new_analysis_module_does_not_import_legacy_analysis_worker(self) -> None:
        """新 Application 目录不得重新依赖旧 analysis_service 或其 Worker 入口。"""

        legacy_modules = {
            "app.services.llm_service.analysis_service",
            "app.services.llm_service.worker",
        }
        legacy_imports: list[str] = []
        for source_path in _ANALYSIS_MODULE_ROOT.rglob("*.py"):
            tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_names: Iterable[str] = (alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imported_names = (node.module or "",)
                else:
                    continue
                for imported_name in imported_names:
                    if imported_name in legacy_modules:
                        legacy_imports.append(f"{source_path}: {imported_name}")
        self.assertEqual([], legacy_imports)


if __name__ == "__main__":  # pragma: no cover - 允许离线单文件执行。
    unittest.main()
