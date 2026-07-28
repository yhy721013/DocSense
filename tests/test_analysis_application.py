"""阶段 1F-3：RunAnalysisTask 的严格编排与故障收敛测试。"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import hashlib
import inspect
import json
from pathlib import Path
from typing import Iterator
import unittest

from app.modules.analysis.application import RunAnalysisOutcome, RunAnalysisTask
from app.modules.analysis.application import run_analysis as run_analysis_module
from app.modules.analysis.application.model_workflow import _AnalysisModelWorkflow
from app.modules.analysis.application.knowledge_handoff import _AnalysisKnowledgeHandoff
from app.modules.analysis.domain.architecture_recall import (
    ArchitecturePromptBudgetError,
    ArchitectureRecallCandidate,
    ArchitectureRecallDecision,
    RecallChannelRanking,
)
from app.modules.analysis.domain.task_inputs import (
    AnalysisPolicySnapshot,
    AnalysisSubmissionSnapshot,
    AnalysisTaskInputV1,
)
from app.modules.analysis.ports import (
    AnalysisAuditOutcome,
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
    AnalysisInteractionAuditReceipt,
    AnalysisKnowledgeWriteOutcome,
    AnalysisKnowledgeWriteResult,
    AnalysisRagCloseOutcome,
    AnalysisRagCloseResult,
    AnalysisRagLifecycleEvent,
    AnalysisRagLifecycleOutcome,
    AnalysisRagOperation,
    AnalysisRagResult,
    AnalysisRagSessionOpenError,
    AnalysisRagSessionOpenStage,
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
    WaitForAnalysisCallbackRelease,
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


class _ExitFailingRagFactory:
    """模拟 Transport 释放异常，验证已提交终态不会被 Factory 的 ``__exit__`` 改写。"""

    def __init__(self, script: StrictAnalysisFakeScript) -> None:
        self._script = script
        self._rag = StrictAnalysisPortFake(script)

    @contextmanager
    def create(self, execution) -> Iterator[StrictAnalysisPortFake]:  # type: ignore[no-untyped-def]
        self._script.invoke(
            "rag.factory.create",
            execution,
            correlation_key=str(execution.task_id),
        )
        yield self._rag
        raise RuntimeError("transport close failed")


class _MemoryAnalysisResourceStore:
    """只供 Application 编排测试使用的 CAS 内存 Store，不执行网络或文件 I/O。"""

    def __init__(self) -> None:
        self.record: AnalysisResourceRecord | None = None

    def create(self, command: AnalysisResourceCommand) -> AnalysisResourceRecord:
        if self.record is None:
            self.record = AnalysisResourceRecord(
                execution=command.execution,
                state=command.target_state,
                version=0,
                record_payload=command.record_payload,
            )
        return self.record

    def get(self, execution):  # type: ignore[no-untyped-def]
        if self.record is None or self.record.execution != execution:
            return None
        return self.record

    def advance(self, command: AnalysisResourceCommand) -> AnalysisResourceRecord:
        if (
            self.record is None
            or self.record.execution != command.execution
            or self.record.state is not command.expected_state
            or self.record.version != command.expected_version
        ):
            raise RuntimeError("测试资源 CAS 未命中")
        self.record = AnalysisResourceRecord(
            execution=command.execution,
            state=command.target_state,
            version=self.record.version + 1,
            record_payload=command.record_payload,
        )
        return self.record

    def list_recoverable(self, *, limit: int):  # type: ignore[no-untyped-def]
        return AnalysisResourceScanBatch(())

    def defer_recovery(self, execution, *, expected_version, retry_at, reason):  # type: ignore[no-untyped-def]
        if self.record is None or self.record.execution != execution:
            raise RuntimeError("测试资源不存在")
        if self.record.version != expected_version:
            raise RuntimeError("测试资源延期 CAS 未命中")
        self.record = AnalysisResourceRecord(
            execution=execution,
            state=self.record.state,
            version=self.record.version + 1,
            record_payload=self.record.record_payload,
            recovery_deferral_count=1,
            next_recovery_at=retry_at,
            last_recovery_reason=reason,
        )
        return self.record

    def quarantine_recovery_record(
        self,
        execution,
        *,
        expected_state,
        expected_version,
        reason,
    ):  # type: ignore[no-untyped-def]
        if (
            self.record is None
            or self.record.execution != execution
            or self.record.state is not expected_state
            or self.record.version != expected_version
        ):
            return False
        self.record = AnalysisResourceRecord(
            execution=execution,
            state=AnalysisResourceState.QUARANTINED,
            version=self.record.version + 1,
            record_payload=self.record.record_payload,
            recovery_deferral_count=max(
                1,
                self.record.recovery_deferral_count,
            ),
            next_recovery_at=self.record.next_recovery_at
            or "2026-07-27T00:00:00+00:00",
            last_recovery_reason=reason,
        )
        return True


class _MemoryAnalysisCallbackPort:
    """验证正常 Worker 回调只在终态条件写之后发生，不触发真实 HTTP。"""

    def __init__(self, script: StrictAnalysisFakeScript) -> None:
        self._script = script
        self.calls: list[str] = []

    def acquire(self, request: AnalysisCallbackRequest) -> AnalysisCallbackAcquireResult:
        self.calls.append("acquire")
        if not any(operation == "task.finish" for operation, _ in self._script.calls):
            raise AssertionError("终态未提交前不得获取 Callback Guard")
        return AnalysisCallbackAcquireResult(
            execution=request.execution,
            outcome=AnalysisCallbackAcquireOutcome.ACQUIRED,
            lease=AnalysisCallbackGuardLease(
                execution=request.execution,
                lease_token="memory-analysis-callback-lease",
                lease_version=1,
                expires_at="2030-01-01T00:00:30+00:00",
            ),
        )

    def wait_until_released(self, request: WaitForAnalysisCallbackRelease) -> AnalysisCallbackWaitResult:
        return AnalysisCallbackWaitResult(
            execution=request.execution,
            outcome=AnalysisCallbackWaitOutcome.RELEASED,
        )

    def deliver(self, request: AnalysisCallbackDeliveryRequest) -> AnalysisCallbackDelivery:
        self.calls.append("deliver")
        return AnalysisCallbackDelivery(
            execution=request.lease.execution,
            lease_token=request.lease.lease_token,
            lease_version=request.lease.lease_version,
            outcome=AnalysisCallbackDeliveryOutcome.DELIVERED,
        )

    def complete(self, lease, delivery, payload):  # type: ignore[no-untyped-def]
        self.calls.append("complete")
        if delivery.execution != lease.execution:
            raise AssertionError("测试 Callback delivery 与 lease 不一致")
        return True

    def freeze_expired(self, *, limit: int) -> AnalysisCallbackGuardSweepResult:
        return AnalysisCallbackGuardSweepResult(scanned_count=0, frozen_count=0)


def _fixture() -> tuple[AnalysisTaskInputV1, TaskExecutionSnapshot[AnalysisTaskInputV1]]:
    """构造单候选输入，使本测试聚焦 Application 编排而非模型分类黄金。"""

    raw_params = {
        "fileName": "application-demo.txt",
        "filePath": "https://example.invalid/application-demo.txt",
        "architectureList": [
            {
                "id": 103,
                "name": "装备型号",
                "parentId": None,
                "path": "103",
                "pathName": "装备型号",
                "remark": "装备型号资料",
            }
        ],
    }
    submission = AnalysisSubmissionSnapshot.from_request_params(
        raw_params,
        policy_snapshot=AnalysisPolicySnapshot.default(),
    )
    task_id = TaskId("analysis-application-task-1")
    task_input = AnalysisTaskInputV1.from_submission(
        submission,
        task_id=task_id.value,
        batch_id="1" * 32,
        batch_sequence=1,
        accepted_at="2026-07-26T12:00:00+08:00",
        trace_id="analysis-application-trace-1",
    )
    execution = TaskExecutionSnapshot(
        task_id=task_id,
        task_type="file",
        business_ref=TaskBusinessRef("file", task_input.file_name),
        execution_state="accepted",
        public_status="0",
        progress=0.0,
        message="",
        input_snapshot=task_input,
        accepted_at=task_input.accepted_at,
        trace_id=task_input.trace_id,
    )
    return task_input, execution


def _legacy_combined_fixture() -> tuple[
    AnalysisTaskInputV1,
    TaskExecutionSnapshot[AnalysisTaskInputV1],
]:
    """构造两个可见候选的 legacy combined 输入，避免被单候选优化为直接抽取。"""

    raw_params = {
        "fileName": "combined-budget-demo.txt",
        "filePath": "https://example.invalid/combined-budget-demo.txt",
        "architectureList": [
            {
                "id": 103,
                "name": "装备型号",
                "parentId": None,
                "path": "103",
                "pathName": "装备型号",
                "remark": "装备型号资料",
            },
            {
                "id": 104,
                "name": "装备性能",
                "parentId": None,
                "path": "104",
                "pathName": "装备性能",
                "remark": "装备性能资料",
            },
        ],
    }
    policy = replace(AnalysisPolicySnapshot.default(), classification_mode="legacy")
    submission = AnalysisSubmissionSnapshot.from_request_params(
        raw_params,
        policy_snapshot=policy,
    )
    task_id = TaskId("analysis-combined-budget-task-1")
    task_input = AnalysisTaskInputV1.from_submission(
        submission,
        task_id=task_id.value,
        batch_id="2" * 32,
        batch_sequence=1,
        accepted_at="2026-07-26T12:00:00+08:00",
        trace_id="analysis-combined-budget-trace-1",
    )
    execution = TaskExecutionSnapshot(
        task_id=task_id,
        task_type="file",
        business_ref=TaskBusinessRef("file", task_input.file_name),
        execution_state="accepted",
        public_status="0",
        progress=0.0,
        message="",
        input_snapshot=task_input,
        accepted_at=task_input.accepted_at,
        trace_id=task_input.trace_id,
    )
    return task_input, execution


class RunAnalysisTaskTests(unittest.TestCase):
    """每个测试都用严格 Script 证明未发生未配置的副作用。"""

    def _build_application(
        self,
        script: StrictAnalysisFakeScript,
        *,
        rag_factory: object | None = None,
        resources: object | None = None,
        callbacks: object | None = None,
        callback_url: str = "",
    ) -> RunAnalysisTask:
        ports = StrictAnalysisPortFake(script)
        return RunAnalysisTask(
            task_commands=StrictAnalysisTaskCommandFake(script),
            progress_publisher=StrictAnalysisGuardedProgressFake(script),
            workspaces=StrictAnalysisTaskWorkspaceFake(script),
            files=ports,
            rag_factory=(
                rag_factory
                if rag_factory is not None
                else StrictAnalysisRagFactoryFake(script, ports)
            ),
            knowledge=ports,
            audit=ports,
            translation=ports,
            resources=resources,
            callbacks=callbacks,
            callback_url=callback_url,
        )

    def test_public_import_surface_and_constructor_signature_exposes_optional_1f6_ports(self) -> None:
        """1F-6 仅追加内部可选 Port；原有八项依赖仍保持关键字调用兼容。"""

        self.assertEqual(
            (
                "AnalysisApplicationContractError",
                "AnalysisTaskCompletion",
                "AnalysisTaskPersistenceError",
                "RunAnalysisOutcome",
                "RunAnalysisResult",
                "RunAnalysisTask",
            ),
            run_analysis_module.__all__,
        )
        signature = inspect.signature(RunAnalysisTask)
        self.assertEqual(
            (
                "task_commands",
                "progress_publisher",
                "workspaces",
                "files",
                "rag_factory",
                "knowledge",
                "audit",
                "translation",
                "resources",
                "callbacks",
                "callback_url",
            ),
            tuple(signature.parameters),
        )
        self.assertTrue(
            all(
                parameter.kind is inspect.Parameter.KEYWORD_ONLY
                for parameter in signature.parameters.values()
            )
        )
        self.assertTrue(
            all(
                signature.parameters[name].default is inspect.Parameter.empty
                for name in (
                    "task_commands",
                    "progress_publisher",
                    "workspaces",
                    "files",
                    "rag_factory",
                    "knowledge",
                    "audit",
                    "translation",
                )
            )
        )
        self.assertEqual(None, signature.parameters["resources"].default)
        self.assertEqual(None, signature.parameters["callbacks"].default)
        self.assertEqual("", signature.parameters["callback_url"].default)
        self.assertEqual(
            ("self", "task_id"),
            tuple(inspect.signature(RunAnalysisTask.execute).parameters),
        )

    def test_recall_payload_accepts_immutable_domain_pairs(self) -> None:
        """审计投影必须消费领域层的不可变二元组，不能假定其为可变字典。"""

        decision = ArchitectureRecallDecision(
            tree_fingerprint="tree-fingerprint",
            query_digest="query-digest",
            base_leaf_ids=(103,),
            candidates=(
                ArchitectureRecallCandidate(
                    architecture_id=103,
                    path_name="装备/型号",
                    node_type="leaf",
                    remark="型号资料",
                    rank=1,
                    rrf_score=0.5,
                    channel_ranks=(("lexical", 1),),
                    protected_reasons=("exact:103",),
                ),
            ),
            channel_rankings=(RecallChannelRanking("lexical", (103,)),),
            rrf_scores=((103, 0.5),),
            protected_reasons=((103, ("exact:103",)),),
            direct_exact_ids=(103,),
            direct_tree_ids=(),
            candidate_projection_chars=120,
            prompt_chars=200,
            elapsed_ms=1.25,
        )

        payload = _AnalysisModelWorkflow.recall_payload(
            decision,
            prompt_chars=200,
        ).to_dict()

        self.assertEqual({"103": 0.5}, payload["rrf_scores"])
        self.assertEqual({"103": ["exact:103"]}, payload["protected_reasons"])

    def test_frozen_prompt_limit_rejects_oversized_legacy_prompt(self) -> None:
        """Worker 必须执行受理时冻结的 Prompt 上限，不能静默回退到进程默认值。"""

        task_input, _execution = _legacy_combined_fixture()
        task_input = replace(
            task_input,
            policy_snapshot=replace(
                task_input.policy_snapshot,
                classification_prompt_char_limit=100,
            ),
        )

        with self.assertRaises(ArchitecturePromptBudgetError):
            _AnalysisModelWorkflow().build_plan(
                task_input,
                "用于构造文件分析 Prompt 的离线正文",
            )

    @staticmethod
    def _expect_progress(script: StrictAnalysisFakeScript) -> None:
        script.expect("task.progress", True)
        script.expect("progress.publish", True)
        script.expect("task.is_latest", True)

    @staticmethod
    def _expect_task_start(
        script: StrictAnalysisFakeScript,
        execution: TaskExecutionSnapshot[AnalysisTaskInputV1],
    ) -> TaskExecutionSnapshot[AnalysisTaskInputV1]:
        running = replace(execution, execution_state="running")
        script.expect("task.get", execution)
        script.expect(
            "task.claim",
            TaskClaimResult(TaskClaimOutcome.CLAIMED, running),
        )
        return running

    @staticmethod
    def _rag_happy_values(
        task_input: AnalysisTaskInputV1,
    ) -> tuple[
        PreparedAnalysisDocument,
        AnalysisRagSessionRef,
        AnalysisRagSessionRef,
        AnalysisRagSessionOpenResult,
        AnalysisRagResult,
    ]:
        from app.modules.analysis.ports import AnalysisExecutionRef, AnalysisRagOperation

        execution = AnalysisExecutionRef(
            task_id=TaskId(task_input.task_id),
            file_name=task_input.file_name,
            batch_id=task_input.batch_id,
            batch_sequence=task_input.batch_sequence,
        )
        prepared = PreparedAnalysisDocument(
            execution=execution,
            source_path="C:/analysis/application-demo.txt",
            upload_path="C:/analysis/rag-input.txt",
            original_text="装备型号资料\n摘要正文",
        )
        pending = AnalysisRagSessionRef(
            execution=execution,
            session_ref="context:application::conversation:application",
            context_ref="context:application",
            conversation_ref="conversation:application",
        )
        bound = pending.with_bound_document(
            document_ref="document:application",
            document_location="location:application",
            content_sha256="a" * 64,
            ingested_file_name="rag-input.txt",
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
        result = AnalysisRagResult(
            execution=execution,
            session=bound,
            operation=AnalysisRagOperation.EXTRACTION,
            attempt_number=1,
            answer=(
                '{"architectureId":103,"fileDataItem":'
                '{"summary":"摘要","keyword":"装备"}}'
            ),
            lifecycle_events=(
                AnalysisRagLifecycleEvent(
                    sequence_no=3,
                    operation="document_upload",
                    attempt_number=1,
                    outcome=AnalysisRagLifecycleOutcome.SUCCEEDED,
                    external_ref=bound.document_location,
                ),
                AnalysisRagLifecycleEvent(
                    sequence_no=4,
                    operation="document_bind",
                    attempt_number=1,
                    outcome=AnalysisRagLifecycleOutcome.SUCCEEDED,
                    external_ref=bound.document_ref,
                ),
            ),
        )
        return prepared, pending, bound, opened, result

    def _expect_happy_path(
        self,
        script: StrictAnalysisFakeScript,
        *,
        close_outcome: AnalysisRagCloseOutcome = AnalysisRagCloseOutcome.CONFIRMED,
        translation_outcome: AnalysisTranslationOutcome = AnalysisTranslationOutcome.SUCCEEDED,
    ) -> tuple[TaskExecutionSnapshot[AnalysisTaskInputV1], AnalysisRagSessionRef]:
        task_input, execution = _fixture()
        running = self._expect_task_start(script, execution)
        prepared, _pending, bound, opened, rag_result = self._rag_happy_values(task_input)
        self._expect_progress(script)
        script.expect(
            "workspace.create",
            AnalysisTaskWorkspace(prepared.execution, "C:/analysis/task-1"),
        )
        script.expect("file.prepare", prepared)
        self._expect_progress(script)
        recall_receipt = AnalysisRecallAuditReceipt(
            execution=prepared.execution,
            idempotency_key=f"analysis-recall:{prepared.execution.task_id.value}",
            audit_id="recall:1",
            version=0,
        )
        script.expect("audit.reserve_recall", recall_receipt)
        script.expect("task.is_latest", True)
        script.expect("rag.factory.create", None)
        script.expect("rag.open_session", opened)
        script.expect("rag.execute", rag_result)
        script.expect(
            "audit.finalize_recall",
            AnalysisRecallAuditReceipt(
                execution=prepared.execution,
                idempotency_key=recall_receipt.idempotency_key,
                audit_id=recall_receipt.audit_id,
                version=1,
                finalized=True,
            ),
        )
        interaction_receipt = AnalysisInteractionAuditReceipt(
            execution=prepared.execution,
            idempotency_key=f"analysis-rag:{prepared.execution.task_id.value}",
            audit_id="interaction:1",
        )
        script.expect("audit.persist_interaction", interaction_receipt)
        script.expect("task.is_latest", True)
        knowledge_key = RunAnalysisTask._knowledge_idempotency_key(
            file_name=task_input.file_name,
            architecture_id=103,
            content_sha256=bound.content_sha256,
        )
        script.expect(
            "knowledge.persist",
            AnalysisKnowledgeWriteResult(
                execution=prepared.execution,
                idempotency_key=knowledge_key,
                outcome=AnalysisKnowledgeWriteOutcome.COMMITTED,
                external_ref="knowledge:103",
            ),
        )
        self._expect_progress(script)
        if translation_outcome is AnalysisTranslationOutcome.SUCCEEDED:
            translation = AnalysisTranslationResult(
                execution=prepared.execution,
                kind=AnalysisTranslationKind.DOCUMENT,
                outcome=translation_outcome,
                document_translation_one="单语",
                document_translation_two="双语",
            )
        else:
            translation = AnalysisTranslationResult(
                execution=prepared.execution,
                kind=AnalysisTranslationKind.DOCUMENT,
                outcome=translation_outcome,
                error_code="document_translation_failed",
            )
        script.expect("translation.translate", translation)
        self._expect_progress(script)
        script.expect("task.finish", True)
        script.expect("progress.publish", True)
        script.expect("task.is_latest", True)
        if close_outcome is AnalysisRagCloseOutcome.CONFIRMED:
            close_event = AnalysisRagLifecycleEvent(
                sequence_no=5,
                operation="context_delete",
                attempt_number=1,
                outcome=AnalysisRagLifecycleOutcome.SUCCEEDED,
                external_ref=bound.context_ref,
            )
            close = AnalysisRagCloseResult(
                execution=prepared.execution,
                session=bound,
                outcome=close_outcome,
                lifecycle_events=(close_event,),
            )
        else:
            close_event = AnalysisRagLifecycleEvent(
                sequence_no=5,
                operation="context_delete",
                attempt_number=1,
                outcome=AnalysisRagLifecycleOutcome.OUTCOME_UNKNOWN,
                external_ref=bound.context_ref,
                error_code="context_delete_outcome_unknown",
            )
            close = AnalysisRagCloseResult(
                execution=prepared.execution,
                session=bound,
                outcome=close_outcome,
                lifecycle_events=(close_event,),
                detail_code="rag_close_outcome_unknown",
            )
        script.expect("rag.close_session", close)
        script.expect("audit.append_lifecycle_events", None)
        return running, bound

    def test_happy_path_preserves_stage_order_and_single_success_terminal(self) -> None:
        """用拆分前冻结的离线轨迹锁定成功分支的副作用顺序与幂等键。"""

        script = StrictAnalysisFakeScript()
        running, _bound = self._expect_happy_path(script)

        result = self._build_application(script).execute(running.task_id)

        file_request = next(
            argument
            for operation, argument in script.calls
            if operation == "file.prepare"
        )

        self.assertEqual(
            running.input_snapshot.file_path,
            file_request.source_url,
        )

        trace_path = Path(__file__).with_name("fixtures") / "analysis_application_1f3s_happy_trace.json"
        expected_trace = json.loads(trace_path.read_text(encoding="utf-8"))
        self.assertEqual(RunAnalysisOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(expected_trace["outcome"], result.outcome.value)
        self.assertEqual(
            expected_trace["operations"],
            [operation for operation, _argument in script.calls],
        )
        self.assertEqual(
            expected_trace["knowledge_idempotency_key"],
            RunAnalysisTask._knowledge_idempotency_key(
                file_name="application-demo.txt",
                architecture_id=103,
                content_sha256="a" * 64,
            ),
        )
        rag_attempts = [
            {
                "operation": request.operation.value,
                "attempt_number": request.attempt_number,
                "prompt_sha256": hashlib.sha256(
                    request.prompt.encode("utf-8")
                ).hexdigest(),
            }
            for operation, request in script.calls
            if operation == "rag.execute"
        ]
        self.assertEqual(expected_trace["rag_attempts"], rag_attempts)
        recall_record = next(
            argument
            for operation, argument in script.calls
            if operation == "audit.reserve_recall"
        )
        recall_payload = recall_record.payload.to_dict()
        # 耗时取自单调时钟，不能把本机调度抖动冻结为测试期望；其余召回投影必须字节等价。
        recall_payload.pop("recall_elapsed_ms")
        self.assertEqual(expected_trace["recall_payload"], recall_payload)
        interaction_record = next(
            argument
            for operation, argument in script.calls
            if operation == "audit.persist_interaction"
        )
        interaction_attempts = [
            {
                "operation": attempt.operation.value,
                "attempt_number": attempt.attempt_number,
                "prompt_digest": attempt.prompt_digest,
                "error_code": attempt.error_code,
            }
            for attempt in interaction_record.attempts
        ]
        self.assertEqual(expected_trace["interaction_attempts"], interaction_attempts)
        knowledge_request = next(
            argument
            for operation, argument in script.calls
            if operation == "knowledge.persist"
        )
        self.assertEqual(
            expected_trace["knowledge"],
            {
                "architecture_id": knowledge_request.architecture_id,
                "document_ref": knowledge_request.document.document_ref,
            },
        )
        self.assertEqual("task.finish", script.calls[-5][0])
        script.assert_exhausted()

    def test_full_translation_uses_converted_path_only_for_legacy_office(self) -> None:
        """Legacy 用 processing OOXML；普通格式继续使用 raw source，避免扩大行为变化。"""

        task_input, _task_execution = _fixture()
        execution = AnalysisExecutionRef(
            task_id=TaskId(task_input.task_id),
            file_name=task_input.file_name,
            batch_id=task_input.batch_id,
            batch_sequence=task_input.batch_sequence,
        )
        script = StrictAnalysisFakeScript()
        ports = StrictAnalysisPortFake(script)
        handoff = _AnalysisKnowledgeHandoff(ports, ports)
        translation_result = AnalysisTranslationResult(
            execution=execution,
            kind=AnalysisTranslationKind.DOCUMENT,
            outcome=AnalysisTranslationOutcome.SUCCEEDED,
            document_translation_one="单语",
            document_translation_two="双语",
        )
        for prepared, expected_path in (
            (
                PreparedAnalysisDocument(
                    execution=execution,
                    source_path="C:/analysis/raw.doc",
                    processing_path=(
                        "C:/analysis/prepared-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.docx"
                    ),
                    upload_path=(
                        "C:/analysis/prepared-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.docx"
                    ),
                    original_text="正文",
                    internal_prepared_basename=(
                        "prepared-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.docx"
                    ),
                ),
                "C:/analysis/prepared-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.docx",
            ),
            (
                PreparedAnalysisDocument(
                    execution=execution,
                    source_path="C:/analysis/raw.pdf",
                    processing_path="C:/analysis/normalized.pdf",
                    upload_path="C:/analysis/rag-input.pdf",
                    original_text="正文",
                ),
                "C:/analysis/raw.pdf",
            ),
        ):
            with self.subTest(expected_path=expected_path):
                script.expect("translation.translate", translation_result)
                mapped_result = {"fileDataItem": {"summary": "摘要"}}
                handoff.enrich_translations(
                    execution=execution,
                    snapshot=task_input,
                    prepared=prepared,
                    mapped_result=mapped_result,
                )
                request = script.calls[-1][1]
                self.assertEqual(expected_path, request.source_path)
                self.assertEqual(
                    "单语",
                    mapped_result["fileDataItem"]["documentTranslationOne"],
                )
        script.assert_exhausted()

    def test_optional_resource_port_persists_close_intent_and_reaches_cleaned(self) -> None:
        """1F-6 内部链路必须先记录资源意图，再 close、审计，最后才标记 cleaned。"""

        script = StrictAnalysisFakeScript()
        running, _bound = self._expect_happy_path(script)
        resources = _MemoryAnalysisResourceStore()

        result = self._build_application(script, resources=resources).execute(running.task_id)

        self.assertEqual(RunAnalysisOutcome.SUCCEEDED, result.outcome)
        self.assertIsNotNone(resources.record)
        assert resources.record is not None
        self.assertEqual(AnalysisResourceState.CLEANED, resources.record.state)
        payload = resources.record.record_payload.to_dict()
        self.assertEqual("permanent", payload["ownership"]["document"])
        self.assertEqual("confirmed", payload["cleanup"]["session_close"]["state"])
        self.assertEqual("confirmed", payload["cleanup"]["audit_append"]["state"])
        script.assert_exhausted()

    def test_optional_callback_port_runs_once_after_success_terminal(self) -> None:
        """正常 Worker 使用 1F-6 Guard Port，且不能在 ``task.finish`` 前投递。"""

        script = StrictAnalysisFakeScript()
        running, _bound = self._expect_happy_path(script)
        callbacks = _MemoryAnalysisCallbackPort(script)

        result = self._build_application(
            script,
            callbacks=callbacks,
            callback_url="https://callback.invalid/analysis",
        ).execute(running.task_id)

        self.assertEqual(RunAnalysisOutcome.SUCCEEDED, result.outcome)
        self.assertEqual(["acquire", "deliver", "complete"], callbacks.calls)
        script.assert_exhausted()

    def test_stale_claim_has_no_workspace_rag_or_terminal_side_effect(self) -> None:
        task_input, execution = _fixture()
        script = StrictAnalysisFakeScript()
        script.expect("task.get", execution)
        script.expect(
            "task.claim",
            TaskClaimResult(TaskClaimOutcome.STALE, execution),
        )

        result = self._build_application(script).execute(TaskId(task_input.task_id))

        self.assertEqual(RunAnalysisOutcome.NOT_CLAIMED, result.outcome)
        script.assert_exhausted()

    def test_stale_progress_stops_before_workspace_and_file_preparation(self) -> None:
        task_input, execution = _fixture()
        script = StrictAnalysisFakeScript()
        self._expect_task_start(script, execution)
        script.expect("task.progress", False)

        result = self._build_application(script).execute(execution.task_id)

        self.assertEqual(RunAnalysisOutcome.STALE, result.outcome)
        script.assert_exhausted()

    def test_recall_audit_failure_prevents_factory_and_knowledge(self) -> None:
        task_input, execution = _fixture()
        script = StrictAnalysisFakeScript()
        self._expect_task_start(script, execution)
        prepared, _pending, _bound, _opened, _result = self._rag_happy_values(task_input)
        self._expect_progress(script)
        script.expect("workspace.create", AnalysisTaskWorkspace(prepared.execution, "C:/analysis/task-1"))
        script.expect("file.prepare", prepared)
        self._expect_progress(script)
        script.expect("audit.reserve_recall", RuntimeError("sqlite failure"))
        script.expect("task.finish", True)
        script.expect("progress.publish", True)
        script.expect("task.is_latest", True)

        result = self._build_application(script).execute(execution.task_id)

        self.assertEqual(RunAnalysisOutcome.FAILED, result.outcome)
        self.assertEqual("analysis_execution", result.stage)
        script.assert_exhausted()

    def test_rag_factory_create_failure_finalizes_once_without_external_session(self) -> None:
        """Factory 尚未给出 SessionRef 时，失败只能收敛任务，不能伪造 close 或知识写入。"""

        task_input, execution = _fixture()
        script = StrictAnalysisFakeScript()
        self._expect_task_start(script, execution)
        prepared, _pending, _bound, _opened, _result = self._rag_happy_values(task_input)
        self._expect_progress(script)
        script.expect("workspace.create", AnalysisTaskWorkspace(prepared.execution, "C:/analysis/task-1"))
        script.expect("file.prepare", prepared)
        self._expect_progress(script)
        recall = AnalysisRecallAuditReceipt(
            prepared.execution,
            f"analysis-recall:{prepared.execution.task_id.value}",
            "recall:1",
            0,
        )
        script.expect("audit.reserve_recall", recall)
        script.expect("task.is_latest", True)
        script.expect("rag.factory.create", RuntimeError("transport create failed"))
        script.expect(
            "audit.finalize_recall",
            AnalysisRecallAuditReceipt(
                prepared.execution,
                recall.idempotency_key,
                recall.audit_id,
                1,
                True,
            ),
        )
        script.expect("task.finish", True)
        script.expect("progress.publish", True)
        script.expect("task.is_latest", True)

        result = self._build_application(script).execute(execution.task_id)

        self.assertEqual(RunAnalysisOutcome.FAILED, result.outcome)
        self.assertEqual("analysis_execution", result.stage)
        script.assert_exhausted()

    def test_open_partial_failure_is_audited_without_complete_session(self) -> None:
        """只有 Context 的打开失败也必须原子保存，不能因缺完整 Session 丢失恢复引用。"""

        task_input, execution = _fixture()
        script = StrictAnalysisFakeScript()
        self._expect_task_start(script, execution)
        prepared, _pending, _bound, _opened, _result = self._rag_happy_values(task_input)
        self._expect_progress(script)
        script.expect(
            "workspace.create",
            AnalysisTaskWorkspace(prepared.execution, "C:/analysis/task-1"),
        )
        script.expect("file.prepare", prepared)
        self._expect_progress(script)
        recall = AnalysisRecallAuditReceipt(
            prepared.execution,
            f"analysis-recall:{prepared.execution.task_id.value}",
            "recall:partial-open",
            0,
        )
        script.expect("audit.reserve_recall", recall)
        script.expect("task.is_latest", True)
        script.expect("rag.factory.create", None)
        lifecycle_events = (
            AnalysisRagLifecycleEvent(
                1,
                "context_create",
                1,
                AnalysisRagLifecycleOutcome.SUCCEEDED,
                "context:partial-open",
            ),
            AnalysisRagLifecycleEvent(
                2,
                "conversation_create",
                1,
                AnalysisRagLifecycleOutcome.FAILED,
                error_code="conversation_create_failed",
            ),
            AnalysisRagLifecycleEvent(
                3,
                "context_rollback",
                1,
                AnalysisRagLifecycleOutcome.FAILED,
                "context:partial-open",
                "context_rollback_failed",
            ),
        )
        script.expect(
            "rag.open_session",
            AnalysisRagSessionOpenError(
                "打开失败且回滚失败",
                execution=prepared.execution,
                stage=AnalysisRagSessionOpenStage.CONVERSATION_CREATE,
                lifecycle_events=lifecycle_events,
            ),
        )
        script.expect(
            "audit.finalize_recall",
            AnalysisRecallAuditReceipt(
                prepared.execution,
                recall.idempotency_key,
                recall.audit_id,
                1,
                True,
            ),
        )
        interaction = AnalysisInteractionAuditReceipt(
            prepared.execution,
            f"analysis-rag:{prepared.execution.task_id.value}",
            "interaction:partial-open",
        )
        script.expect("audit.persist_interaction", interaction)
        script.expect("task.finish", True)
        script.expect("progress.publish", True)
        script.expect("task.is_latest", True)

        result = self._build_application(script).execute(execution.task_id)

        self.assertEqual(RunAnalysisOutcome.FAILED, result.outcome)
        audit_records = [
            argument
            for operation, argument in script.calls
            if operation == "audit.persist_interaction"
        ]
        self.assertEqual(1, len(audit_records))
        self.assertIsNone(audit_records[0].session)
        self.assertEqual(lifecycle_events, audit_records[0].lifecycle_events)
        script.assert_exhausted()

    def test_interaction_audit_failure_prevents_knowledge_and_preserves_session(self) -> None:
        task_input, execution = _fixture()
        script = StrictAnalysisFakeScript()
        self._expect_task_start(script, execution)
        prepared, _pending, _bound, opened, rag_result = self._rag_happy_values(task_input)
        self._expect_progress(script)
        script.expect("workspace.create", AnalysisTaskWorkspace(prepared.execution, "C:/analysis/task-1"))
        script.expect("file.prepare", prepared)
        self._expect_progress(script)
        receipt = AnalysisRecallAuditReceipt(
            prepared.execution,
            f"analysis-recall:{prepared.execution.task_id.value}",
            "recall:1",
            0,
        )
        script.expect("audit.reserve_recall", receipt)
        script.expect("task.is_latest", True)
        script.expect("rag.factory.create", None)
        script.expect("rag.open_session", opened)
        script.expect("rag.execute", rag_result)
        script.expect(
            "audit.finalize_recall",
            AnalysisRecallAuditReceipt(
                prepared.execution,
                receipt.idempotency_key,
                receipt.audit_id,
                1,
                True,
            ),
        )
        script.expect("audit.persist_interaction", RuntimeError("audit unavailable"))
        script.expect("task.finish", True)
        script.expect("progress.publish", True)
        script.expect("task.is_latest", True)

        result = self._build_application(script).execute(execution.task_id)

        self.assertEqual(RunAnalysisOutcome.FAILED, result.outcome)
        self.assertEqual("audit", result.stage)
        script.assert_exhausted()

    def test_knowledge_unknown_writes_failure_but_does_not_close_or_translate(self) -> None:
        task_input, execution = _fixture()
        script = StrictAnalysisFakeScript()
        self._expect_task_start(script, execution)
        prepared, _pending, bound, opened, rag_result = self._rag_happy_values(task_input)
        self._expect_progress(script)
        script.expect("workspace.create", AnalysisTaskWorkspace(prepared.execution, "C:/analysis/task-1"))
        script.expect("file.prepare", prepared)
        self._expect_progress(script)
        recall = AnalysisRecallAuditReceipt(prepared.execution, f"analysis-recall:{prepared.execution.task_id.value}", "recall:1", 0)
        script.expect("audit.reserve_recall", recall)
        script.expect("task.is_latest", True)
        script.expect("rag.factory.create", None)
        script.expect("rag.open_session", opened)
        script.expect("rag.execute", rag_result)
        script.expect("audit.finalize_recall", AnalysisRecallAuditReceipt(prepared.execution, recall.idempotency_key, recall.audit_id, 1, True))
        script.expect("audit.persist_interaction", AnalysisInteractionAuditReceipt(prepared.execution, f"analysis-rag:{prepared.execution.task_id.value}", "interaction:1"))
        script.expect("task.is_latest", True)
        key = RunAnalysisTask._knowledge_idempotency_key(file_name=task_input.file_name, architecture_id=103, content_sha256=bound.content_sha256)
        script.expect(
            "knowledge.persist",
            AnalysisKnowledgeWriteResult(
                execution=prepared.execution,
                idempotency_key=key,
                outcome=AnalysisKnowledgeWriteOutcome.OUTCOME_UNKNOWN,
                detail_code="knowledge_write_outcome_unknown",
            ),
        )
        script.expect("task.finish", True)
        script.expect("progress.publish", True)
        script.expect("task.is_latest", True)

        result = self._build_application(script).execute(execution.task_id)

        self.assertEqual(RunAnalysisOutcome.FAILED, result.outcome)
        self.assertEqual("knowledge_index_unknown", result.stage)
        script.assert_exhausted()

    def test_combined_json_repair_exhausts_phase_budget_before_architecture_repair(self) -> None:
        """combined 已调用 JSON repair 两次时，不得再偷偷发送第三个分类 repair 请求。"""

        task_input, execution = _legacy_combined_fixture()
        script = StrictAnalysisFakeScript()
        self._expect_task_start(script, execution)
        prepared, _pending, bound, opened, _rag_result = self._rag_happy_values(task_input)
        self._expect_progress(script)
        script.expect("workspace.create", AnalysisTaskWorkspace(prepared.execution, "C:/analysis/task-1"))
        script.expect("file.prepare", prepared)
        self._expect_progress(script)
        recall = AnalysisRecallAuditReceipt(
            prepared.execution,
            f"analysis-recall:{prepared.execution.task_id.value}",
            "recall:combined-budget",
            0,
        )
        script.expect("audit.reserve_recall", recall)
        script.expect("task.is_latest", True)
        script.expect("rag.factory.create", None)
        script.expect("rag.open_session", opened)
        script.expect(
            "rag.execute",
            AnalysisRagResult(
                execution=prepared.execution,
                session=bound,
                operation=AnalysisRagOperation.COMBINED,
                attempt_number=1,
                answer="这不是 JSON",
                lifecycle_events=(
                    AnalysisRagLifecycleEvent(
                        sequence_no=3,
                        operation="document_upload",
                        attempt_number=1,
                        outcome=AnalysisRagLifecycleOutcome.SUCCEEDED,
                        external_ref=bound.document_location,
                    ),
                    AnalysisRagLifecycleEvent(
                        sequence_no=4,
                        operation="document_bind",
                        attempt_number=1,
                        outcome=AnalysisRagLifecycleOutcome.SUCCEEDED,
                        external_ref=bound.document_ref,
                    ),
                ),
            ),
        )
        # 第二次调用只负责 JSON repair；其结果虽是 JSON，但 architectureId 越界。严格
        # Script 故意不配置第三次 ``rag.execute``，从而锁定阶段预算不允许 architecture repair。
        script.expect(
            "rag.execute",
            AnalysisRagResult(
                execution=prepared.execution,
                session=bound,
                operation=AnalysisRagOperation.EXTRACTION_REPAIR,
                attempt_number=1,
                answer='{"architectureId":999,"fileDataItem":{"summary":"摘要"}}',
            ),
        )
        finalized = AnalysisRecallAuditReceipt(
            prepared.execution,
            recall.idempotency_key,
            recall.audit_id,
            1,
            True,
        )
        script.expect("audit.finalize_recall", finalized)
        interaction = AnalysisInteractionAuditReceipt(
            prepared.execution,
            f"analysis-rag:{prepared.execution.task_id.value}",
            "interaction:combined-budget",
        )
        script.expect("audit.persist_interaction", interaction)
        script.expect("task.finish", True)
        script.expect("progress.publish", True)
        script.expect("task.is_latest", True)
        script.expect(
            "rag.close_session",
            AnalysisRagCloseResult(
                execution=prepared.execution,
                session=bound,
                outcome=AnalysisRagCloseOutcome.CONFIRMED,
                lifecycle_events=(
                    AnalysisRagLifecycleEvent(
                        sequence_no=5,
                        operation="context_delete",
                        attempt_number=1,
                        outcome=AnalysisRagLifecycleOutcome.SUCCEEDED,
                        external_ref=bound.context_ref,
                    ),
                ),
            ),
        )
        script.expect("audit.append_lifecycle_events", None)

        result = self._build_application(script).execute(execution.task_id)

        self.assertEqual(RunAnalysisOutcome.FAILED, result.outcome)
        self.assertEqual("architecture_contract", result.stage)
        script.assert_exhausted()

    def test_translation_failure_degrades_to_success_and_close_unknown_does_not_rewrite_terminal(self) -> None:
        script = StrictAnalysisFakeScript()
        running, _bound = self._expect_happy_path(
            script,
            translation_outcome=AnalysisTranslationOutcome.FAILED,
            close_outcome=AnalysisRagCloseOutcome.OUTCOME_UNKNOWN,
        )

        result = self._build_application(script).execute(running.task_id)

        self.assertEqual(RunAnalysisOutcome.SUCCEEDED, result.outcome)
        self.assertEqual("", result.error_code)
        script.assert_exhausted()

    def test_rag_factory_exit_failure_does_not_rewrite_completed_terminal(self) -> None:
        """业务 close 已审计后，Transport 释放失败只记录日志并返回原成功结果。"""

        script = StrictAnalysisFakeScript()
        running, _bound = self._expect_happy_path(script)

        result = self._build_application(
            script,
            rag_factory=_ExitFailingRagFactory(script),
        ).execute(running.task_id)

        self.assertEqual(RunAnalysisOutcome.SUCCEEDED, result.outcome)
        script.assert_exhausted()


if __name__ == "__main__":
    unittest.main()
