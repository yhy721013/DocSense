"""知识谱系类别文件对话的阶段 0 黄金合同资产测试。"""

from __future__ import annotations

import hashlib
import json
import unittest
from pathlib import Path
from typing import Any

from app.services.chat.domain.limits import MAX_CHAT_ARCHITECTURE_ID

_TESTS_DIR = Path(__file__).resolve().parent
_ASSET_PATH = _TESTS_DIR / "contracts" / "chat_architecture_scope_state_machine.json"
_FILES_SCOPE_ASSET_PATH = _TESTS_DIR / "contracts" / "chat_scope_state_machine.json"
_REPOSITORY_ROOT = _TESTS_DIR.parent
_FILES_CHAT_DOC_PATH = _REPOSITORY_ROOT / "docs" / "接口文档" / "文件对话.md"
_ARCHITECTURE_CHAT_DOC_PATH = (
    _REPOSITORY_ROOT / "docs" / "接口文档" / "知识谱系类别文件对话.md"
)


class ChatArchitectureContractAssetTests(unittest.TestCase):
    """冻结已批准且已完成路由切换的 architecture Chat 合同。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(_ASSET_PATH.read_text(encoding="utf-8"))

    def test_asset_is_strict_and_records_completed_route_cutover(self) -> None:
        """黄金资产必须自洽，并准确记录阶段 5 已完成生产路径切换。"""
        self.assertEqual(1, self.contract["schemaVersion"])
        self.assertEqual(
            "route_cutover_completed_at_stage_5",
            self.contract["implementationGate"],
        )
        self.assertEqual(
            {
                "schemaVersion",
                "implementationGate",
                "publicContract",
                "architectureId",
                "scopeRules",
                "selectionModes",
                "errors",
                "cases",
                "history",
            },
            set(self.contract),
        )

    def test_selector_and_public_stream_contract_are_frozen(self) -> None:
        public_contract = self.contract["publicContract"]

        self.assertEqual("exactly_one_by_field_presence", public_contract["selectorMode"])
        self.assertEqual("architectureId", public_contract["architectureRequestField"])
        self.assertEqual("fileNames", public_contract["filesRequestField"])
        self.assertEqual("text/event-stream", public_contract["successContentType"])
        self.assertEqual(["error"], public_contract["errorBodyFields"])
        self.assertEqual(
            ["chatInfo", "textChunk", "done", "error", "aborted"],
            public_contract["sseEventTypes"],
        )

    def test_authoritative_docs_include_aborted_and_safe_integer_boundary(
        self,
    ) -> None:
        """防止运行时黄金资产与两份权威 SSE/ID 文档再次发生漂移。"""
        files_document = _FILES_CHAT_DOC_PATH.read_text(encoding="utf-8")
        architecture_document = _ARCHITECTURE_CHAT_DOC_PATH.read_text(
            encoding="utf-8"
        )

        for document in (files_document, architecture_document):
            with self.subTest(document_chars=len(document)):
                self.assertIn(
                    "| `aborted` | `{\"chatId\":",
                    document,
                )
                self.assertIn(
                    "`done`、`aborted`",
                    document,
                )
        self.assertIn(
            f"`1..{MAX_CHAT_ARCHITECTURE_ID}`",
            architecture_document,
        )
        self.assertIn(
            "architectureId必须为1到9007199254740991之间的正整数",
            architecture_document,
        )

    def test_existing_file_names_asset_is_byte_frozen(self) -> None:
        """architecture 改造不得顺手改变既有 fileNames 黄金状态机。"""
        digest = hashlib.sha256(_FILES_SCOPE_ASSET_PATH.read_bytes()).hexdigest().upper()
        self.assertEqual(
            self.contract["publicContract"]["fileNamesBaselineSha256"],
            digest,
        )

    def test_architecture_id_rules_match_existing_web_normalization_contract(self) -> None:
        rules = self.contract["architectureId"]

        self.assertEqual(1, rules["minimum"])
        self.assertEqual(MAX_CHAT_ARCHITECTURE_ID, rules["maximum"])
        self.assertEqual(
            ["json_integer", "ascii_decimal_string"],
            rules["acceptedKinds"],
        )
        self.assertTrue(rules["decimalStringAllowsLeadingZero"])
        self.assertTrue(rules["historyAlwaysUsesJsonNumber"])
        self.assertIn(True, rules["rejectedExamples"])
        self.assertIn("１", rules["rejectedExamples"])

    def test_error_matrix_has_unique_ids_and_exact_public_text(self) -> None:
        errors = {item["id"]: item for item in self.contract["errors"]}

        self.assertEqual(len(errors), len(self.contract["errors"]))
        self.assertEqual(
            (400, "architectureId与fileNames不能同时传入"),
            self._status_and_text(errors["selector_conflict"]),
        )
        self.assertEqual(
            (400, "architectureId不能为空"),
            self._status_and_text(errors["architecture_id_present_but_null"]),
        )
        self.assertEqual(
            (400, "fileNames必须为数组"),
            self._status_and_text(errors["both_selectors_absent"]),
        )
        self.assertEqual(
            (404, "architectureId对应类别不存在或没有可用于对话的文件"),
            self._status_and_text(errors["catalog_not_found_or_empty"]),
        )
        self.assertEqual(
            (400, "architectureId对应类别文件无法形成有效对话范围"),
            self._status_and_text(errors["catalog_invalid"]),
        )
        self.assertEqual(
            (400, "fileNames超过文件对话数量上限"),
            self._status_and_text(errors["scope_limit"]),
        )
        self.assertEqual(
            (409, "当前对话的范围模式不匹配"),
            self._status_and_text(errors["scope_mode_conflict"]),
        )
        self.assertEqual(
            (409, "当前对话已绑定其他architectureId"),
            self._status_and_text(errors["architecture_id_conflict"]),
        )

    def test_state_machine_defers_catalog_outcome_for_existing_same_id(self) -> None:
        cases = {item["id"]: item for item in self.contract["cases"]}

        initial = cases["new_resolved_x"]
        self.assertEqual("architecture_initial", initial["expectedSelectionMode"])
        self.assertTrue(initial["createsBinding"])
        self.assertTrue(initial["createsScopeRevision"])

        for case_id in (
            "existing_same_x_resolved_candidate",
            "existing_same_x_empty_candidate",
        ):
            with self.subTest(case_id=case_id):
                case = cases[case_id]
                self.assertEqual(200, case["expectedStatus"])
                self.assertEqual("architecture_reuse", case["expectedSelectionMode"])
                self.assertFalse(case["createsBinding"])
                self.assertFalse(case["createsScopeRevision"])
                self.assertTrue(case["reusesScopeHead"])

    def test_scope_snapshot_and_history_rules_are_unambiguous(self) -> None:
        scope_rules = self.contract["scopeRules"]
        history = self.contract["history"]

        self.assertTrue(scope_rules["directFilesOnly"])
        self.assertFalse(scope_rules["includeDescendants"])
        self.assertFalse(scope_rules["existingSessionRequeriesCatalog"])
        self.assertTrue(scope_rules["workerLoadsScopeByRunId"])
        self.assertEqual(
            ["role", "content", "timestamp", "architectureId"],
            history["architectureUserFields"],
        )
        self.assertEqual(
            ["role", "content", "timestamp", "files"],
            history["fileUserFields"],
        )
        self.assertEqual(
            ["role", "content", "timestamp"],
            history["assistantFields"],
        )

    @staticmethod
    def _status_and_text(error: dict[str, Any]) -> tuple[int, str]:
        return int(error["status"]), str(error["text"])


if __name__ == "__main__":
    unittest.main()
