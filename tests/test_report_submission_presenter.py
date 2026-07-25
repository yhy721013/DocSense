"""报告提交 Presenter 的框架无关 202/400/409 契约测试。"""

from __future__ import annotations

import json
import unittest

from app.modules.report.application import SubmitReportResult
from app.modules.tasks.domain import TaskId
from app.presenters.report_submission import ReportSubmissionResponsePresenter


class ReportSubmissionResponsePresenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.presenter = ReportSubmissionResponsePresenter()

    def test_success_discards_internal_task_and_returns_zero_bytes(self) -> None:
        presentation = self.presenter.present_success(
            SubmitReportResult(
                task_id=TaskId("internal-report-task"),
                progress_notified=False,
                dispatcher_notified=False,
            )
        )

        self.assertEqual(202, presentation.status_code)
        self.assertEqual(b"", presentation.body)
        self.assertIsNone(presentation.content_type)

    def test_conflict_keeps_existing_error_shape_and_text(self) -> None:
        presentation = self.presenter.present_conflict()

        self.assertEqual(409, presentation.status_code)
        self.assertEqual(
            {"error": "任务正在处理中"},
            json.loads(presentation.body.decode("utf-8")),
        )
        self.assertEqual(
            "application/json; charset=utf-8",
            presentation.content_type,
        )

    def test_bad_request_uses_only_validated_message(self) -> None:
        presentation = self.presenter.present_bad_request("params不能为空")

        self.assertEqual(400, presentation.status_code)
        self.assertEqual(
            {"error": "params不能为空"},
            json.loads(presentation.body.decode("utf-8")),
        )

    def test_present_success_rejects_unknown_result_type(self) -> None:
        with self.assertRaises(TypeError):
            self.presenter.present_success(object())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
