"""阶段 2-6 步骤 3：配置所有权与 Analysis v5 Profile 工厂。"""

from __future__ import annotations

from pathlib import Path
import sqlite3
import unittest

from app.modules.analysis.adapters.execution_profile_factory import (
    KNOWLEDGE_PROTOCOL_VERSION,
    RAG_PROVIDER_ID,
    build_analysis_execution_profile,
)
from app.modules.analysis.adapters.runtime_config import (
    AnalysisExecutionCapabilityConfig,
    AnalysisInfrastructureConfigurationError,
    load_analysis_execution_capability_config,
)
from app.modules.analysis.adapters.sqlite import (
    SQLiteAnalysisAuditPersistence,
    bootstrap_analysis_task_control_database,
)
from app.modules.tasks.adapters.sqlite import (
    SQLiteConnectionFactory,
    SQLiteTransactionManager,
    build_sqlite_task_control_uow_factories,
)
from app.modules.tasks.domain import TaskBatchRef, TaskBusinessRef, TaskId
from app.modules.tasks.ports import (
    TaskAdmissionOutcome,
    TaskAdmissionRequest,
)
from app.services.llm_service.interaction_audit_service import InteractionAuditResult
from tests import workspace_tempdir


class _Files:
    source_transport_profile_id = "fake-source-v1"
    max_download_bytes = 123456
    rag_projection_profile_id = "3" * 64


class _InteractionAuditStoreFake:
    """只模拟共享审计边界，不把测试事实写入旧 Task Service。"""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.lifecycle: list[tuple[tuple, dict]] = []

    def create_llm_interaction_with_trace(self, **kwargs):
        self.created.append(dict(kwargs))
        return InteractionAuditResult(interaction_id=7)

    def get_llm_interaction_by_execution(self, *args):
        return {
            "id": 7,
            "execution_id": args[2],
            "audit_idempotency_key": args[3],
        }

    def append_llm_interaction_lifecycle_events(self, *args, **kwargs):
        self.lifecycle.append((args, dict(kwargs)))
        return 1


