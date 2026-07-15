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
    ArchitectureRecallAuditError,
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


def _recall_decision_args(execution_id: str) -> dict:
    """构造包含 64 位领域 ID 的最小召回审计输入。"""
    first_id = 1_778_670_713_864_013
    second_id = 1_778_670_713_864_014
    return {
        "execution_id": execution_id,
        "tree_fingerprint": "a" * 64,
        "query_digest": "b" * 64,
        "base_top64": [first_id, second_id],
        "final_candidates": [
            {
                "id": first_id,
                "pathName": "装备领域/CVN-78/基础数据",
                "nodeType": "leaf",
            },
            {
                "id": second_id,
                "pathName": "装备领域/CVN-78/战技指标",
                "nodeType": "leaf",
                "remark": "用于模型判别的短备注",
            },
        ],
        "channel_rankings": {
            "exact": [first_id],
            "lexical": [second_id, first_id],
            "tree": [first_id, second_id],
            "rule": [],
        },
        "rrf_scores": {first_id: 0.043, second_id: 0.039},
        "protected_reasons": {first_id: ["exact:model_alias"]},
        "prompt_chars": 2400,
        "recall_elapsed_ms": 18,
    }


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

    def test_legacy_task_database_incrementally_creates_recall_audit_table(self):
        """旧任务库升级只增建召回表，且不以外键级联删除历史 execution。"""
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
                    INSERT INTO llm_tasks (
                        business_type, business_key, request_payload, status,
                        created_at, updated_at
                    ) VALUES ('file', 'legacy-recall.pdf', '{}', '1', 'now', 'now')
                    """
                )

            service = LLMTaskService(db_path=db_path)
            task = service.get_task("file", "legacy-recall.pdf")
            result = service.upsert_architecture_recall_decision(
                **_recall_decision_args(task["execution_id"])
            )
            with sqlite3.connect(db_path) as conn:
                columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(llm_architecture_recall_decisions)"
                    ).fetchall()
                }
                foreign_keys = conn.execute(
                    "PRAGMA foreign_key_list(llm_architecture_recall_decisions)"
                ).fetchall()

            self.assertTrue(result.created)
            self.assertIn("tree_fingerprint", columns)
            self.assertIn("finalization_digest", columns)
            self.assertEqual(foreign_keys, [])

    def test_recall_audit_upsert_finalize_and_get_preserve_initial_decision(self):
        """终结只补结果字段，模型候选、排名、分数和保护原因必须保持原值。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task = service.create_file_task("ford.pdf", {"businessType": "file"})
            decision_args = _recall_decision_args(task["execution_id"])

            initial_result = service.upsert_architecture_recall_decision(
                **decision_args
            )
            initial_record = service.get_architecture_recall_decision(
                task["execution_id"]
            )
            finalize_result = service.finalize_architecture_recall_decision(
                execution_id=task["execution_id"],
                returned_architecture_id=decision_args["base_top64"][1],
                returned_rank=2,
                total_elapsed_ms=240,
            )
            final_record = service.get_architecture_recall_decision(
                task["execution_id"]
            )

            self.assertTrue(initial_result.created)
            self.assertFalse(initial_result.finalized)
            self.assertTrue(finalize_result.created)
            self.assertTrue(finalize_result.finalized)
            for field_name in (
                "tree_fingerprint",
                "query_digest",
                "base_top64",
                "final_candidates",
                "channel_rankings",
                "rrf_scores",
                "protected_reasons",
                "prompt_chars",
                "recall_elapsed_ms",
                "created_at",
            ):
                self.assertEqual(initial_record[field_name], final_record[field_name])
            self.assertEqual(
                final_record["returned_architecture_id"],
                decision_args["base_top64"][1],
            )
            self.assertEqual(final_record["returned_rank"], 2)
            self.assertEqual(final_record["total_elapsed_ms"], 240)
            self.assertTrue(final_record["finalized"])

    def test_recall_audit_is_idempotent_and_rejects_conflicting_replays(self):
        """同 execution 同内容可重放，不同初始或终结内容必须稳定报冲突。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task = service.create_file_task("nimitz.pdf", {"businessType": "file"})
            decision_args = _recall_decision_args(task["execution_id"])

            service.upsert_architecture_recall_decision(**decision_args)
            replay = service.upsert_architecture_recall_decision(**decision_args)
            conflicting_args = dict(decision_args)
            conflicting_args["prompt_chars"] = decision_args["prompt_chars"] + 1
            with self.assertRaisesRegex(
                ArchitectureRecallAuditError,
                "初始决策发生幂等冲突",
            ):
                service.upsert_architecture_recall_decision(**conflicting_args)

            first_finalize = service.finalize_architecture_recall_decision(
                execution_id=task["execution_id"],
                returned_architecture_id=decision_args["base_top64"][0],
                returned_rank=1,
                total_elapsed_ms=120,
            )
            finalize_replay = service.finalize_architecture_recall_decision(
                execution_id=task["execution_id"],
                returned_architecture_id=decision_args["base_top64"][0],
                returned_rank=1,
                total_elapsed_ms=120,
            )
            with self.assertRaisesRegex(
                ArchitectureRecallAuditError,
                "终结结果发生幂等冲突",
            ):
                service.finalize_architecture_recall_decision(
                    execution_id=task["execution_id"],
                    returned_architecture_id=decision_args["base_top64"][0],
                    returned_rank=1,
                    total_elapsed_ms=121,
                )

            self.assertTrue(replay.reused)
            self.assertTrue(first_finalize.created)
            self.assertTrue(finalize_replay.reused)

    def test_recall_audit_validates_candidate_quantity_and_text_limits(self):
        """Top-64、最终 128 候选及模型投影文本均受严格持久化上限约束。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task = service.create_file_task("limits.pdf", {"businessType": "file"})
            decision_args = _recall_decision_args(task["execution_id"])

            too_many_base = dict(decision_args)
            too_many_base["base_top64"] = list(range(1, 66))
            too_many_base["final_candidates"] = [
                {"id": node_id, "pathName": f"领域/{node_id}", "nodeType": "leaf"}
                for node_id in range(1, 66)
            ]
            with self.assertRaisesRegex(ValueError, "base_top64数量超出上限64"):
                service.upsert_architecture_recall_decision(**too_many_base)

            too_many_final = dict(decision_args)
            too_many_final["base_top64"] = []
            too_many_final["final_candidates"] = [
                {"id": node_id, "pathName": f"领域/{node_id}", "nodeType": "leaf"}
                for node_id in range(1, 130)
            ]
            too_many_final["protected_reasons"] = {}
            with self.assertRaisesRegex(ValueError, "final_candidates数量超出上限128"):
                service.upsert_architecture_recall_decision(**too_many_final)

            oversized_remark = dict(decision_args)
            oversized_remark["final_candidates"] = [
                dict(decision_args["final_candidates"][0], remark="x" * 513),
                decision_args["final_candidates"][1],
            ]
            with self.assertRaisesRegex(ValueError, "remark超出长度上限"):
                service.upsert_architecture_recall_decision(**oversized_remark)

            unicode_numeric_id = dict(decision_args)
            unicode_numeric_id["final_candidates"] = [
                dict(decision_args["final_candidates"][0], id="１２３"),
                decision_args["final_candidates"][1],
            ]
            with self.assertRaisesRegex(
                ValueError,
                r"final_candidates\[0\]\.id必须是正整数",
            ):
                service.upsert_architecture_recall_decision(**unicode_numeric_id)

            invalid_node_type = dict(decision_args)
            invalid_node_type["final_candidates"] = [
                dict(decision_args["final_candidates"][0], nodeType="branch"),
                decision_args["final_candidates"][1],
            ]
            with self.assertRaisesRegex(ValueError, "nodeType只能是leaf或parent"):
                service.upsert_architecture_recall_decision(**invalid_node_type)

            with self.assertRaisesRegex(ValueError, "error_message超出"):
                service.finalize_architecture_recall_decision(
                    execution_id=task["execution_id"],
                    returned_architecture_id=None,
                    returned_rank=None,
                    total_elapsed_ms=20,
                    failure_stage="architecture_recall",
                    error_message="x" * 4097,
                )

    @patch(
        "app.services.llm_service.interaction_audit_service.time.sleep",
        return_value=None,
    )
    def test_recall_audit_reuses_bounded_sqlite_lock_retry(self, sleep_mock):
        """召回审计与交互审计共用有限 SQLite 锁重试执行器。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task = service.create_file_task("locked.pdf", {"businessType": "file"})
            decision_args = _recall_decision_args(task["execution_id"])
            original_connect = service._connect
            lock_failures = 0

            def flaky_connect(*, timeout_seconds=5.0):
                nonlocal lock_failures
                if timeout_seconds == 0.0 and lock_failures < 2:
                    lock_failures += 1
                    raise sqlite3.OperationalError("database is locked")
                return original_connect(timeout_seconds=timeout_seconds)

            with patch.object(service, "_connect", side_effect=flaky_connect):
                result = service.upsert_architecture_recall_decision(**decision_args)

            self.assertTrue(result.created)
            self.assertEqual(lock_failures, 2)
            self.assertEqual(sleep_mock.call_count, 2)

    def test_recall_audit_history_survives_task_execution_replacement(self):
        """同文件重跑替换当前 execution 后，旧召回记录仍必须可读取。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            first = service.create_file_task("rerun.pdf", {"businessType": "file"})
            service.upsert_architecture_recall_decision(
                **_recall_decision_args(first["execution_id"])
            )
            service.finalize_architecture_recall_decision(
                execution_id=first["execution_id"],
                returned_architecture_id=1_778_670_713_864_013,
                returned_rank=1,
                total_elapsed_ms=90,
            )

            second = service.create_file_task("rerun.pdf", {"businessType": "file"})
            historical = service.get_architecture_recall_decision(
                first["execution_id"]
            )

            self.assertNotEqual(first["execution_id"], second["execution_id"])
            self.assertIsNotNone(historical)
            self.assertTrue(historical["finalized"])
            with self.assertRaisesRegex(
                ArchitectureRecallAuditError,
                "execution不存在或已被新执行替换",
            ):
                service.finalize_architecture_recall_decision(
                    execution_id=first["execution_id"],
                    returned_architecture_id=1_778_670_713_864_013,
                    returned_rank=1,
                    total_elapsed_ms=90,
                )

    def test_recall_audit_requires_current_execution_and_initial_record(self):
        """不存在 execution 或未先落初始决策时必须以稳定错误失败。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            with self.assertRaisesRegex(
                ArchitectureRecallAuditError,
                "对应execution不存在",
            ):
                service.upsert_architecture_recall_decision(
                    **_recall_decision_args("missing-execution")
                )

            task = service.create_file_task("missing-initial.pdf", {"businessType": "file"})
            with self.assertRaisesRegex(
                ArchitectureRecallAuditError,
                "缺少初始召回决策",
            ):
                service.finalize_architecture_recall_decision(
                    execution_id=task["execution_id"],
                    returned_architecture_id=None,
                    returned_rank=None,
                    total_elapsed_ms=5,
                    failure_stage="architecture_recall",
                    error_message="没有有效召回信号",
                )

    def test_recall_audit_validates_failure_stage_and_records_index_failure(self):
        """稳定阶段仅允许约定枚举，索引失败可在无树指纹和无候选时留痕。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task = service.create_file_task("bad-tree.pdf", {"businessType": "file"})
            empty_decision = _recall_decision_args(task["execution_id"])
            empty_decision.update(
                {
                    "tree_fingerprint": "",
                    "base_top64": [],
                    "final_candidates": [],
                    "channel_rankings": {},
                    "rrf_scores": {},
                    "protected_reasons": {},
                    "prompt_chars": 0,
                    "recall_elapsed_ms": 3,
                }
            )
            service.upsert_architecture_recall_decision(**empty_decision)

            with self.assertRaisesRegex(ValueError, "稳定失败阶段"):
                service.finalize_architecture_recall_decision(
                    execution_id=task["execution_id"],
                    returned_architecture_id=None,
                    returned_rank=None,
                    total_elapsed_ms=5,
                    failure_stage="unknown_stage",
                    error_message="bad tree",
                )

            result = service.finalize_architecture_recall_decision(
                execution_id=task["execution_id"],
                returned_architecture_id=None,
                returned_rank=None,
                total_elapsed_ms=5,
                failure_stage="architecture_index",
                error_message="领域树存在环",
            )
            record = service.get_architecture_recall_decision(task["execution_id"])

            self.assertTrue(result.finalized)
            self.assertEqual(record["tree_fingerprint"], "")
            self.assertEqual(record["failure_stage"], "architecture_index")
            self.assertEqual(record["error_message"], "领域树存在环")

    def test_recall_audit_records_prompt_chars_above_runtime_budget(self):
        """32K 是模型发送门禁，审计仍须保存实际超限值和预算失败阶段。"""
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task = service.create_file_task("oversized-prompt.pdf", {"businessType": "file"})
            decision_args = _recall_decision_args(task["execution_id"])
            decision_args["prompt_chars"] = 32_001

            service.upsert_architecture_recall_decision(**decision_args)
            service.finalize_architecture_recall_decision(
                execution_id=task["execution_id"],
                returned_architecture_id=None,
                returned_rank=None,
                total_elapsed_ms=30,
                failure_stage="architecture_prompt_budget",
                error_message="分类Prompt超过32000字符发送门禁",
            )
            record = service.get_architecture_recall_decision(task["execution_id"])

            self.assertEqual(record["prompt_chars"], 32_001)
            self.assertEqual(record["failure_stage"], "architecture_prompt_budget")

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
