"""阶段 1D-6：Callback Guard、同步恢复与资源恢复的生产边界验收。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier, Lock
from types import SimpleNamespace
import inspect
import unittest
from unittest.mock import Mock, patch

import requests

from app import create_app
from app.blueprints.llm import llm_weaponry
from app.adapters.web.flask.weaponry_requests import parse_weaponry_request
from app.integrations.anythingllm import AnythingLLMHTTPError, AnythingLLMTimeoutError
from app.integrations.anythingllm.models import AnythingLLMWorkspace
from app.modules.tasks.adapters import LegacyTaskCommandAdapter
from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.modules.tasks.ports import (
    ExpectedTaskCompletion,
    TaskSubmissionCommand,
    TaskSubmissionOutcome,
)
from app.modules.weaponry.adapters import (
    AnythingLLMWeaponryResourceCleanupAdapter,
    DatabaseServiceWeaponryDocumentScopeAdapter,
    SQLiteWeaponryCallbackAdapter,
    SQLiteWeaponryCallbackRecoverySource,
    SQLiteWeaponryResourceStoreAdapter,
    WeaponryInfrastructureConfig,
    WeaponryTaskCommandCodec,
)
from app.modules.weaponry.application import (
    RecoverWeaponryCallbackSynchronously,
    WeaponryResourceRecoveryOutcome,
    WeaponryResourceRecoveryService,
)
from app.modules.weaponry.domain import (
    AUXILIARY_GUIDANCE_NONE,
    DOCUMENT_SCOPE_EXPLICIT,
    EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1,
    EXTRACTION_PROMPT_VERSION,
    FILE_AGGREGATE_STRATEGY,
    MAX_TABLE_ROWS,
    TABLE_MERGE_POLICY_VERSION,
    WEAPONRY_INPUT_SCHEMA_VERSION,
    WEAPONRY_STATUS_SUCCEEDED,
    WEAPONRY_SUCCESS_MESSAGE,
    AuxiliaryGuidancePolicySnapshot,
    EvidenceSelectionPolicy,
    WeaponryDocumentScope,
    WeaponryDocumentSnapshot,
    WeaponryExecutionIdentity,
    WeaponryExecutionPolicySnapshot,
    WeaponryFieldResult,
    WeaponryCallbackPayload,
    WeaponryResult,
    WeaponrySubmission,
)
from app.modules.weaponry.ports import (
    AcquireWeaponryCallback,
    CleanupWeaponryExternalResource,
    DeliverWeaponryCallback,
    PrepareWeaponryResourceCleanup,
    RegisterWeaponryResource,
    ReleaseUnknownWeaponryCallback,
    ReserveWeaponryInteraction,
    WeaponryCallIdentity,
    WeaponryCallbackAcquireReason,
    WeaponryCallbackDeliveryOutcome,
    WeaponryCallbackDeliveryResult,
    WeaponryExternalResourceCleanupResult,
    WeaponryOperation,
    WeaponryResourceCleanupOutcome,
    WeaponryResourceKind,
    WeaponryResourceOwnership,
    WeaponryResourceRecord,
    WeaponryResourceRecordState,
    WeaponryTrackedResource,
)
from app.services.llm_service.task_service import LLMTaskService
from app.container import create_application_services
from app.services.core.config import (
    AnythingLLMConfig,
    ChatInfrastructureConfig,
    LLMIntegrationConfig,
    ReportInfrastructureConfig,
)
from tests import workspace_tempdir
from tests.fakes import (
    FakeWeaponryExternalResourceCleanupPort,
    FakeWeaponryInteractionAuditPort,
    FakeWeaponryTaskCommandPort,
)
from tests.offline_application import build_offline_application_services


def _submission(
    architecture_id: int,
    *,
    marker: str = "callback",
    field_type: str = "INPUT",
) -> WeaponrySubmission:
    """构造完整 Schema v2 提交，测试不得绕过公开请求解析器。"""

    field: dict[str, object] = {
        "templateClassifyId": 7001,
        "fieldName": "舰级名称",
        "fieldType": field_type,
        "fieldDescription": "提取正式舰级名称",
        "analyseData": "",
        "analyseDataSource": [],
    }
    if field_type == "TABLE":
        field["tableFieldList"] = [
            [
                {
                    "fieldName": "型号",
                    "fieldType": "INPUT",
                    "fieldDescription": "正式型号",
                }
            ]
        ]
    parsed = parse_weaponry_request(
        {
            "businessType": "weaponry",
            "params": {
                "architectureId": architecture_id,
                "filePathList": [f"https://files.local/{marker}.pdf"],
                "weaponryTemplateFieldList": [field],
            },
        }
    )
    document = WeaponryDocumentSnapshot(
        sequence_no=1,
        document_key=f"document:{marker}",
        file_name=f"{marker}.pdf",
        original_name=f"原始-{marker}.pdf",
        ingested_file_name=f"{marker}.pdf",
        source_architecture_id=architecture_id,
        external_document_ref=f"custom-documents/{marker}.json",
    )
    return parsed.to_submission(
        document_scope=WeaponryDocumentScope(
            mode=DOCUMENT_SCOPE_EXPLICIT,
            requested_file_names=(f"{marker}.pdf",),
            documents=(document,),
        ),
        evidence_selection_policy=EvidenceSelectionPolicy(
            profile_id="test-only-stage1d6-callback-profile",
            provider_fingerprint="test-provider-v1",
            embedding_fingerprint="test-embedding-v1",
            document_processing_fingerprint="test-processing-v1",
        ),
        execution_policy=WeaponryExecutionPolicySnapshot(
            extraction_strategy=FILE_AGGREGATE_STRATEGY,
            extraction_prompt_version=EXTRACTION_PROMPT_VERSION,
            extraction_context_strategy=(
                EXTRACTION_CONTEXT_PROVIDED_EVIDENCE_V1
            ),
            extraction_model_fingerprint="test-model-v1",
            table_merge_policy_version=TABLE_MERGE_POLICY_VERSION,
            max_table_rows=MAX_TABLE_ROWS,
        ),
        auxiliary_guidance_policy=AuxiliaryGuidancePolicySnapshot(
            policy_id=AUXILIARY_GUIDANCE_NONE,
            catalog_fingerprint="",
            top_n=0,
            max_context_chars=0,
        ),
        trace_id=f"trace-stage1d6-{architecture_id}-{marker}",
    )


def _task_command(
    submission: WeaponrySubmission,
) -> TaskSubmissionCommand[WeaponrySubmission]:
    return TaskSubmissionCommand(
        task_type="weaponry",
        business_ref=TaskBusinessRef("weaponry", submission.business_key),
        input_schema_version=WEAPONRY_INPUT_SCHEMA_VERSION,
        submission=submission,
        trace_id=submission.trace_id,
    )


def _finish_success(
    commands: LegacyTaskCommandAdapter,
    submission: WeaponrySubmission,
) -> tuple[TaskId, WeaponryCallbackPayload]:
    """原子受理并提交成功终态，返回 task_id 与公开 Callback DTO。"""

    created = commands.create_if_allowed(_task_command(submission))
    if created.outcome is not TaskSubmissionOutcome.ACCEPTED:
        raise AssertionError("测试前置任务未被受理")
    assert created.execution is not None
    claimed = commands.claim(created.execution.task_id)
    assert claimed.execution is not None
    snapshot = claimed.execution.input_snapshot
    result = WeaponryResult(
        identity=WeaponryExecutionIdentity(
            snapshot.task_id,
            snapshot.architecture_id,
        ),
        status=WEAPONRY_STATUS_SUCCEEDED,
        fields=tuple(
            WeaponryFieldResult(specification=field)
            for field in snapshot.fields
        ),
    )
    finished = commands.finish_if_current(
        ExpectedTaskCompletion(
            expected_task_id=claimed.execution.task_id,
            business_ref=claimed.execution.business_ref,
            execution_state="succeeded",
            public_status=WEAPONRY_STATUS_SUCCEEDED,
            message=WEAPONRY_SUCCESS_MESSAGE,
            result=result,
        )
    )
    if not finished:
        raise AssertionError("测试前置终态 CAS 未命中")
    return claimed.execution.task_id, result.to_callback()


class WeaponryCallbackGuardIntegrationTests(unittest.TestCase):
    def test_http_3xx_is_rejected_and_explicit_recovery_accepts_only_2xx(self) -> None:
        """重定向不得伪装成功；甲方严格 2xx 才能完成投递。"""

        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            service = LLMTaskService(database)
            commands = LegacyTaskCommandAdapter(service, WeaponryTaskCommandCodec())
            task_id, payload = _finish_success(commands, _submission(10502))
            callbacks = SQLiteWeaponryCallbackAdapter(
                service,
                callback_url="http://callback.invalid/result",
                callback_timeout=5.0,
                lease_seconds=15.0,
            )

            redirect = Mock(status_code=302)
            accepted = Mock(status_code=204)
            with patch(
                "app.modules.weaponry.adapters.callback_guard.requests.post",
                side_effect=(redirect, accepted),
            ) as callback_post, patch(
                "app.modules.weaponry.adapters.callback_guard.save_callback_history_payload"
            ):
                first = callbacks.acquire(
                    AcquireWeaponryCallback(task_id, 10502)
                )
                assert first.lease is not None
                rejected = callbacks.deliver(
                    DeliverWeaponryCallback(first.lease, payload)
                )
                self.assertTrue(callbacks.complete(first.lease, rejected, payload))

                second = callbacks.acquire(
                    AcquireWeaponryCallback(
                        task_id,
                        10502,
                        WeaponryCallbackAcquireReason.EXPLICIT_CHECK_TASK_RECOVERY,
                    )
                )
                assert second.lease is not None
                succeeded = callbacks.deliver(
                    DeliverWeaponryCallback(second.lease, payload)
                )
                self.assertTrue(callbacks.complete(second.lease, succeeded, payload))

            self.assertEqual(2, callback_post.call_count)
            self.assertTrue(
                all(
                    call.kwargs["allow_redirects"] is False
                    for call in callback_post.call_args_list
                )
            )

            latest = service.get_task("weaponry", "10502")

        self.assertEqual(WeaponryCallbackDeliveryOutcome.REJECTED, rejected.outcome)
        self.assertEqual(WeaponryCallbackDeliveryOutcome.SUCCESS, succeeded.outcome)
        self.assertEqual("success", latest["callback_status"])
        redirect.close.assert_called_once_with()
        accepted.close.assert_called_once_with()

    def test_read_timeout_freezes_business_key_and_blocks_new_submission(self) -> None:
        """请求可能到达甲方时必须冻结，不得自动重发或接受同键新任务。"""

        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            service = LLMTaskService(database)
            commands = LegacyTaskCommandAdapter(service, WeaponryTaskCommandCodec())
            task_id, payload = _finish_success(commands, _submission(10503))
            callbacks = SQLiteWeaponryCallbackAdapter(
                service,
                callback_url="http://callback.invalid/result",
                callback_timeout=5.0,
                lease_seconds=15.0,
            )
            acquired = callbacks.acquire(AcquireWeaponryCallback(task_id, 10503))
            assert acquired.lease is not None
            with patch(
                "app.modules.weaponry.adapters.callback_guard.requests.post",
                side_effect=requests.exceptions.ReadTimeout("read timeout"),
            ), patch(
                "app.modules.weaponry.adapters.callback_guard.save_callback_history_payload"
            ):
                delivery = callbacks.deliver(
                    DeliverWeaponryCallback(acquired.lease, payload)
                )
                self.assertTrue(
                    callbacks.complete(acquired.lease, delivery, payload)
                )
            blocked = commands.create_if_allowed(_task_command(_submission(10503)))
            latest = service.get_task("weaponry", "10503")

        self.assertEqual(
            WeaponryCallbackDeliveryOutcome.DELIVERY_OUTCOME_UNKNOWN,
            delivery.outcome,
        )
        self.assertEqual(
            TaskSubmissionOutcome.CALLBACK_OUTCOME_UNKNOWN,
            blocked.outcome,
        )
        self.assertEqual("outcome_unknown", latest["callback_status"])

    def test_released_old_callback_is_stale_after_new_task_and_skips_network(self) -> None:
        """人工确认并解除旧 unknown 后，新任务使旧租约在 HTTP 前永久失权。"""

        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            service = LLMTaskService(database)
            commands = LegacyTaskCommandAdapter(service, WeaponryTaskCommandCodec())
            task_id, payload = _finish_success(commands, _submission(10504))
            transport = Mock()
            callbacks = SQLiteWeaponryCallbackAdapter(
                service,
                callback_url="http://callback.invalid/result",
                callback_timeout=5.0,
                lease_seconds=15.0,
                transport=transport,
            )
            acquired = callbacks.acquire(AcquireWeaponryCallback(task_id, 10504))
            assert acquired.lease is not None
            with patch(
                "app.modules.weaponry.adapters.callback_guard.save_callback_history_payload"
            ):
                self.assertTrue(
                    callbacks.complete(
                        acquired.lease,
                        # 模拟已发出请求但未取得确定响应。
                        _callback_unknown_result(),
                        payload,
                    )
                )
            callbacks.release_unknown(
                ReleaseUnknownWeaponryCallback(
                    architecture_id=10504,
                    released_by="stage1d6-test",
                    reason="已确认旧 Worker 停止并隔离",
                    worker_stopped_confirmed=True,
                )
            )
            second = commands.create_if_allowed(_task_command(_submission(10504)))
            stale = callbacks.deliver(
                DeliverWeaponryCallback(acquired.lease, payload)
            )

        self.assertEqual(TaskSubmissionOutcome.ACCEPTED, second.outcome)
        self.assertEqual(WeaponryCallbackDeliveryOutcome.STALE, stale.outcome)
        transport.assert_not_called()

    def test_fifty_synchronous_recoveries_make_exactly_one_http_call(self) -> None:
        """50 个并发 check-task 等价调用最多只有一个发送者。"""

        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            service = LLMTaskService(database)
            commands = LegacyTaskCommandAdapter(service, WeaponryTaskCommandCodec())
            _finish_success(commands, _submission(10505))
            call_lock = Lock()
            call_count = 0

            def transport(_: dict[str, object]):
                nonlocal call_count
                with call_lock:
                    call_count += 1
                return _callback_success_result()

            callbacks = SQLiteWeaponryCallbackAdapter(
                service,
                callback_url="http://callback.invalid/result",
                callback_timeout=5.0,
                lease_seconds=15.0,
                transport=transport,
            )
            recovery = RecoverWeaponryCallbackSynchronously(
                source=SQLiteWeaponryCallbackRecoverySource(service),
                callbacks=callbacks,
            )
            barrier = Barrier(50)

            def recover_once() -> bool:
                barrier.wait(timeout=10.0)
                return recovery.execute(10505)

            with patch(
                "app.modules.weaponry.adapters.callback_guard.save_callback_history_payload"
            ), ThreadPoolExecutor(max_workers=50) as executor:
                results = tuple(executor.map(lambda _: recover_once(), range(50)))
            latest = service.get_task("weaponry", "10505")

        self.assertEqual(1, call_count)
        self.assertEqual(1, sum(results))
        self.assertEqual(1, latest["callback_attempts"])
        self.assertEqual("success", latest["callback_status"])

    def test_recovery_source_round_trips_input_and_empty_table_payloads(self) -> None:
        """同步恢复只重建已持久化公开载荷，不重新运行抽取。"""

        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            service = LLMTaskService(database)
            commands = LegacyTaskCommandAdapter(service, WeaponryTaskCommandCodec())
            for architecture_id, field_type in ((10506, "INPUT"), (10507, "TABLE")):
                _finish_success(
                    commands,
                    _submission(
                        architecture_id,
                        marker=f"roundtrip-{field_type.lower()}",
                        field_type=field_type,
                    ),
                )
            source = SQLiteWeaponryCallbackRecoverySource(service)

            for architecture_id in (10506, 10507):
                with self.subTest(architecture_id=architecture_id):
                    candidate = source.load_recoverable(architecture_id)
                    assert candidate is not None
                    latest = service.get_task("weaponry", str(architecture_id))
                    self.assertEqual(
                        latest["result_payload"],
                        candidate.payload.to_public_dict(),
                    )

    def test_check_task_uses_same_guarded_recovery_instead_of_legacy_replay(self) -> None:
        """甲方规定的同步副作用必须复用 Worker 的同一个 Callback Guard。"""

        with workspace_tempdir() as runtime_directory:
            services = build_offline_application_services(
                runtime_directory,
                callback_url="http://callback.invalid/result",
            )
            weaponry = services.weaponry_services
            assert weaponry is not None
            _finish_success(weaponry.task_commands, _submission(10508))
            response_value = Mock(status_code=200)
            with patch(
                "app.modules.weaponry.adapters.callback_guard.requests.post",
                return_value=response_value,
            ) as callback_post, patch(
                "app.modules.weaponry.adapters.callback_guard.save_callback_history_payload"
            ), patch.object(
                services.task_service,
                "replay_callback_if_needed",
                side_effect=AssertionError("weaponry 不得进入遗留直发入口"),
            ):
                response = create_app(services=services).test_client().post(
                    "/llm/check-task",
                    json={
                        "businessType": "weaponry",
                        "params": [{"architectureId": "00010508"}],
                    },
                )

            latest = services.task_service.get_task("weaponry", "10508")

        self.assertEqual(200, response.status_code)
        # check-task 成功体不公开回调恢复细节；内部持久化状态仍需可验证。
        self.assertEqual(b"", response.data)
        self.assertEqual("success", latest["callback_status"])
        callback_post.assert_called_once()
        self.assertFalse(callback_post.call_args.kwargs["allow_redirects"])
        response_value.close.assert_called_once_with()


def _callback_unknown_result() -> WeaponryCallbackDeliveryResult:
    """集中构造未知投递结果，避免测试散落魔法枚举。"""

    return WeaponryCallbackDeliveryResult(
        WeaponryCallbackDeliveryOutcome.DELIVERY_OUTCOME_UNKNOWN,
        "ReadTimeout",
    )


def _callback_success_result() -> WeaponryCallbackDeliveryResult:
    """集中构造严格 2xx 对应的成功投递结果。"""

    return WeaponryCallbackDeliveryResult(
        WeaponryCallbackDeliveryOutcome.SUCCESS,
        "http_status=200",
    )


def _owned_workspace(task_id: TaskId, *, suffix: str = "scope") -> WeaponryTrackedResource:
    return WeaponryTrackedResource(
        resource_id=f"retrieval-scope:{suffix}",
        kind=WeaponryResourceKind.RETRIEVAL_SCOPE,
        external_ref=f"workspace-{suffix}",
        ownership=WeaponryResourceOwnership.OWNED,
        idempotency_key=f"weaponry:{task_id.value}:retrieval-scope:{suffix}",
    )


def _create_pending_record(
    store: SQLiteWeaponryResourceStoreAdapter,
    *,
    task_id: TaskId,
    architecture_id: int,
) -> WeaponryTrackedResource:
    record = store.create(
        WeaponryResourceRecord(
            task_id=task_id,
            business_ref=TaskBusinessRef("weaponry", str(architecture_id)),
        )
    )
    resource = _owned_workspace(task_id)
    record = store.register(
        RegisterWeaponryResource(task_id, resource, record.version)
    )
    store.prepare_cleanup(
        PrepareWeaponryResourceCleanup(task_id, record.version)
    )
    return resource


class WeaponryResourceRecoveryIntegrationTests(unittest.TestCase):
    def test_tracking_without_execution_is_discovered_once_and_quarantined(self) -> None:
        """孤儿资源不能永久漏扫，也不能在缺少执行证据时被自动删除。"""

        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            service = LLMTaskService(database)
            commands = LegacyTaskCommandAdapter(service, WeaponryTaskCommandCodec())
            store = SQLiteWeaponryResourceStoreAdapter(database)
            task_id = TaskId("weaponry-orphan-resource")
            record = store.create(
                WeaponryResourceRecord(
                    task_id=task_id,
                    business_ref=TaskBusinessRef("weaponry", "10600"),
                )
            )
            store.register(
                RegisterWeaponryResource(
                    task_id,
                    _owned_workspace(task_id, suffix="orphan"),
                    record.version,
                )
            )
            cleaner = FakeWeaponryExternalResourceCleanupPort()
            recovery = WeaponryResourceRecoveryService(
                store=store,
                cleaner=cleaner,
                audit=FakeWeaponryInteractionAuditPort(),
                task_commands=commands,
            )

            first = recovery.run_once(limit=50)
            second = recovery.run_once(limit=50)
            current = store.get(task_id)

        self.assertEqual(1, first.scanned_count)
        self.assertEqual(1, first.quarantined_count)
        self.assertEqual(0, second.scanned_count)
        self.assertEqual(WeaponryResourceRecordState.QUARANTINED, current.state)
        self.assertEqual(
            "weaponry_execution_missing_for_resource",
            current.last_error_code,
        )
        self.assertEqual([], cleaner.calls)

    def test_terminal_tracking_is_discovered_and_cleaned_but_active_is_untouched(self) -> None:
        """只以 execution 终态为清理证据，accepted/running 不按年龄猜死。"""

        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            service = LLMTaskService(database)
            commands = LegacyTaskCommandAdapter(service, WeaponryTaskCommandCodec())
            terminal_task_id, _ = _finish_success(commands, _submission(10601))
            active = commands.create_if_allowed(_task_command(_submission(10602)))
            assert active.execution is not None
            active_task_id = active.execution.task_id

            store = SQLiteWeaponryResourceStoreAdapter(
                database,
                cleanup_lease_seconds=2.0,
                retry_delay_seconds=30.0,
            )
            cleaner = FakeWeaponryExternalResourceCleanupPort()
            for task_id, architecture_id in (
                (terminal_task_id, 10601),
                (active_task_id, 10602),
            ):
                record = store.create(
                    WeaponryResourceRecord(
                        task_id=task_id,
                        business_ref=TaskBusinessRef(
                            "weaponry",
                            str(architecture_id),
                        ),
                    )
                )
                resource = _owned_workspace(task_id, suffix=str(architecture_id))
                store.register(
                    RegisterWeaponryResource(task_id, resource, record.version)
                )
                if task_id == terminal_task_id:
                    cleaner.results[resource.resource_id] = (
                        WeaponryExternalResourceCleanupResult(
                            WeaponryResourceCleanupOutcome.SUCCEEDED
                        )
                    )

            recovery = WeaponryResourceRecoveryService(
                store=store,
                cleaner=cleaner,
                audit=FakeWeaponryInteractionAuditPort(),
                task_commands=commands,
            )
            sweep = recovery.run_once(limit=50)
            terminal_record = store.get(terminal_task_id)
            active_record = store.get(active_task_id)

        self.assertEqual(1, sweep.scanned_count)
        self.assertEqual(1, sweep.cleaned_count)
        self.assertEqual(WeaponryResourceRecordState.CLEANED, terminal_record.state)
        self.assertEqual(WeaponryResourceRecordState.TRACKING, active_record.state)
        self.assertEqual(1, len(cleaner.calls))

    def test_definite_failure_uses_persistent_cooldown_and_unknown_is_quarantined(self) -> None:
        """明确失败可退避重试；删除结果未知永久退出自动扫描。"""

        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            store = SQLiteWeaponryResourceStoreAdapter(
                database,
                cleanup_lease_seconds=2.0,
                retry_delay_seconds=60.0,
            )
            cleaner = FakeWeaponryExternalResourceCleanupPort()
            audit = FakeWeaponryInteractionAuditPort()
            recovery = WeaponryResourceRecoveryService(
                store=store,
                cleaner=cleaner,
                audit=audit,
                task_commands=FakeWeaponryTaskCommandPort(),
            )

            failed_task = TaskId("weaponry-cleanup-failed")
            failed_resource = _create_pending_record(
                store,
                task_id=failed_task,
                architecture_id=10603,
            )
            cleaner.results[failed_resource.resource_id] = (
                WeaponryExternalResourceCleanupResult(
                    WeaponryResourceCleanupOutcome.FAILED,
                    "upstream_rejected",
                )
            )
            failed = recovery.recover(failed_task)
            failed_record = store.get(failed_task)
            assert failed_record is not None

            unknown_task = TaskId("weaponry-cleanup-unknown")
            unknown_resource = _create_pending_record(
                store,
                task_id=unknown_task,
                architecture_id=10604,
            )
            cleaner.results[unknown_resource.resource_id] = (
                WeaponryExternalResourceCleanupResult(
                    WeaponryResourceCleanupOutcome.OUTCOME_UNKNOWN,
                    "delete_timeout",
                )
            )
            unknown = recovery.recover(unknown_task)
            unknown_record = store.get(unknown_task)

            immediately_recoverable = store.list_recoverable(limit=50)

        self.assertEqual(WeaponryResourceRecoveryOutcome.PENDING, failed.outcome)
        self.assertGreater(
            datetime.fromisoformat(failed_record.next_retry_at),
            datetime.now(timezone.utc),
        )
        self.assertNotIn(failed_task, immediately_recoverable)
        self.assertEqual(
            WeaponryResourceRecoveryOutcome.QUARANTINED,
            unknown.outcome,
        )
        self.assertEqual(
            WeaponryResourceRecordState.QUARANTINED,
            unknown_record.state,
        )
        self.assertNotIn(unknown_task, immediately_recoverable)

    def test_pending_external_audit_quarantines_without_delete(self) -> None:
        """外部调用结果尚未提交时保留现场，禁止把清理当成重放或补偿。"""

        with workspace_tempdir() as runtime_directory:
            database = str(Path(runtime_directory) / "tasks.sqlite3")
            store = SQLiteWeaponryResourceStoreAdapter(
                database,
                cleanup_lease_seconds=2.0,
                retry_delay_seconds=30.0,
            )
            task_id = TaskId("weaponry-pending-audit")
            resource = _create_pending_record(
                store,
                task_id=task_id,
                architecture_id=10605,
            )
            cleaner = FakeWeaponryExternalResourceCleanupPort()
            cleaner.results[resource.resource_id] = (
                WeaponryExternalResourceCleanupResult(
                    WeaponryResourceCleanupOutcome.SUCCEEDED
                )
            )
            audit = FakeWeaponryInteractionAuditPort()
            audit.reserve(
                ReserveWeaponryInteraction(
                    business_ref=TaskBusinessRef("weaponry", "10605"),
                    call=WeaponryCallIdentity(
                        task_id=task_id,
                        field_sequence=1,
                        document_sequence=None,
                        operation=WeaponryOperation.TARGET_RETRIEVAL,
                    ),
                    input_digest="0" * 64,
                    input_chars=8,
                )
            )
            recovery = WeaponryResourceRecoveryService(
                store=store,
                cleaner=cleaner,
                audit=audit,
                task_commands=FakeWeaponryTaskCommandPort(),
            )

            result = recovery.recover(task_id)
            record = store.get(task_id)

        self.assertEqual(WeaponryResourceRecoveryOutcome.QUARANTINED, result.outcome)
        self.assertEqual(WeaponryResourceRecordState.QUARANTINED, record.state)
        self.assertEqual([], cleaner.calls)


class _CleanupClientFactory:
    """只为资源清理 Adapter 提供任务级上下文，绝不创建网络 Session。"""

    def __init__(self) -> None:
        self.workspaces = Mock()
        self.threads = Mock()

    @contextmanager
    def create(self):
        yield SimpleNamespace(
            documents=Mock(),
            workspaces=self.workspaces,
            threads=self.threads,
        )


class AnythingLLMWeaponryResourceCleanupAdapterTests(unittest.TestCase):
    def test_404_is_idempotent_success_409_is_failed_and_timeout_is_unknown(self) -> None:
        task_id = TaskId("weaponry-cleanup-http")
        resource = _owned_workspace(task_id, suffix="http")

        cases = (
            (
                AnythingLLMHTTPError("missing", status_code=404),
                WeaponryResourceCleanupOutcome.SUCCEEDED,
            ),
            (
                AnythingLLMHTTPError("conflict", status_code=409),
                WeaponryResourceCleanupOutcome.FAILED,
            ),
            (
                AnythingLLMTimeoutError("timeout"),
                WeaponryResourceCleanupOutcome.OUTCOME_UNKNOWN,
            ),
        )
        for error, expected in cases:
            with self.subTest(expected=expected.value):
                factory = _CleanupClientFactory()
                factory.workspaces.delete_workspace.side_effect = error
                adapter = AnythingLLMWeaponryResourceCleanupAdapter(factory)

                # Cleanup DTO 已保证这里只可能传入 owned 资源。
                result = adapter.cleanup(
                    CleanupWeaponryExternalResource(task_id, resource)
                )

                self.assertEqual(expected, result.outcome)
                factory.workspaces.delete_workspace.assert_called_once_with(
                    resource.external_ref,
                    user_id=1,
                )
                factory.workspaces.list_workspaces.assert_not_called()

    def test_workspace_delete_400_requires_authoritative_absence_readback(self) -> None:
        task_id = TaskId("weaponry-cleanup-http-400")
        resource = _owned_workspace(task_id, suffix="http-400")
        delete_error = AnythingLLMHTTPError("bad request", status_code=400)

        cases = (
            ("absent", [], None, WeaponryResourceCleanupOutcome.SUCCEEDED),
            (
                "still-present",
                [
                    AnythingLLMWorkspace(
                        id=resource.external_ref,
                        slug=resource.external_ref,
                        name=resource.external_ref,
                    )
                ],
                None,
                WeaponryResourceCleanupOutcome.FAILED,
            ),
            (
                "readback-failed",
                [],
                AnythingLLMTimeoutError("injected readback timeout"),
                WeaponryResourceCleanupOutcome.FAILED,
            ),
        )
        for name, workspaces, readback_error, expected in cases:
            with self.subTest(name=name):
                factory = _CleanupClientFactory()
                factory.workspaces.delete_workspace.side_effect = delete_error
                factory.workspaces.list_workspaces.return_value = workspaces
                factory.workspaces.list_workspaces.side_effect = readback_error
                adapter = AnythingLLMWeaponryResourceCleanupAdapter(factory)

                result = adapter.cleanup(
                    CleanupWeaponryExternalResource(task_id, resource)
                )

                self.assertEqual(expected, result.outcome)
                factory.workspaces.list_workspaces.assert_called_once_with(user_id=1)


class WeaponryProductionCompositionTests(unittest.TestCase):
    def test_public_route_contains_only_adapter_application_presenter_flow(self) -> None:
        """永久阻止路由线程、遗留 Service 和供应商 Client 回流。"""

        source = inspect.getsource(llm_weaponry)
        for forbidden in (
            "threading.Thread",
            "run_weaponry_task",
            "create_weaponry_task",
            "AnythingLLMClient",
            "list_document_records",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)
        self.assertIn("parse_weaponry_request", source)
        self.assertIn("document_scope.resolve", source)
        self.assertIn("submit.execute", source)
        self.assertIn("present_success", source)

    def test_production_factory_binds_one_real_chain_without_opening_network_session(
        self,
    ) -> None:
        """生产组合根必须真实绑定新链；构造阶段仍保持外部 Transport 惰性。"""

        with workspace_tempdir() as runtime_directory:
            root = Path(runtime_directory)
            weaponry_config = WeaponryInfrastructureConfig(
                runtime_mode="single_instance",
                scan_interval_seconds=0.05,
                accepted_batch_size=50,
                dispatch_failure_retry_seconds=30.0,
                maintenance_interval_seconds=30.0,
                maintenance_limit=50,
                running_sample_limit=20,
                stop_timeout_seconds=1.0,
                cleanup_http_timeout_seconds=60.0,
                cleanup_lease_seconds=130.0,
                provider_fingerprint="stage1d6-provider-v1",
                embedding_fingerprint="stage1d6-embedding-v1",
                document_processing_fingerprint="stage1d6-processing-v1",
                extraction_model_fingerprint="stage1d6-model-v1",
            )
            with patch(
                "app.container.RUNTIME_DIR",
                root,
            ), patch(
                "app.container.KNOWLEDGE_BASE_DB_PATH",
                root / "knowledge.sqlite3",
            ), patch(
                "app.container.CHAT_DB_PATH",
                root / "chat.sqlite3",
            ), patch(
                "app.container.load_chat_infrastructure_config",
                return_value=ChatInfrastructureConfig.single_instance(),
            ), patch(
                "app.container.load_report_infrastructure_config",
                return_value=ReportInfrastructureConfig.single_instance(),
            ), patch(
                "app.container.load_weaponry_infrastructure_config",
                return_value=weaponry_config,
            ), patch(
                "app.container.load_anythingllm_config",
                return_value=AnythingLLMConfig(
                    base_url="http://anythingllm.invalid/api/v1",
                    api_key="test-key",
                    timeout=5.0,
                    storage_root=None,
                ),
            ), patch(
                "app.container.load_llm_integration_config",
                return_value=LLMIntegrationConfig(
                    callback_url="http://callback.invalid/result",
                    callback_timeout=5.0,
                    task_db_path=str(root / "tasks.sqlite3"),
                    download_timeout=5.0,
                    download_dir=str(root / "downloads"),
                ),
            ), patch("requests.Session") as session_factory:
                services = create_application_services()

            try:
                weaponry = services.weaponry_services
                assert weaponry is not None
                self.assertIsInstance(
                    weaponry.callbacks,
                    SQLiteWeaponryCallbackAdapter,
                )
                self.assertIs(
                    weaponry.callbacks,
                    weaponry.callback_recovery.callbacks,
                )
                self.assertIs(
                    weaponry.resource_recovery,
                    weaponry.dispatcher.resource_maintenance,
                )
                self.assertIs(
                    weaponry.execution_limiter,
                    services.upload_task_limiter,
                )
                self.assertIsInstance(
                    weaponry.document_scope,
                    DatabaseServiceWeaponryDocumentScopeAdapter,
                )
                self.assertEqual("new", weaponry.snapshot().lifecycle_state)
                session_factory.assert_not_called()
            finally:
                services.close()


if __name__ == "__main__":
    unittest.main()
