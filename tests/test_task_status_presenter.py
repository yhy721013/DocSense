"""阶段 1B-1：check-task 框架无关 Presenter 契约测试。"""

from __future__ import annotations

import json
import unittest

from app.modules.tasks.application import (
    RequestCallbackRecoveryItemResult,
    RequestCallbackRecoveryResult,
)
from app.modules.tasks.domain import (
    CALLBACK_FAILED,
    TaskBusinessRef,
    TaskId,
    TaskLookupItem,
    TaskSnapshot,
)
from app.modules.tasks.ports import (
    CallbackRecoveryCommandOutcome,
    CallbackRecoveryCommandResult,
)
from app.presenters.task_status import (
    CheckTaskResponsePresenter,
    TaskStatusHttpPresentation,
)


def _lookup(file_name: str) -> TaskLookupItem:
    return TaskLookupItem(
        business_ref=TaskBusinessRef("file", file_name),
        response_key="fileName",
        response_value=file_name,
    )


def _found_item(file_name: str) -> RequestCallbackRecoveryItemResult:
    lookup = _lookup(file_name)
    snapshot = TaskSnapshot(
        task_id=TaskId(f"internal-task-{file_name}"),
        task_type="file_analysis",
        business_ref=lookup.business_ref,
        execution_state="succeeded",
        public_status="2",
        progress=1.0,
        message="解析完成",
        callback_status=CALLBACK_FAILED,
        created_at="2026-07-16T10:00:00+08:00",
        updated_at="2026-07-16T10:01:00+08:00",
    )
    command = CallbackRecoveryCommandResult(
        expected_task_id=snapshot.task_id,
        business_ref=snapshot.business_ref,
        outcome=CallbackRecoveryCommandOutcome.CREATED,
        recovery_request_id=f"internal-recovery-{file_name}",
    )
    return RequestCallbackRecoveryItemResult(
        lookup=lookup,
        snapshot=snapshot,
        command=command,
    )


def _missing_item(file_name: str) -> RequestCallbackRecoveryItemResult:
    return RequestCallbackRecoveryItemResult(
        lookup=_lookup(file_name),
        snapshot=None,
        command=None,
    )


class CheckTaskResponsePresenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.presenter = CheckTaskResponsePresenter()

    def test_single_success_is_http_200_with_exact_zero_byte_body(self) -> None:
        result = RequestCallbackRecoveryResult((_found_item("success.pdf"),))

        response = self.presenter.present(result)

        self.assertEqual(200, response.status_code)
        self.assertEqual(b"", response.body)
        self.assertEqual(0, len(response.body))
        self.assertIsNone(response.content_type)
        self.assertNotIn(b"internal-task", response.body)
        self.assertNotIn(b"internal-recovery", response.body)

    def test_batch_partial_missing_is_still_empty_success(self) -> None:
        result = RequestCallbackRecoveryResult(
            (
                _found_item("first.pdf"),
                _missing_item("missing.pdf"),
                _found_item("last.pdf"),
            )
        )

        response = self.presenter.present(result)

        self.assertEqual(200, response.status_code)
        self.assertEqual(b"", response.body)
        self.assertIsNone(response.content_type)

    def test_single_missing_preserves_existing_404_json_error(self) -> None:
        result = RequestCallbackRecoveryResult((_missing_item("missing.pdf"),))

        response = self.presenter.present(result)

        self.assertEqual(404, response.status_code)
        self.assertEqual(
            {"error": "任务不存在"},
            json.loads(response.body.decode("utf-8")),
        )
        self.assertEqual("application/json; charset=utf-8", response.content_type)

    def test_bad_request_preserves_existing_error_field_and_message(self) -> None:
        for message in (
            "businessType无效",
            "params不能为空",
            "fileName不能为空",
        ):
            with self.subTest(message=message):
                response = self.presenter.present_bad_request(message)
                self.assertEqual(400, response.status_code)
                self.assertEqual(
                    {"error": message},
                    json.loads(response.body.decode("utf-8")),
                )
                self.assertEqual(
                    "application/json; charset=utf-8",
                    response.content_type,
                )

    def test_presenter_rejects_untyped_results_and_error_objects(self) -> None:
        with self.assertRaisesRegex(TypeError, "result"):
            self.presenter.present(object())  # type: ignore[arg-type]
        with self.assertRaisesRegex(TypeError, "error_message"):
            self.presenter.present_bad_request(RuntimeError("secret"))  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "不能为空"):
            self.presenter.present_bad_request("  ")

    def test_presentation_requires_bytes_and_valid_status(self) -> None:
        with self.assertRaisesRegex(TypeError, "body"):
            TaskStatusHttpPresentation(200, "")  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "status_code"):
            TaskStatusHttpPresentation(99, b"")


if __name__ == "__main__":
    unittest.main()
