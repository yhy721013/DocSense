"""Debug Presenter 的精确 JSON 字段投影测试。"""

from __future__ import annotations

import unittest

from app.modules.debug.application import (
    CallbackPreviewResult,
    CallbackRecord,
    ChatAvailableFile,
    ChatBootstrapResult,
    ChatDebugSession,
)
from app.presenters.debug import present_callback_preview, present_chat_bootstrap


class DebugPresenterTests(unittest.TestCase):
    def test_callback_presenter_uses_frozen_field_names(self) -> None:
        record = CallbackRecord("a.json", "a.json", "now", 2)
        payload = present_callback_preview(
            CallbackPreviewResult(True, "读取成功", {"businessType": "file"}, (record,), record)
        )
        self.assertEqual(
            {"message", "ok", "payload", "records", "selectedRecord"},
            set(payload),
        )
        self.assertEqual(
            {"fileName", "id", "modifiedAt", "sizeBytes"},
            set(payload["records"][0]),
        )

    def test_callback_presenter_thaws_nested_application_snapshot(self) -> None:
        """内部 tuple/只读 Mapping 必须还原为公开 JSON 的 list/dict。"""

        result = CallbackPreviewResult(
            True,
            "读取成功",
            {"nested": {"values": [1, 2]}},
            (),
            None,
        )
        payload = present_callback_preview(result)
        self.assertEqual({"nested": {"values": [1, 2]}}, payload["payload"])
        self.assertIsInstance(payload["payload"]["nested"]["values"], list)

    def test_chat_presenter_does_not_expose_internal_metrics(self) -> None:
        payload = present_chat_bootstrap(
            ChatBootstrapResult(
                ok=True,
                message="读取成功",
                sessions=(ChatDebugSession(10001, ("a.pdf",), "c", "u"),),
                available_files=(ChatAvailableFile("a.pdf", 7),),
                active_scope_member_count=1,
                workspace_binding_count=9,
            )
        )
        self.assertEqual({"data", "message", "ok"}, set(payload))
        self.assertEqual({"availableFiles", "sessions"}, set(payload["data"]))
        self.assertEqual(["a.pdf"], payload["data"]["sessions"][0]["fileNames"])
        self.assertNotIn("workspaceBindingCount", payload)


if __name__ == "__main__":
    unittest.main()
