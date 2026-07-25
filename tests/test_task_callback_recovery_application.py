"""阶段 1B-1：check-task 可靠恢复命令边界测试。

本文件只使用 Task Read 和可靠命令 Fake，不创建 Flask 应用、SQLite、RabbitMQ、后台
线程或网络 Session。目标是冻结未来 MySQL/Outbox Adapter 必须满足的批量原子、活动
命令复用、latest-wins 分类和异常传播语义，而不是模拟 Callback Worker 已经存在。
"""

from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from app.modules.tasks.application import (
    CallbackRecoveryCommandContractError,
    CallbackRecoveryTaskReadContractError,
    CheckTaskRequest,
    CheckTaskStatusRequest,
    RequestCallbackRecoveryService,
)
from app.modules.tasks.domain import (
    CALLBACK_FAILED,
    TaskBusinessRef,
    TaskId,
    TaskLookupItem,
    TaskSnapshot,
)
from app.modules.tasks.ports import (
    CALLBACK_RECOVERY_COMMAND_SCHEMA_VERSION,
    CALLBACK_RECOVERY_TRIGGER_CHECK_TASK,
    CallbackRecoveryCommand,
    CallbackRecoveryCommandOutcome,
    CallbackRecoveryCommandPort,
    CallbackRecoveryCommandResult,
)
from tests.fakes import FakeCallbackRecoveryCommandPort, FakeTaskReadPort


def _business_ref(file_name: str) -> TaskBusinessRef:
    return TaskBusinessRef("file", file_name)


def _lookup(file_name: str) -> TaskLookupItem:
    return TaskLookupItem(
        business_ref=_business_ref(file_name),
        response_key="fileName",
        response_value=file_name,
    )


def _snapshot(file_name: str, *, task_id: str | None = None) -> TaskSnapshot:
    return TaskSnapshot(
        task_id=TaskId(task_id or f"task-{file_name}"),
        task_type="file_analysis",
        business_ref=_business_ref(file_name),
        execution_state="succeeded",
        public_status="2",
        progress=1.0,
        message="解析完成",
        callback_status=CALLBACK_FAILED,
        created_at="2026-07-16T10:00:00+08:00",
        updated_at="2026-07-16T10:01:00+08:00",
    )


def _request(*file_names: str) -> CheckTaskRequest:
    return CheckTaskRequest(tuple(_lookup(name) for name in file_names))


class CallbackRecoveryCommandDtoTests(unittest.TestCase):
    """验证命令信封只携带可靠登记所需的最小内部信息。"""

    def test_command_normalizes_trace_fields_and_has_no_public_payload(self) -> None:
        snapshot = _snapshot("minimal.pdf")

        command = CallbackRecoveryCommand(
            expected_task_id=snapshot.task_id,
            business_ref=snapshot.business_ref,
            trace_id=" trace-1b1 ",
            correlation_id=" correlation-1b1 ",
        )

        self.assertEqual(CALLBACK_RECOVERY_TRIGGER_CHECK_TASK, command.trigger)
        self.assertEqual(
            CALLBACK_RECOVERY_COMMAND_SCHEMA_VERSION,
            command.schema_version,
        )
        self.assertEqual("trace-1b1", command.trace_id)
        self.assertEqual("correlation-1b1", command.correlation_id)
        self.assertNotIn("recovery_request_id", vars(command))
        self.assertNotIn("params", vars(command))
        self.assertNotIn("payload", vars(command))
        self.assertNotIn("result", vars(command))

    def test_command_rejects_unknown_trigger_schema_and_trace_types(self) -> None:
        snapshot = _snapshot("invalid-command.pdf")
        kwargs = {
            "expected_task_id": snapshot.task_id,
            "business_ref": snapshot.business_ref,
        }

        with self.assertRaisesRegex(ValueError, "trigger"):
            CallbackRecoveryCommand(**kwargs, trigger="automatic_retry")
        with self.assertRaisesRegex(ValueError, "schema_version"):
            CallbackRecoveryCommand(**kwargs, schema_version=2)
        with self.assertRaisesRegex(TypeError, "trace_id"):
            CallbackRecoveryCommand(**kwargs, trace_id=123)

    def test_result_enforces_recovery_request_id_by_outcome(self) -> None:
        snapshot = _snapshot("result-invariant.pdf")

        with self.assertRaisesRegex(ValueError, "recovery_request_id"):
            CallbackRecoveryCommandResult(
                expected_task_id=snapshot.task_id,
                business_ref=snapshot.business_ref,
                outcome=CallbackRecoveryCommandOutcome.CREATED,
            )
        with self.assertRaisesRegex(ValueError, "不得包含"):
            CallbackRecoveryCommandResult(
                expected_task_id=snapshot.task_id,
                business_ref=snapshot.business_ref,
                outcome=CallbackRecoveryCommandOutcome.STALE,
                recovery_request_id="unexpected-id",
            )
        with self.assertRaisesRegex(TypeError, "outcome"):
            CallbackRecoveryCommandResult(
                expected_task_id=snapshot.task_id,
                business_ref=snapshot.business_ref,
                outcome="created",  # type: ignore[arg-type]
                recovery_request_id="recovery-1",
            )

    def test_existing_sync_request_name_is_a_compatibility_alias(self) -> None:
        self.assertIs(CheckTaskRequest, CheckTaskStatusRequest)


