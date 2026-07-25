"""遗留任务只读 Adapter 的离线转换测试。"""

from __future__ import annotations

import unittest

from app.modules.tasks.adapters import LegacyTaskReadAdapter
from app.modules.tasks.domain import TaskBusinessRef, TaskId
from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir


class LegacyTaskReadAdapterTests(unittest.TestCase):
    def test_reads_latest_by_business_key_and_same_execution_by_id(self) -> None:
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            created = service.create_file_task(
                "adapter.pdf",
                {"businessType": "file"},
                status="1",
            )
            service.update_task_progress(
                "file",
                "adapter.pdf",
                progress=0.28000000004,
                message="处理中",
                status="1",
            )
            adapter = LegacyTaskReadAdapter(service)

            latest = adapter.get_latest(TaskBusinessRef("file", "adapter.pdf"))
            by_id = adapter.get_by_id(TaskId(created["execution_id"]))

        self.assertIsNotNone(latest)
        self.assertEqual(latest, by_id)
        self.assertEqual(0.28, latest.progress)
        self.assertEqual("legacy_status:1", latest.execution_state)

    def test_many_preserves_duplicates_order_and_missing_positions(self) -> None:
        with workspace_tempdir() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            service.create_report_task(132, {"businessType": "report"})
            adapter = LegacyTaskReadAdapter(service)
            refs = (
                TaskBusinessRef("report", "missing"),
                TaskBusinessRef("report", "132"),
                TaskBusinessRef("report", "132"),
            )

            snapshots = adapter.get_latest_many(refs)

        self.assertIsNone(snapshots[0])
        self.assertEqual("132", snapshots[1].business_ref.business_key)
        self.assertEqual(snapshots[1], snapshots[2])


if __name__ == "__main__":
    unittest.main()