class AnalysisInfrastructureMigrationTests(unittest.TestCase):
    def test_capability_loader_requires_two_strict_sha256_values(self) -> None:
        config = load_analysis_execution_capability_config(
            {
                "DOCSENSE_ANALYSIS_RAG_PROVIDER_FINGERPRINT": "A" * 64,
                "DOCSENSE_ANALYSIS_RAG_MODEL_FINGERPRINT": "b" * 64,
            }
        )
        self.assertEqual("a" * 64, config.rag_provider_fingerprint)
        self.assertEqual("b" * 64, config.rag_model_fingerprint)

        for environment in (
            {},
            {"DOCSENSE_ANALYSIS_RAG_PROVIDER_FINGERPRINT": "a" * 64},
            {
                "DOCSENSE_ANALYSIS_RAG_PROVIDER_FINGERPRINT": "not-a-digest",
                "DOCSENSE_ANALYSIS_RAG_MODEL_FINGERPRINT": "b" * 64,
            },
        ):
            with self.subTest(environment=environment):
                with self.assertRaises(AnalysisInfrastructureConfigurationError):
                    load_analysis_execution_capability_config(environment)

    def test_profile_uses_actual_file_identity_and_reuses_provider_for_knowledge(self) -> None:
        capabilities = AnalysisExecutionCapabilityConfig("1" * 64, "2" * 64)
        profile = build_analysis_execution_profile(
            capabilities=capabilities,
            files=_Files(),
        )
        self.assertEqual("fake-source-v1", profile.source_transport_profile_id)
        self.assertEqual(123456, profile.max_download_bytes)
        self.assertEqual("3" * 64, profile.rag_projection_profile_id)
        self.assertEqual(RAG_PROVIDER_ID, profile.rag_provider_id)
        self.assertEqual("1" * 64, profile.rag_provider_fingerprint)
        self.assertEqual("1" * 64, profile.knowledge_provider_fingerprint)
        self.assertEqual(KNOWLEDGE_PROTOCOL_VERSION, profile.knowledge_protocol_version)

    def test_core_config_no_longer_owns_analysis_runtime_symbols(self) -> None:
        source = (
            Path(__file__).parents[1] / "app/services/core/config.py"
        ).read_text(encoding="utf-8")
        for symbol in (
            "class AnalysisClassificationConfig",
            "class AnalysisInfrastructureConfig",
            "def load_analysis_classification_config",
            "def load_analysis_infrastructure_config",
        ):
            self.assertNotIn(symbol, source)

    def test_v2_audit_persistence_owns_component_facts_without_legacy_service(self) -> None:
        """Recall/交互引用进入组件库，共享交互正文仍只委托唯一 Writer。"""

        with workspace_tempdir() as temporary_root:
            root = Path(temporary_root)
            old_path = root / "old.sqlite3"
            control_path = root / "task-control.sqlite3"
            sqlite3.connect(old_path).close()
            bootstrap = bootstrap_analysis_task_control_database(old_path, control_path)
            manager = SQLiteTransactionManager(
                SQLiteConnectionFactory(bootstrap, busy_timeout_ms=100)
            )
            request = TaskAdmissionRequest(
                task_id=TaskId("analysis-audit-v2-task"),
                task_type="file",
                business_ref=TaskBusinessRef("file", "audit-v2.txt"),
                input_schema_version=5,
                input_snapshot=("audit-v2.txt",),
                input_payload={"schema_version": 5},
                public_request_payload={"businessType": "file"},
                initial_public_status="0",
                trace_id="trace-analysis-audit-v2",
                accepted_at="2026-08-15T00:00:00.000000Z",
                batch=TaskBatchRef("a" * 32, 1),
            )
            factories = build_sqlite_task_control_uow_factories(manager)
            with factories.admission() as unit_of_work:
                result = unit_of_work.admission.admit_one(request)
                self.assertIs(TaskAdmissionOutcome.ACCEPTED, result.outcome)
                unit_of_work.commit()

            shared_store = _InteractionAuditStoreFake()
            persistence = SQLiteAnalysisAuditPersistence(manager, shared_store)
            reserve = persistence.upsert_architecture_recall_decision(
                execution_id=request.task_id.value,
                tree_fingerprint="",
                query_digest="b" * 64,
                base_top64=[103],
                final_candidates=[{"id": 103}],
                channel_rankings={"exact": [103]},
                rrf_scores={"103": 0.1},
                protected_reasons={"103": ["test"]},
                prompt_chars=10,
                recall_elapsed_ms=1,
            )
            self.assertTrue(reserve.created)
            finalized = persistence.finalize_architecture_recall_decision(
                execution_id=request.task_id.value,
                returned_architecture_id=103,
                returned_rank=1,
                total_elapsed_ms=2,
                failure_stage=None,
                error_message="",
            )
            self.assertTrue(finalized.finalized)

            audit_result = persistence.create_llm_interaction_with_trace(
                execution_id=request.task_id.value,
                business_key=request.business_ref.business_key,
                audit_idempotency_key=f"analysis-rag:{request.task_id.value}",
                status="succeeded",
            )
            self.assertEqual(7, audit_result.interaction_id)
            self.assertEqual(1, len(shared_store.created))
            # 共享 Writer 已提交后若组件引用重放，必须复用原事实而不是制造第二条引用。
            persistence.create_llm_interaction_with_trace(
                execution_id=request.task_id.value,
                business_key=request.business_ref.business_key,
                audit_idempotency_key=f"analysis-rag:{request.task_id.value}",
                status="succeeded",
            )
            with manager.begin(read_only=True) as transaction:
                recall = transaction.connection.execute(
                    "SELECT outcome, version FROM analysis_recall_decisions"
                ).fetchone()
                interaction_count = transaction.connection.execute(
                    "SELECT COUNT(*) FROM analysis_interaction_audit_refs"
                ).fetchone()[0]
                transaction.commit()
            self.assertEqual(("succeeded", 2), tuple(recall))
            self.assertEqual(1, interaction_count)


if __name__ == "__main__":
    unittest.main()
