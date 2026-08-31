"""Chat 公开身份值对象与内部 UUID 边界测试。"""

from __future__ import annotations

import unittest

from app.modules.chat.domain.identity import (
    ConversationIdentity,
    ConversationIdentityBinding,
    FileChatIdentity,
    MAX_JAVASCRIPT_SAFE_INTEGER,
    WeaponryChatIdentity,
    parse_identity_key,
    require_conversation_id,
)


_CONVERSATION_ID = "abcdefab-1234-5678-9234-567812345678"


class ChatIdentityTests(unittest.TestCase):
    def test_file_identity_has_stable_key_and_satisfies_protocol(self) -> None:
        identity = FileChatIdentity(chat_id=7)
        self.assertIsInstance(identity, ConversationIdentity)
        self.assertEqual("file", identity.identity_kind)
        self.assertEqual("file:7", identity.identity_key)
        self.assertEqual(identity, parse_identity_key(identity.identity_key))

    def test_weaponry_identity_has_stable_composite_key(self) -> None:
        identity = WeaponryChatIdentity(user_id=11, architecture_id=29)
        self.assertIsInstance(identity, ConversationIdentity)
        self.assertEqual("weaponry", identity.identity_kind)
        self.assertEqual("weaponry:11:29", identity.identity_key)
        self.assertEqual(identity, parse_identity_key(identity.identity_key))

    def test_weaponry_fields_enforce_javascript_safe_integer_limit(self) -> None:
        WeaponryChatIdentity(
            user_id=MAX_JAVASCRIPT_SAFE_INTEGER,
            architecture_id=MAX_JAVASCRIPT_SAFE_INTEGER,
        )
        for field_name in ("user_id", "architecture_id"):
            values = {"user_id": 1, "architecture_id": 1}
            values[field_name] = MAX_JAVASCRIPT_SAFE_INTEGER + 1
            with self.subTest(field=field_name), self.assertRaises(ValueError):
                WeaponryChatIdentity(**values)

    def test_bool_float_string_zero_and_negative_are_rejected(self) -> None:
        for value in (True, 1.0, "1", 0, -1):
            with self.subTest(file=value), self.assertRaises(ValueError):
                FileChatIdentity(chat_id=value)  # type: ignore[arg-type]
            with self.subTest(user=value), self.assertRaises(ValueError):
                WeaponryChatIdentity(  # type: ignore[arg-type]
                    user_id=value,
                    architecture_id=1,
                )

    def test_internal_id_requires_canonical_uuid(self) -> None:
        self.assertEqual(_CONVERSATION_ID, require_conversation_id(_CONVERSATION_ID))
        for value in (
            "",
            "public-1",
            _CONVERSATION_ID.upper(),
            "{" + _CONVERSATION_ID + "}",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                require_conversation_id(value)

    def test_file_binding_is_permanent_and_weaponry_can_be_released(self) -> None:
        ConversationIdentityBinding(
            conversation_id=_CONVERSATION_ID,
            identity_kind="file",
            chat_id=7,
            user_id=None,
            architecture_id=None,
            active=True,
            created_at="2026-08-02T00:00:00+00:00",
        )
        with self.assertRaisesRegex(ValueError, "remain active"):
            ConversationIdentityBinding(
                conversation_id=_CONVERSATION_ID,
                identity_kind="file",
                chat_id=7,
                user_id=None,
                architecture_id=None,
                active=False,
                created_at="2026-08-02T00:00:00+00:00",
                released_at="2026-08-02T00:01:00+00:00",
            )
        released = ConversationIdentityBinding(
            conversation_id=_CONVERSATION_ID,
            identity_kind="weaponry",
            chat_id=None,
            user_id=11,
            architecture_id=29,
            active=False,
            created_at="2026-08-02T00:00:00+00:00",
            released_at="2026-08-02T00:01:00+00:00",
        )
        self.assertFalse(released.active)


if __name__ == "__main__":
    unittest.main()
