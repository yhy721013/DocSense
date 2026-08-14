"""阶段 2-5 第 1 步 Weaponry v2 输入与 Codec Port 永久门禁。"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import unittest

from app.modules.weaponry.adapters import WeaponryTaskCommandCodec
from app.modules.weaponry.domain import WEAPONRY_INPUT_SCHEMA_VERSION


_ROOT = Path(__file__).resolve().parents[1]
_CONTRACT_PATH = (
    Path(__file__).parent / "contracts" / "stage2_weaponry_v2_contract.json"
)
_CODEC_PATH = _ROOT / "app/modules/weaponry/adapters/task_codec.py"
_CONTAINER_PATH = _ROOT / "app/container.py"
_LEGACY_TASK_SERVICE_PATH = _ROOT / "app/services/llm_service/task_service.py"


class WeaponryInputContractTests(unittest.TestCase):
    """锁定不升版、不补环境默认值和不反向依赖旧控制面的边界。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))

    def test_v2_remains_the_only_new_write_version(self) -> None:
        input_contract = self.contract["input"]
        codec = WeaponryTaskCommandCodec()

        self.assertEqual("2-5-completed", self.contract["stage"])
        self.assertFalse(self.contract["publicContractChanged"])
        self.assertTrue(self.contract["productionWired"])
        self.assertEqual(2, WEAPONRY_INPUT_SCHEMA_VERSION)
        self.assertEqual(2, codec.write_schema_version)
        self.assertEqual(2, input_contract["newWriteVersion"])
        self.assertTrue(input_contract["versionBumpForbidden"])
        self.assertEqual(3, input_contract["specialtyReservedVersion"])
        self.assertFalse(input_contract["workerMayReadCurrentEnvironmentToBackfill"])

    def test_machine_contract_matches_codec_canonical_payload(self) -> None:
        tree = ast.parse(_CODEC_PATH.read_text(encoding="utf-8-sig"))
        input_keys: set[str] | None = None
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(
                isinstance(target, ast.Name) and target.id == "_INPUT_KEYS"
                for target in node.targets
            ):
                continue
            if not isinstance(node.value, ast.Call):
                break
            argument = node.value.args[0]
            if isinstance(argument, (ast.Set, ast.List, ast.Tuple)):
                input_keys = {
                    item.value
                    for item in argument.elts
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                }
            break

        self.assertIsNotNone(input_keys)
        self.assertEqual(
            set(self.contract["input"]["encodedTopLevelFields"]),
            input_keys,
        )

    def test_codec_depends_on_port_not_adapter_or_legacy_service(self) -> None:
        source = _CODEC_PATH.read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        imported_modules = {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }

        self.assertIn("app.modules.tasks.ports", imported_modules)
        self.assertFalse(
            any(name.startswith("app.modules.tasks.adapters") for name in imported_modules)
        )
        self.assertNotIn("app.services.llm_service.task_service", imported_modules)
        self.assertNotIn("LLMTaskService", source)

    def test_future_switch_is_frozen_as_one_indivisible_wave(self) -> None:
        switch = self.contract["futureAtomicSwitch"]
        evidence = self.contract["evidence"]

        self.assertTrue(switch["submitRunProgressTerminalAndCallbackControlTogether"])
        self.assertTrue(switch["dualWriteForbidden"])
        self.assertTrue(switch["legacyAuthorityBackfillForbidden"])
        self.assertEqual(7, switch["scheduledAfterPrerequisiteSteps"])
        self.assertTrue(switch["completed"])
        self.assertFalse(evidence["runPyExecuted"])
        self.assertFalse(evidence["providerCalled"])
        self.assertFalse(evidence["multiInstanceOrProductionClaimed"])

    def test_runtime_config_migration_has_no_legacy_facade(self) -> None:
        migration = self.contract["runtimeConfigMigration"]
        runtime_path = _ROOT / migration["path"]
        legacy_path = (
            _ROOT / "app/modules/weaponry/adapters/infrastructure_config.py"
        )

        self.assertTrue(runtime_path.is_file())
        self.assertFalse(legacy_path.exists())
        self.assertTrue(migration["legacyModuleRemoved"])
        self.assertFalse(migration["legacyAliasesRetained"])
        self.assertFalse(migration["environmentKeysChanged"])
        self.assertFalse(migration["defaultsChanged"])
        self.assertFalse(migration["validationOrErrorSemanticsChanged"])
        self.assertFalse(migration["stage2RuntimeFieldsAdded"])

    def test_production_composition_uses_only_weaponry_v2_control_chain(self) -> None:
        """组合根不得重新构造旧 Weaponry Task/Callback/Dispatcher Authority。"""

        source = _CONTAINER_PATH.read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        imported_names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        }
        called_names = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

        self.assertNotIn("SQLiteWeaponryCallbackAdapter", imported_names)
        self.assertNotIn("SQLiteWeaponryCallbackRecoverySource", imported_names)
        self.assertNotIn("LegacyTaskCommandAdapter", imported_names)
        self.assertNotIn("compose_weaponry_application_services", called_names)
        self.assertIn("SubmitWeaponryV2Task", called_names)
        self.assertIn("RunWeaponryV2Workflow", called_names)
        self.assertIn("WeaponryV2TaskDispatcher", called_names)
        self.assertIn("WeaponryV2ApplicationServices", called_names)

    def test_legacy_service_contains_no_weaponry_component_sql(self) -> None:
        """切换后旧 Service 可保留通用投影入口，但不得拥有组件表 DDL/读写。"""

        source = _LEGACY_TASK_SERVICE_PATH.read_text(encoding="utf-8-sig")
        tree = ast.parse(source)
        method_names = {
            node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        }
        string_literals = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }

        self.assertNotIn("get_weaponry_task_document_snapshots", method_names)
        self.assertNotIn("_replace_weaponry_selection_snapshot", method_names)
        self.assertFalse(
            any("weaponry_task_document_snapshots" in value for value in string_literals)
        )


if __name__ == "__main__":
    unittest.main()
