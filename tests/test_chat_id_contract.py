"""文件对话公开 chatId 类型契约的单元测试。"""

from __future__ import annotations

import unittest

from app.modules.chat.domain.chat_id import (
    chat_id_public_value,
    chat_id_storage_key,
    parse_query_chat_id,
    require_public_chat_id,
)


class ChatIdContractTests(unittest.TestCase):
    """确保 API 边界不会因 Python 或 URL 的隐式类型行为放宽契约。"""

    def test_json_chat_id_accepts_only_positive_integer(self) -> None:
        self.assertEqual(10001, require_public_chat_id(10001))

        for invalid_value in ("10001", True, False, 0, -1, 1.0, None):
            with self.subTest(invalid_value=invalid_value):
                with self.assertRaisesRegex(ValueError, "chatId必须为正整数"):
                    require_public_chat_id(invalid_value)

    def test_query_chat_id_requires_canonical_decimal_text(self) -> None:
        self.assertEqual(10001, parse_query_chat_id("10001"))

        for invalid_value in (None, "", "0", "-1", "1.0", "001", " 1", "true"):
            with self.subTest(invalid_value=invalid_value):
                with self.assertRaisesRegex(ValueError, "chatId必须为正整数"):
                    parse_query_chat_id(invalid_value)

    def test_internal_storage_key_and_public_value_round_trip(self) -> None:
        self.assertEqual("10001", chat_id_storage_key(10001))
        self.assertEqual(10001, chat_id_public_value("10001"))

    def test_noncanonical_internal_key_cannot_be_echoed_as_public_value(self) -> None:
        for invalid_value in (" 10001 ", "001", "0", "-1", "legacy-chat", ""):
            with self.subTest(invalid_value=invalid_value):
                with self.assertRaisesRegex(ValueError, "内部chatId不是规范正整数"):
                    chat_id_public_value(invalid_value)


if __name__ == "__main__":
    unittest.main()
