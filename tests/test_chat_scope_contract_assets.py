"""文件对话 Requested/Active/Effective Scope 的阶段 0 黄金资产测试。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any


_ASSET_PATH = (
    Path(__file__).resolve().parent
    / "contracts"
    / "chat_scope_state_machine.json"
)


class ChatScopeContractAssetTests(unittest.TestCase):
    """冻结已确认状态机，不在阶段 0 提前依赖待实现生产代码。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(_ASSET_PATH.read_text(encoding="utf-8"))

    def test_public_contract_adds_no_request_or_response_fields(self) -> None:
        contract = self.contract["publicContract"]

        self.assertEqual("fileNames", contract["requestField"])
        self.assertEqual("files", contract["historyField"])
        self.assertEqual(["name"], contract["historyItemFields"])
        self.assertEqual([], contract["newRequestFields"])
        self.assertEqual([], contract["newResponseFields"])
        self.assertEqual(
            ["chatInfo", "textChunk", "done", "error", "aborted"],
            contract["sseEventTypes"],
        )

    def test_confirmed_state_machine_cases_match_pure_oracle(self) -> None:
        allowed_modes = set(self.contract["selectionModes"])

        for case in self.contract["cases"]:
            with self.subTest(case_id=case["id"]):
                projected = self._project(case)
                self.assertEqual(
                    case["expectedEffectiveFiles"],
                    projected["effectiveFiles"],
                )
                self.assertEqual(
                    case["expectedHistoryFiles"],
                    projected["historyFiles"],
                )
                self.assertEqual(
                    case["expectedSelectionMode"],
                    projected["selectionMode"],
                )
                self.assertEqual(
                    case["createsScopeRevision"],
                    projected["createsScopeRevision"],
                )
                self.assertIn(projected["selectionMode"], allowed_modes)

    def test_empty_request_never_expands_into_history(self) -> None:
        for case in self.contract["cases"]:
            if case["requestedFiles"]:
                continue
            with self.subTest(case_id=case["id"]):
                self.assertEqual([], case["expectedHistoryFiles"])

    @staticmethod
    def _project(case: dict[str, Any]) -> dict[str, Any]:
        """用最小纯规则表达已确认决策，供后续实现做差分基线。"""
        requested = list(case["requestedFiles"])
        if requested:
            return {
                "effectiveFiles": requested,
                "historyFiles": requested,
                "selectionMode": "explicit",
                "createsScopeRevision": True,
            }
        if not case["sessionExists"]:
            return {
                "effectiveFiles": list(case["initialCatalog"]),
                "historyFiles": [],
                "selectionMode": "automatic_initial",
                "createsScopeRevision": True,
            }
        current_scope = case["currentScope"]
        if not isinstance(current_scope, list):
            raise AssertionError("既有会话必须具备可读取的当前活动范围")
        return {
            "effectiveFiles": list(current_scope),
            "historyFiles": [],
            "selectionMode": "active_scope_reuse",
            "createsScopeRevision": False,
        }


if __name__ == "__main__":
    unittest.main()
