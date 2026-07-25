from __future__ import annotations

import unittest
import sqlite3
from pathlib import Path

from app.modules.report.adapters import (
    AnythingLLMReportRagAdapter,
    LegacyReportFileAdapter,
    LocalReportArtifactAdapter,
    ReportTaskCommandCodec,
    SQLiteReportCallbackAdapter,
    SQLiteReportInteractionAuditAdapter,
    SQLiteReportResourceStoreAdapter,
)
from app.modules.report.application import (
    ReportResourceRecoveryService,
    RunReportOutcome,
    RunReportTask,
)
from app.modules.report.domain import ReportId, ReportSubmission
from app.modules.report.ports import (
    ReportCallbackDeliveryOutcome,
    ReportCallbackDeliveryResult,
    ReportResourceState,
)
from app.modules.tasks.adapters import LegacyTaskCommandAdapter
from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.tasks.ports import TaskSubmissionCommand, TaskSubmissionOutcome
from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir
from tests.fakes.report import (
    FakeProgressPublisherPort,
    FakeReportCallbackPort,
    InvocationRecorder,
)
from tests.test_report_rag_adapter import _Backend, _Factory


class ReportRuntimeAdapterTests(unittest.TestCase):
    def test_run_report_task_composes_all_1c5_runtime_adapters(self) -> None:
        """1C-5 全部生产 Adapter 可在无 Flask、无真实网络时完成一次完整执行。"""

        with workspace_tempdir() as tmp:
            root = Path(tmp)
            service = LLMTaskService(db_path=str(root / "tasks.sqlite3"))
            task_id = TaskId("report-runtime-execution-001")
            task_commands = LegacyTaskCommandAdapter(
                service,
                ReportTaskCommandCodec(),
                task_id_factory=lambda: task_id,
                clock=lambda: "2026-07-16T12:00:00+08:00",
            )
            submission = ReportSubmission(
                report_id=ReportId.from_public_value(132),
                source_urls=(
                    "http://files.local/source-a.pdf",
                    "http://files.local/source-b.pdf",
                ),
                template_outline_url="http://files.local/template.docx",
                template_desc="测试模板",
                requirement="生成完整报告",
                trace_id="trace-runtime-001",
            )
            accepted = task_commands.create_if_allowed(
                TaskSubmissionCommand(
                    task_type="report",
                    business_ref=TaskBusinessRef("report", "132"),
                    input_schema_version=1,
                    submission=submission,
                    trace_id=submission.trace_id,
                )
            )
            self.assertEqual(TaskSubmissionOutcome.ACCEPTED, accepted.outcome)

            artifacts = LocalReportArtifactAdapter(root / "artifacts")

            def downloader(
                url: str,
                file_name: str,
                temp_root: str,
                timeout: float,
                max_bytes: int,
            ) -> str:
                self.assertGreater(max_bytes, 0)
                target = Path(temp_root) / file_name
                target.write_text(f"downloaded:{url}", encoding="utf-8")
                return str(target)

            files = LegacyReportFileAdapter(
                artifacts,
                downloader=downloader,
                normalizer=lambda path: path,
                upload_preparer=lambda path: [path],
                word_extractor=lambda _: "第一章\n第二章",
            )
            backend = _Backend()
            rag = AnythingLLMReportRagAdapter(
                _Factory(backend),
                artifact_path_resolver=artifacts.resolve_path,
            )
            audit = SQLiteReportInteractionAuditAdapter(service)
            resources = ReportResourceRecoveryService(
                store=SQLiteReportResourceStoreAdapter(service),
                artifacts=artifacts,
                rag=rag,
                audit=audit,
            )
            recorder = InvocationRecorder()
            callback_payloads: list[dict[str, object]] = []

            def deliver_callback(
                payload: dict[str, object],
            ) -> ReportCallbackDeliveryResult:
                callback_payloads.append(payload)
                return ReportCallbackDeliveryResult(
                    ReportCallbackDeliveryOutcome.SUCCESS,
                    "http_status=204",
                )

            callbacks = SQLiteReportCallbackAdapter(
                service,
                callback_url="http://callback.local/report",
                callback_timeout=5,
                transport=deliver_callback,
            )
            runner = RunReportTask(
                task_commands=task_commands,
                progress_publisher=FakeProgressPublisherPort(recorder),
                files=files,
                artifacts=artifacts,
                rag=rag,
                audit=audit,
                callbacks=callbacks,
                resources=resources,
            )

            result = runner.execute(task_id)

            self.assertEqual(RunReportOutcome.SUCCEEDED, result.outcome)
            self.assertEqual("success", result.callback_outcome)
            execution = service.get_task_execution(task_id.value)
            self.assertEqual("succeeded", execution["execution_state"])
            public_task = service.get_task("report", "132")
            self.assertEqual("1", public_task["status"])
            self.assertEqual("1", public_task["result_payload"]["data"]["status"])
            self.assertEqual(132, public_task["result_payload"]["data"]["reportId"])
            self.assertNotIn("artifact", public_task["result_payload"])
            interactions = service.get_llm_interactions("report", "132")
            self.assertEqual(1, len(interactions))
            self.assertEqual("trace-runtime-001", interactions[0]["trace_id"])
            self.assertEqual("deleted", interactions[0]["workspace_cleanup_status"])
            self.assertEqual(1, len(callback_payloads))
            self.assertEqual(
                public_task["result_payload"],
                callback_payloads[0],
            )
            self.assertEqual("success", execution["callback_status"])
            task_root = (
                artifacts.root / LocalReportArtifactAdapter._namespace(task_id)
            )
            output_path = task_root / "output" / "report.html"
            self.assertTrue(output_path.is_file())
            self.assertFalse(
                (task_root / "scratch").exists()
            )
            resource_record = SQLiteReportResourceStoreAdapter(service).get(task_id)
            self.assertIsNotNone(resource_record)
            self.assertEqual(ReportResourceState.CLEANED, resource_record.state)
            self.assertEqual(
                (resource_record.final_artifact,),
                resource_record.retained,
            )

    def test_audit_failure_quarantines_and_preserves_local_external_scene(self) -> None:
        """审计事务失败时允许失败回调，但不得清理 RAG/Artifact 或写成功终态。"""

        with workspace_tempdir() as tmp:
            root = Path(tmp)
            database_path = root / "tasks.sqlite3"
            service = LLMTaskService(db_path=str(database_path))
            task_id = TaskId("report-runtime-audit-failure")
            task_commands = LegacyTaskCommandAdapter(
                service,
                ReportTaskCommandCodec(),
                task_id_factory=lambda: task_id,
                clock=lambda: "2026-07-16T12:01:00+08:00",
            )
            submission = ReportSubmission(
                report_id=ReportId.from_public_value(133),
                source_urls=("http://files.local/source.pdf",),
                template_outline_url="http://files.local/template.docx",
                template_desc="测试模板",
                requirement="生成报告",
                trace_id="trace-runtime-audit-failure",
            )
            task_commands.create_if_allowed(
                TaskSubmissionCommand(
                    task_type="report",
                    business_ref=TaskBusinessRef("report", "133"),
                    input_schema_version=1,
                    submission=submission,
                    trace_id=submission.trace_id,
                )
            )
            # 使用数据库触发器模拟 attempts/lifecycle 同事务中的不可恢复写故障。触发器仅
            # 存在于临时测试库，不修改生产 Schema；预期整个审计事务回滚且无主记录残留。
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    """
                    CREATE TRIGGER fail_report_interaction_insert
                    BEFORE INSERT ON llm_interactions
                    BEGIN
                        SELECT RAISE(ABORT, 'forced audit failure');
                    END
                    """
                )

            artifacts = LocalReportArtifactAdapter(root / "artifacts")

            def downloader(
                url: str,
                file_name: str,
                temp_root: str,
                timeout: float,
                max_bytes: int,
            ) -> str:
                self.assertGreater(max_bytes, 0)
                target = Path(temp_root) / file_name
                target.write_text("downloaded", encoding="utf-8")
                return str(target)

            files = LegacyReportFileAdapter(
                artifacts,
                downloader=downloader,
                normalizer=lambda path: path,
                upload_preparer=lambda path: [path],
                word_extractor=lambda _: "模板正文",
            )
            backend = _Backend()
            rag = AnythingLLMReportRagAdapter(
                _Factory(backend),
                artifact_path_resolver=artifacts.resolve_path,
            )
            recorder = InvocationRecorder()
            callbacks = FakeReportCallbackPort(recorder)
            audit = SQLiteReportInteractionAuditAdapter(service)
            resources = ReportResourceRecoveryService(
                store=SQLiteReportResourceStoreAdapter(service),
                artifacts=artifacts,
                rag=rag,
                audit=audit,
            )
            runner = RunReportTask(
                task_commands=task_commands,
                progress_publisher=FakeProgressPublisherPort(recorder),
                files=files,
                artifacts=artifacts,
                rag=rag,
                audit=audit,
                callbacks=callbacks,
                resources=resources,
            )

            result = runner.execute(task_id)

            self.assertEqual(RunReportOutcome.FAILED, result.outcome)
            self.assertEqual("report_audit_error", result.error_code)
            self.assertEqual("2", service.get_task("report", "133")["status"])
            self.assertEqual([], service.get_llm_interactions("report", "133"))
            self.assertEqual([], backend.deleted_documents)
            self.assertEqual([], backend.deleted_workspaces)
            self.assertEqual("2", callbacks.delivery_calls[0].payload.status)
            scratch = artifacts.root / LocalReportArtifactAdapter._namespace(task_id) / "scratch"
            self.assertTrue(scratch.exists())
            self.assertTrue(any(path.is_file() for path in scratch.rglob("*")))
            resource_record = SQLiteReportResourceStoreAdapter(service).get(task_id)
            self.assertIsNotNone(resource_record)
            self.assertEqual(ReportResourceState.QUARANTINED, resource_record.state)


if __name__ == "__main__":
    unittest.main()
