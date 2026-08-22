"""阶段 2-0 编码前冻结资产的长期一致性门禁。"""

from __future__ import annotations

import hashlib
import ast
import json
import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TASK_CONTRACT_PATH = (
    PROJECT_ROOT / "tests" / "contracts" / "stage2_task_execution_contract.json"
)
INTERFACE_CONTRACT_PATH = (
    PROJECT_ROOT / "tests" / "contracts" / "stage2_interface_contract_hashes.json"
)
OWNERSHIP_CONTRACT_PATH = (
    PROJECT_ROOT / "tests" / "contracts" / "stage2_ownership_contract.json"
)
RUNTIME_CONFIG_CONTRACT_PATH = (
    PROJECT_ROOT / "tests" / "contracts" / "stage2_runtime_config_ownership.json"
)
BOUNDARY_CONTRACT_PATH = (
    PROJECT_ROOT / "tests" / "contracts" / "stage2_boundary_contract.json"
)
DIRECT_PARENT_SCOPE_PATH = (
    PROJECT_ROOT / "tests" / "contracts" / "stage2_direct_parent_scope.json"
)
BUSINESS_STEP_REGISTRY_PATH = (
    PROJECT_ROOT / "tests" / "contracts" / "stage2_business_step_registry.json"
)
RUNTIME_TOPOLOGY_CONTRACT_PATH = (
    PROJECT_ROOT / "tests" / "contracts" / "stage2_runtime_topology_contract.json"
)