class RequestCallbackRecoveryServiceTests(unittest.TestCase):
    """验证框架无关应用编排和批量事务边界。"""

    def _service(
        self,
        snapshots: tuple[TaskSnapshot, ...] = (),
    ) -> tuple[
        RequestCallbackRecoveryService,
        FakeTaskReadPort,
        FakeCallbackRecoveryCommandPort,
    ]:
        reader = FakeTaskReadPort(snapshots)
        command_port = FakeCallbackRecoveryCommandPort()
        service = RequestCallbackRecoveryService(
            task_reader=reader,
            command_port=command_port,
        )
        return service, reader, command_port

    def test_batch_preserves_order_and_all_four_command_outcomes(self) -> None:
        created = _snapshot("created.pdf")
        active = _snapshot("active.pdf")
        not_needed = _snapshot("not-needed.pdf")
        stale = _snapshot("stale.pdf")
        service, reader, command_port = self._service(
            (created, active, not_needed, stale)
        )
        command_port.configure(
            created.task_id,
            outcome=CallbackRecoveryCommandOutcome.CREATED,
            recovery_request_id="recovery-created",
        )
        command_port.configure(
            active.task_id,
            outcome=CallbackRecoveryCommandOutcome.ALREADY_ACTIVE,
            recovery_request_id="recovery-active",
        )
        command_port.configure(
            not_needed.task_id,
            outcome=CallbackRecoveryCommandOutcome.NOT_NEEDED,
        )
        command_port.configure(
            stale.task_id,
            outcome=CallbackRecoveryCommandOutcome.STALE,
        )
        request = _request(
            "active.pdf",
            "missing.pdf",
            "created.pdf",
            "stale.pdf",
            "not-needed.pdf",
        )

        result = service.request_recovery(
            request,
            trace_id=" trace-batch ",
            correlation_id=" correlation-batch ",
        )

        self.assertEqual(
            [True, False, True, True, True],
            [item.found for item in result.ordered_items],
        )
        self.assertEqual(
            [
                CallbackRecoveryCommandOutcome.ALREADY_ACTIVE,
                None,
                CallbackRecoveryCommandOutcome.CREATED,
                CallbackRecoveryCommandOutcome.STALE,
                CallbackRecoveryCommandOutcome.NOT_NEEDED,
            ],
            [
                item.command.outcome if item.command is not None else None
                for item in result.ordered_items
            ],
        )
        self.assertEqual(1, result.missing_count)
        self.assertEqual(
            1,
            result.count_outcome(CallbackRecoveryCommandOutcome.CREATED),
        )
        self.assertEqual(
            [tuple(item.business_ref for item in request.ordered_items)],
            reader.latest_many_calls,
        )
        self.assertEqual(1, len(command_port.request_many_calls))
        commands = command_port.request_many_calls[0]
        self.assertEqual(
            [
                active.task_id,
                created.task_id,
                stale.task_id,
                not_needed.task_id,
            ],
            [command.expected_task_id for command in commands],
        )
        self.assertTrue(
            all(command.trace_id == "trace-batch" for command in commands)
        )
        self.assertTrue(
            all(
                command.correlation_id == "correlation-batch"
                for command in commands
            )
        )
        self.assertEqual(1, command_port.committed_batches)

    def test_duplicate_task_in_one_batch_creates_then_reuses_same_active_id(self) -> None:
        snapshot = _snapshot("duplicate.pdf")
        service, _, command_port = self._service((snapshot,))
        command_port.configure(
            snapshot.task_id,
            outcome=CallbackRecoveryCommandOutcome.CREATED,
            recovery_request_id="recovery-duplicate",
        )

        result = service.request_recovery(
            _request("duplicate.pdf", "duplicate.pdf")
        )

        first, second = (item.command for item in result.ordered_items)
        assert first is not None
        assert second is not None
        self.assertEqual(CallbackRecoveryCommandOutcome.CREATED, first.outcome)
        self.assertEqual(
            CallbackRecoveryCommandOutcome.ALREADY_ACTIVE,
            second.outcome,
        )
        self.assertEqual(first.recovery_request_id, second.recovery_request_id)
        self.assertEqual(
            {snapshot.task_id: "recovery-duplicate"},
            command_port.active_request_ids,
        )

    def test_repeated_request_reuses_active_command_without_second_creation(self) -> None:
        snapshot = _snapshot("repeat.pdf")
        service, _, command_port = self._service((snapshot,))
        command_port.configure(
            snapshot.task_id,
            outcome=CallbackRecoveryCommandOutcome.CREATED,
            recovery_request_id="recovery-repeat",
        )

        first = service.request_recovery(_request("repeat.pdf"))
        second = service.request_recovery(_request("repeat.pdf"))

        first_command = first.ordered_items[0].command
        second_command = second.ordered_items[0].command
        assert first_command is not None
        assert second_command is not None
        self.assertEqual(CallbackRecoveryCommandOutcome.CREATED, first_command.outcome)
        self.assertEqual(
            CallbackRecoveryCommandOutcome.ALREADY_ACTIVE,
            second_command.outcome,
        )
        self.assertEqual(
            first_command.recovery_request_id,
            second_command.recovery_request_id,
        )
        self.assertEqual(2, command_port.committed_batches)

    def test_fifty_concurrent_requests_create_only_one_active_command(self) -> None:
        """并发重复登记仍只能产生一个活动 ID；不代表生产队列容量验收。"""

        snapshot = _snapshot("concurrent.pdf")
        service, _, command_port = self._service((snapshot,))
        command_port.configure(
            snapshot.task_id,
            outcome=CallbackRecoveryCommandOutcome.CREATED,
            recovery_request_id="recovery-concurrent",
        )
        start_barrier = Barrier(50)

        def request_once() -> tuple[CallbackRecoveryCommandOutcome, str]:
            # 让 50 个线程在进入应用服务前汇合，避免线程池快速串行调度造成假并发。
            start_barrier.wait()
            result = service.request_recovery(_request("concurrent.pdf"))
            command = result.ordered_items[0].command
            assert command is not None
            return command.outcome, command.recovery_request_id

        with ThreadPoolExecutor(max_workers=50) as executor:
            outcomes = tuple(executor.map(lambda _: request_once(), range(50)))

        self.assertEqual(
            1,
            sum(
                outcome is CallbackRecoveryCommandOutcome.CREATED
                for outcome, _ in outcomes
            ),
        )
        self.assertEqual(
            49,
            sum(
                outcome is CallbackRecoveryCommandOutcome.ALREADY_ACTIVE
                for outcome, _ in outcomes
            ),
        )
        self.assertEqual(
            {"recovery-concurrent"},
            {request_id for _, request_id in outcomes},
        )
        self.assertEqual(
            {snapshot.task_id: "recovery-concurrent"},
            command_port.active_request_ids,
        )

    def test_all_missing_skips_command_port_and_keeps_single_missing_semantics(self) -> None:
        service, _, command_port = self._service()

        single = service.request_recovery(_request("missing.pdf"))
        batch = service.request_recovery(
            _request("missing-a.pdf", "missing-b.pdf")
        )

        self.assertTrue(single.single_missing)
        self.assertFalse(batch.single_missing)
        self.assertEqual(2, batch.missing_count)
        self.assertEqual([], command_port.request_many_calls)
        self.assertEqual(0, command_port.committed_batches)

    def test_transaction_failure_propagates_without_partial_success(self) -> None:
        snapshot = _snapshot("transaction-failed.pdf")
        service, _, command_port = self._service((snapshot,))
        command_port.configure(
            snapshot.task_id,
            outcome=CallbackRecoveryCommandOutcome.CREATED,
        )
        command_port.transaction_error = RuntimeError("mysql commit failed")

        with self.assertLogs(
            "app.modules.tasks.application.request_callback_recovery",
            level="ERROR",
        ):
            with self.assertRaisesRegex(RuntimeError, "mysql commit failed"):
                service.request_recovery(_request("transaction-failed.pdf"))

        self.assertEqual({}, command_port.active_request_ids)
        self.assertEqual(0, command_port.committed_batches)
        self.assertEqual(1, len(command_port.request_many_calls))

    def test_fake_rolls_back_whole_batch_when_later_item_is_not_configured(self) -> None:
        first = _snapshot("first.pdf")
        second = _snapshot("second.pdf")
        service, _, command_port = self._service((first, second))
        command_port.configure(
            first.task_id,
            outcome=CallbackRecoveryCommandOutcome.CREATED,
        )

        with self.assertLogs(
            "app.modules.tasks.application.request_callback_recovery",
            level="ERROR",
        ):
            with self.assertRaisesRegex(AssertionError, "未配置"):
                service.request_recovery(_request("first.pdf", "second.pdf"))

        self.assertEqual({}, command_port.active_request_ids)
        self.assertEqual(0, command_port.committed_batches)

    def test_task_reader_must_preserve_length_type_and_business_ref(self) -> None:
        expected = _snapshot("expected.pdf")
        wrong = _snapshot("wrong.pdf")
        cases = (
            ((), "长度"),
            ([expected], "必须返回 tuple"),
            ((object(),), "TaskSnapshot"),
            ((wrong,), "其他业务键"),
        )

        for forced_result, error_text in cases:
            with self.subTest(error_text=error_text):
                service, reader, _ = self._service((expected,))
                reader.forced_many_result = forced_result  # type: ignore[assignment]
                with self.assertRaisesRegex(
                    CallbackRecoveryTaskReadContractError,
                    error_text,
                ):
                    service.request_recovery(_request("expected.pdf"))

    def test_command_port_must_preserve_length_type_and_identity_order(self) -> None:
        snapshot = _snapshot("contract.pdf")
        valid_result = CallbackRecoveryCommandResult(
            expected_task_id=snapshot.task_id,
            business_ref=snapshot.business_ref,
            outcome=CallbackRecoveryCommandOutcome.NOT_NEEDED,
        )

        class InvalidCommandPort:
            def __init__(self, result: object) -> None:
                self.result = result

            def request_many(
                self,
                commands: tuple[CallbackRecoveryCommand, ...],
            ) -> object:
                self.commands = commands
                return self.result

        invalid_cases = (
            ((), "长度"),
            ([valid_result], "必须返回 tuple"),
            ((object(),), "非类型化"),
            (
                (
                    CallbackRecoveryCommandResult(
                        expected_task_id=TaskId("other-task"),
                        business_ref=snapshot.business_ref,
                        outcome=CallbackRecoveryCommandOutcome.NOT_NEEDED,
                    ),
                ),
                "TaskId",
            ),
            (
                (
                    CallbackRecoveryCommandResult(
                        expected_task_id=snapshot.task_id,
                        business_ref=_business_ref("other.pdf"),
                        outcome=CallbackRecoveryCommandOutcome.NOT_NEEDED,
                    ),
                ),
                "业务键",
            ),
        )

        for invalid_result, error_text in invalid_cases:
            with self.subTest(error_text=error_text):
                reader = FakeTaskReadPort((snapshot,))
                service = RequestCallbackRecoveryService(
                    task_reader=reader,
                    command_port=InvalidCommandPort(invalid_result),
                )
                with self.assertRaisesRegex(
                    CallbackRecoveryCommandContractError,
                    error_text,
                ):
                    service.request_recovery(_request("contract.pdf"))

        self.assertTrue(
            isinstance(FakeCallbackRecoveryCommandPort(), CallbackRecoveryCommandPort)
        )
        self.assertEqual(
            CallbackRecoveryCommandOutcome.NOT_NEEDED,
            valid_result.outcome,
        )

    def test_trace_fields_reject_non_strings_before_any_port_call(self) -> None:
        snapshot = _snapshot("trace.pdf")
        service, reader, command_port = self._service((snapshot,))

        with self.assertRaisesRegex(TypeError, "trace_id"):
            service.request_recovery(
                _request("trace.pdf"),
                trace_id={"unsafe": "request-body"},  # type: ignore[arg-type]
            )

        self.assertEqual([], reader.latest_many_calls)
        self.assertEqual([], command_port.request_many_calls)


if __name__ == "__main__":
    unittest.main()
