import hashlib
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
                prompt_digest=hashlib.sha256(
                    "提取文档字段".encode("utf-8")
                ).hexdigest(),
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
    task = service.get_task("file", "demo.pdf") or service.create_file_task(
        "demo.pdf",
        {"businessType": "file"},
    )
    result = service.create_llm_interaction_with_trace(
        business_type="file",
        business_key="demo.pdf",
        execution_id=task["execution_id"],
        prompt="提取文档字段",
        trace=_successful_trace(),
        status="succeeded",
    )
    return result.interaction_id


class LLMTaskServiceTests(unittest.TestCase):
    def test_create_file_task_defaults_to_processing(self):
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task = service.create_file_task(
                file_name="demo.pdf",
                request_payload={"businessType": "file"},
            )
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

    def test_recreating_completed_task_generates_new_execution_id(self):
        """同一业务键主动重跑必须获得新执行身份，不能复用上一轮审计幂等键。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            first = service.create_file_task("demo.pdf", {"businessType": "file"})
            service.mark_business_result(
                "file", "demo.pdf", {"status": "2"}, status="2"
            )
            second = service.create_file_task("demo.pdf", {"businessType": "file"})

            self.assertNotEqual(first["execution_id"], second["execution_id"])

    def test_weaponry_task_persists_explicit_document_snapshot(self):
        """显式选文快照必须与对外原始请求分离，并按执行身份读取。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            payload = {
                "businessType": "weaponry",
                "params": {
                    "architectureId": 1001,
                    "filePathList": ["https://host/download/cross-category.pdf"],
                },
            }
            task = service.create_weaponry_task(
                1001,
                payload,
                selected_documents=(
                    {
                        "file_name": "cross-category.pdf",
                        "original_name": "跨分类来源.pdf",
                        "ingested_file_name": "cross-category.mhtml.normalized.pdf",
                        "source_architecture_id": 2002,
                        "doc_path": "custom-documents/cross-category.json",
                        "anything_doc_id": "doc-2002",
                    },
                ),
            )

            snapshots = service.get_weaponry_task_document_snapshots(
                architecture_id=1001,
                execution_id=task["execution_id"],
            )

        self.assertEqual(payload["params"]["filePathList"], ["https://host/download/cross-category.pdf"])
        self.assertEqual(snapshots[0]["file_name"], "cross-category.pdf")
        self.assertEqual(
            snapshots[0]["ingested_file_name"],
            "cross-category.mhtml.normalized.pdf",
        )
        self.assertEqual(snapshots[0]["source_architecture_id"], 2002)
        self.assertEqual(snapshots[0]["doc_path"], "custom-documents/cross-category.json")

    def test_weaponry_task_reissue_replaces_old_execution_document_snapshot(self):
        """同一类别重跑不能让旧 execution_id 读取到新任务的选文范围。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            first = service.create_weaponry_task(
                1001,
                {"businessType": "weaponry"},
                selected_documents=(
                    {
                        "file_name": "first.pdf",
                        "original_name": "first.pdf",
                        "ingested_file_name": "first.pdf",
                        "source_architecture_id": 1001,
                        "doc_path": "custom-documents/first.json",
                    },
                ),
            )
            second = service.create_weaponry_task(
                1001,
                {"businessType": "weaponry"},
                selected_documents=(
                    {
                        "file_name": "second.pdf",
                        "original_name": "second.pdf",
                        "ingested_file_name": "second.pdf",
                        "source_architecture_id": 2002,
                        "doc_path": "custom-documents/second.json",
                    },
                ),
            )

            old_snapshots = service.get_weaponry_task_document_snapshots(
                architecture_id=1001,
                execution_id=first["execution_id"],
            )
            current_snapshots = service.get_weaponry_task_document_snapshots(
                architecture_id=1001,
                execution_id=second["execution_id"],
            )

        self.assertNotEqual(first["execution_id"], second["execution_id"])
        self.assertEqual(old_snapshots, [])
        self.assertEqual([item["file_name"] for item in current_snapshots], ["second.pdf"])
        self.assertEqual(
            [item["ingested_file_name"] for item in current_snapshots],
            ["second.pdf"],
        )

    def test_legacy_task_database_is_migrated_with_audit_version_marker(self):
        """旧任务库升级后应为历史任务和交互补充可区分的执行与审计版本。"""
        with workspace_tempdir() as tmp:
            db_path = f"{tmp}/tasks.sqlite3"
            with sqlite3.connect(db_path) as conn:
                conn.execute(
                    """
                    CREATE TABLE llm_tasks (
                        business_type TEXT NOT NULL,
                        business_key TEXT NOT NULL,
                        request_payload TEXT NOT NULL,
                        status TEXT NOT NULL,
                        progress REAL NOT NULL DEFAULT 0,
                        message TEXT NOT NULL DEFAULT '',
                        result_payload TEXT,
                        callback_status TEXT NOT NULL DEFAULT 'pending',
                        callback_attempts INTEGER NOT NULL DEFAULT 0,
                        last_callback_error TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (business_type, business_key)
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE llm_interactions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        business_type TEXT NOT NULL,
                        business_key TEXT NOT NULL,
                        workspace_name TEXT NOT NULL DEFAULT '',
                        workspace_slug TEXT NOT NULL DEFAULT '',
                        thread_slug TEXT NOT NULL DEFAULT '',
                        prompt TEXT NOT NULL DEFAULT '',
                        response TEXT,
                        sources_json TEXT NOT NULL DEFAULT '[]',
                        status TEXT NOT NULL,
                        error_message TEXT NOT NULL DEFAULT '',
                        workspace_cleanup_status TEXT NOT NULL DEFAULT 'pending',
                        workspace_cleanup_error TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        completed_at TEXT
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO llm_tasks (
                        business_type, business_key, request_payload, status,
                        created_at, updated_at
                    ) VALUES ('file', 'legacy.pdf', '{}', '2', 'now', 'now')
                    """
                )
                conn.execute(
                    """
                    INSERT INTO llm_interactions (
                        business_type, business_key, status, created_at
                    ) VALUES ('file', 'legacy.pdf', 'succeeded', 'now')
                    """
                )

            service = LLMTaskService(db_path=db_path)
            task = service.get_task("file", "legacy.pdf")
            interaction = service.get_llm_interactions("file", "legacy.pdf")[0]
            self.assertTrue(task["execution_id"].startswith("legacy-task:"))
            self.assertTrue(
                interaction["execution_id"].startswith("legacy-interaction:")
            )
            self.assertEqual(interaction["audit_schema_version"], 1)

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
            task = service.create_file_task("demo.pdf", {"businessType": "file"})

            result = service.create_llm_interaction_with_trace(
                business_type="file",
                business_key="demo.pdf",
                execution_id=task["execution_id"],
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
            self.assertEqual(attempts[0]["query_mode"], "query")
            self.assertEqual(attempts[0]["source_marker_status"], "matched")
            self.assertEqual([item["sequence_no"] for item in events], [1, 2])
            self.assertEqual(interactions[0]["audit_schema_version"], 2)

    def test_atomic_audit_uses_same_canonical_prompt_as_rag_attempt(self):
        """首尾换行和 CRLF 不得再次制造主审计与实际模型调用摘要不一致。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task = service.create_file_task("demo.pdf", {"businessType": "file"})

            result = service.create_llm_interaction_with_trace(
                business_type="file",
                business_key="demo.pdf",
                execution_id=task["execution_id"],
                prompt="\r\n提取文档字段\r\n",
                trace=_successful_trace(),
                status="succeeded",
            )

            interaction = service.get_llm_interactions("file", "demo.pdf")[0]
            self.assertEqual(result.audit_status, "succeeded")
            self.assertEqual(interaction["prompt"], "提取文档字段")

    def test_atomic_audit_reuses_same_execution_and_rejects_conflicting_trace(self):
        """相同执行的完全一致重放幂等复用，内容变化则拒绝覆盖。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task = service.create_file_task("demo.pdf", {"businessType": "file"})
            arguments = {
                "business_type": "file",
                "business_key": "demo.pdf",
                "execution_id": task["execution_id"],
                "prompt": "提取文档字段",
                "trace": _successful_trace(),
                "status": "succeeded",
            }

            first = service.create_llm_interaction_with_trace(**arguments)
            second = service.create_llm_interaction_with_trace(**arguments)

            self.assertTrue(first.created)
            self.assertTrue(second.reused)
            self.assertEqual(first.interaction_id, second.interaction_id)
            self.assertEqual(
                len(service.get_llm_interactions("file", "demo.pdf")),
                1,
            )

            with self.assertRaisesRegex(InteractionAuditError, "内容发生冲突"):
                service.create_llm_interaction_with_trace(
                    **{**arguments, "status": "failed", "error_message": "业务失败"}
                )

    def test_atomic_audit_rejects_main_prompt_from_different_attempt(self):
        """主表 Prompt 与最终模型调用不对应时必须在获取数据库写锁前失败。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task = service.create_file_task("demo.pdf", {"businessType": "file"})

            with self.assertRaisesRegex(ValueError, "最后一次 RagAttempt"):
                service.create_llm_interaction_with_trace(
                    business_type="file",
                    business_key="demo.pdf",
                    execution_id=task["execution_id"],
                    prompt="另一份提示词",
                    trace=_successful_trace(),
                    status="succeeded",
                )

            self.assertEqual(service.get_llm_interactions("file", "demo.pdf"), [])

    def test_atomic_audit_rolls_back_main_record_when_attempt_insert_fails(self):
        """模型调用明细失败时不得留下只有主记录的假成功审计。"""
        with workspace_tempdir() as tmp:
            db_path = f"{tmp}/tasks.sqlite3"
            service = LLMTaskService(db_path=db_path)
            task = service.create_file_task("demo.pdf", {"businessType": "file"})
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
                    execution_id=task["execution_id"],
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
            task = service.create_file_task("demo.pdf", {"businessType": "file"})
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
                    execution_id=task["execution_id"],
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

    @patch(
        "app.services.llm_service.interaction_audit_service.time.sleep",
        return_value=None,
    )
    def test_atomic_audit_retries_only_transient_lock_and_then_succeeds(self, sleep_mock):
        """短暂锁冲突允许有限退避，锁释放后提交完整事务。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task = service.create_file_task("demo.pdf", {"businessType": "file"})
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
                    execution_id=task["execution_id"],
                    prompt="提取文档字段",
                    trace=_successful_trace(),
                    status="succeeded",
                )

            self.assertEqual(result.audit_status, "succeeded")
            self.assertEqual(lock_failures, 2)
            self.assertEqual(sleep_mock.call_count, 2)

    def test_atomic_audit_recovers_from_real_sqlite_write_lock(self):
        """真实 BEGIN IMMEDIATE 写锁释放后，审计应在有限重试内成功。"""
        with workspace_tempdir() as tmp:
            db_path = f"{tmp}/tasks.sqlite3"
            service = LLMTaskService(db_path=db_path)
            task = service.create_file_task("demo.pdf", {"businessType": "file"})
            lock_connection = sqlite3.connect(db_path, timeout=0)
            lock_connection.execute("BEGIN IMMEDIATE")
            released = False

            def release_lock(_delay: float) -> None:
                nonlocal released
                if not released:
                    lock_connection.rollback()
                    released = True

            try:
                with patch(
                    "app.services.llm_service.interaction_audit_service.time.sleep",
                    side_effect=release_lock,
                ):
                    result = service.create_llm_interaction_with_trace(
                        business_type="file",
                        business_key="demo.pdf",
                        execution_id=task["execution_id"],
                        prompt="提取文档字段",
                        trace=_successful_trace(),
                        status="succeeded",
                    )
            finally:
                lock_connection.close()

            self.assertTrue(released)
            self.assertEqual(result.audit_status, "succeeded")

    @patch(
        "app.services.llm_service.interaction_audit_service.time.sleep",
        return_value=None,
    )
    def test_atomic_audit_lock_retry_exhaustion_returns_no_success(self, sleep_mock):
        """锁冲突达到硬上限后必须失败，不能返回审计成功凭据。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task = service.create_file_task("demo.pdf", {"businessType": "file"})
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
                        execution_id=task["execution_id"],
                        prompt="提取文档字段",
                        trace=_successful_trace(),
                        status="succeeded",
                    )

            self.assertEqual(connect_attempts, 5)
            self.assertEqual(sleep_mock.call_count, 4)

    @patch(
        "app.services.llm_service.interaction_audit_service.time.sleep",
        return_value=None,
    )
    def test_atomic_audit_does_not_retry_non_lock_operational_error(self, sleep_mock):
        """只读数据库等永久异常必须立即失败，避免无意义重试掩盖根因。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task = service.create_file_task("demo.pdf", {"businessType": "file"})
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
                        execution_id=task["execution_id"],
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
            task = service.create_file_task("demo.pdf", {"businessType": "file"})
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
                execution_id=task["execution_id"],
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
                # append 接口只接受包含删除事件的关闭批次。这里使用合法的
                # context_delete，确保测试越过批次前置校验，真正命中“已存在
                # sequence_no=2 但内容不同”的审计冲突分支。
                operation="context_delete",
                attempt=1,
                success=True,
                external_ref="context-001",
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

    def test_empty_callback_replay_migrates_historical_pending_to_skipped(self):
        """补偿入口遇到空配置时应修正历史 pending，且不得制造回调尝试。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            service.create_file_task("demo.pdf", {"businessType": "file"})
            service.mark_business_result(
                "file", "demo.pdf", {"status": "2"}, status="2"
            )

            replayed = service.replay_callback_if_needed(
                "file",
                "demo.pdf",
                callback_url="   ",
                timeout=5,
            )

            task = service.get_task("file", "demo.pdf")
            self.assertFalse(replayed)
            self.assertEqual(task["callback_status"], "skipped")
            self.assertEqual(task["callback_attempts"], 0)

    def test_callback_skipped_does_not_override_actual_callback_result(self):
        """真实成功或失败结果一旦产生，skipped 不得覆盖审计事实。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            service.create_file_task("success.pdf", {"businessType": "file"})
            service.mark_business_result(
                "file", "success.pdf", {"status": "2"}, status="2"
            )
            service.mark_callback_success("file", "success.pdf")
            service.create_file_task("failed.pdf", {"businessType": "file"})
            service.mark_business_result(
                "file", "failed.pdf", {"status": "3"}, status="3"
            )
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

    def test_callback_state_machine_rejects_non_terminal_and_terminal_overwrite(self):
        """回调状态只能沿合法方向转换，处理中任务和既有终态不得被覆盖。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            service.create_file_task("processing.pdf", {"businessType": "file"})
            with self.assertRaisesRegex(ValueError, "任务尚未完成"):
                service.mark_callback_skipped("file", "processing.pdf")

            service.create_file_task("success.pdf", {"businessType": "file"})
            service.mark_business_result(
                "file", "success.pdf", {"status": "2"}, status="2"
            )
            service.mark_callback_success("file", "success.pdf")
            with self.assertRaisesRegex(ValueError, "非法回调状态转换"):
                service.mark_callback_failed("file", "success.pdf", "late failure")

            service.create_file_task("skipped.pdf", {"businessType": "file"})
            service.mark_business_result(
                "file", "skipped.pdf", {"status": "2"}, status="2"
            )
            service.mark_callback_skipped("file", "skipped.pdf")
            with self.assertRaisesRegex(ValueError, "非法回调状态转换"):
                service.mark_callback_success("file", "skipped.pdf")

    def test_rag_resource_lease_survives_audit_failure_for_recovery(self):
        """审计失败时外部资源引用必须保留在独立租约中供巡检。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            leases = service.rag_resource_leases
            leases.begin(
                execution_id="execution-lease",
                business_type="file",
                business_key="lease.pdf",
            )
            leases.record_resources(
                execution_id="execution-lease",
                context_ref="context:1",
                conversation_ref="conversation:1",
                document_ref="document:1",
                external_location="custom-documents/lease.pdf.json",
            )
            leases.mark_audit_result(
                execution_id="execution-lease",
                interaction_id=None,
                error_message="audit database unavailable",
            )

            open_leases = leases.list_open()
            self.assertEqual(1, len(open_leases))
            self.assertEqual("audit_failed", open_leases[0].status)
            self.assertEqual("document:1", open_leases[0].document_ref)

            leases.mark_closed(
                execution_id="execution-lease",
                manual_recovery=True,
            )
            self.assertEqual([], leases.list_open())
