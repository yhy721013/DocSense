"""阶段 1D-5 武器谱 Dispatcher、严格配置和离线组合根验收。"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from itertools import count
from pathlib import Path
import sqlite3
import threading
import time
import unittest

from app.modules.tasks.adapters import (
    FileProcessSingletonGuard,
    LegacyTaskCommandAdapter,
    UploadTaskLimiter,
)
from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.tasks.ports import (
    TaskClaimOutcome,
    TaskSubmissionCommand,
    TaskSubmissionOutcome,
)
from app.modules.weaponry.adapters import (
    LocalWeaponryTaskDispatcher,
    WeaponryInfrastructureConfig,
    WeaponryInfrastructureConfigurationError,
    WeaponryRuntimeCapabilities,
    WeaponryTaskCommandCodec,
    build_weaponry_runtime_policies,
    load_weaponry_infrastructure_config,
)
from app.modules.weaponry.application import (
    RunWeaponryOutcome,
    RunWeaponryResult,
    WeaponryResourceRecoverySweepResult,
)
from app.modules.weaponry.composition import (
    compose_weaponry_application_services,
)
from app.modules.weaponry.domain import (
    WEAPONRY_INPUT_SCHEMA_VERSION,
    FrozenJsonObject,
    WeaponryDocumentScope,
    WeaponryFieldSpecification,
    WeaponrySubmission,
)
from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir
from tests.fakes import (
    FakeAuxiliaryGuidancePort,
    FakeEvidenceExtractionPort,
    FakeTargetEvidenceRetrievalPort,
    FakeWeaponryCallbackPort,
    FakeWeaponryDocumentScopePort,
    FakeWeaponryExternalResourceCleanupPort,
    FakeWeaponryInteractionAuditPort,
    FakeWeaponryProgressPublisherPort,
    FakeWeaponryResourceStorePort,
    FakeWeaponryTaskCommandPort,
    FakeWeaponryTranslationPort,
    WeaponryInvocationRecorder,
)


def _config(**overrides: object) -> WeaponryInfrastructureConfig:
    values: dict[str, object] = {
        "runtime_mode": "single_instance",
        "scan_interval_seconds": 0.02,
        "accepted_batch_size": 7,
        "dispatch_failure_retry_seconds": 1.0,
        "maintenance_interval_seconds": 0.02,
        "maintenance_limit": 5,
        "running_sample_limit": 5,
        "stop_timeout_seconds": 0.5,
        "cleanup_http_timeout_seconds": 1.0,
        "cleanup_lease_seconds": 7.0,
        "provider_fingerprint": "stage1d5-provider-v1",
        "embedding_fingerprint": "stage1d5-embedding-v1",
        "document_processing_fingerprint": "stage1d5-processing-v1",
        "extraction_model_fingerprint": "stage1d5-extraction-v1",
    }
    values.update(overrides)
    return WeaponryInfrastructureConfig(**values)  # type: ignore[arg-type]


def _capabilities(
    config: WeaponryInfrastructureConfig,
) -> WeaponryRuntimeCapabilities:
    """由测试 Fake 显式声明能力，禁止生产代码从期望配置反推真实能力。"""

    return WeaponryRuntimeCapabilities(
        provider_fingerprint=config.provider_fingerprint,
        embedding_fingerprint=config.embedding_fingerprint,
        document_processing_fingerprint=(
            config.document_processing_fingerprint
        ),
        extraction_model_fingerprint=config.extraction_model_fingerprint,
        query_version=config.query_version,
        score_semantics=config.score_semantics,
        score_protocol=config.score_protocol,
        ranking_strategy=config.ranking_strategy,
        reference_filter_strategy=config.reference_filter_strategy,
        extraction_context_strategy=config.extraction_context_strategy,
    )


def _submission(architecture_id: int, config: WeaponryInfrastructureConfig) -> WeaponrySubmission:
    template = {
        "templateClassifyId": 1,
        "fieldName": "主要用途",
        "fieldDescription": "说明装备承担的任务",
        "fieldType": "INPUT",
        "analyseData": "",
        "analyseDataSource": [],
    }
    policies = build_weaponry_runtime_policies(config)
    return WeaponrySubmission(
        architecture_id=architecture_id,
        request_projection=FrozenJsonObject.from_mapping(
            {
                "businessType": "weaponry",
                "params": {
                    "architectureId": architecture_id,
                    "filePathList": [],
                    "weaponryTemplateFieldList": [template],
                },
            },
            name="stage1d5_request",
        ),
        fields=(WeaponryFieldSpecification.from_mapping(template),),
        document_scope=WeaponryDocumentScope(
            mode="category",
            requested_file_names=(),
            documents=(),
        ),
        evidence_selection_policy=policies.evidence_selection,
        execution_policy=policies.execution,
        auxiliary_guidance_policy=policies.auxiliary_guidance,
        trace_id=f"trace-stage1d5-{architecture_id}",
    )


def _accept(task_commands, architecture_id: int, config: WeaponryInfrastructureConfig) -> TaskId:
    submission = _submission(architecture_id, config)
    result = task_commands.create_if_allowed(
        TaskSubmissionCommand(
            task_type="weaponry",
            business_ref=TaskBusinessRef("weaponry", str(architecture_id)),
            input_schema_version=WEAPONRY_INPUT_SCHEMA_VERSION,
            submission=submission,
            trace_id=submission.trace_id,
        )
    )
    if result.outcome is not TaskSubmissionOutcome.ACCEPTED:
        raise AssertionError(f"测试任务受理失败: {result.outcome}")
    assert result.execution is not None
    return result.execution.task_id


def _wait_until(predicate, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class _BoundedMaintenanceStub:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.calls: list[int] = []
        self._lock = threading.Lock()

    def run_once(
        self,
        *,
        limit: int,
        stop_requested: Callable[[], bool] | None = None,
    ) -> object:
        with self._lock:
            self.calls.append(limit)
        if stop_requested is not None and stop_requested():
            return {"limit": limit, "stopped": True}
        if self.error is not None:
            raise self.error
        return {"limit": limit}


class _ProgressingResourceMaintenanceStub(_BoundedMaintenanceStub):
    """模拟“空启动扫描 → 清理一项仍 pending → 最后一项 cleaned”。"""

    def run_once(
        self,
        *,
        limit: int,
        stop_requested: Callable[[], bool] | None = None,
    ) -> object:
        with self._lock:
            self.calls.append(limit)
            call_count = len(self.calls)
        if stop_requested is not None and stop_requested():
            return WeaponryResourceRecoverySweepResult(
                requested_limit=limit,
                scanned_count=0,
                cleaned_count=0,
                cleaned_resource_count=0,
                pending_count=0,
                quarantined_count=0,
                not_ready_count=0,
                missing_count=0,
                failed_count=0,
            )
        if call_count == 1:
            return WeaponryResourceRecoverySweepResult(
                requested_limit=limit,
                scanned_count=0,
                cleaned_count=0,
                cleaned_resource_count=0,
                pending_count=0,
                quarantined_count=0,
                not_ready_count=0,
                missing_count=0,
                failed_count=0,
            )
        if call_count == 2:
            return WeaponryResourceRecoverySweepResult(
                requested_limit=limit,
                scanned_count=1,
                cleaned_count=0,
                cleaned_resource_count=1,
                pending_count=1,
                quarantined_count=0,
                not_ready_count=0,
                missing_count=0,
                failed_count=0,
            )
        return WeaponryResourceRecoverySweepResult(
            requested_limit=limit,
            scanned_count=1,
            cleaned_count=1,
            cleaned_resource_count=1,
            pending_count=0,
            quarantined_count=0,
            not_ready_count=0,
            missing_count=0,
            failed_count=0,
        )


class _ClaimingRunner:
    def __init__(self, task_commands) -> None:
        self.task_commands = task_commands
        self.executed: list[TaskId] = []
        self._lock = threading.Lock()
        self.all_executed = threading.Event()
        self.expected_count = 0

    def execute(self, task_id: TaskId) -> RunWeaponryResult:
        claim = self.task_commands.claim(task_id)
        if claim.outcome is not TaskClaimOutcome.CLAIMED:
            raise AssertionError(f"未取得执行权: {claim.outcome}")
        with self._lock:
            self.executed.append(task_id)
            if len(self.executed) >= self.expected_count:
                self.all_executed.set()
        return RunWeaponryResult(
            task_id,
            RunWeaponryOutcome.SUCCEEDED,
            selected_evidence_count=1,
        )


class _BrokenReleaseLimiter:
    """归还许可时损坏，验证 Dispatcher fail-closed readiness。"""

    def acquire_interruptibly(
        self,
        cancel_requested,
        *,
        poll_interval_seconds: float,
    ) -> bool:
        return not cancel_requested()

    def release(self) -> None:
        raise RuntimeError("forced limiter release failure")


class _BrokenReleaseProcessGuard:
    """模拟线程已经退出但 OS 单实例锁释放报告失败。"""

    def acquire(self) -> bool:
        return True

    def release(self) -> None:
        raise RuntimeError("forced process guard release failure")


class _TrackingEnvironment(Mapping[str, str]):
    """记录读取过的环境键，证明术语 false 分支真正短路。"""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)
        self.accessed: list[str] = []

    def __getitem__(self, key: str) -> str:
        self.accessed.append(key)
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


class WeaponryInfrastructureConfigTests(unittest.TestCase):
    @staticmethod
    def _environment(**overrides: str) -> dict[str, str]:
        values = {
            "DOCSENSE_WEAPONRY_PROVIDER_FINGERPRINT": "provider-v1",
            "DOCSENSE_WEAPONRY_EMBEDDING_FINGERPRINT": "embedding-v1",
            "DOCSENSE_WEAPONRY_DOCUMENT_PROCESSING_FINGERPRINT": "processing-v1",
            "DOCSENSE_WEAPONRY_EXTRACTION_MODEL_FINGERPRINT": "model-v1",
            "WEAPONRY_TERMS_RULE_CONTEXT_ENABLED": "false",
        }
        values.update(overrides)
        return values

    def test_terms_disabled_does_not_read_workspace_directory_or_catalog(self) -> None:
        environment = _TrackingEnvironment(
            self._environment(
                WEAPONRY_TERMS_WORKSPACE_NAME="must-not-read",
                WEAPONRY_TERMS_DIR="must-not-read",
                WEAPONRY_TERMS_CATALOG_FINGERPRINT="must-not-read",
                DOCSENSE_WEAPONRY_TERMS_CANDIDATE_TOP_N="999",
                DOCSENSE_WEAPONRY_TERMS_MAX_CONTEXT_CHARS="999",
            )
        )
        config = load_weaponry_infrastructure_config(environment)
        self.assertFalse(config.terms_rule_context_enabled)
        forbidden = {
            "WEAPONRY_TERMS_WORKSPACE_NAME",
            "WEAPONRY_TERMS_DIR",
            "WEAPONRY_TERMS_CATALOG_FINGERPRINT",
            "DOCSENSE_WEAPONRY_TERMS_CANDIDATE_TOP_N",
            "DOCSENSE_WEAPONRY_TERMS_MAX_CONTEXT_CHARS",
        }
        self.assertTrue(forbidden.isdisjoint(environment.accessed))

    def test_terms_enabled_defers_policy_until_automatic_fingerprint_is_frozen(
        self,
    ) -> None:
        config = load_weaponry_infrastructure_config(
            self._environment(
                WEAPONRY_TERMS_RULE_CONTEXT_ENABLED="true",
                WEAPONRY_TERMS_WORKSPACE_NAME="terms-read-only",
                WEAPONRY_TERMS_DIR="terms",
                WEAPONRY_TERMS_CATALOG_FINGERPRINT="terms-20260719",
                DOCSENSE_WEAPONRY_TERMS_CANDIDATE_TOP_N="6",
                DOCSENSE_WEAPONRY_TERMS_MAX_CONTEXT_CHARS="1200",
            )
        )
        self.assertTrue(config.terms_rule_context_enabled)
        self.assertIsNone(config.terms_catalog_fingerprint)
        policies = build_weaponry_runtime_policies(
            replace(
                config,
                terms_catalog_fingerprint=(
                    "terms-manifest-v1:sha256:" + ("a" * 64)
                ),
            )
        )
        self.assertEqual(
            "terms-rules-column-compact-v2",
            policies.auxiliary_guidance.policy_id,
        )
        self.assertEqual(1200, policies.auxiliary_guidance.max_context_chars)

    def test_deprecated_mode_one_and_unknown_modes_fail_before_composition(self) -> None:
        for mode in ("1", "legacy", ""):
            with self.subTest(mode=mode):
                with self.assertRaises(WeaponryInfrastructureConfigurationError):
                    load_weaponry_infrastructure_config(
                        self._environment(WEAPONRY_ANALYSE_MODE=mode)
                    )
        accepted = load_weaponry_infrastructure_config(
            self._environment(WEAPONRY_ANALYSE_MODE="2")
        )
        self.assertEqual("single_instance", accepted.runtime_mode)

    def test_strategy_protocol_and_runtime_mode_mismatch_fail_fast(self) -> None:
        cases = (
            {"DOCSENSE_WEAPONRY_RUNTIME_MODE": "cluster"},
            {"DOCSENSE_WEAPONRY_QUERY_VERSION": "changed-query"},
            {"DOCSENSE_WEAPONRY_SCORE_PROTOCOL": "changed-score"},
            {
                "DOCSENSE_WEAPONRY_REFERENCE_FILTER_STRATEGY":
                "changed-reference-filter"
            },
            {
                "DOCSENSE_WEAPONRY_EXTRACTION_CONTEXT_STRATEGY":
                "evidence_only_context_v1"
            },
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(WeaponryInfrastructureConfigurationError):
                    load_weaponry_infrastructure_config(
                        self._environment(**overrides)
                    )

    def test_config_has_no_query_or_selected_evidence_character_limit(self) -> None:
        config = _config()
        self.assertFalse(hasattr(config, "query_max_chars"))
        self.assertFalse(hasattr(config, "selected_evidence_max_chars"))

    def test_production_gate_requirement_is_strict_and_defaults_to_development(self) -> None:
        development = load_weaponry_infrastructure_config(self._environment())
        production = load_weaponry_infrastructure_config(
            self._environment(DOCSENSE_WEAPONRY_REQUIRE_PRODUCTION_GATE="true")
        )

        self.assertFalse(development.production_gate_required)
        self.assertTrue(production.production_gate_required)
        with self.assertRaises(WeaponryInfrastructureConfigurationError):
            load_weaponry_infrastructure_config(
                self._environment(
                    DOCSENSE_WEAPONRY_REQUIRE_PRODUCTION_GATE="maybe"
                )
            )


class LocalWeaponryTaskDispatcherTests(unittest.TestCase):
    def _commands(self, root: Path):
        service = LLMTaskService(str(root / "tasks.sqlite3"))
        task_numbers = count(1)
        clock_numbers = count(0)
        commands = LegacyTaskCommandAdapter(
            service,
            WeaponryTaskCommandCodec(),
            task_id_factory=lambda: TaskId(
                f"weaponry-dispatch-{next(task_numbers):04d}"
            ),
            clock=lambda: (
                "2026-07-19T00:00:"
                f"{next(clock_numbers):02d}+00:00"
            ),
        )
        return service, commands

    @staticmethod
    def _dispatcher(
        *,
        commands,
        runner,
        config: WeaponryInfrastructureConfig,
        lock_path: Path,
        resource: _BoundedMaintenanceStub | None = None,
        callback: _BoundedMaintenanceStub | None = None,
        limiter: UploadTaskLimiter | None = None,
        process_guard: object | None = None,
    ) -> LocalWeaponryTaskDispatcher:
        return LocalWeaponryTaskDispatcher(
            task_commands=commands,
            queue_inspector=commands,
            runner=runner,
            resource_maintenance=resource or _BoundedMaintenanceStub(),
            callback_guard_maintenance=callback or _BoundedMaintenanceStub(),
            config=config,
            execution_limiter=limiter or UploadTaskLimiter(1),
            process_guard=(
                process_guard
                or FileProcessSingletonGuard(
                    lock_path,
                    component_name="武器谱 Dispatcher",
                )
            ),  # type: ignore[arg-type]
        )

    def test_fifty_persisted_tasks_use_one_worker_zero_buffer_and_fifo(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            service, commands = self._commands(root)
            config = _config(accepted_batch_size=7)
            expected = tuple(
                _accept(commands, 20000 + index, config) for index in range(50)
            )
            runner = _ClaimingRunner(commands)
            runner.expected_count = 50
            dispatcher = self._dispatcher(
                commands=commands,
                runner=runner,
                config=config,
                lock_path=root / "locks" / "weaponry.lock",
            )
            for task_id in expected:
                dispatcher.dispatch(task_id)
            before = dispatcher.snapshot()
            self.assertEqual(50, before.dispatch_count)
            self.assertEqual(49, before.merged_wakeup_count)
            self.assertEqual(0, before.buffered_task_count)
            try:
                dispatcher.start()
                dispatcher.start()
                self.assertTrue(runner.all_executed.wait(timeout=10))
                snapshot = dispatcher.snapshot()
                self.assertEqual(1, snapshot.worker_thread_count)
                self.assertEqual(3, snapshot.maintenance_thread_count)
                self.assertEqual(0, snapshot.buffered_task_count)
                self.assertEqual(50, snapshot.execution_count)
                self.assertEqual(expected, tuple(runner.executed))
                self.assertEqual((), commands.list_accepted("weaponry", limit=100))
                with sqlite3.connect(service.db_path) as connection:
                    row_count = connection.execute(
                        "SELECT COUNT(*) FROM llm_task_executions WHERE business_type='weaponry'"
                    ).fetchone()[0]
                self.assertEqual(50, row_count)
            finally:
                dispatcher.close()

    def test_poison_task_is_persistently_cooled_without_starving_fifo(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            service, commands = self._commands(root)
            config = _config(accepted_batch_size=1)
            poison = _accept(commands, 30001, config)
            healthy = _accept(commands, 30002, config)
            processed = threading.Event()

            class _Runner:
                def execute(self, task_id: TaskId) -> RunWeaponryResult:
                    if task_id == poison:
                        raise RuntimeError("permanent pre-claim error")
                    claim = commands.claim(task_id)
                    if claim.outcome is not TaskClaimOutcome.CLAIMED:
                        raise AssertionError(claim.outcome)
                    processed.set()
                    return RunWeaponryResult(
                        task_id,
                        RunWeaponryOutcome.SUCCEEDED,
                        selected_evidence_count=1,
                    )

            dispatcher = self._dispatcher(
                commands=commands,
                runner=_Runner(),
                config=config,
                lock_path=root / "locks" / "weaponry.lock",
            )
            try:
                dispatcher.start()
                self.assertTrue(processed.wait(timeout=5))
                time.sleep(0.12)
                snapshot = dispatcher.snapshot()
                self.assertEqual(1, snapshot.execution_failure_count)
                self.assertEqual(1, snapshot.accepted_deferral_count)
                self.assertEqual("accepted", commands.get_execution(poison).execution_state)
                self.assertEqual("running", commands.get_execution(healthy).execution_state)
                with sqlite3.connect(service.db_path) as connection:
                    retry = connection.execute(
                        "SELECT dispatch_failure_count, next_dispatch_at "
                        "FROM llm_task_executions WHERE execution_id=?",
                        (poison.value,),
                    ).fetchone()
                self.assertEqual(1, retry[0])
                self.assertTrue(retry[1])
            finally:
                dispatcher.close()

    def test_two_maintenance_tasks_continue_while_model_execution_blocks(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            _service, commands = self._commands(root)
            config = _config()
            _accept(commands, 40001, config)
            entered = threading.Event()
            release = threading.Event()
            resource = _BoundedMaintenanceStub()
            callback = _BoundedMaintenanceStub()

            class _Runner:
                def execute(self, task_id: TaskId) -> RunWeaponryResult:
                    commands.claim(task_id)
                    entered.set()
                    release.wait(timeout=5)
                    return RunWeaponryResult(
                        task_id,
                        RunWeaponryOutcome.SUCCEEDED,
                        selected_evidence_count=1,
                    )

            dispatcher = self._dispatcher(
                commands=commands,
                runner=_Runner(),
                config=config,
                lock_path=root / "locks" / "weaponry.lock",
                resource=resource,
                callback=callback,
            )
            try:
                dispatcher.start()
                self.assertTrue(entered.wait(timeout=5))
                self.assertTrue(
                    _wait_until(
                        lambda: len(resource.calls) >= 3 and len(callback.calls) >= 3
                    )
                )
                snapshot = dispatcher.snapshot()
                self.assertGreaterEqual(snapshot.resource_maintenance_count, 3)
                self.assertGreaterEqual(snapshot.callback_guard_maintenance_count, 3)
                self.assertEqual(3, snapshot.maintenance_thread_count)
            finally:
                release.set()
                dispatcher.close()

    def test_running_is_observed_only_and_second_local_owner_is_rejected(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            service, commands = self._commands(root)
            config = _config()
            running_id = _accept(commands, 50001, config)
            self.assertEqual(TaskClaimOutcome.CLAIMED, commands.claim(running_id).outcome)
            first_runner = _ClaimingRunner(commands)
            first_runner.expected_count = 1
            first = self._dispatcher(
                commands=commands,
                runner=first_runner,
                config=config,
                lock_path=root / "locks" / "weaponry.lock",
            )
            second = self._dispatcher(
                commands=commands,
                runner=_ClaimingRunner(commands),
                config=config,
                lock_path=root / "locks" / "weaponry.lock",
            )
            try:
                first.start()
                with self.assertRaisesRegex(RuntimeError, "单实例"):
                    second.start()
                time.sleep(0.08)
                self.assertEqual([], first_runner.executed)
                self.assertEqual(
                    "running",
                    service.get_task_execution(running_id.value)["execution_state"],
                )
            finally:
                second.close()
                first.close()

    def test_result_categories_have_separate_metrics(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            _service, commands = self._commands(root)
            config = _config()
            task_ids = tuple(
                _accept(commands, 60000 + index, config) for index in range(6)
            )
            results = {
                task_ids[0]: RunWeaponryResult(
                    task_ids[0],
                    RunWeaponryOutcome.SUCCEEDED,
                    selected_evidence_count=0,
                    model_call_count=0,
                ),
                task_ids[1]: RunWeaponryResult(
                    task_ids[1],
                    RunWeaponryOutcome.SUCCEEDED,
                    diagnostic_error_codes=(
                        "provider_payload_too_large",
                    ),
                ),
                task_ids[2]: RunWeaponryResult(
                    task_ids[2],
                    RunWeaponryOutcome.SUCCEEDED,
                    diagnostic_error_codes=(
                        "evidence_selection_protocol_invalid",
                    ),
                ),
                task_ids[3]: RunWeaponryResult(
                    task_ids[3],
                    RunWeaponryOutcome.FAILED,
                    error_code="provider_payload_too_large",
                ),
                task_ids[4]: RunWeaponryResult(
                    task_ids[4],
                    RunWeaponryOutcome.FAILED,
                    error_code="evidence_selection_protocol_invalid",
                ),
                task_ids[5]: RunWeaponryResult(
                    task_ids[5],
                    RunWeaponryOutcome.FAILED,
                    error_code="callback_delivery_failed",
                ),
            }

            class _Runner:
                def execute(self, task_id: TaskId) -> RunWeaponryResult:
                    claim = commands.claim(task_id)
                    if claim.outcome is not TaskClaimOutcome.CLAIMED:
                        raise AssertionError(claim.outcome)
                    return results[task_id]

            dispatcher = self._dispatcher(
                commands=commands,
                runner=_Runner(),
                config=config,
                lock_path=root / "locks" / "weaponry.lock",
            )
            try:
                dispatcher.start()
                self.assertTrue(
                    _wait_until(lambda: dispatcher.snapshot().execution_count == 6)
                )
                snapshot = dispatcher.snapshot()
                self.assertEqual(1, snapshot.business_zero_result_count)
                self.assertEqual(2, snapshot.provider_capacity_error_count)
                self.assertEqual(2, snapshot.input_contract_error_count)
                self.assertEqual(1, snapshot.other_failed_result_count)
                self.assertEqual(3, snapshot.succeeded_result_count)
            finally:
                dispatcher.close()

    def test_stop_cancels_shared_limiter_wait_without_starting_business(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            service, commands = self._commands(root)
            config = _config()
            task_id = _accept(commands, 70001, config)
            limiter = UploadTaskLimiter(1)
            self.assertTrue(
                limiter.acquire_interruptibly(
                    lambda: False,
                    poll_interval_seconds=0.01,
                )
            )
            executed = threading.Event()

            class _Runner:
                def execute(self, current_task_id: TaskId) -> RunWeaponryResult:
                    executed.set()
                    return RunWeaponryResult(
                        current_task_id,
                        RunWeaponryOutcome.SUCCEEDED,
                    )

            dispatcher = self._dispatcher(
                commands=commands,
                runner=_Runner(),
                config=config,
                lock_path=root / "locks" / "weaponry.lock",
                limiter=limiter,
            )
            limiter_held = True
            try:
                dispatcher.start()
                self.assertTrue(
                    _wait_until(
                        lambda: dispatcher.snapshot().waiting_task_id == task_id
                    )
                )
                self.assertTrue(dispatcher.stop(timeout_seconds=0.5))
                limiter.release()
                limiter_held = False
                time.sleep(0.08)
                self.assertFalse(executed.is_set())
                self.assertEqual(
                    "accepted",
                    service.get_task_execution(task_id.value)["execution_state"],
                )
            finally:
                if limiter_held:
                    limiter.release()
                dispatcher.close()

    def test_stop_timeout_does_not_reset_running_execution(self) -> None:
        """停机超时只能报告未停完，不能猜测重置正在执行的持久任务。"""

        with workspace_tempdir() as tmp:
            root = Path(tmp)
            service, commands = self._commands(root)
            config = _config(stop_timeout_seconds=0.05)
            task_id = _accept(commands, 70501, config)
            entered = threading.Event()
            release = threading.Event()

            class _BlockingRunner:
                def execute(self, current_task_id: TaskId) -> RunWeaponryResult:
                    claim = commands.claim(current_task_id)
                    if claim.outcome is not TaskClaimOutcome.CLAIMED:
                        raise AssertionError(claim.outcome)
                    entered.set()
                    if not release.wait(timeout=5):
                        raise AssertionError("测试未及时释放运行中任务")
                    return RunWeaponryResult(
                        current_task_id,
                        RunWeaponryOutcome.SUCCEEDED,
                        selected_evidence_count=1,
                    )

            dispatcher = self._dispatcher(
                commands=commands,
                runner=_BlockingRunner(),
                config=config,
                lock_path=root / "locks" / "weaponry.lock",
            )
            try:
                dispatcher.start()
                self.assertTrue(entered.wait(timeout=5))
                self.assertFalse(dispatcher.stop(timeout_seconds=0.05))
                timed_out = dispatcher.snapshot()
                self.assertEqual("stopping", timed_out.lifecycle_state)
                self.assertEqual(task_id, timed_out.current_task_id)
                self.assertEqual(
                    "running",
                    service.get_task_execution(task_id.value)["execution_state"],
                )

                release.set()
                self.assertTrue(
                    _wait_until(
                        lambda: dispatcher.snapshot().worker_thread_count == 0
                    )
                )
                # Runner 替身故意不写终态；Dispatcher 也不得越权代写或重置。
                self.assertEqual(
                    "running",
                    service.get_task_execution(task_id.value)["execution_state"],
                )
            finally:
                release.set()
                dispatcher.close()

            self.assertEqual("closed", dispatcher.snapshot().lifecycle_state)

    def test_limiter_release_failure_clears_readiness_and_records_fatal_error(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            _service, commands = self._commands(root)
            config = _config()
            _accept(commands, 71001, config)
            runner = _ClaimingRunner(commands)
            runner.expected_count = 1
            dispatcher = self._dispatcher(
                commands=commands,
                runner=runner,
                config=config,
                lock_path=root / "locks" / "weaponry.lock",
                limiter=_BrokenReleaseLimiter(),  # type: ignore[arg-type]
            )
            try:
                with self.assertLogs(
                    "app.modules.weaponry.adapters.local_dispatcher",
                    level="CRITICAL",
                ):
                    dispatcher.start()
                    self.assertTrue(
                        _wait_until(
                            lambda: bool(dispatcher.snapshot().fatal_error)
                        )
                    )
                snapshot = dispatcher.snapshot()
                self.assertFalse(snapshot.ready)
                self.assertEqual(
                    "execution_limiter_release_failed",
                    snapshot.fatal_error,
                )
            finally:
                dispatcher.close()

    def test_process_guard_release_failure_is_exposed_as_fatal(self) -> None:
        """线程退出不代表单实例协调必然健康，锁释放异常必须进入快照。"""

        with workspace_tempdir() as tmp:
            root = Path(tmp)
            _service, commands = self._commands(root)
            dispatcher = self._dispatcher(
                commands=commands,
                runner=_ClaimingRunner(commands),
                config=_config(),
                lock_path=root / "unused.lock",
                process_guard=_BrokenReleaseProcessGuard(),
            )
            with self.assertLogs(
                "app.modules.weaponry.adapters.local_dispatcher",
                level="CRITICAL",
            ):
                dispatcher.start()
                self.assertTrue(dispatcher.stop(timeout_seconds=0.5))

            snapshot = dispatcher.snapshot()
            self.assertFalse(snapshot.ready)
            self.assertEqual(
                "process_guard_release_failed",
                snapshot.fatal_error,
            )
            dispatcher.close()

    def test_shared_limiter_serializes_two_business_dispatchers(self) -> None:
        """两个独立 Dispatcher 同时就绪时，重型执行仍只有一个在途。"""

        with workspace_tempdir() as tmp:
            root = Path(tmp)
            (root / "a").mkdir()
            (root / "b").mkdir()
            _service_a, commands_a = self._commands(root / "a")
            _service_b, commands_b = self._commands(root / "b")
            config = _config()
            _accept(commands_a, 72001, config)
            _accept(commands_b, 72002, config)
            limiter = UploadTaskLimiter(1)
            state_lock = threading.Lock()
            current = 0
            maximum = 0
            completed = threading.Event()
            completed_count = 0

            class _Runner:
                def __init__(self, commands) -> None:
                    self.commands = commands

                def execute(self, task_id: TaskId) -> RunWeaponryResult:
                    nonlocal current, maximum, completed_count
                    claim = self.commands.claim(task_id)
                    if claim.outcome is not TaskClaimOutcome.CLAIMED:
                        raise AssertionError(claim.outcome)
                    with state_lock:
                        current += 1
                        maximum = max(maximum, current)
                    time.sleep(0.05)
                    with state_lock:
                        current -= 1
                        completed_count += 1
                        if completed_count == 2:
                            completed.set()
                    return RunWeaponryResult(
                        task_id,
                        RunWeaponryOutcome.SUCCEEDED,
                        selected_evidence_count=1,
                    )

            first = self._dispatcher(
                commands=commands_a,
                runner=_Runner(commands_a),
                config=config,
                lock_path=root / "locks" / "weaponry-a.lock",
                limiter=limiter,
            )
            second = self._dispatcher(
                commands=commands_b,
                runner=_Runner(commands_b),
                config=config,
                lock_path=root / "locks" / "weaponry-b.lock",
                limiter=limiter,
            )
            try:
                first.start()
                second.start()
                self.assertTrue(completed.wait(timeout=5))
                self.assertEqual(1, maximum)
                self.assertIs(first.execution_limiter, second.execution_limiter)
            finally:
                first.close()
                second.close()

    def test_maintenance_failure_is_isolated_and_readiness_remains_true(self) -> None:
        with workspace_tempdir() as tmp:
            root = Path(tmp)
            _service, commands = self._commands(root)
            config = _config()
            resource = _BoundedMaintenanceStub(error=RuntimeError("resource down"))
            callback = _BoundedMaintenanceStub()
            runner = _ClaimingRunner(commands)
            dispatcher = self._dispatcher(
                commands=commands,
                runner=runner,
                config=config,
                lock_path=root / "locks" / "weaponry.lock",
                resource=resource,
                callback=callback,
            )
            try:
                with self.assertLogs(
                    "app.modules.weaponry.adapters.local_dispatcher",
                    level="ERROR",
                ):
                    dispatcher.start()
                    self.assertTrue(
                        _wait_until(
                            lambda: (
                                dispatcher.snapshot().resource_maintenance_failure_count
                                >= 2
                                and dispatcher.snapshot().callback_guard_maintenance_count
                                >= 2
                            )
                        )
                    )
                snapshot = dispatcher.snapshot()
                self.assertTrue(snapshot.ready)
                self.assertEqual("", snapshot.fatal_error)
            finally:
                dispatcher.close()

    def test_terminal_cleanup_wakes_maintenance_and_progress_continues_immediately(
        self,
    ) -> None:
        """终态只发常量空间提示；持久清理有进展时无需再等待固定周期。"""

        with workspace_tempdir() as tmp:
            root = Path(tmp)
            _service, commands = self._commands(root)
            config = _config(maintenance_interval_seconds=30.0)
            resource = _ProgressingResourceMaintenanceStub()
            callback = _BoundedMaintenanceStub()
            finished = threading.Event()

            class _CleanupPendingRunner:
                def execute(self, task_id: TaskId) -> RunWeaponryResult:
                    claim = commands.claim(task_id)
                    if claim.outcome is not TaskClaimOutcome.CLAIMED:
                        raise AssertionError(claim.outcome)
                    finished.set()
                    return RunWeaponryResult(
                        task_id,
                        RunWeaponryOutcome.SUCCEEDED,
                        cleanup_state="cleanup_pending",
                        selected_evidence_count=1,
                    )

            dispatcher = self._dispatcher(
                commands=commands,
                runner=_CleanupPendingRunner(),
                config=config,
                lock_path=root / "locks" / "weaponry.lock",
                resource=resource,
                callback=callback,
            )
            try:
                dispatcher.start()
                self.assertTrue(
                    _wait_until(
                        lambda: len(resource.calls) == 1 and len(callback.calls) == 1
                    )
                )
                task_id = _accept(commands, 73001, config)
                dispatcher.dispatch(task_id)

                self.assertTrue(finished.wait(timeout=1.0))
                self.assertTrue(
                    _wait_until(lambda: len(resource.calls) >= 3, timeout=1.0)
                )
                # Callback Guard 没有资源积压提示，不能被本次资源唤醒连带执行。
                self.assertEqual(1, len(callback.calls))
                self.assertGreaterEqual(
                    dispatcher.snapshot().resource_maintenance_count,
                    3,
                )
            finally:
                dispatcher.close()


class WeaponryOfflineCompositionTests(unittest.TestCase):
    def test_composition_is_lazy_and_uses_one_instance_chain(self) -> None:
        with workspace_tempdir() as tmp:
            recorder = WeaponryInvocationRecorder()
            tasks = FakeWeaponryTaskCommandPort(recorder)
            progress = FakeWeaponryProgressPublisherPort(recorder)
            retrieval = FakeTargetEvidenceRetrievalPort(recorder)
            extraction = FakeEvidenceExtractionPort(recorder)
            guidance = FakeAuxiliaryGuidancePort(recorder)
            translation = FakeWeaponryTranslationPort(recorder)
            audit = FakeWeaponryInteractionAuditPort(recorder)
            callbacks = FakeWeaponryCallbackPort(recorder)
            resources = FakeWeaponryResourceStorePort(recorder)
            limiter = UploadTaskLimiter(1)
            config = _config()
            before = {
                thread.ident
                for thread in threading.enumerate()
                if thread.name.startswith("docsense-weaponry")
            }
            services = compose_weaponry_application_services(
                task_commands=tasks,
                progress_publisher=progress,
                retrieval=retrieval,
                extraction=extraction,
                guidance=guidance,
                translation=translation,
                audit=audit,
                callbacks=callbacks,
                callback_recovery_source=callbacks,
                resources=resources,
                resource_cleaner=FakeWeaponryExternalResourceCleanupPort(recorder),
                document_scope=FakeWeaponryDocumentScopePort(),
                execution_limiter=limiter,
                process_guard=FileProcessSingletonGuard(
                    Path(tmp) / "locks" / "weaponry.lock"
                ),
                config=config,
                capabilities=_capabilities(config),
            )
            after = {
                thread.ident
                for thread in threading.enumerate()
                if thread.name.startswith("docsense-weaponry")
            }
            self.assertEqual(before, after)
            self.assertEqual("new", services.snapshot().lifecycle_state)
            self.assertIs(services.submit.dispatcher, services.dispatcher)
            self.assertIs(services.dispatcher.runner, services.runner)
            self.assertIs(services.runner.callbacks, callbacks)
            self.assertIs(services.execution_limiter, limiter)
            self.assertIs(services.submit.task_commands, services.runner.task_commands)
            services.close()

    def test_startup_gate_failure_releases_process_lock_before_worker_starts(
        self,
    ) -> None:
        with workspace_tempdir() as tmp:
            recorder = WeaponryInvocationRecorder()
            tasks = FakeWeaponryTaskCommandPort(recorder)
            callbacks = FakeWeaponryCallbackPort(recorder)
            config = _config()
            lock_path = Path(tmp) / "locks" / "weaponry.lock"
            services = compose_weaponry_application_services(
                task_commands=tasks,
                progress_publisher=FakeWeaponryProgressPublisherPort(recorder),
                retrieval=FakeTargetEvidenceRetrievalPort(recorder),
                extraction=FakeEvidenceExtractionPort(recorder),
                guidance=FakeAuxiliaryGuidancePort(recorder),
                translation=FakeWeaponryTranslationPort(recorder),
                audit=FakeWeaponryInteractionAuditPort(recorder),
                callbacks=callbacks,
                callback_recovery_source=callbacks,
                resources=FakeWeaponryResourceStorePort(recorder),
                resource_cleaner=FakeWeaponryExternalResourceCleanupPort(recorder),
                document_scope=FakeWeaponryDocumentScopePort(),
                execution_limiter=UploadTaskLimiter(1),
                process_guard=FileProcessSingletonGuard(lock_path),
                config=config,
                capabilities=_capabilities(config),
                startup_gate=lambda: (_ for _ in ()).throw(
                    RuntimeError("terms catalog unavailable")
                ),
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "terms catalog unavailable",
            ):
                services.start()
            snapshot = services.snapshot()
            self.assertEqual("stopped", snapshot.lifecycle_state)
            self.assertEqual(0, snapshot.worker_thread_count)
            self.assertEqual(0, snapshot.maintenance_thread_count)

            probe = FileProcessSingletonGuard(lock_path)
            self.assertTrue(probe.acquire())
            probe.release()

    def test_capability_fingerprint_mismatch_fails_before_threads(self) -> None:
        config = _config()
        capabilities = _capabilities(config)
        mismatched = WeaponryRuntimeCapabilities(
            **{
                **{
                    name: getattr(capabilities, name)
                    for name in capabilities.__dataclass_fields__
                },
                "embedding_fingerprint": "unexpected-embedding",
            }
        )
        recorder = WeaponryInvocationRecorder()
        tasks = FakeWeaponryTaskCommandPort(recorder)
        with workspace_tempdir() as tmp:
            with self.assertRaisesRegex(
                WeaponryInfrastructureConfigurationError,
                "embedding_fingerprint",
            ):
                compose_weaponry_application_services(
                    task_commands=tasks,
                    progress_publisher=FakeWeaponryProgressPublisherPort(recorder),
                    retrieval=FakeTargetEvidenceRetrievalPort(recorder),
                    extraction=FakeEvidenceExtractionPort(recorder),
                    guidance=FakeAuxiliaryGuidancePort(recorder),
                    translation=FakeWeaponryTranslationPort(recorder),
                    audit=FakeWeaponryInteractionAuditPort(recorder),
                    callbacks=(callbacks := FakeWeaponryCallbackPort(recorder)),
                    callback_recovery_source=callbacks,
                    resources=FakeWeaponryResourceStorePort(recorder),
                    resource_cleaner=FakeWeaponryExternalResourceCleanupPort(recorder),
                    document_scope=FakeWeaponryDocumentScopePort(),
                    execution_limiter=UploadTaskLimiter(1),
                    process_guard=FileProcessSingletonGuard(
                        Path(tmp) / "locks" / "weaponry.lock"
                    ),
                    config=config,
                    capabilities=mismatched,
                )


if __name__ == "__main__":
    unittest.main()
