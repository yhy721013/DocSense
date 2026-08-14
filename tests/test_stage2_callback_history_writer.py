"""阶段 2-4 Callback History 唯一 Writer 与兼容门面验收。"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from app.infrastructure.observability.callback_history import (
    save_callback_history_payload as save_shared_history,
)
from app.services.utils.callback_client import (
    save_callback_history_payload as save_legacy_history,
)
from tests import workspace_tempdir


class Stage2CallbackHistoryWriterTests(unittest.TestCase):
    def test_shared_writer_keeps_path_payload_and_no_overwrite_semantics(self) -> None:
        with workspace_tempdir() as tmp:
            history_dir = Path(tmp) / "callback"
            payload = {
                "businessType": "report",
                "data": {"reportId": 0, "status": "2"},
                "msg": "生成成功",
            }
            timestamp = datetime(2026, 8, 13, 20, 0, 0, 123456)

            first = save_shared_history(
                payload,
                history_dir=history_dir,
                timestamp=timestamp,
            )
            second = save_shared_history(
                payload,
                history_dir=history_dir,
                timestamp=timestamp,
            )

            self.assertEqual("report-0-20260813T200000123456.json", first.name)
            self.assertEqual("report-0-20260813T200000123456-2.json", second.name)
            self.assertEqual(payload, json.loads(first.read_text(encoding="utf-8")))

    def test_legacy_facade_delegates_to_shared_physical_writer(self) -> None:
        with workspace_tempdir() as tmp:
            history_dir = Path(tmp) / "callback"
            payload = {"businessType": "report", "data": {"reportId": 132}}
            with patch(
                "app.services.utils.callback_client._save_callback_history_payload",
                wraps=save_shared_history,
            ) as shared_writer:
                path = save_legacy_history(payload, history_dir=history_dir)

            self.assertTrue(path.is_file())
            shared_writer.assert_called_once()

    def test_report_adapter_imports_shared_writer_directly(self) -> None:
        source = (
            Path(__file__).parents[1]
            / "app"
            / "modules"
            / "report"
            / "adapters"
            / "callback_guard.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "app.infrastructure.observability.callback_history",
            source,
        )
        self.assertNotIn("app.services.utils.callback_client", source)


if __name__ == "__main__":
    unittest.main()
