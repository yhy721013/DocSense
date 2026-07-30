"""文件对话 Web 范围选择器与领域隔离测试。"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from app.adapters.web import (
    ChatScopeSelectorValidationError,
    parse_chat_scope_selector,
)
from app.services.chat.domain.document_scope import (
    CHAT_SCOPE_MODE_ARCHITECTURE,
    CHAT_SCOPE_MODE_FILES,
)
from app.services.chat.domain.limits import MAX_CHAT_ARCHITECTURE_ID


_ROOT = Path(__file__).resolve().parents[1]


class ChatScopeSelectorWebTests(unittest.TestCase):
    """验证字段存在性、ID 规范化和既有 fileNames 行为。"""

    def test_architecture_and_file_names_are_mutually_exclusive_by_presence(self) -> None:
        for file_names in ([], ["a.pdf"], None):
            with self.subTest(file_names=file_names):
                with self.assertRaisesRegex(
                    ChatScopeSelectorValidationError,
                    "architectureId与fileNames不能同时传入",
                ):
                    parse_chat_scope_selector(
                        {"architectureId": 7, "fileNames": file_names}
                    )

    def test_architecture_id_is_normalized_to_domain_integer(self) -> None:
        for raw_value in (1, "1", "0001", MAX_CHAT_ARCHITECTURE_ID):
            with self.subTest(raw_value=raw_value):
                selector = parse_chat_scope_selector(
                    {"architectureId": raw_value}
                )
                self.assertEqual(CHAT_SCOPE_MODE_ARCHITECTURE, selector.scope_mode)
                self.assertIsInstance(selector.architecture_id, int)
                self.assertEqual(int(raw_value), selector.architecture_id)
                self.assertEqual((), selector.file_names)

    def test_architecture_id_rejects_null_bool_and_non_decimal_values(self) -> None:
        cases = (
            (None, "architectureId不能为空"),
            (True, "architectureId必须为1到9007199254740991之间的正整数"),
            (1.0, "architectureId必须为1到9007199254740991之间的正整数"),
            ("1e3", "architectureId必须为1到9007199254740991之间的正整数"),
            ("１", "architectureId必须为1到9007199254740991之间的正整数"),
            (
                MAX_CHAT_ARCHITECTURE_ID + 1,
                "architectureId必须为1到9007199254740991之间的正整数",
            ),
            ("9" * 10000, "architectureId必须为1到9007199254740991之间的正整数"),
        )
        for raw_value, expected_error in cases:
            with self.subTest(raw_type=type(raw_value).__name__):
                with self.assertRaisesRegex(
                    ChatScopeSelectorValidationError,
                    expected_error,
                ):
                    parse_chat_scope_selector({"architectureId": raw_value})

    def test_missing_both_selectors_preserves_existing_file_error(self) -> None:
        with self.assertRaisesRegex(
            ChatScopeSelectorValidationError,
            "fileNames必须为数组",
        ):
            parse_chat_scope_selector({})

    def test_file_names_keep_stable_trim_and_dedup_behavior(self) -> None:
        selector = parse_chat_scope_selector(
            {"fileNames": [" a.pdf ", "a.pdf", "b.pdf"]}
        )

        self.assertEqual(CHAT_SCOPE_MODE_FILES, selector.scope_mode)
        self.assertEqual(("a.pdf", "b.pdf"), selector.file_names)
        self.assertIsNone(selector.architecture_id)


class ChatScopeDomainBoundaryTests(unittest.TestCase):
    """Chat Domain 不得反向依赖 Web、Weaponry、SQLite 或供应商实现。"""

    def test_scope_domain_imports_only_chat_domain_and_standard_library(self) -> None:
        forbidden_prefixes = (
            "flask",
            "sqlite3",
            "requests",
            "app.adapters",
            "app.modules.weaponry",
            "app.integrations",
        )
        for relative_path in (
            "app/services/chat/domain/document_candidates.py",
            "app/services/chat/domain/document_scope.py",
        ):
            source = (_ROOT / relative_path).read_text(encoding="utf-8")
            tree = ast.parse(source)
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module)
            self.assertFalse(
                [
                    name
                    for name in imports
                    if name.startswith(forbidden_prefixes)
                ],
                relative_path,
            )


if __name__ == "__main__":
    unittest.main()