class Stage2TaskExecutionContractAssetTests(unittest.TestCase):
    """防止实现阶段临场改变已经冻结的任务控制语义。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(TASK_CONTRACT_PATH.read_text(encoding="utf-8"))

    def test_task_attempt_and_step_states_remain_separate(self) -> None:
        """Task、执行权和步骤结果不得重新合并成一条模糊状态机。"""

        task_states = set(self.contract["taskStateMachine"]["states"])
        attempt_states = set(self.contract["attemptStateMachine"]["states"])
        step_states = set(self.contract["stepStateMachine"]["states"])

        self.assertIn("recovery_required", task_states)
        self.assertNotIn("outcome_unknown", task_states)
        self.assertIn("leased", attempt_states)
        self.assertNotIn("leased", task_states)
        self.assertIn("outcome_unknown", step_states)
        self.assertNotIn("queued", task_states)
        self.assertNotIn("cancelled", task_states)

        completion = self.contract["stepStateMachine"]["completionCommand"]
        self.assertTrue(completion["explicitTransitionRequired"])
        self.assertEqual(
            {"succeed", "fail", "mark_outcome_unknown"},
            set(completion["executionAuthorityTransitions"]),
        )
        self.assertTrue(completion["outcomeUnknownRequiresRecoveryIsolation"])
        self.assertTrue(completion["outcomeUnknownAtomicallyAbandonsAttempt"])
        self.assertTrue(completion["outcomeUnknownAtomicallyCreatesRecoveryCase"])
        self.assertEqual("duplicate_step_intent", completion["duplicateIntentOutcome"])
        self.assertTrue(
            completion["duplicateIntentRequiresExactAuthorityDefinitionVersionKeyAndTime"]
        )
        retry_step = self.contract["stepStateMachine"]["recoveryAuthorizedTransition"]
        self.assertFalse(retry_step["stepAttemptIdempotencyIndex"]["unique"])
        self.assertTrue(
            retry_step["stepAttemptIdempotencyIndex"][
                "stableKeyMayRepeatAcrossRecoveryAuthorizedAttempts"
            ]
        )

    def test_recovery_cannot_directly_restart_or_reuse_old_attempt(self) -> None:
        """恢复授权只能回 accepted，之后必须由普通 claim 创建新执行权。"""

        recovery = self.contract["recovery"]
        retry = recovery["decisions"]["retry_authorized"]
        forbidden = {
            tuple(item)
            for item in self.contract["taskStateMachine"][
                "forbiddenDirectTransitions"
            ]
        }

        self.assertEqual("accepted", retry["taskState"])
        self.assertFalse(retry["startsRunnerDirectly"])
        self.assertFalse(retry["incrementsRecoveryGeneration"])
        self.assertTrue(retry["nextExecutionRequiresOrdinaryClaim"])
        self.assertIn(("recovery_required", "running"), forbidden)
        self.assertFalse(recovery["checkTaskCanRecoverBusinessExecution"])

    def test_complete_authority_is_required_and_secret_is_not_public(self) -> None:
        """TaskId/latest 不能继续充当阶段 2 的完整写权限。"""

        authority = self.contract["authority"]
        required = set(authority["requiredFields"])
        public_forbidden = set(self.contract["publicExposureForbiddenFields"])

        self.assertEqual(
            {
                "task_id",
                "attempt_no",
                "owner_id",
                "lease_token",
                "fencing_token",
                "lease_expires_at",
            },
            required,
        )
        self.assertFalse(authority["taskIdAloneCanWrite"])
        self.assertFalse(authority["ownerIdIsAuthority"])
        self.assertIn("lease_token", authority["secretFields"])
        self.assertTrue(required.issubset(public_forbidden))
        owner = authority["claimOwnerIdentity"]
        self.assertEqual("TaskOwnerIdentity", owner["typeName"])
        self.assertEqual(
            {
                "instance_start_id",
                "process_id",
                "executor_name",
                "worker_slot",
            },
            set(owner["requiredFields"]),
        )
        self.assertTrue(owner["ownerIdDerived"])
        self.assertTrue(owner["persistSplitFields"])

    def test_analysis_batch_identity_is_explicit_and_ordered(self) -> None:
        """Store 不得从 payload 猜测 Analysis 批次，也不得重排请求。"""

        admission = self.contract["admission"]
        batch = admission["batchAdmission"]
        self.assertEqual("TaskBatchRef", admission["batchReferenceType"])
        self.assertEqual({"batch_id", "sequence"}, set(admission["batchReferenceFields"]))
        self.assertTrue(admission["fileTasksRequireBatch"])
        self.assertTrue(admission["nonFileTasksForbidBatch"])
        self.assertTrue(batch["allOrNothing"])
        self.assertTrue(batch["singleBatchId"])
        self.assertEqual(1, batch["sequenceStartsAt"])
        self.assertTrue(batch["sequenceContinuous"])
        self.assertTrue(batch["requestOrderPreserved"])

    def test_reaper_is_a_classifier_not_a_running_resetter(self) -> None:
        """租约过期只能产生恢复候选，不能自动授权重放副作用。"""

        reaper = self.contract["reaper"]
        self.assertEqual("classifier_not_resetter", reaper["role"])
        self.assertFalse(reaper["leaseExpiryAloneAllowsRetry"])
        self.assertTrue(reaper["genericRunningToAcceptedResetForbidden"])
        self.assertEqual(
            {
                "retry_safe",
                "finalize_from_checkpoint",
                "reconcile_required",
                "mark_stale",
                "defer",
            },
            set(reaper["classifications"]),
        )
        persistence = reaper["classificationPersistence"]
        self.assertEqual("classify_candidate_if_current", persistence["method"])
        self.assertEqual("create_case_if_current", persistence["legacyMethodForbidden"])
        self.assertEqual(
            {"finalize_from_checkpoint", "reconcile_required"},
            set(persistence["caseIdRequiredFor"]),
        )
        self.assertEqual(
            {"retry_safe", "defer"},
            set(persistence["nextActionAtRequiredFor"]),
        )
        self.assertTrue(persistence["sourceAttemptAndFencingCasRequired"])
        retry_safe = reaper["retrySafeTransition"]
        self.assertEqual("running", retry_safe["source"])
        self.assertEqual("accepted", retry_safe["target"])
        self.assertFalse(retry_safe["ordinaryWorkerMayInvoke"])
        self.assertTrue(retry_safe["sourceAttemptAndFencingCasRequired"])
        self.assertEqual(
            [["running", "accepted"]],
            self.contract["taskStateMachine"]["reaperClassifiedTransitions"],
        )

    def test_recovery_heartbeat_and_terminal_projection_are_explicit(self) -> None:
        recovery = self.contract["recovery"]
        heartbeat = recovery["heartbeat"]
        self.assertEqual("heartbeat_case", heartbeat["method"])
        self.assertTrue(heartbeat["completeRecoveryAuthorityRequired"])
        self.assertTrue(heartbeat["successfulRenewalInvalidatesOldAuthority"])

        keep = recovery["decisions"]["keep_quarantined"]
        terminal = recovery["decisions"]["finalize_from_checkpoint"]
        self.assertTrue(keep["optionalNextObservationAt"])
        self.assertEqual(
            "TaskRecoveryTerminalProjection",
            terminal["terminalProjectionType"],
        )
        self.assertTrue(terminal["sourceCheckpointCasRequired"])
        self.assertTrue(terminal["latestProjectionUpdatedAtomically"])
        self.assertFalse(terminal["storeMayGuessPublicProjectionByBusinessType"])

        claim_rules = recovery["claimRules"]
        self.assertEqual(
            {"open", "awaiting_evidence"},
            set(claim_rules["directlyClaimableStates"]),
        )
        self.assertTrue(claim_rules["observingTakeoverRequiresPersistedLeaseExpiry"])
        self.assertFalse(claim_rules["unexpiredOwnerMayBePreempted"])
        operations = recovery["operations"]
        self.assertTrue(operations["intentCommittedBeforeExternalIo"])
        self.assertTrue(operations["observationAndOperationConvergeAtomically"])
        retry = recovery["decisions"]["retry_authorized"]
        self.assertEqual("TaskRecoveryStepResolution", retry["stepResolutionType"])
        self.assertTrue(retry["sourceStepAttemptAndRowVersionCasRequired"])
        self.assertTrue(retry["oldOutcomeUnknownStepAttemptRemainsImmutable"])

    def test_canonical_profile_vector_is_reproducible(self) -> None:
        """Profile 身份不依赖字典插入顺序、空白或平台默认编码。"""

        profile = self.contract["canonicalProfile"]
        vector = profile["testVector"]
        canonical_json = json.dumps(
            vector["payload"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        material = (
            f"{vector['schemaName']}\n{vector['schemaVersion']}\n"
            f"{canonical_json}"
        ).encode("utf-8")
        digest = hashlib.sha256(material).hexdigest().upper()

        self.assertEqual(vector["canonicalJson"], canonical_json)
        self.assertEqual(vector["sha256"], digest)


class Stage2InterfaceContractAssetTests(unittest.TestCase):
    """确保阶段 2 内部重构不会静默改变任何既有公开接口契约。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(INTERFACE_CONTRACT_PATH.read_text(encoding="utf-8"))

    @staticmethod
    def _canonical_sha256(path: Path) -> str:
        """按冻结规则消除 BOM/换行差异后计算内容摘要。

        这里仅规范化不影响 Markdown 语义的跨平台文本表示；正文中的空格、
        标点、字段名和示例内容均参与摘要，任何契约内容变化都会触发失败。
        """

        text = path.read_bytes().decode("utf-8-sig")
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest().upper()

    def test_interface_file_set_remains_frozen(self) -> None:
        """接口目录中文件的新增或删除同样属于需要显式确认的契约变化。"""

        contract_root = PROJECT_ROOT / self.contract["contractRoot"]
        actual_paths = {
            path.relative_to(PROJECT_ROOT).as_posix()
            for path in contract_root.rglob("*")
            if path.is_file()
        }
        expected_paths = {item["path"] for item in self.contract["files"]}

        self.assertEqual(expected_paths, actual_paths)

    def test_interface_contents_match_frozen_hashes(self) -> None:
        """逐文件验证接口正文，禁止内部重构夹带公开契约漂移。"""

        for item in self.contract["files"]:
            with self.subTest(path=item["path"]):
                actual = self._canonical_sha256(PROJECT_ROOT / item["path"])
                self.assertEqual(item["canonicalSha256"], actual)

    def test_asset_declares_no_public_contract_change(self) -> None:
        """阶段 2-0 的基线资产必须明确声明此次没有接口契约变更。"""

        self.assertFalse(self.contract["publicContractChanged"])
        self.assertIn(
            "add_or_remove_request_parameter",
            self.contract["forbiddenWithoutExplicitConfirmation"],
        )
        self.assertIn(
            "add_or_remove_response_field",
            self.contract["forbiddenWithoutExplicitConfirmation"],
        )


