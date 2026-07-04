import sqlite3
import unittest
from unittest.mock import patch

from app.ports.rag import (
    RagAttempt,
    RagExecutionTrace,
    RagLifecycleEvent,
    RagSource,
)
from app.services.llm_service.task_service import (
    InteractionAuditError,
    LLMTaskService,
)
from tests import workspace_tempdir


def _successful_trace() -> RagExecutionTrace:
    """构造同时包含准备事件和模型调用的最小成功轨迹。"""
    source = RagSource(
        document_ref="document:doc-001",
        text="用于审计的来源片段",
        title="demo.pdf",
        score=0.92,
    )
    return RagExecutionTrace(
        context_name="analysis-demo",
        context_ref="context-001",
        conversation_ref="conversation-001",
        attempts=(
            RagAttempt(
                operation="analyse",
                attempt=1,
                prompt_kind="analysis",
                raw_response='{"summary":"摘要"}',
                sources=(source,),
                failure_stage=None,
                error_message=None,
            ),
        ),
        failure_stage=None,
        error_message=None,
        lifecycle_events=(
            RagLifecycleEvent(
                sequence_no=1,
                operation="context_create",
                attempt=1,
                success=True,
                external_ref="context-001",
                failure_stage=None,
                error_message=None,
            ),
            RagLifecycleEvent(
                sequence_no=2,
                operation="conversation_create",
                attempt=1,
                success=True,
                external_ref="conversation-001",
                failure_stage=None,
                error_message=None,
            ),
        ),
    )


def _create_audited_interaction(service: LLMTaskService) -> int:
    """创建一条已提交审计，供生命周期追加测试复用。"""
    result = service.create_llm_interaction_with_trace(
        business_type="file",
        business_key="demo.pdf",
        prompt="提取文档字段",
        trace=_successful_trace(),
        status="succeeded",
    )
    return result.interaction_id


