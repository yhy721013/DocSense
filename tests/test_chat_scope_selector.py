"""文件对话 Web 范围选择器与领域隔离测试。"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from app.adapters.web import (
    ChatScopeSelectorValidationError,
    parse_chat_scope_selector,
)
from app.modules.chat.domain.document_scope import (
    CHAT_SCOPE_MODE_FILES,
)


_ROOT = Path(__file__).resolve().parents[1]


class ChatScopeSelectorWebTests(unittest.TestCase):
    """验证旧 architecture 模式下线和既有 fileNames 行为。"""

    def test_architecture_id_is_never_recognized_on_file_route(self) -> None:
        for params in (
            {"architectureId": 7},
            {"architectureId": "0007"},
            {"architectureId": 7, "fileNames": []},
        ):
            with self.subTest(params=params):
                with self.assertRaisesRegex(
                    ChatScopeSelectorValidationError,
                    "architectureId与fileNames不能同时传入",
                ):
                    parse_chat_scope_selector(params)

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
            "app/modules/chat/domain/document_candidates.py",
            "app/modules/chat/domain/document_scope.py",
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
