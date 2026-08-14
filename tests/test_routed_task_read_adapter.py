"""Task Read 迁移路由的离线契约测试。"""

from __future__ import annotations

import unittest

from app.modules.tasks.adapters import RoutedTaskReadAdapter
from app.modules.tasks.domain import TaskBusinessRef, TaskId
from tests.fakes.tasks import FakeTaskReadPort


class RoutedTaskReadAdapterTests(unittest.TestCase):
    def test_business_key_reads_follow_explicit_migration_set(self) -> None:
        """Report/Weaponry 读取 v2，尚未迁移的 file 仍读取遗留控制面。"""

        v2_reader = FakeTaskReadPort()
        legacy_reader = FakeTaskReadPort()
        adapter = RoutedTaskReadAdapter(
            v2_reader=v2_reader,
            legacy_reader=legacy_reader,
            v2_business_types=frozenset({"report", "weaponry"}),
        )
        report_ref = TaskBusinessRef("report", "1")
        weaponry_ref = TaskBusinessRef("weaponry", "2")
        file_ref = TaskBusinessRef("file", "sample.pdf")

        adapter.get_latest_many((report_ref, weaponry_ref, file_ref))

        self.assertEqual([report_ref, weaponry_ref], v2_reader.latest_calls)
        self.assertEqual([file_ref], legacy_reader.latest_calls)

    def test_task_id_reads_probe_v2_before_legacy(self) -> None:
        """TaskId 不携带业务类型，必须保持 v2 优先、旧库兜底的兼容顺序。"""

        v2_reader = FakeTaskReadPort()
        legacy_reader = FakeTaskReadPort()
        adapter = RoutedTaskReadAdapter(
            v2_reader=v2_reader,
            legacy_reader=legacy_reader,
            v2_business_types=frozenset({"report", "weaponry"}),
        )
        task_id = TaskId("task-read-route-order")

        self.assertIsNone(adapter.get_by_id(task_id))

        self.assertEqual([task_id], v2_reader.by_id_calls)
        self.assertEqual([task_id], legacy_reader.by_id_calls)


if __name__ == "__main__":
    unittest.main()
