"""阶段 2-2 根 Schema 与组件 Manifest 的机器契约门禁。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from app.modules.tasks.adapters.sqlite.schema import (
    TaskControlSchemaError,
    _load_manifest,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = (
    PROJECT_ROOT / "app" / "modules" / "tasks" / "adapters" / "sqlite" / "database_contract.json"
)
DECISION_DOCUMENT_PATH = (
    PROJECT_ROOT
    / "docs"
    / "重构记录"
    / "阶段2资产"
    / "260812-阶段2-2根Schema与组件Manifest设计.md"
)
MAIN_DESIGN_PATH = (
    PROJECT_ROOT / "docs" / "重构记录" / "260809-阶段2统一任务执行内核文件级实施设计.md"
)


class Stage2TaskControlSchemaContractTests(unittest.TestCase):
    """防止实现阶段临场改变数据库身份、组件边界或失败关闭策略。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        cls.composition = cls.contract["schemaComposition"]
        cls.root_contract = cls.composition["rootManifest"]
        cls.root_manifest_path = PROJECT_ROOT / cls.root_contract["manifestAsset"]
        cls.root_manifest = json.loads(
            cls.root_manifest_path.read_text(encoding="utf-8")
        )

    def test_decision_is_confirmed_internal_and_documented(self) -> None:
        self.assertEqual(6, self.contract["assetVersion"])
        self.assertTrue(self.contract["runtimeAsset"])
        self.assertEqual(
            "2-6-fresh-bootstrap-confirmed",
            self.contract["stage"],
        )
        self.assertFalse(self.contract["publicContractChanged"])
        self.assertEqual("user_confirmed", self.composition["decisionStatus"])
        self.assertTrue(DECISION_DOCUMENT_PATH.is_file())
        self.assertEqual(
            DECISION_DOCUMENT_PATH.relative_to(PROJECT_ROOT).as_posix(),
            self.composition["decisionDocument"],
        )
        main_design = MAIN_DESIGN_PATH.read_text(encoding="utf-8")
        self.assertIn("task_control_schema_components", main_design)
        self.assertIn("根 + 全部已登记且当前代码认识的组件", main_design)
        fresh = self.contract["initialization"]["freshInstall"]
        self.assertEqual("user_confirmed", fresh["decisionStatus"])
        self.assertTrue(fresh["ordinaryApplicationAutoDetectionForbidden"])
        self.assertTrue(fresh["explicitOneShotConfirmationRequired"])
        self.assertTrue(fresh["legacyFileSetMustBeAbsent"])
        self.assertTrue(fresh["targetFileSetMustBeAbsent"])
        self.assertEqual("fail_closed", fresh["repeatInvocation"])

    def test_root_identity_and_registry_fields_are_complete(self) -> None:
        identity = self.contract["databaseIdentity"]
        self.assertEqual(1146307378, identity["applicationId"])
        self.assertEqual(2, identity["userVersion"])
        self.assertEqual(2, identity["schemaGeneration"])
        self.assertEqual("task_control_schema_components", identity["componentRegistryTable"])
        self.assertEqual(
            {
                "metadata_id",
                "schema_name",
                "schema_generation",
                "compatible_generation_min",
                "compatible_generation_max",
                "root_manifest_version",
                "schema_fingerprint",
                "db_instance_uuid",
                "created_at",
            },
            set(identity["requiredMetadataFields"]),
        )
        self.assertEqual(
            {
                "component_name",
                "component_version",
                "root_schema_generation",
                "schema_fingerprint",
                "manifest_profile",
                "installed_at",
            },
            set(self.composition["componentRegistry"]["requiredFields"]),
        )

    def test_root_and_planned_components_have_non_overlapping_ownership(self) -> None:
        root = self.root_contract
        root_tables = tuple(root["ownedTables"])
        root_indexes = tuple(root["ownedIndexes"])
        self.assertEqual(len(root_tables), len(set(root_tables)))
        self.assertEqual(len(root_indexes), len(set(root_indexes)))
        self.assertIn("task_control_schema_metadata", root_tables)
        self.assertIn("task_control_schema_components", root_tables)
        self.assertIn("llm_task_executions", root_tables)
        self.assertIn("llm_tasks", root_tables)
        self.assertIn("task_attempts", root_tables)
        self.assertIn("task_steps", root_tables)
        self.assertIn("task_recovery_cases", root_tables)
        self.assertIn("task_recovery_operations", root_tables)
        self.assertIn("task_events", root_tables)
        self.assertIn("callback_delivery_guards", root_tables)
        self.assertFalse(root["businessComponentsMayAlterRootObjects"])
        self.assertTrue(root["sqliteSequenceForbidden"])
        self.assertTrue(root["autoincrementForbidden"])

        components = self.composition["plannedComponents"]
        names = [item["componentName"] for item in components]
        self.assertEqual(
            ["report_control", "weaponry_control", "analysis_control"],
            names,
        )
        self.assertEqual(len(names), len(set(names)))
        prefixes = [prefix for item in components for prefix in item["objectPrefixes"]]
        self.assertEqual(len(prefixes), len(set(prefixes)))
        for component in components:
            self.assertFalse(component["manifestFrozen"])
            self.assertFalse(component["precreateInStage22"])
            for prefix in component["objectPrefixes"]:
                self.assertFalse(any(name.startswith(prefix) for name in root_tables))
                self.assertFalse(any(name.startswith(prefix) for name in root_indexes))

    def test_root_manifest_is_complete_and_matches_declared_ownership(self) -> None:
        self.assertTrue(self.root_manifest_path.is_file())
        self.assertTrue(
            self.root_manifest_path.is_relative_to(PROJECT_ROOT / "app"),
            "生产 Bootstrap 不得反向加载 tests 目录中的契约资产",
        )
        self.assertEqual("canonical_json_v1", self.root_manifest["manifestProfile"])
        self.assertEqual("core", self.root_manifest["componentName"])
        self.assertEqual(1, self.root_manifest["componentVersion"])
        self.assertEqual(2, self.root_manifest["rootSchemaGeneration"])
        self.assertEqual(
            {"collation": "BINARY", "order": "ASC"},
            self.root_manifest["indexTermDefaults"],
        )
        self.assertEqual(
            1,
            self.root_manifest_path.read_text(encoding="utf-8").count(
                '"indexTermDefaults"'
            ),
        )

        tables = self.root_manifest["tables"]
        indexes = self.root_manifest["indexes"]
        table_names = [table["name"] for table in tables]
        index_names = [index["name"] for index in indexes]
        self.assertEqual(self.root_contract["ownedTables"], table_names)
        self.assertEqual(self.root_contract["ownedIndexes"], index_names)
        self.assertEqual(len(table_names), len(set(table_names)))
        self.assertEqual(len(index_names), len(set(index_names)))

        tables_by_name = {table["name"]: table for table in tables}
        allowed_types = {"INTEGER", "REAL", "TEXT", "BLOB"}
        for table in tables:
            with self.subTest(table=table["name"]):
                columns = table["columns"]
                column_names = [column["name"] for column in columns]
                self.assertTrue(columns)
                self.assertEqual(len(column_names), len(set(column_names)))
                self.assertTrue(
                    all(column["type"] in allowed_types for column in columns)
                )
                self.assertTrue(
                    all(type(column["notNull"]) is bool for column in columns)
                )
                primary_key_positions = sorted(
                    column["primaryKeyPosition"]
                    for column in columns
                    if column["primaryKeyPosition"]
                )
                self.assertEqual(
                    list(range(1, len(primary_key_positions) + 1)),
                    primary_key_positions,
                )
                constraint_ids = [
                    constraint["id"]
                    for group in ("uniqueConstraints", "checkConstraints")
                    for constraint in table[group]
                ]
                self.assertEqual(len(constraint_ids), len(set(constraint_ids)))
                for unique in table["uniqueConstraints"]:
                    self.assertTrue(set(unique["columns"]).issubset(column_names))

        for table in tables:
            source_columns = {column["name"] for column in table["columns"]}
            for foreign_key in table["foreignKeys"]:
                with self.subTest(
                    table=table["name"],
                    referenced=foreign_key["referencedTable"],
                ):
                    self.assertTrue(set(foreign_key["columns"]).issubset(source_columns))
                    target = tables_by_name[foreign_key["referencedTable"]]
                    target_columns = {column["name"] for column in target["columns"]}
                    self.assertTrue(
                        set(foreign_key["referencedColumns"]).issubset(target_columns)
                    )
                    self.assertEqual(
                        len(foreign_key["columns"]),
                        len(foreign_key["referencedColumns"]),
                    )

        for index in indexes:
            with self.subTest(index=index["name"]):
                table = tables_by_name[index["table"]]
                columns = {column["name"] for column in table["columns"]}
                self.assertTrue(index["columns"])
                self.assertTrue(set(index["columns"]).issubset(columns))
                self.assertIs(type(index["unique"]), bool)
                self.assertTrue(index["where"] is None or isinstance(index["where"], str))

    def test_manifest_loader_rejects_duplicate_json_keys_before_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text(
                '{"componentName":"core","componentName":"core"}',
                encoding="utf-8",
            )
            with self.assertRaises(TaskControlSchemaError) as captured:
                _load_manifest(path)
        self.assertEqual("manifest_duplicate_key", captured.exception.code)

    def test_recovery_decision_manifest_persists_v3_convergence_inputs(self) -> None:
        """下一观察时间和 Terminal Projection 必须是受约束根事实，不能只存在内存中。"""

        decision_table = next(
            table
            for table in self.root_manifest["tables"]
            if table["name"] == "task_recovery_decisions"
        )
        columns = {column["name"]: column for column in decision_table["columns"]}
        self.assertFalse(columns["next_observation_at"]["notNull"])
        self.assertEqual(
            "optional_persisted_utc",
            columns["next_observation_at"]["semanticType"],
        )
        self.assertFalse(columns["terminal_projection_payload"]["notNull"])
        self.assertEqual(
            "canonical_json",
            columns["terminal_projection_payload"]["semanticType"],
        )
        check_ids = {item["id"] for item in decision_table["checkConstraints"]}
        self.assertTrue(
            {
                "recovery_decision_next_observation_kind",
                "recovery_decision_terminal_projection",
                "recovery_decision_terminal_projection_json",
                "recovery_decision_next_observation",
            }.issubset(check_ids)
        )

    def test_recovery_v4_manifest_freezes_intent_and_step_resolution(self) -> None:
        """事务外 I/O 前的 Intent 和重试 Step 证据都必须是可重启根事实。"""

        tables = {
            table["name"]: table for table in self.root_manifest["tables"]
        }
        operation = tables["task_recovery_operations"]
        operation_columns = {
            column["name"]: column for column in operation["columns"]
        }
        self.assertEqual(
            {
                "operation_id",
                "case_id",
                "recovery_generation",
                "recovery_fencing_token",
                "operation_kind",
                "step_key",
                "idempotency_key",
                "intent_digest",
                "external_ref",
                "state",
                "intent_at",
                "result_at",
            },
            set(operation_columns),
        )
        observation = tables["task_recovery_observations"]
        self.assertIn(
            "operation_id",
            {column["name"] for column in observation["columns"]},
        )
        decision = tables["task_recovery_decisions"]
        decision_columns = {
            column["name"]: column for column in decision["columns"]
        }
        self.assertEqual(
            "canonical_json",
            decision_columns["step_resolution_payload"]["semanticType"],
        )
        self.assertTrue(
            {
                "recovery_decision_retry_resolution",
                "recovery_decision_retry_resolution_json",
            }.issubset(
                {item["id"] for item in decision["checkConstraints"]}
            )
        )

    def test_full_root_manifest_fingerprint_is_frozen_and_reproducible(self) -> None:
        canonical = json.dumps(
            self.root_manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        canonical_bytes = canonical.encode("utf-8")
        material = (
            f"{self.contract['databaseIdentity']['schemaName']}\n"
            f"{self.contract['databaseIdentity']['schemaGeneration']}\n"
        ).encode("utf-8") + canonical_bytes
        digest = hashlib.sha256(material).hexdigest().upper()
        self.assertEqual(
            self.root_contract["canonicalManifestUtf8Bytes"],
            len(canonical_bytes),
        )
        self.assertEqual(self.root_contract["expectedFingerprint"], digest)

    def test_canonical_fingerprint_profile_vector_is_reproducible(self) -> None:
        profile = self.composition["canonicalJsonProfile"]
        self.assertEqual("canonical_json_v1", profile["name"])
        self.assertFalse(profile["allowFloat"])
        self.assertFalse(profile["allowNaN"])
        self.assertFalse(profile["allowInfinity"])

        vector = self.composition["fingerprintProfileTestVector"]
        canonical = json.dumps(
            vector["payload"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        material = (
            f"{vector['schemaName']}\n{vector['schemaGeneration']}\n{canonical}"
        ).encode("utf-8")
        digest = hashlib.sha256(material).hexdigest().upper()
        self.assertEqual(vector["canonicalJson"], canonical)
        self.assertEqual(vector["sha256"], digest)

    def test_actual_schema_is_exact_union_and_never_auto_repaired(self) -> None:
        verification = self.composition["actualSchemaVerification"]
        registry = self.composition["componentRegistry"]
        self.assertTrue(verification["actualObjectsMustEqualManifestUnion"])
        self.assertFalse(verification["sqliteSchemaSqlIncludedInFingerprint"])
        self.assertEqual(
            "check_partial_predicate_and_foreign_key_deferrability_verification_only",
            verification["sqliteSchemaSqlUse"],
        )
        self.assertTrue(verification["autoIndexesVerifiedFromDeclaredConstraints"])
        self.assertTrue(verification["integrityCheckRequired"])
        self.assertTrue(verification["foreignKeyCheckRequired"])
        self.assertTrue(verification["createIfNotExistsRepairForbidden"])
        self.assertTrue(verification["alterTableRepairForbidden"])
        self.assertTrue(registry["unknownComponentFailsClosed"])
        self.assertTrue(registry["unregisteredObjectFailsClosed"])
        self.assertTrue(registry["objectOwnershipOverlapFailsClosed"])
        self.assertFalse(registry["componentMayAlterRootObject"])

    def test_stage22_starts_with_no_business_component(self) -> None:
        profile = self.composition["stage22RuntimeProfile"]
        self.assertEqual([], profile["knownComponents"])
        self.assertEqual([], profile["requiredComponents"])
        self.assertEqual([], profile["registeredComponentsOnFreshDatabase"])
        self.assertEqual([], self.composition["componentRegistry"]["initialRows"])

    def test_component_installation_is_explicit_atomic_and_non_downgradable(self) -> None:
        installation = self.composition["componentInstallation"]
        self.assertTrue(installation["explicitInstallerOnly"])
        self.assertTrue(installation["startupSchemaLockRequired"])
        self.assertTrue(installation["backgroundWorkersStoppedRequired"])
        self.assertEqual("BEGIN EXCLUSIVE", installation["transactionMode"])
        self.assertTrue(installation["networkIoForbidden"])
        self.assertTrue(installation["componentRegistryWrittenAfterObjectVerification"])
        self.assertTrue(installation["allSuccessOrRollback"])
        self.assertTrue(installation["postCommitFullValidationRequired"])
        self.assertTrue(installation["skipVersionForbidden"])
        self.assertTrue(installation["downgradeForbidden"])
        self.assertTrue(installation["oldBinaryUnknownComponentFailsClosed"])


if __name__ == "__main__":
    unittest.main()