class Stage2OwnershipContractAssetTests(unittest.TestCase):
    """确保 Services 文件与 SQLite 表均有完整、唯一的迁移所有权。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(OWNERSHIP_CONTRACT_PATH.read_text(encoding="utf-8"))

    @staticmethod
    def _declared_sqlite_tables() -> set[str]:
        """只扫描 Python 字符串中的 CREATE TABLE，避免把注释示例误判为 Schema。"""

        pattern = re.compile(
            r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["`\[]?'
            r"([A-Za-z_][A-Za-z0-9_]*)",
            re.IGNORECASE,
        )
        tables: set[str] = set()
        for path in (PROJECT_ROOT / "app").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    tables.update(pattern.findall(node.value))
        # 阶段 2-2 根表与后续业务组件表都由生产 Manifest 动态生成，不会以静态
        # CREATE TABLE 字符串出现。这里显式列出已经发布的 Manifest，既避免动态 DDL
        # 绕过唯一 Writer 盘点，也避免无约束扫描其他 JSON 后误认非 Schema 资产。
        manifest_paths = (
            PROJECT_ROOT
            / "app/modules/tasks/adapters/sqlite/root_schema_manifest.json",
            PROJECT_ROOT
            / "app/modules/report/adapters/sqlite/report_control_manifest.json",
            PROJECT_ROOT
            / "app/modules/weaponry/adapters/sqlite/weaponry_control_manifest.json",
            PROJECT_ROOT
            / "app/modules/analysis/adapters/sqlite/analysis_control_manifest.json",
        )
        for manifest_path in manifest_paths:
            if not manifest_path.is_file():
                continue
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            tables.update(str(table["name"]) for table in manifest["tables"])
        return tables

    def test_every_services_file_is_in_the_ownership_inventory(self) -> None:
        """新增 Services 债务必须先登记职责、目标所有者与退出阶段。"""

        actual = {
            path.relative_to(PROJECT_ROOT).as_posix()
            for path in (PROJECT_ROOT / "app" / "services").rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        self.assertEqual(set(self.contract["serviceFiles"]), actual)

    def test_every_current_sqlite_table_has_exactly_one_target_writer(self) -> None:
        """当前代码出现的每张显式表都必须且只能落入一个所有权组。"""

        table_to_writer: dict[str, str] = {}
        duplicates: set[str] = set()
        for group in self.contract["currentTableGroups"]:
            self.assertTrue(str(group["currentWriter"]).strip())
            self.assertTrue(str(group["targetWriter"]).strip())
            self.assertTrue(str(group["exitStage"]).strip())
            for table in group["tables"]:
                if table in table_to_writer:
                    duplicates.add(table)
                table_to_writer[table] = group["targetWriter"]

        self.assertEqual(set(), duplicates)
        self.assertEqual(self._declared_sqlite_tables(), set(table_to_writer))

    def test_migration_is_single_direction_without_dual_write(self) -> None:
        """迁移兼容只能旧到新，目标 Store 不得反向调用巨型 Service。"""

        rules = self.contract["migrationRules"]
        self.assertTrue(rules["legacyEntryMayOnlyDelegateToTargetWriter"])
        self.assertFalse(rules["targetWriterMayCallLegacyService"])
        self.assertTrue(rules["dualWriteForbidden"])
        self.assertTrue(rules["cyclicDelegationForbidden"])
        self.assertFalse(rules["stage22ProductionCutover"])
        self.assertTrue(rules["authorityRuntimeRequiredBeforeBusinessCutover"])
        self.assertEqual(["2-4", "2-5", "2-6"], rules["businessWaveCutoverStages"])
        self.assertTrue(rules["taskAndCallbackCutoverIsAtomicPerBusiness"])


class Stage2RuntimeConfigOwnershipAssetTests(unittest.TestCase):
    """确保阶段 2 触达的环境键有唯一所有者、校验、日志和样例归属。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(
            RUNTIME_CONFIG_CONTRACT_PATH.read_text(encoding="utf-8")
        )
        cls.entries = [
            (group, entry)
            for group in cls.contract["groups"]
            for entry in group["entries"]
        ]

    def test_config_keys_are_unique_and_fully_described(self) -> None:
        """同一环境键不能由多个 Runtime Config 竞争读取。"""

        keys = [entry["key"] for _, entry in self.entries]
        self.assertEqual(len(keys), len(set(keys)))
        for group, entry in self.entries:
            self.assertTrue(str(group["targetOwner"]).strip())
            self.assertTrue(str(group["logPolicy"]).strip())
            self.assertTrue(str(entry["default"]).strip())
            self.assertTrue(str(entry["validation"]).strip())

    def test_current_keys_have_matching_env_example_lines(self) -> None:
        """已生效配置必须能在样例文件找到冻结的完整行。"""

        env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
        for group, entry in self.entries:
            if group["envExampleStatus"] == "planned_add_with_implementation":
                self.assertIn("plannedEnvExampleLine", entry)
                continue
            with self.subTest(key=entry["key"]):
                self.assertIn(entry["envExampleLine"], env_example)

    def test_current_touched_config_literals_are_all_registered(self) -> None:
        """对当前配置源做字面量盘点，新增键不得绕过所有权矩阵。"""

        sources = [
            PROJECT_ROOT / "app" / "services" / "core" / "config.py",
            PROJECT_ROOT
            / "app"
            / "modules"
            / "report"
            / "adapters"
            / "runtime_config.py",
            PROJECT_ROOT
            / "app"
            / "modules"
            / "analysis"
            / "adapters"
            / "runtime_config.py",
            PROJECT_ROOT
            / "app"
            / "modules"
            / "tasks"
            / "adapters"
            / "runtime_config.py",
            PROJECT_ROOT
            / "app"
            / "modules"
            / "weaponry"
            / "adapters"
            / "runtime_config.py",
        ]
        pattern = re.compile(
            r"(?:DOCSENSE_(?:TASK|REPORT|ANALYSIS|WEAPONRY)_[A-Z0-9_]+|"
            r"WEAPONRY_(?:ANALYSE_MODE|TERMS_[A-Z0-9_]+))"
        )
        actual: set[str] = set()
        for path in sources:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    actual.update(pattern.findall(node.value))
        registered = {
            entry["key"]
            for group, entry in self.entries
            if group["envExampleStatus"] != "planned_add_with_implementation"
        }
        self.assertEqual(actual, registered)

    def test_task_keys_have_one_runtime_loader_after_stage23(self) -> None:
        """2-3 实现后，配置键必须由唯一 Task Runtime Config 装载。"""

        task_group = next(
            group for group in self.contract["groups"] if group["name"] == "task_runtime_current"
        )
        self.assertEqual("present", task_group["envExampleStatus"])
        self.assertTrue(
            (
                PROJECT_ROOT
                / "app"
                / "modules"
                / "tasks"
                / "adapters"
                / "runtime_config.py"
            ).exists()
        )


