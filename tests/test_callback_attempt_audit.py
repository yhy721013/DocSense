"""共享 Callback Guard attempt CAS 与追加式审计的离线验收。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import unittest

from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir
from tests.task_service_fixtures import seed_legacy_file_task


_ACQUIRED_AT = "2030-01-01T00:00:00+00:00"
_COMPLETED_AT = "2030-01-01T00:00:01+00:00"
_COMPLETED_PUBLIC_STATUS = {"file": "2", "report": "1", "weaponry": "2"}


def _create_terminal_task(
    service: LLMTaskService,
    database_path: Path,
    *,
    business_type: str,
    business_key: str,
) -> dict[str, object]:
    """用公开创建入口和最小事务夹具构造可取得 Callback Guard 的终态任务。"""

    request_payload = {"businessType": business_type, "testOnly": True}
    if business_type == "file":
        task = seed_legacy_file_task(service,business_key, request_payload)
    elif business_type == "report":
        task = service.create_report_task(int(business_key), request_payload)
    elif business_type == "weaponry":
        task = service.create_weaponry_task(int(business_key), request_payload)
    else:  # pragma: no cover - 测试帮助函数只接受固定矩阵。
        raise AssertionError(f"未知测试业务类型: {business_type}")

    # 本测试只验证 Guard，不重复业务 Worker 的领域编排。创建入口已经生成 execution，
    # 这里以一个短事务同时终结 execution 和公开投影，避免构造伪造的孤立任务行。
    with sqlite3.connect(database_path) as connection:
        execution_cursor = connection.execute(
            """
            INSERT INTO llm_task_executions (
                execution_id, business_type, business_key,
                input_schema_version, input_payload, execution_state,
                public_status, progress, message, result_payload,
                callback_status, callback_outcome, trace_id,
                created_at, completed_at, updated_at
            )
            VALUES (?, ?, ?, 1, '{}', 'succeeded', ?, 1.0, '', '{}',
                    'pending', '', 'u3-callback-attempt-test', ?, ?, ?)
            """,
            (
                task["execution_id"],
                business_type,
                business_key,
                _COMPLETED_PUBLIC_STATUS[business_type],
                _ACQUIRED_AT,
                _ACQUIRED_AT,
                _ACQUIRED_AT,
            ),
        )
        projection_cursor = connection.execute(
            """
            UPDATE llm_tasks
            SET status = ?, callback_status = 'pending', callback_attempts = 0,
                last_callback_error = '', updated_at = ?
            WHERE execution_id = ? AND business_type = ? AND business_key = ?
            """,
            (
                _COMPLETED_PUBLIC_STATUS[business_type],
                _ACQUIRED_AT,
                task["execution_id"],
                business_type,
                business_key,
            ),
        )
        if execution_cursor.rowcount != 1 or projection_cursor.rowcount != 1:
            raise AssertionError("Callback Guard 测试终态夹具写入失败")
    return task


def _acquire(
    service: LLMTaskService,
    task: dict[str, object],
    *,
    business_type: str,
    business_key: str,
    token: str,
    explicit_unknown: bool = False,
    expected_callback_attempts: int | None = None,
    trace_id: str = "",
) -> dict[str, object]:
    return service.acquire_callback_delivery_guard(
        expected_execution_id=str(task["execution_id"]),
        business_type=business_type,
        business_key=business_key,
        lease_token=token,
        lease_seconds=30.0,
        acquired_at=_ACQUIRED_AT,
        allow_failed_retry=explicit_unknown,
        allow_outcome_unknown_retry=explicit_unknown,
        expected_callback_attempts=expected_callback_attempts,
        delivery_trigger=(
            "explicit_check_task_recovery"
            if explicit_unknown
            else "initial_delivery"
        ),
        request_trace_id=trace_id,
    )


def _freeze_as_unknown(
    service: LLMTaskService,
    task: dict[str, object],
    *,
    business_type: str,
    business_key: str,
) -> None:
    acquired = _acquire(
        service,
        task,
        business_type=business_type,
        business_key=business_key,
        token=f"initial-{business_type}-{business_key}",
    )
    if acquired["outcome"] != "acquired":
        raise AssertionError("测试夹具未取得首次 Callback Guard")
    completed = service.complete_callback_delivery_guard(
        expected_execution_id=str(task["execution_id"]),
        business_type=business_type,
        business_key=business_key,
        lease_token=str(acquired["lease_token"]),
        fencing_token=int(acquired["fencing_token"]),
        delivery_outcome="delivery_outcome_unknown",
        detail="injected read timeout",
        completed_at=_COMPLETED_AT,
    )
    if not completed:
        raise AssertionError("测试夹具未能冻结 unknown Callback Guard")


class CallbackAttemptAuditTests(unittest.TestCase):
    def test_initial_worker_acquire_keeps_all_business_types_compatible(self) -> None:
        """正常 Worker 不带 attempt 快照，三类业务仍可取得首次发送权。"""

        with workspace_tempdir() as runtime_directory:
            database_path = Path(runtime_directory) / "tasks.sqlite3"
            service = LLMTaskService(str(database_path))
            for index, business_type in enumerate(("file", "report", "weaponry")):
                business_key = (
                    f"initial-{index}.pdf"
                    if business_type == "file"
                    else str(2000 + index)
                )
                task = _create_terminal_task(
                    service,
                    database_path,
                    business_type=business_type,
                    business_key=business_key,
                )
                acquired = _acquire(
                    service,
                    task,
                    business_type=business_type,
                    business_key=business_key,
                    token=f"initial-token-{index}",
                )
                self.assertEqual("acquired", acquired["outcome"])
                events = service.list_callback_delivery_attempt_events(
                    business_type=business_type,
                    business_key=business_key,
                )
                self.assertEqual(1, len(events))
                self.assertEqual("authorized", events[0]["event_type"])
                self.assertEqual("initial_delivery", events[0]["trigger"])
                self.assertEqual(1, events[0]["callback_attempt"])

    def test_ordinary_trigger_cannot_enable_unknown_retry(self) -> None:
        """仅设置 allow 标志不足以授权；普通 Worker trigger 必须 fail closed。"""

        with workspace_tempdir() as runtime_directory:
            service = LLMTaskService(
                str(Path(runtime_directory) / "tasks.sqlite3")
            )
            with self.assertRaisesRegex(ValueError, "check-task携带attempt快照"):
                service.acquire_callback_delivery_guard(
                    expected_execution_id="execution",
                    business_type="report",
                    business_key="132",
                    lease_token="token",
                    lease_seconds=30.0,
                    acquired_at=_ACQUIRED_AT,
                    allow_failed_retry=True,
                    allow_outcome_unknown_retry=True,
                    expected_callback_attempts=1,
                    delivery_trigger="initial_delivery",
                )

    def test_unknown_retry_whitelist_uses_attempt_cas_and_trace_audit(self) -> None:
        """三类 unknown 只有事实一致且 attempt 匹配时才能各自取得一次新租约。"""

        with workspace_tempdir() as runtime_directory:
            database_path = Path(runtime_directory) / "tasks.sqlite3"
            service = LLMTaskService(str(database_path))
            matrix = (("file", "retry.pdf"), ("report", "3132"), ("weaponry", "4101"))
            for business_type, business_key in matrix:
                task = _create_terminal_task(
                    service,
                    database_path,
                    business_type=business_type,
                    business_key=business_key,
                )
                _freeze_as_unknown(
                    service,
                    task,
                    business_type=business_type,
                    business_key=business_key,
                )
                trace_id = f"check-task-{business_type}"
                acquired = _acquire(
                    service,
                    task,
                    business_type=business_type,
                    business_key=business_key,
                    token=f"retry-{business_type}",
                    explicit_unknown=True,
                    expected_callback_attempts=1,
                    trace_id=trace_id,
                )
                self.assertEqual("acquired", acquired["outcome"])
                stale = _acquire(
                    service,
                    task,
                    business_type=business_type,
                    business_key=business_key,
                    token=f"stale-{business_type}",
                    explicit_unknown=True,
                    expected_callback_attempts=1,
                    trace_id=trace_id,
                )
                self.assertEqual("stale", stale["outcome"])
                events = service.list_callback_delivery_attempt_events(
                    business_type=business_type,
                    business_key=business_key,
                )
                retry_authorization = next(
                    event
                    for event in events
                    if event["event_type"] == "authorized"
                    and event["callback_attempt"] == 2
                )
                self.assertEqual(
                    "explicit_check_task_recovery",
                    retry_authorization["trigger"],
                )
                self.assertEqual(trace_id, retry_authorization["request_trace_id"])

    def test_audit_insert_failure_rolls_back_guard_projection_and_attempt(self) -> None:
        """授权审计不可写时，不得留下发送权或已消耗的 callback attempt。"""

        with workspace_tempdir() as runtime_directory:
            database_path = Path(runtime_directory) / "tasks.sqlite3"
            service = LLMTaskService(str(database_path))
            task = _create_terminal_task(
                service,
                database_path,
                business_type="file",
                business_key="audit-rollback.pdf",
            )
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    """
                    CREATE TRIGGER fail_callback_attempt_audit
                    BEFORE INSERT ON callback_delivery_attempt_events
                    BEGIN
                        SELECT RAISE(ABORT, 'injected callback audit failure');
                    END
                    """
                )

            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "injected callback audit failure",
            ):
                _acquire(
                    service,
                    task,
                    business_type="file",
                    business_key="audit-rollback.pdf",
                    token="rollback-token",
                )

            current = service.get_task("file", "audit-rollback.pdf")
            self.assertIsNotNone(current)
            assert current is not None
            self.assertEqual("pending", current["callback_status"])
            self.assertEqual(0, current["callback_attempts"])
            guard = service.observe_callback_delivery_guard(
                business_type="file",
                business_key="audit-rollback.pdf",
                observed_at=_ACQUIRED_AT,
            )
            self.assertEqual("idle", guard["state"])

    def test_fifty_same_snapshot_callers_have_one_unknown_retry_owner(self) -> None:
        """同一快照的 50 个竞争者只能有一个 acquired，其余不会滚动成下一轮。"""

        with workspace_tempdir() as runtime_directory:
            database_path = Path(runtime_directory) / "tasks.sqlite3"
            service = LLMTaskService(str(database_path))
            task = _create_terminal_task(
                service,
                database_path,
                business_type="report",
                business_key="5132",
            )
            _freeze_as_unknown(
                service,
                task,
                business_type="report",
                business_key="5132",
            )

            def compete(index: int) -> str:
                result = _acquire(
                    service,
                    task,
                    business_type="report",
                    business_key="5132",
                    token=f"concurrent-{index}",
                    explicit_unknown=True,
                    expected_callback_attempts=1,
                    trace_id="concurrent-check-task",
                )
                return str(result["outcome"])

            with ThreadPoolExecutor(max_workers=50) as pool:
                outcomes = list(pool.map(compete, range(50)))

            self.assertEqual(1, outcomes.count("acquired"))
            self.assertEqual(49, outcomes.count("stale"))
            current = service.get_task("report", "5132")
            self.assertIsNotNone(current)
            assert current is not None
            self.assertEqual(2, current["callback_attempts"])

    def test_fifty_distinct_business_keys_do_not_share_guard_or_audit(self) -> None:
        """不同业务键的租约、attempt 与审计事件保持隔离。"""

        with workspace_tempdir() as runtime_directory:
            database_path = Path(runtime_directory) / "tasks.sqlite3"
            service = LLMTaskService(str(database_path))
            tasks: list[tuple[str, dict[str, object]]] = []
            for index in range(50):
                business_key = f"isolated-{index}.pdf"
                tasks.append(
                    (
                        business_key,
                        _create_terminal_task(
                            service,
                            database_path,
                            business_type="file",
                            business_key=business_key,
                        ),
                    )
                )

            def acquire_distinct(item: tuple[str, dict[str, object]]) -> str:
                business_key, task = item
                result = _acquire(
                    service,
                    task,
                    business_type="file",
                    business_key=business_key,
                    token=f"token-{business_key}",
                )
                return str(result["outcome"])

            with ThreadPoolExecutor(max_workers=50) as pool:
                outcomes = list(pool.map(acquire_distinct, tasks))

            self.assertEqual(["acquired"] * 50, outcomes)
            for business_key, _task in tasks:
                events = service.list_callback_delivery_attempt_events(
                    business_type="file",
                    business_key=business_key,
                )
                self.assertEqual(1, len(events))
                self.assertEqual(1, events[0]["callback_attempt"])

    def test_release_audit_and_attempt_events_are_independent(self) -> None:
        """人工解除只追加 release audit，不覆盖或冒充一次 Callback attempt。"""

        with workspace_tempdir() as runtime_directory:
            database_path = Path(runtime_directory) / "tasks.sqlite3"
            service = LLMTaskService(str(database_path))
            task = _create_terminal_task(
                service,
                database_path,
                business_type="weaponry",
                business_key="6101",
            )
            _freeze_as_unknown(
                service,
                task,
                business_type="weaponry",
                business_key="6101",
            )
            released = service.release_callback_delivery_guard(
                business_type="weaponry",
                business_key="6101",
                released_by="u3-test-operator",
                release_reason="确认旧 Worker 已隔离，仅验证审计隔离",
                worker_stopped_confirmed=True,
                released_at="2030-01-01T00:00:02+00:00",
            )
            self.assertEqual("released", released)
            attempt_events = service.list_callback_delivery_attempt_events(
                business_type="weaponry",
                business_key="6101",
            )
            release_audits = (
                service.list_callback_delivery_guard_release_audits(
                    business_type="weaponry",
                    business_key="6101",
                )
            )
            self.assertEqual(
                {"authorized", "completed"},
                {event["event_type"] for event in attempt_events},
            )
            self.assertEqual(1, len(release_audits))
            self.assertEqual("u3-test-operator", release_audits[0]["released_by"])

    def test_expired_lease_appends_unknown_followup_event(self) -> None:
        """维护观察只冻结过期租约，并沿用原授权的 attempt/trigger/trace。"""

        with workspace_tempdir() as runtime_directory:
            database_path = Path(runtime_directory) / "tasks.sqlite3"
            service = LLMTaskService(str(database_path))
            task = _create_terminal_task(
                service,
                database_path,
                business_type="file",
                business_key="expired-audit.pdf",
            )
            acquired = _acquire(
                service,
                task,
                business_type="file",
                business_key="expired-audit.pdf",
                token="expired-token",
                trace_id="initial-worker-trace",
            )
            self.assertEqual("acquired", acquired["outcome"])
            observed = service.observe_callback_delivery_guard(
                business_type="file",
                business_key="expired-audit.pdf",
                observed_at="2030-01-01T00:01:00+00:00",
            )
            self.assertEqual("outcome_unknown", observed["state"])
            events = service.list_callback_delivery_attempt_events(
                business_type="file",
                business_key="expired-audit.pdf",
            )
            self.assertEqual(
                {"authorized", "lease_expired_unknown"},
                {event["event_type"] for event in events},
            )
            self.assertEqual({1}, {event["callback_attempt"] for event in events})
            self.assertEqual(
                {"initial-worker-trace"},
                {event["request_trace_id"] for event in events},
            )

    def test_guard_inconsistency_freeze_appends_diagnostic_event(self) -> None:
        """execution 已 sending 但 Guard 缺失时只冻结，不重新授权 HTTP。"""

        with workspace_tempdir() as runtime_directory:
            database_path = Path(runtime_directory) / "tasks.sqlite3"
            service = LLMTaskService(str(database_path))
            task = _create_terminal_task(
                service,
                database_path,
                business_type="report",
                business_key="7132",
            )
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    """
                    UPDATE llm_task_executions
                    SET callback_status = 'sending'
                    WHERE execution_id = ?
                    """,
                    (task["execution_id"],),
                )
            result = _acquire(
                service,
                task,
                business_type="report",
                business_key="7132",
                token="must-not-be-authorized",
                trace_id="inconsistency-trace",
            )
            self.assertEqual("outcome_unknown", result["outcome"])
            events = service.list_callback_delivery_attempt_events(
                business_type="report",
                business_key="7132",
            )
            self.assertEqual(1, len(events))
            self.assertEqual("guard_inconsistent_unknown", events[0]["event_type"])
            self.assertEqual("guard_state_inconsistent", events[0]["delivery_outcome"])

    def test_additive_attempt_schema_migrates_idempotently_without_deleting_legacy_data(
        self,
    ) -> None:
        """模拟升级前数据库，证明新审计表可重复创建且不会删除既有业务表或数据。"""

        with workspace_tempdir() as runtime_directory:
            database_path = Path(runtime_directory) / "tasks.sqlite3"
            initial_service = LLMTaskService(str(database_path))
            seed_legacy_file_task(initial_service,
                "legacy-before-attempt-audit.pdf",
                {"businessType": "file", "legacy": True},
            )
            with sqlite3.connect(database_path) as connection:
                # 仅在临时测试库移除本阶段新增结构，精确模拟旧版本 Schema；额外 sentinel
                # 表用于证明初始化只增不删，工作目录和生产数据均不会被访问。
                connection.execute("DROP TABLE callback_delivery_attempt_events")
                connection.execute(
                    "CREATE TABLE legacy_schema_sentinel (value TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT INTO legacy_schema_sentinel (value) VALUES ('preserved')"
                )

            LLMTaskService(str(database_path))
            LLMTaskService(str(database_path))

            with sqlite3.connect(database_path) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                sentinel = connection.execute(
                    "SELECT value FROM legacy_schema_sentinel"
                ).fetchone()
            self.assertIn("callback_delivery_attempt_events", tables)
            self.assertIn("llm_tasks", tables)
            self.assertIn("llm_task_executions", tables)
            self.assertEqual(("preserved",), sentinel)
            self.assertIsNotNone(
                initial_service.get_task("file", "legacy-before-attempt-audit.pdf")
            )

    def test_damaged_unknown_guard_remains_frozen_without_new_attempt(self) -> None:
        """unknown 的 Guard owner 损坏时必须 fail-closed，不能因显式请求盲目补发。"""

        with workspace_tempdir() as runtime_directory:
            database_path = Path(runtime_directory) / "tasks.sqlite3"
            service = LLMTaskService(str(database_path))
            task = _create_terminal_task(
                service,
                database_path,
                business_type="report",
                business_key="9132",
            )
            _freeze_as_unknown(
                service,
                task,
                business_type="report",
                business_key="9132",
            )
            before_events = service.list_callback_delivery_attempt_events(
                business_type="report",
                business_key="9132",
            )
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    """
                    UPDATE callback_delivery_guards
                    SET owner_execution_id = ?
                    WHERE business_type = ? AND business_key = ?
                    """,
                    ("corrupted-owner", "report", "9132"),
                )

            result = _acquire(
                service,
                task,
                business_type="report",
                business_key="9132",
                token="must-not-send-damaged-unknown",
                explicit_unknown=True,
                expected_callback_attempts=1,
                trace_id="damaged-unknown-trace",
            )

            self.assertEqual("outcome_unknown", result["outcome"])
            self.assertEqual(
                1,
                service.get_task("report", "9132")["callback_attempts"],
            )
            self.assertEqual(
                before_events,
                service.list_callback_delivery_attempt_events(
                    business_type="report",
                    business_key="9132",
                ),
            )


if __name__ == "__main__":  # pragma: no cover - 仅供本地定向调试。
    unittest.main()
