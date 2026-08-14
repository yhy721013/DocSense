"""阶段 2-4 Report v2 生产运行、独立维护与旧 SQL 收口机器契约门禁。"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import unittest

from app.modules.report.application import REPORT_STEP_REGISTRY


_ROOT = Path(__file__).resolve().parents[1]
_CONTRACT = (
    Path(__file__).parent / "contracts" / "stage2_report_v2_runner_contract.json"
)


class ReportV2RunnerContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(_CONTRACT.read_text(encoding="utf-8"))

    def test_contract_records_atomic_production_switch_without_public_change(self) -> None:
        self.assertEqual("2-4-step-8", self.contract["stage"])
        self.assertFalse(self.contract["publicContractChanged"])
        self.assertTrue(self.contract["productionWired"])
        self.assertFalse(self.contract["productionSwitch"]["reportLegacyControlWrites"])
        self.assertFalse(self.contract["productionSwitch"]["dualWrite"])
        self.assertEqual(1, self.contract["productionSwitch"]["progressHubCount"])
        self.assertFalse(self.contract["evidence"]["runPyExecuted"])
        self.assertFalse(self.contract["evidence"]["multiInstanceOrProductionClaimed"])

    def test_legacy_report_sql_cleanup_is_single_direction_and_non_destructive(self) -> None:
        """旧入口只能删除，既有历史表不得在服务初始化时被破坏。"""

        cleanup = self.contract["legacySqlCleanup"]
        self.assertTrue(cleanup["dedicatedReportTaskEntryRemoved"])
        self.assertTrue(cleanup["reportResourceDdlRemoved"])
        self.assertTrue(cleanup["deadInlineAuditSqlRemoved"])
        self.assertTrue(cleanup["legacySingleInteractionWriteEntriesRemoved"])
        self.assertFalse(cleanup["existingLegacyTablesDropped"])
        self.assertEqual(
            {
                "create_report_resource_record",
                "get_report_resource_record",
                "save_report_resource_record",
                "prepare_report_resource_cleanup",
                "defer_report_resource_recovery",
                "list_recoverable_report_resource_ids",
            },
            set(cleanup["reportResourceMethodsRemoved"]),
        )
        self.assertEqual(
            {
                "analysis_callback_guard",
                "weaponry_callback_guard",
                "analysis_resource_records",
                "interaction_schema_and_read_compatibility",
            },
            set(cleanup["retainedSharedCapabilities"]),
        )

    def test_maintenance_is_persisted_bounded_and_independent(self) -> None:
        maintenance = self.contract["maintenance"]
        self.assertEqual(1, maintenance["dispatcherOwnedThreadCount"])
        self.assertFalse(maintenance["sharesBusinessExecutorLoop"])
        self.assertTrue(maintenance["startupScan"])
        self.assertTrue(maintenance["periodicScan"])
        self.assertTrue(maintenance["eventWakeupIsLossyHint"])
        self.assertTrue(maintenance["jobFailuresAreIsolated"])
        self.assertTrue(maintenance["sharedStopDeadline"])

        jobs = {job["name"]: job for job in maintenance["jobs"]}
        callback = jobs["report_callback_guard_sweep"]
        resource = jobs["report_resource_recovery"]
        self.assertTrue(callback["bounded"])
        self.assertTrue(callback["freezesExpiredSendingOnly"])
        self.assertFalse(callback["sendsHttp"])
        self.assertFalse(callback["autoResendsUnknown"])
        self.assertTrue(resource["bounded"])
        self.assertTrue(resource["terminalFactsDiscoveredByPersistentScan"])
        self.assertFalse(resource["reversesCommittedTerminal"])
        self.assertTrue(
            self.contract["failurePolicy"]["terminalNeverWaitsForResourceCleanup"]
        )

    def test_runner_has_no_environment_or_sqlite_dependency(self) -> None:
        """Application Runner 只依赖领域与 Port，不读取配置或具体数据库。"""

        runner_path = _ROOT / "app/modules/report/application/run_report_v2.py"
        tree = ast.parse(runner_path.read_text(encoding="utf-8-sig"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported.update(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        self.assertNotIn("os", imported)
        self.assertFalse(
            any(name.startswith("app.modules.report.adapters") for name in imported)
        )
        self.assertFalse(any(name.startswith("sqlite3") for name in imported))

    def test_machine_contract_covers_every_frozen_report_step(self) -> None:
        """Registry 是唯一 Step 集合，专项成功用例必须实例化其全部模式。"""

        registry_patterns = {definition.key_pattern for definition in REPORT_STEP_REGISTRY}
        implementation_source = "\n".join(
            (
                (_ROOT / "app/modules/report/application/run_report_v2.py").read_text(
                    encoding="utf-8-sig"
                ),
                (_ROOT / "app/modules/report/adapters/anythingllm_rag.py").read_text(
                    encoding="utf-8-sig"
                ),
            )
        )
        for pattern in registry_patterns:
            stable_prefix = pattern.split("{", maxsplit=1)[0]
            with self.subTest(step=pattern):
                self.assertIn(stable_prefix, implementation_source)

        atomic_facts = {
            fact
            for group in self.contract["atomicTransactions"]
            for fact in group
        }
        self.assertIn("callback_eligibility", atomic_facts)
        self.assertIn("final_artifact_ref", atomic_facts)
        self.assertTrue(
            self.contract["externalEffects"]["stepIntentCommittedBeforeEffect"]
        )


if __name__ == "__main__":
    unittest.main()