class Stage2BoundaryContractAssetTests(unittest.TestCase):
    """冻结 Callback 诊断、Knowledge 迁移输入和 Web 协议边界。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(BOUNDARY_CONTRACT_PATH.read_text(encoding="utf-8"))

    @staticmethod
    def _direct_importers(module_name: str) -> set[str]:
        importers: set[str] = set()
        for path in (PROJECT_ROOT / "app").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == module_name:
                    importers.add(path.relative_to(PROJECT_ROOT).as_posix())
                elif isinstance(node, ast.Import) and any(
                    alias.name == module_name for alias in node.names
                ):
                    importers.add(path.relative_to(PROJECT_ROOT).as_posix())
        return importers

    def test_callback_history_is_explicitly_non_authoritative(self) -> None:
        """本地 JSON 的存在或写入失败都不得成为恢复或投递决策。"""

        semantics = self.contract["callbackHistory"]["semantics"]
        self.assertTrue(semantics["bestEffort"])
        self.assertTrue(semantics["appendOnlyNoOverwrite"])
        self.assertFalse(semantics["authoritativeForDelivery"])
        self.assertFalse(semantics["authoritativeForRetry"])
        self.assertFalse(semantics["authoritativeForTaskRecovery"])
        self.assertFalse(semantics["writeFailureChangesBusinessOutcome"])
        self.assertFalse(semantics["automaticTtlEnabled"])

    def test_generic_callback_post_has_no_production_importer(self) -> None:
        """无人使用的通用发送函数不能重新成为业务 Callback Facade。"""

        self.assertEqual(
            [],
            self.contract["callbackHistory"]["genericPostCallbackProductionConsumers"],
        )
        for path in (PROJECT_ROOT / "app").rglob("*.py"):
            if path.as_posix().endswith("app/services/utils/callback_client.py"):
                continue
            self.assertNotIn(
                "post_callback_payload",
                path.read_text(encoding="utf-8-sig"),
                msg=str(path),
            )

    def test_knowledge_database_direct_dependencies_remain_frozen(self) -> None:
        """阶段 2 新代码不得继续扩大 DatabaseService 具体依赖面。"""

        actual = self._direct_importers("app.services.core.database")
        expected = set(self.contract["knowledgeDatabase"]["currentDirectDependencies"])
        self.assertEqual(expected, actual)
        self.assertTrue(self.contract["knowledgeDatabase"]["stage2PhysicalMoveForbidden"])
        self.assertTrue(self.contract["knowledgeDatabase"]["newStage2DirectDependenciesForbidden"])

    def test_web_adapter_file_set_and_import_boundary_remain_frozen(self) -> None:
        """Web Adapter 只做协议适配，不能构造数据库、线程或供应商实现。"""

        web = self.contract["webAdapters"]
        root = PROJECT_ROOT / web["root"]
        actual_files = {
            path.relative_to(PROJECT_ROOT).as_posix()
            for path in root.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        self.assertEqual(set(web["files"]), actual_files)
        self.assertTrue(web["retainedAsTargetBoundary"])

        forbidden = tuple(web["forbiddenImports"])
        violations: list[str] = []
        forbidden_thread_constructors = set(web["forbiddenThreadConstructors"])
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            for node in ast.walk(tree):
                imported: list[str] = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.append(node.module)
                elif isinstance(node, ast.Import):
                    imported.extend(alias.name for alias in node.names)
                if any(
                    name == blocked or name.startswith(f"{blocked}.")
                    for name in imported
                    for blocked in forbidden
                ):
                    violations.append(path.relative_to(PROJECT_ROOT).as_posix())
                if isinstance(node, ast.ImportFrom) and node.module == "threading":
                    if any(alias.name in forbidden_thread_constructors for alias in node.names):
                        violations.append(path.relative_to(PROJECT_ROOT).as_posix())
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name) and node.func.id in forbidden_thread_constructors:
                        violations.append(path.relative_to(PROJECT_ROOT).as_posix())
                    elif (
                        isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and f"{node.func.value.id}.{node.func.attr}"
                        in forbidden_thread_constructors
                    ):
                        violations.append(path.relative_to(PROJECT_ROOT).as_posix())
        self.assertEqual([], violations)


class Stage2DirectParentScopeAssetTests(unittest.TestCase):
    """确保未获接口授权的直接父节点专项不会混入通用 Task 内核。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(DIRECT_PARENT_SCOPE_PATH.read_text(encoding="utf-8"))

    def test_specialty_is_blocked_without_blocking_stage2_kernel(self) -> None:
        """专项有自己的停止条件，但不能借此冻结无关的通用内核。"""

        self.assertEqual(
            "blocked_by_own_stage0_confirmation_gate",
            self.contract["status"],
        )
        self.assertTrue(self.contract["publicContractConfirmationRequired"])
        self.assertFalse(self.contract["blocksStage2TaskKernel"])
        self.assertEqual(5, len(self.contract["unresolvedDecisions"]))

    def test_weaponry_schema_version_remains_v2(self) -> None:
        """阶段 2 通用迁移不能抢占专项保留的 Weaponry Input v3。"""

        path = (
            PROJECT_ROOT
            / "app"
            / "modules"
            / "weaponry"
            / "domain"
            / "task_inputs.py"
        )
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        values = [
            node.value.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "WEAPONRY_INPUT_SCHEMA_VERSION"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
        ]
        self.assertEqual([2], values)

    def test_specialty_only_symbols_are_absent_from_runtime(self) -> None:
        """环境键、父关系表和 v3 字段在获批前必须保持未实现。"""

        reserved = self.contract["reservedForSpecialtyOnly"]
        production_text = "\n".join(
            path.read_text(encoding="utf-8-sig")
            for path in (PROJECT_ROOT / "app").rglob("*.py")
        )
        env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

        self.assertNotIn(reserved["environmentKey"], production_text)
        self.assertNotIn(reserved["environmentKey"], env_example)
        for symbol in reserved["schemaTables"] + reserved["inputFields"]:
            with self.subTest(symbol=symbol):
                self.assertNotIn(symbol, production_text)


