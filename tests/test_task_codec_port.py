"""阶段 2-1：通用 Task Codec 必须归属于 Port 层。"""

from __future__ import annotations

import ast
from pathlib import Path
import unittest

from app.modules.tasks.ports import (
    EncodedTaskResult,
    EncodedTaskSubmission,
    TaskCommandCodec,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TaskCodecPortTests(unittest.TestCase):
    """锁定 Codec 边界，避免后续业务模块重新依赖通用 Adapter。"""

    def test_codec_contracts_are_owned_by_ports(self) -> None:
        expected_module = "app.modules.tasks.ports.task_codec"
        self.assertEqual(EncodedTaskSubmission.__module__, expected_module)
        self.assertEqual(EncodedTaskResult.__module__, expected_module)
        self.assertEqual(TaskCommandCodec.__module__, expected_module)

    def test_legacy_adapter_no_longer_defines_codec_contracts(self) -> None:
        source_path = (
            _PROJECT_ROOT
            / "app"
            / "modules"
            / "tasks"
            / "adapters"
            / "legacy_task_commands.py"
        )
        syntax_tree = ast.parse(source_path.read_text(encoding="utf-8"))
        defined_names = {
            node.name
            for node in syntax_tree.body
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertNotIn("EncodedTaskSubmission", defined_names)
        self.assertNotIn("EncodedTaskResult", defined_names)
        self.assertNotIn("TaskCommandCodec", defined_names)

    def test_business_codecs_import_contracts_from_ports(self) -> None:
        for relative_path in (
            "app/modules/report/adapters/task_codec.py",
            "app/modules/weaponry/adapters/task_codec.py",
        ):
            with self.subTest(path=relative_path):
                source = (_PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
                self.assertIn("from app.modules.tasks.ports import", source)
                self.assertNotIn("from app.modules.tasks.adapters import", source)


if __name__ == "__main__":
    unittest.main()
