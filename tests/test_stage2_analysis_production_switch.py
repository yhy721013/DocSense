"""阶段 2-6 步骤 7：Analysis 生产组合根原子切换静态门禁。"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
import unittest

from app.container import create_application_services
from app.modules.analysis.adapters import AnalysisV2TaskDispatcher


class AnalysisProductionSwitchTests(unittest.TestCase):
    _CONTRACT_PATH = (
        Path(__file__).parent / "contracts" / "stage2_analysis_v2_contract.json"
    )

    def test_final_machine_contract_records_single_v2_control_plane(self) -> None:
        contract = json.loads(self._CONTRACT_PATH.read_text(encoding="utf-8"))
        self.assertEqual("2-6-completed", contract["stage"])
        self.assertTrue(contract["productionWired"])
        self.assertFalse(contract["publicContractChanged"])
        self.assertEqual(
            {
                "businessType": "file",
                "executorName": "file",
                "futureQueueRoute": "file",
            },
            contract["taskIdentity"],
        )
        self.assertEqual(7, contract["atomicSwitch"]["completedStep"])
        self.assertTrue(contract["atomicSwitch"]["dualWriteForbidden"])
        self.assertFalse(
            contract["atomicSwitch"]["legacyAnalysisProductionChainConstructed"]
        )
        self.assertTrue(contract["authority"]["authorityLossStopsWithoutTerminal"])
        self.assertEqual(
            [
                "task_id",
                "attempt_no",
                "lease_token",
                "fencing_token",
                "lease_expires_at",
            ],
            contract["authority"]["completeFields"],
        )
        self.assertTrue(
            contract["freshBootstrap"]["ordinaryApplicationAutoFreshForbidden"]
        )

    def test_production_factory_constructs_only_v2_analysis_control_chain(self) -> None:
        """生产工厂不得残留旧受理、Runner、Callback 或资源双写构造。"""

        source = inspect.getsource(create_application_services)
        required = (
            "SQLiteAnalysisV2BatchAdmissionAdapter(",
            "AnalysisV5TaskCommandCodec(",
            "RunAnalysisV2Workflow(",
            "TaskControlAnalysisCallbackAdapter(",
            "SQLiteAnalysisV2CallbackRecoverySource(",
            "SQLiteAnalysisV2ResourceStoreAdapter(",
            "SQLiteAnalysisExecutionUnitOfWorkFactory(",
            "AnalysisV2TaskDispatcher(",
            'task_type="file"',
            'executor_name="file"',
        )
        for marker in required:
            with self.subTest(marker=marker):
                self.assertIn(marker, source)

        forbidden = (
            "SQLiteAnalysisBatchCommandAdapter(",
            "SQLiteAnalysisCallbackAdapter(",
            "SQLiteAnalysisCallbackRecoverySource(",
            "SQLiteAnalysisResourceStoreAdapter(",
            "RunAnalysisTask(",
            "LegacyAnalysisAuditAdapter(",
            "analysis-dispatcher.lock",
            "compose_analysis_application_services(",
        )
        for marker in forbidden:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, source)

    def test_root_schema_and_public_reads_include_file_in_v2_only(self) -> None:
        """启动安装 Analysis 组件，公开 Progress/check-task 不再回退旧 Task DB。"""

        source = inspect.getsource(create_application_services)
        self.assertIn(
            "ANALYSIS_CONTROL_COMPONENT_NAME: load_analysis_control_manifest()",
            source,
        )
        self.assertIn(
            "ANALYSIS_CONTROL_COMPONENT_NAME: ANALYSIS_CONTROL_COMPONENT_VERSION",
            source,
        )
        self.assertIn('frozenset({"report", "weaponry", "file"})', source)

    def test_v2_dispatcher_declares_persisted_authority_without_process_lock(self) -> None:
        source = inspect.getsource(AnalysisV2TaskDispatcher)
        self.assertIn("def uses_task_control_authority", source)
        self.assertIn("return True", source)
        self.assertNotIn("ProcessSingletonGuard", source)
        self.assertNotIn("FileProcessSingletonGuard", source)


if __name__ == "__main__":
    unittest.main()
