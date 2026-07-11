"""供应商无关文件对话资源标识的单元测试。"""

from __future__ import annotations

import unittest

from app.services.chat.domain.resource_ids import (
    chat_scoped_external_ref,
    parse_chat_scoped_external_ref,
)


class ChatScopedExternalRefTests(unittest.TestCase):
    """确保持久化租约序列化不会限制供应商标识。"""

    def test_round_trip_preserves_references_with_delimiter_characters(self) -> None:
        """外部引用可含任意分隔符，租约恢复仍应准确定位资源。"""
        context_ref = "tenant::workspace/中文?version=1"
        resource_ref = "thread::child/with:punctuation"

        encoded = chat_scoped_external_ref(
            context_ref=context_ref,
            resource_ref=resource_ref,
        )

        self.assertEqual(
            (context_ref, resource_ref),
            parse_chat_scoped_external_ref(encoded),
        )

    def test_parser_rejects_non_envelope_value(self) -> None:
        """损坏的恢复记录必须在发起远端删除前被拒绝。"""
        with self.assertRaisesRegex(ValueError, "scoped chat resource"):
            parse_chat_scoped_external_ref("workspace::thread")


if __name__ == "__main__":
    unittest.main()
