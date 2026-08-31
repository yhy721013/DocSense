"""Chat Workspace 业务身份命名纯规则测试。"""

from __future__ import annotations

import unittest

from app.modules.chat.domain.identity import (
    ConversationIdentityBinding,
    MAX_JAVASCRIPT_SAFE_INTEGER,
)
from app.modules.chat.domain.workspace_naming import chat_workspace_name


_CONVERSATION_ID = "abcdefab-1234-5678-9234-567812345678"
_CREATED_AT = "2026-08-02T00:00:00+00:00"


def _file_binding(chat_id: int) -> ConversationIdentityBinding:
    """构造有效 File Binding，避免各测试重复无关持久化字段。"""

    return ConversationIdentityBinding(
        conversation_id=_CONVERSATION_ID,
        identity_kind="file",
        chat_id=chat_id,
        user_id=None,
        architecture_id=None,
        active=True,
        created_at=_CREATED_AT,
    )


def _weaponry_binding(
    *,
    user_id: int,
    architecture_id: int,
) -> ConversationIdentityBinding:
    """构造有效 Weaponry Binding，并保持公开 ID 的整数规范。"""

    return ConversationIdentityBinding(
        conversation_id=_CONVERSATION_ID,
        identity_kind="weaponry",
        chat_id=None,
        user_id=user_id,
        architecture_id=architecture_id,
        active=True,
        created_at=_CREATED_AT,
    )


def _malformed_binding(**overrides: object) -> ConversationIdentityBinding:
    """只为验证防御分支构造绕过领域初始化的损坏持久化投影。"""

    values: dict[str, object] = {
        "conversation_id": _CONVERSATION_ID,
        "identity_kind": "file",
        "chat_id": 1,
        "user_id": None,
        "architecture_id": None,
        "active": True,
        "created_at": _CREATED_AT,
        "released_at": "",
    }
    values.update(overrides)
    binding = object.__new__(ConversationIdentityBinding)
    for field_name, value in values.items():
        # 正常生产构造严禁绕过 ``__post_init__``。测试显式制造损坏对象，确保未来
        # 反序列化或迁移缺陷不会生成看似合法的供应商资源名。
        object.__setattr__(binding, field_name, value)
    return binding


class ChatWorkspaceNamingTests(unittest.TestCase):
    """验证两类身份都得到稳定、无内部 ID 的精确名称。"""

    def test_file_workspace_uses_public_chat_id(self) -> None:
        binding = _file_binding(10001)

        self.assertEqual("chat-id10001", chat_workspace_name(binding))

    def test_weaponry_workspace_uses_user_and_architecture_ids(self) -> None:
        binding = _weaponry_binding(user_id=10001, architecture_id=20001)

        self.assertEqual(
            "wChat-user10001-arch20001",
            chat_workspace_name(binding),
        )

    def test_weaponry_workspace_accepts_javascript_safe_upper_bound(self) -> None:
        binding = _weaponry_binding(
            user_id=MAX_JAVASCRIPT_SAFE_INTEGER,
            architecture_id=MAX_JAVASCRIPT_SAFE_INTEGER,
        )

        self.assertEqual(
            "wChat-user9007199254740991-arch9007199254740991",
            chat_workspace_name(binding),
        )

    def test_file_workspace_does_not_truncate_large_valid_chat_id(self) -> None:
        # 文件对话公开合同当前没有额外上界。纯领域规则必须完整保留合法整数，不能
        # 因供应商可能存在的名称限制而在内部静默截断或改成哈希。
        large_chat_id = 10**100 + 7

        self.assertEqual(
            f"chat-id{large_chat_id}",
            chat_workspace_name(_file_binding(large_chat_id)),
        )

    def test_workspace_name_does_not_contain_internal_conversation_id(self) -> None:
        for binding in (
            _file_binding(7),
            _weaponry_binding(user_id=11, architecture_id=29),
        ):
            with self.subTest(identity_kind=binding.identity_kind):
                self.assertNotIn(
                    binding.conversation_id,
                    chat_workspace_name(binding),
                )

    def test_non_binding_input_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            chat_workspace_name(object())  # type: ignore[arg-type]

    def test_damaged_file_binding_without_chat_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires chat_id"):
            chat_workspace_name(_malformed_binding(chat_id=None))

    def test_damaged_weaponry_binding_without_required_ids_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires user_id"):
            chat_workspace_name(
                _malformed_binding(
                    identity_kind="weaponry",
                    chat_id=None,
                    user_id=None,
                    architecture_id=9,
                )
            )

    def test_unknown_identity_kind_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported"):
            chat_workspace_name(_malformed_binding(identity_kind="future-kind"))


if __name__ == "__main__":
    unittest.main()
