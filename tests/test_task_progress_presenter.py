"""阶段 1B-2 Progress WebSocket Presenter 测试。"""

from __future__ import annotations

import json
import unittest

from app.modules.tasks.application import CurrentProgressItem, ProgressSnapshotSource
from app.modules.tasks.domain import ProgressKey, ProgressSnapshot, TaskId
from app.presenters.task_progress import ProgressWebSocketPresenter


def _snapshot(business_type: str, business_key: str, progress: float) -> ProgressSnapshot:
    return ProgressSnapshot(
        key=ProgressKey(business_type, business_key),
        task_id=TaskId(f"task-{business_type}-{business_key}"),
        progress=progress,
        message="内部消息不得公开",
        internal_state="running",
        sequence_no=2,
        updated_at="2026-07-16T12:00:00+08:00",
    )


class ProgressWebSocketPresenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.presenter = ProgressWebSocketPresenter()

    def test_file_report_and_weaponry_key_types_match_existing_contract(self) -> None:
        cases = (
            (
                _snapshot("file", "demo.pdf", 0.25),
                {"progress": 0.25, "fileName": "demo.pdf"},
            ),
            (
                _snapshot("report", "132", 0.5),
                {"progress": 0.5, "reportId": 132},
            ),
            (
                _snapshot("weaponry", "10502", 0.75),
                {"progress": 0.75, "architectureId": "10502"},
            ),
        )
        for snapshot, expected_data in cases:
            with self.subTest(business_type=snapshot.key.business_type):
                message = self.presenter.present_snapshot(snapshot)
                self.assertEqual(snapshot.key.business_type, message["businessType"])
                self.assertEqual(expected_data, message["data"])
                self.assertNotIn("message", message["data"])

    def test_missing_current_snapshot_preserves_exists_false_extension(self) -> None:
        item = CurrentProgressItem(
            key=ProgressKey("file", "missing.pdf"),
            snapshot=None,
            source=ProgressSnapshotSource.MISSING,
        )

        self.assertEqual(
            {
                "businessType": "file",
                "data": {
                    "progress": 0.0,
                    "fileName": "missing.pdf",
                    "exists": False,
                },
            },
            self.presenter.present_current(item),
        )

    def test_error_message_has_existing_two_fields_and_strict_json(self) -> None:
        message = self.presenter.present_error(" 参数无效 ")
        encoded = self.presenter.serialize(message)

        self.assertEqual({"type": "error", "message": "参数无效"}, message)
        self.assertEqual(message, json.loads(encoded))
        self.assertNotIn("ack", encoded)


if __name__ == "__main__":
    unittest.main()