class Stage2BusinessStepRegistryAssetTests(unittest.TestCase):
    """确保三类业务的最终 Step、恢复矩阵和输入 Profile 均已冻结。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(
            BUSINESS_STEP_REGISTRY_PATH.read_text(encoding="utf-8")
        )

    def test_every_step_has_complete_identity_and_recovery_definition(self) -> None:
        """任何运行步骤都必须能从注册表确定副作用边界与五类恢复结论。"""

        effect_kinds = {"pure", "local_write", "external_read", "external_write"}
        replay_policies = {
            "safe",
            "idempotent_after_probe",
            "reconcile_only",
            "never_auto",
        }
        recovery_decisions = {
            "retry_safe",
            "finalize_from_checkpoint",
            "reconcile_required",
            "defer",
            "mark_stale",
        }
        schemas = self.contract["schemaCatalog"]
        matrices = self.contract["recoveryMatrices"]

        for business_name, business in self.contract["businesses"].items():
            step_keys = [step["stepKey"] for step in business["steps"]]
            self.assertEqual(
                len(step_keys),
                len(set(step_keys)),
                msg=f"{business_name} 存在重复 step_key",
            )
            for step in business["steps"]:
                with self.subTest(business=business_name, step=step["stepKey"]):
                    self.assertGreater(step["definitionVersion"], 0)
                    self.assertIn(step["effectKind"], effect_kinds)
                    self.assertIn(step["replayPolicy"], replay_policies)
                    self.assertTrue(step["idempotencyKey"].strip())
                    self.assertTrue(step["inputProfileFields"])
                    self.assertTrue(step["successResultRef"].strip())
                    self.assertIn(step["schemaRef"], schemas)
                    self.assertIn(step["recoveryMatrixRef"], matrices)
                    self.assertEqual(
                        recovery_decisions,
                        set(matrices[step["recoveryMatrixRef"]]),
                    )

    def test_external_writes_are_never_declared_blindly_replay_safe(self) -> None:
        """外部写必须先有 intent，并携带 checkpoint/外部引用供恢复对账。"""

        schemas = self.contract["schemaCatalog"]
        for business_name, business in self.contract["businesses"].items():
            for step in business["steps"]:
                if step["effectKind"] != "external_write":
                    continue
                with self.subTest(business=business_name, step=step["stepKey"]):
                    schema = schemas[step["schemaRef"]]
                    self.assertNotEqual("safe", step["replayPolicy"])
                    self.assertIn("idempotency_key", schema["intent"])
                    self.assertIn("intent_recorded_at", schema["intent"])
                    self.assertIn("outcome", schema["checkpoint"])
                    self.assertIn("external_resource_ref", schema["externalRef"])

    def test_candidate_steps_and_current_analysis_operations_are_covered(self) -> None:
        """设计候选和当前 Analysis 模型操作都不能落到未登记分支。"""

        required = {
            "report": {
                "source.download:{source_sequence}",
                "document.prepare:{source_sequence}",
                "template.download",
                "template.extract",
                "rag.session.open",
                "rag.document.upload:{artifact_sequence}",
                "rag.generate",
                "interaction_audit.commit",
                "report.render",
                "artifact.publish",
                "terminal.commit",
            },
            "weaponry": {
                "document_scope.load",
                "rag.workspace.create",
                "rag.document.bind:{document_sequence}",
                "retrieval.execute:{field_sequence}",
                "evidence.select:{field_sequence}",
                "field_model.execute:{field_sequence}:{document_sequence}:{model_attempt}",
                "result.map",
                "terminal.commit",
            },
            "analysis": {
                "source.download",
                "document.prepare",
                "recall.reserve",
                "rag.session.open",
                "rag.document.upload",
                "classification.execute:{attempt_number}",
                "identity.reselect",
                "extraction.execute:{attempt_number}",
                "recall.finalize",
                "interaction_audit.commit",
                "knowledge.workspace.ensure",
                "knowledge.document.bind",
                "translation.execute",
                "terminal.commit",
            },
        }
        for business_name, expected in required.items():
            actual = {
                step["stepKey"]
                for step in self.contract["businesses"][business_name]["steps"]
            }
            self.assertTrue(expected.issubset(actual), msg=business_name)

        covered_operations = {
            operation
            for step in self.contract["businesses"]["analysis"]["steps"]
            for operation in step.get("coversOperations", [])
        }
        rag_port = (
            PROJECT_ROOT / "app" / "modules" / "analysis" / "ports" / "rag.py"
        )
        tree = ast.parse(rag_port.read_text(encoding="utf-8-sig"))
        operation_values = {
            item.value.value
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "AnalysisRagOperation"
            for item in node.body
            if isinstance(item, ast.Assign)
            and isinstance(item.value, ast.Constant)
            and isinstance(item.value.value, str)
        }
        self.assertEqual(operation_values, covered_operations)

    def test_current_call_sites_and_profile_migration_are_explicit(self) -> None:
        """注册表必须能追溯当前代码，并区分现状版本与后续内部输入版本。"""

        for business_name, business in self.contract["businesses"].items():
            profile = business["canonicalProfile"]
            self.assertTrue(profile["topLevelFields"])
            for step in business["steps"]:
                self.assertTrue(step["currentCallSites"])
                for call_site in step["currentCallSites"]:
                    relative_path, marker = call_site.split(":", maxsplit=1)
                    path = PROJECT_ROOT / relative_path
                    with self.subTest(
                        business=business_name,
                        step=step["stepKey"],
                        call_site=call_site,
                    ):
                        self.assertTrue(path.is_file())
                        self.assertIn(marker, path.read_text(encoding="utf-8-sig"))

        businesses = self.contract["businesses"]
        self.assertEqual("ReportInputSnapshot/v1", businesses["report"]["currentInputSchema"])
        self.assertEqual("ReportInputSnapshot/v2", businesses["report"]["targetInputSchema"])
        self.assertEqual("WeaponryInputSnapshot/v2", businesses["weaponry"]["targetInputSchema"])
        self.assertTrue(businesses["weaponry"]["canonicalProfile"]["reusedWithoutVersionBump"])
        self.assertEqual("AnalysisTaskInput/v4", businesses["analysis"]["currentInputSchema"])
        self.assertEqual("AnalysisTaskInput/v5", businesses["analysis"]["targetInputSchema"])

    def test_policy_and_unknown_effects_fail_closed(self) -> None:
        """恢复策略必须是纯函数，未知外部效果不能被普通 Worker 偷跑。"""

        policy = self.contract["policyPort"]
        self.assertTrue(policy["pureDecisionOnly"])
        self.assertFalse(policy["mayReadDatabase"])
        self.assertFalse(policy["mayCallNetwork"])
        self.assertIn(
            "unknown_external_effect_never_maps_to_retry_safe_without_definitely_not_sent_evidence",
            self.contract["failClosedRules"],
        )
        self.assertIn(
            "callback_delivery_attempt",
            self.contract["excludedIndependentStateMachines"],
        )
        self.assertIn(
            "document_processing_external_operation",
            self.contract["excludedIndependentStateMachines"],
        )


class Stage2RuntimeTopologyContractAssetTests(unittest.TestCase):
    """冻结 Executor、UoW、Clock/租约和受理解锁的最终拓扑。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(
            RUNTIME_TOPOLOGY_CONTRACT_PATH.read_text(encoding="utf-8")
        )
        runtime_contract = json.loads(
            RUNTIME_CONFIG_CONTRACT_PATH.read_text(encoding="utf-8")
        )
        cls.task_config = {
            entry["key"]: entry
            for group in runtime_contract["groups"]
            if group["name"] == "task_runtime_current"
            for entry in group["entries"]
        }

    def test_one_kernel_has_three_type_isolated_instances(self) -> None:
        """三个业务实例必须复用一个内核，并且只能领取自己的 task type。"""

        topology = self.contract["executorTopology"]
        instances = topology["instances"]
        self.assertEqual(3, len(instances))
        self.assertEqual(
            {"report", "weaponry", "file"},
            {item["taskType"] for item in instances},
        )
        self.assertEqual(3, len({item["name"] for item in instances}))
        self.assertEqual(3, len({item["workerCountKey"] for item in instances}))
        self.assertTrue(topology["rules"]["oneKernelThreeInstances"])
        self.assertTrue(topology["rules"]["instanceScansOnlyOwnTaskType"])
        self.assertFalse(topology["rules"]["businessDispatcherOwnsSchedulingAlgorithm"])
        self.assertFalse(topology["rules"]["acceptedIdsCachedInMemory"])
        self.assertTrue(topology["rules"]["inflightTaskIdsBoundedByWorkerCount"])

    def test_fair_capacity_is_bounded_and_does_not_claim_global_fifo(self) -> None:
        """唯一重型许可必须跨业务无饥饿，同时不伪造跨业务严格顺序。"""

        topology = self.contract["executorTopology"]
        fair = topology["fairCapacity"]
        self.assertEqual(
            "strict_round_robin_over_nonempty_business_queues_v1",
            fair["algorithm"],
        )
        self.assertEqual(["report", "weaponry", "file"], fair["businessOrder"])
        self.assertEqual(1, fair["initialCapacity"])
        self.assertEqual(1, fair["maxConsecutiveGrantsWhenAnotherBusinessWaits"])
        self.assertEqual("worker_count", fair["boundedWaitersPerBusiness"])
        self.assertFalse(topology["rules"]["crossBusinessStrictFifoPromised"])
        self.assertTrue(fair["cancelAndStopInterruptible"])
        self.assertTrue(fair["permitReleaseInFinally"])

        heavy_key = fair["capacityKey"]
        self.assertEqual(str(fair["initialCapacity"]), self.task_config[heavy_key]["default"])
        layers = {item["name"] for item in topology["limiterLayers"]}
        self.assertEqual(
            {
                "business_worker_slot",
                "shared_heavy_business",
                "anythingllm_ingestion",
                "model_call",
                "document_processing",
                "callback_delivery",
                "resource_maintenance",
            },
            layers,
        )
        permit = topology["permitProtocol"]
        self.assertTrue(permit["heavyAndOperationPermitGrantedAtomically"])
        self.assertTrue(permit["nestedBlockingPermitWaitForbidden"])
        self.assertFalse(permit["databaseTransactionHeldWhileWaiting"])
        self.assertFalse(permit["callbackConsumesBusinessHeavyPermit"])

    def test_unit_of_work_cases_are_complete_and_external_io_stays_outside(self) -> None:
        """跨 Store 原子组必须共用连接，慢 I/O 不能进入写事务。"""

        unit_of_work = self.contract["unitOfWork"]
        rules = unit_of_work["rules"]
        self.assertEqual("BEGIN IMMEDIATE", unit_of_work["infrastructure"]["beginMode"])
        self.assertTrue(rules["storesShareOneConnectionPerUnitOfWork"])
        self.assertFalse(rules["storeMayCommit"])
        self.assertFalse(rules["storeMayRollback"])
        self.assertFalse(rules["storeMayCloseConnection"])
        self.assertTrue(rules["applicationMustCommitExplicitly"])
        self.assertTrue(rules["normalExitWithoutCommitRollsBack"])
        self.assertFalse(rules["nestedUnitOfWorkAllowed"])
        self.assertFalse(rules["connectionSharedAcrossThreads"])
        self.assertFalse(rules["networkInsideTransactionAllowed"])
        self.assertFalse(rules["modelCallInsideTransactionAllowed"])
        self.assertFalse(rules["fileConversionInsideTransactionAllowed"])
        self.assertFalse(rules["objectDeletionInsideTransactionAllowed"])
        self.assertFalse(rules["blockingPermitWaitInsideTransactionAllowed"])

        self.assertEqual(
            {
                "admission",
                "claim",
                "start",
                "heartbeat",
                "begin_external_step",
                "complete_step",
                "progress",
                "business_terminal",
                "reaper_classify",
                "recovery_claim",
                "recovery_heartbeat",
                "recovery_operation_intent",
                "recovery_observation",
                "recovery_decision",
            },
            {item["name"] for item in unit_of_work["atomicCases"]},
        )
        begin_external = next(
            item
            for item in unit_of_work["atomicCases"]
            if item["name"] == "begin_external_step"
        )
        self.assertIn("step_intent", begin_external["facts"])
        self.assertEqual(["external_io"], begin_external["outside"])

    def test_clock_owner_and_lease_defaults_satisfy_frozen_inequalities(self) -> None:
        """默认配置必须与归属矩阵一致，并为 busy/jitter 留出安全余量。"""

        clock = self.contract["clockAndLease"]
        defaults = clock["defaults"]
        for entry in defaults.values():
            self.assertEqual(
                str(entry["seconds"]),
                self.task_config[entry["key"]]["default"],
            )

        heartbeat = defaults["heartbeat"]["seconds"]
        busy = defaults["sqliteBusy"]["seconds"]
        jitter = defaults["clockJitter"]["seconds"]
        minimum_lease = 3 * heartbeat + 2 * busy + jitter
        self.assertGreaterEqual(defaults["taskLease"]["seconds"], minimum_lease)
        self.assertGreaterEqual(defaults["recoveryLease"]["seconds"], minimum_lease)
        self.assertLessEqual(defaults["reaperScan"]["seconds"], defaults["taskLease"]["seconds"])
        self.assertLessEqual(defaults["reaperScan"]["seconds"], defaults["recoveryLease"]["seconds"])
        self.assertGreaterEqual(
            defaults["stopGrace"]["seconds"],
            heartbeat + busy,
        )

        self.assertRegex("2026-08-12T01:02:03.123456Z", clock["persistedRegex"])
        self.assertFalse(clock["monotonicPersisted"])
        self.assertTrue(clock["fakeClockRequiredForLeaseTests"])
        self.assertFalse(clock["realSleepUsedToProveLease"])
        self.assertFalse(clock["owner"]["ownerIdIsAuthority"])
        self.assertFalse(clock["owner"]["leaseTokenLogged"])
        self.assertEqual(
            {"task_id", "attempt_no", "lease_token", "fencing_token", "lease_expires_at"},
            set(clock["owner"]["completeAuthorityFields"]),
        )

    def test_admission_and_unlock_matrix_preserves_public_contract(self) -> None:
        """内部恢复隔离不得新增公开状态，也不得把 Callback/清理变成业务重跑。"""

        admission = self.contract["admissionAndUnlock"]
        self.assertFalse(admission["publicStateExpansion"])
        self.assertFalse(admission["publicParameterChange"])
        rules = admission["globalRules"]
        self.assertFalse(rules["timeoutAloneUnlocks"])
        self.assertFalse(rules["manualSqlUnlockAllowed"])
        self.assertFalse(rules["checkTaskMayRestartBusinessStep"])
        self.assertFalse(rules["unknownProjectedAsFakePublicFailure"])
        self.assertFalse(rules["unrelatedDifferentBusinessKeyBlockedByUnknown"])
        self.assertFalse(rules["terminalCleanupUnknownBlocksNewSubmission"])
        self.assertTrue(rules["unlockRequiresPersistedRecoveryDecision"])

        matrix = {item["scope"]: item for item in admission["matrix"]}
        expected_scopes = {
            "report_core_unknown",
            "report_terminal_cleanup_unknown",
            "weaponry_core_unknown",
            "weaponry_terminal_cleanup_unknown",
            "analysis_core_unknown",
            "analysis_terminal_cleanup_unknown",
            "callback_delivery_unknown",
        }
        self.assertEqual(expected_scopes, set(matrix))
        for scope, item in matrix.items():
            with self.subTest(scope=scope):
                self.assertTrue(item["sameKeyAdmission"])
                self.assertTrue(item["differentKeyAdmission"])
                self.assertTrue(item["publicProjection"])
                self.assertTrue(item["blockedEffects"])
                self.assertTrue(item["unlockEvidence"])
                self.assertTrue(item["unlockDecision"])
        self.assertIn(
            "business_step_replay",
            matrix["callback_delivery_unknown"]["blockedEffects"],
        )
        self.assertIn(
            "whole_batch_implicit_supersede",
            matrix["analysis_core_unknown"]["blockedEffects"],
        )


if __name__ == "__main__":
    unittest.main()