class LLMTaskServiceTests(unittest.TestCase):
    def test_create_file_task_defaults_to_processing(self):
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task = service.create_file_task(file_name="demo.pdf", request_payload={"businessType": "file"})
            self.assertEqual(task["business_key"], "demo.pdf")
            self.assertEqual(task["status"], "1")
            self.assertEqual(task["callback_status"], "pending")

    def test_create_file_task_can_start_as_pending(self):
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task = service.create_file_task(
                file_name="demo-2.pdf",
                request_payload={"businessType": "file"},
                status="0",
            )
            self.assertEqual(task["status"], "0")
            self.assertEqual(task["progress"], 0.0)

    def test_get_tasks_returns_snapshots_in_request_order(self):
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            service.create_file_task("a.pdf", {"businessType": "file"}, status="1")
            service.create_file_task("b.pdf", {"businessType": "file"}, status="0")

            tasks = service.get_tasks("file", ["a.pdf", "b.pdf"])

            self.assertEqual([item["business_key"] for item in tasks], ["a.pdf", "b.pdf"])

    def test_llm_interaction_persists_content_and_cleanup_status(self):
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")

            interaction_id = service.create_llm_interaction(
                business_type="file",
                business_key="demo.pdf",
                workspace_name="llm-file-1000",
                workspace_slug="llm-file-1000",
                thread_slug="analysis-demo",
                prompt="提取文档字段",
                response='{"summary":"摘要"}',
                sources=[{"title": "demo.pdf", "text": "原文片段"}],
                status="succeeded",
            )
            service.update_llm_interaction_cleanup(interaction_id, status="deleted")

            interactions = service.get_llm_interactions("file", "demo.pdf")

            self.assertEqual(len(interactions), 1)
            self.assertEqual(interactions[0]["prompt"], "提取文档字段")
            self.assertEqual(interactions[0]["response"], '{"summary":"摘要"}')
            self.assertEqual(interactions[0]["sources"][0]["text"], "原文片段")
            self.assertEqual(interactions[0]["workspace_cleanup_status"], "deleted")

    def test_completed_task_with_failed_callback_should_replay(self):
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            service.create_report_task(report_id=7, request_payload={"businessType": "report"})
            service.mark_business_completed("report", "7", {"details": "<div>ok</div>"}, status="1")
            service.mark_callback_failed("report", "7", "timeout")
            self.assertTrue(service.should_replay_callback("report", "7"))

    def test_atomic_audit_persists_main_attempts_and_lifecycle_events(self):
        """完整事务提交后才返回 succeeded 门禁结果，并保留全部审计明细。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")

            result = service.create_llm_interaction_with_trace(
                business_type="file",
                business_key="demo.pdf",
                prompt="提取文档字段",
                trace=_successful_trace(),
                status="succeeded",
            )

            self.assertEqual(result.audit_status, "succeeded")
            interactions = service.get_llm_interactions("file", "demo.pdf")
            attempts = service.get_llm_interaction_attempts(result.interaction_id)
            events = service.get_llm_interaction_lifecycle_events(result.interaction_id)
            self.assertEqual(len(interactions), 1)
            self.assertEqual(interactions[0]["workspace_slug"], "context-001")
            self.assertEqual(interactions[0]["thread_slug"], "conversation-001")
            self.assertEqual(interactions[0]["sources"][0]["document_ref"], "document:doc-001")
            self.assertEqual([item["sequence_no"] for item in attempts], [1])
            self.assertEqual(attempts[0]["operation"], "analyse")
            self.assertEqual([item["sequence_no"] for item in events], [1, 2])

    def test_atomic_audit_rolls_back_main_record_when_attempt_insert_fails(self):
        """模型调用明细失败时不得留下只有主记录的假成功审计。"""
        with workspace_tempdir() as tmp:
            db_path = f"{tmp}/tasks.sqlite3"
            service = LLMTaskService(db_path=db_path)
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TRIGGER reject_audit_attempt
                    BEFORE INSERT ON llm_interaction_attempts
                    BEGIN
                        SELECT RAISE(ABORT, 'forced attempt failure');
                    END
                    """
                )

            with self.assertRaises(InteractionAuditError):
                service.create_llm_interaction_with_trace(
                    business_type="file",
                    business_key="demo.pdf",
                    prompt="提取文档字段",
                    trace=_successful_trace(),
                    status="succeeded",
                )

            self.assertEqual(service.get_llm_interactions("file", "demo.pdf"), [])

    def test_atomic_audit_rolls_back_all_rows_when_lifecycle_insert_fails(self):
        """生命周期明细失败时主记录和已插入的 attempts 也必须整体回滚。"""
        with workspace_tempdir() as tmp:
            db_path = f"{tmp}/tasks.sqlite3"
            service = LLMTaskService(db_path=db_path)
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TRIGGER reject_audit_lifecycle
                    BEFORE INSERT ON llm_interaction_lifecycle_events
                    BEGIN
                        SELECT RAISE(ABORT, 'forced lifecycle failure');
                    END
                    """
                )

            with self.assertRaises(InteractionAuditError):
                service.create_llm_interaction_with_trace(
                    business_type="file",
                    business_key="demo.pdf",
                    prompt="提取文档字段",
                    trace=_successful_trace(),
                    status="succeeded",
                )

            self.assertEqual(service.get_llm_interactions("file", "demo.pdf"), [])
            with sqlite3.connect(db_path) as conn:
                attempts_count = conn.execute(
                    "SELECT COUNT(*) FROM llm_interaction_attempts"
                ).fetchone()[0]
            self.assertEqual(attempts_count, 0)

    @patch("app.services.llm_service.task_service.time.sleep", return_value=None)
    def test_atomic_audit_retries_only_transient_lock_and_then_succeeds(self, sleep_mock):
        """短暂锁冲突允许有限退避，锁释放后提交完整事务。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            original_connect = service._connect
            lock_failures = 0

            def flaky_connect(*, timeout_seconds=5.0):
                nonlocal lock_failures
                if timeout_seconds == 0.0 and lock_failures < 2:
                    lock_failures += 1
                    raise sqlite3.OperationalError("database is locked")
                return original_connect(timeout_seconds=timeout_seconds)

            with patch.object(service, "_connect", side_effect=flaky_connect):
                result = service.create_llm_interaction_with_trace(
                    business_type="file",
                    business_key="demo.pdf",
                    prompt="提取文档字段",
                    trace=_successful_trace(),
                    status="succeeded",
                )

            self.assertEqual(result.audit_status, "succeeded")
            self.assertEqual(lock_failures, 2)
            self.assertEqual(sleep_mock.call_count, 2)

    @patch("app.services.llm_service.task_service.time.sleep", return_value=None)
    def test_atomic_audit_lock_retry_exhaustion_returns_no_success(self, sleep_mock):
        """锁冲突达到硬上限后必须失败，不能返回审计成功凭据。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            connect_attempts = 0

            def always_locked(*, timeout_seconds=5.0):
                nonlocal connect_attempts
                connect_attempts += 1
                raise sqlite3.OperationalError("database is locked")

            with patch.object(service, "_connect", side_effect=always_locked):
                with self.assertRaisesRegex(InteractionAuditError, "重试已耗尽"):
                    service.create_llm_interaction_with_trace(
                        business_type="file",
                        business_key="demo.pdf",
                        prompt="提取文档字段",
                        trace=_successful_trace(),
                        status="succeeded",
                    )

            self.assertEqual(connect_attempts, 3)
            self.assertEqual(sleep_mock.call_count, 2)

    @patch("app.services.llm_service.task_service.time.sleep", return_value=None)
    def test_atomic_audit_does_not_retry_non_lock_operational_error(self, sleep_mock):
        """只读数据库等永久异常必须立即失败，避免无意义重试掩盖根因。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            connect_attempts = 0

            def read_only_error(*, timeout_seconds=5.0):
                nonlocal connect_attempts
                connect_attempts += 1
                raise sqlite3.OperationalError("attempt to write a readonly database")

            with patch.object(service, "_connect", side_effect=read_only_error):
                with self.assertRaises(InteractionAuditError):
                    service.create_llm_interaction_with_trace(
                        business_type="file",
                        business_key="demo.pdf",
                        prompt="提取文档字段",
                        trace=_successful_trace(),
                        status="succeeded",
                    )

            self.assertEqual(connect_attempts, 1)
            sleep_mock.assert_not_called()

    def test_preparation_failure_is_audited_without_fake_model_attempt(self):
        """准备阶段失败只记录生命周期事件，不伪造空的 RagAttempt。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            trace = RagExecutionTrace(
                context_name="analysis-demo",
                context_ref="context-001",
                conversation_ref=None,
                attempts=(),
                failure_stage="conversation_create",
                error_message="创建会话失败",
                lifecycle_events=(
                    RagLifecycleEvent(
                        sequence_no=1,
                        operation="context_create",
                        attempt=1,
                        success=True,
                        external_ref="context-001",
                        failure_stage=None,
                        error_message=None,
                    ),
                    RagLifecycleEvent(
                        sequence_no=2,
                        operation="conversation_create",
                        attempt=1,
                        success=False,
                        external_ref=None,
                        failure_stage="conversation_create",
                        error_message="创建会话失败",
                    ),
                ),
            )

            result = service.create_llm_interaction_with_trace(
                business_type="file",
                business_key="demo.pdf",
                prompt="提取文档字段",
                trace=trace,
                status="failed",
            )

            self.assertEqual(service.get_llm_interaction_attempts(result.interaction_id), [])
            events = service.get_llm_interaction_lifecycle_events(result.interaction_id)
            self.assertEqual([item["operation"] for item in events], [
                "context_create",
                "conversation_create",
            ])

    def test_lifecycle_append_is_idempotent_and_updates_cleanup_atomically(self):
        """重复提交相同关闭事件不产生重复行，清理状态保持同一确定结果。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            interaction_id = _create_audited_interaction(service)
            close_event = RagLifecycleEvent(
                sequence_no=3,
                operation="context_delete",
                attempt=1,
                success=True,
                external_ref="context-001",
                failure_stage=None,
                error_message=None,
            )

            first_inserted = service.append_llm_interaction_lifecycle_events(
                interaction_id,
                (close_event,),
                cleanup_status="deleted",
            )
            second_inserted = service.append_llm_interaction_lifecycle_events(
                interaction_id,
                (close_event,),
                cleanup_status="deleted",
            )

            self.assertEqual(first_inserted, 1)
            self.assertEqual(second_inserted, 0)
            events = service.get_llm_interaction_lifecycle_events(interaction_id)
            self.assertEqual([item["sequence_no"] for item in events], [1, 2, 3])
            interaction = service.get_llm_interactions("file", "demo.pdf")[0]
            self.assertEqual(interaction["workspace_cleanup_status"], "deleted")

    def test_lifecycle_append_rejects_sequence_gap_without_updating_cleanup(self):
        """关闭事件缺号时整次追加回滚，清理状态仍保持 pending。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            interaction_id = _create_audited_interaction(service)
            gap_event = RagLifecycleEvent(
                sequence_no=4,
                operation="context_delete",
                attempt=1,
                success=True,
                external_ref="context-001",
                failure_stage=None,
                error_message=None,
            )

            with self.assertRaisesRegex(ValueError, "序号缺口"):
                service.append_llm_interaction_lifecycle_events(
                    interaction_id,
                    (gap_event,),
                    cleanup_status="deleted",
                )

            interaction = service.get_llm_interactions("file", "demo.pdf")[0]
            self.assertEqual(interaction["workspace_cleanup_status"], "pending")
            events = service.get_llm_interaction_lifecycle_events(interaction_id)
            self.assertEqual([item["sequence_no"] for item in events], [1, 2])

    def test_lifecycle_append_rejects_conflicting_existing_sequence(self):
        """同序号不同内容属于审计冲突，禁止覆盖已经提交的历史。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            interaction_id = _create_audited_interaction(service)
            conflicting_event = RagLifecycleEvent(
                sequence_no=2,
                operation="document_upload",
                attempt=1,
                success=True,
                external_ref="document-001",
                failure_stage=None,
                error_message=None,
            )

            with self.assertRaisesRegex(ValueError, "序号冲突"):
                service.append_llm_interaction_lifecycle_events(
                    interaction_id,
                    (conflicting_event,),
                    cleanup_status="deleted",
                )

    def test_lifecycle_insert_failure_rolls_back_cleanup_status(self):
        """关闭事件写入失败时，清理状态不得单独提交形成不一致记录。"""
        with workspace_tempdir() as tmp:
            db_path = f"{tmp}/tasks.sqlite3"
            service = LLMTaskService(db_path=db_path)
            interaction_id = _create_audited_interaction(service)
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TRIGGER reject_close_lifecycle
                    BEFORE INSERT ON llm_interaction_lifecycle_events
                    WHEN NEW.sequence_no >= 3
                    BEGIN
                        SELECT RAISE(ABORT, 'forced close failure');
                    END
                    """
                )
            close_event = RagLifecycleEvent(
                sequence_no=3,
                operation="context_delete",
                attempt=1,
                success=True,
                external_ref="context-001",
                failure_stage=None,
                error_message=None,
            )

            with self.assertRaises(InteractionAuditError):
                service.append_llm_interaction_lifecycle_events(
                    interaction_id,
                    (close_event,),
                    cleanup_status="deleted",
                )

            interaction = service.get_llm_interactions("file", "demo.pdf")[0]
            self.assertEqual(interaction["workspace_cleanup_status"], "pending")

    def test_callback_skipped_is_idempotent_and_does_not_count_attempt(self):
        """未配置回调是零次尝试的明确终态，重复标记不会改变计数。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            service.create_file_task("demo.pdf", {"businessType": "file"})
            service.mark_business_result(
                "file",
                "demo.pdf",
                {"status": "2"},
                status="2",
                message="解析完成",
            )

            self.assertTrue(service.mark_callback_skipped("file", "demo.pdf"))
            self.assertTrue(service.mark_callback_skipped("file", "demo.pdf"))

            task = service.get_task("file", "demo.pdf")
            self.assertEqual(task["callback_status"], "skipped")
            self.assertEqual(task["callback_attempts"], 0)
            self.assertFalse(service.should_replay_callback("file", "demo.pdf"))

    def test_callback_skipped_does_not_override_actual_callback_result(self):
        """真实成功或失败结果一旦产生，skipped 不得覆盖审计事实。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            service.create_file_task("success.pdf", {"businessType": "file"})
            service.mark_callback_success("file", "success.pdf")
            service.create_file_task("failed.pdf", {"businessType": "file"})
            service.mark_callback_failed("file", "failed.pdf", "timeout")

            self.assertFalse(service.mark_callback_skipped("file", "success.pdf"))
            self.assertFalse(service.mark_callback_skipped("file", "failed.pdf"))

            self.assertEqual(
                service.get_task("file", "success.pdf")["callback_status"],
                "success",
            )
            self.assertEqual(
                service.get_task("file", "failed.pdf")["callback_status"],
                "failed",
            )
