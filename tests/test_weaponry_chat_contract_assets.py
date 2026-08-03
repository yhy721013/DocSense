"""知识谱系独立对话目标合同与 AnythingLLM v1.15.0 Fixture 门禁。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from app.integrations.anythingllm.threads import AnythingLLMThreadClient
from app.integrations.anythingllm.transport import SSEEvent
from app.integrations.anythingllm.models import (
    AnythingLLMFinalization,
    AnythingLLMTextDelta,
)


_TESTS_DIR = Path(__file__).resolve().parent
_CONTRACT_PATH = _TESTS_DIR / "contracts" / "weaponry_chat_contract.json"
_STREAM_FIXTURE_PATH = (
    _TESTS_DIR / "contracts" / "anythingllm_v1_15_stream.jsonl"
)
_REFERENCE_BASELINE_PATH = (
    _TESTS_DIR / "contracts" / "chat_module_reference_baseline.json"
)
_REPOSITORY_ROOT = _TESTS_DIR.parent
_INTERFACE_DOC_PATH = (
    _REPOSITORY_ROOT / "docs" / "接口文档" / "知识谱系类别文件对话.md"
)
_ROUTE_SOURCE_PATH = _REPOSITORY_ROOT / "app" / "blueprints" / "llm.py"


class WeaponryChatContractAssetTests(unittest.TestCase):
    """验证目标公开合同自洽，并保留实施前的显式失败门禁。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))
        cls.fixture_rows = [
            json.loads(line)
            for line in _STREAM_FIXTURE_PATH.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        cls.reference_baseline = json.loads(
            _REFERENCE_BASELINE_PATH.read_text(encoding="utf-8")
        )

    def test_contract_has_exact_top_level_schema_and_implementation_gate(self) -> None:
        """合同资产必须标记五个独立路由已切换实现。"""
        self.assertEqual(2, self.contract["schemaVersion"])
        self.assertEqual(
            "implemented",
            self.contract["implementationGate"],
        )
        self.assertEqual(
            {
                "schemaVersion",
                "implementationGate",
                "identity",
                "routes",
                "businessType",
                "strictRequestFields",
                "successSseHeaders",
                "sse",
                "sourceChunk",
                "history",
                "scope",
                "lifecycleStates",
                "completion",
                "deletion",
                "errors",
                "forbiddenPublicFields",
            },
            set(self.contract),
        )

    def test_five_routes_and_request_field_sets_are_exact(self) -> None:
        """五接口必须各自冻结方法、路径和唯一允许字段集合。"""
        routes = {item["id"]: item for item in self.contract["routes"]}
        self.assertEqual(
            {"send", "title", "history", "abort", "delete"},
            set(routes),
        )
        self.assertEqual("POST", routes["send"]["method"])
        self.assertEqual("/llm/weaponry-chat", routes["send"]["path"])
        self.assertEqual(
            ["userId", "architectureId", "message"],
            routes["send"]["paramsFields"],
        )
        for route_id in ("title", "abort", "delete"):
            with self.subTest(route_id=route_id):
                self.assertEqual(
                    ["userId", "architectureId"],
                    routes[route_id]["paramsFields"],
                )
        self.assertEqual("GET", routes["history"]["method"])
        self.assertTrue(routes["history"]["rejectDuplicateQueryFields"])
        self.assertTrue(self.contract["strictRequestFields"])

    def test_identity_headers_and_sse_order_are_frozen(self) -> None:
        """复合身份、成功 Header 与来源事件顺序都属于公开合同。"""
        self.assertEqual(
            ["userId", "architectureId"],
            self.contract["identity"]["fields"],
        )
        self.assertTrue(self.contract["identity"]["getAllowsLeadingZero"])
        self.assertEqual(
            {
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
            self.contract["successSseHeaders"],
        )
        self.assertEqual(
            ["chatInfo", "textChunk*", "sourceChunks", "done"],
            self.contract["sse"]["successOrder"],
        )
        self.assertEqual(1, self.contract["sse"]["sourceChunksOnSuccessCount"])

    def test_chunk_history_and_delete_rules_are_unambiguous(self) -> None:
        """Chunk 窄清洗、裸数组历史和删除留痕规则不得由实现层重新解释。"""
        chunk = self.contract["sourceChunk"]
        self.assertEqual(
            ["content", "fileName", "originalFileName"],
            chunk["fields"],
        )
        self.assertTrue(chunk["contentRequiresJsonString"])
        self.assertTrue(chunk["contentRemovesLeadingDocumentMetadata"])
        self.assertTrue(chunk["contentPreservesRemainingStringValue"])
        self.assertEqual(
            "reject_run",
            chunk["malformedLeadingDocumentMetadataPolicy"],
        )
        self.assertNotIn("contentPreservesExactStringValue", chunk)
        self.assertEqual("reject_entire_scope", chunk["originalFileNameMissingPolicy"])
        self.assertFalse(chunk["originalFileNameFallback"])
        self.assertIsNone(chunk["resourceLimit"])
        self.assertEqual("bare_array", self.contract["history"]["envelope"])
        self.assertIsNone(self.contract["history"]["resourceLimit"])
        self.assertTrue(self.contract["deletion"]["physicalMessageDelete"])
        self.assertTrue(self.contract["deletion"]["physicalSourceChunkDelete"])
        self.assertFalse(self.contract["deletion"]["releasesFileChatIdentity"])

    def test_lifecycle_matrix_and_error_ids_are_complete(self) -> None:
        """生命周期矩阵和关键严格解析错误必须具有唯一标识。"""
        self.assertEqual(
            {
                "missing_or_deleted",
                "active_without_history",
                "active_idle",
                "run_active",
                "title_active",
                "deleting",
                "cleanup_failed",
            },
            set(self.contract["lifecycleStates"]),
        )
        errors = {item["id"]: item for item in self.contract["errors"]}
        self.assertEqual(len(errors), len(self.contract["errors"]))
        self.assertEqual(400, errors["unknown_field"]["status"])
        self.assertEqual("Query参数不能重复", errors["duplicate_query"]["text"])

    def test_fixture_records_normal_finalization_and_query_short_circuit(self) -> None:
        """脱敏 Fixture 必须同时覆盖正常来源终态和 Query 空来源短路。"""
        normal = [
            row for row in self.fixture_rows if row["scenario"] == "normal_sources"
        ]
        query = [
            row for row in self.fixture_rows if row["scenario"] == "query_no_context"
        ]
        self.assertEqual([1, 2, 3], [row["sequence"] for row in normal])
        self.assertEqual("textResponseChunk", normal[1]["event"]["type"])
        self.assertTrue(normal[1]["event"]["close"])
        self.assertEqual("finalizeResponseStream", normal[2]["event"]["type"])
        self.assertEqual(4, len(normal[2]["event"]["sources"]))
        self.assertEqual(1, len(query))
        self.assertEqual("textResponse", query[0]["event"]["type"])
        self.assertEqual([], query[0]["event"]["sources"])

    def test_reference_baseline_counts_are_internally_consistent(self) -> None:
        """机械迁移前引用快照必须完整分组，且同组内不能重复路径。"""
        references = self.reference_baseline["servicesChatReferences"]
        internal = references["internalModuleFiles"]
        tests = references["testFiles"]
        external = references["externalApplicationFiles"]
        self.assertEqual(references["internalModuleFileCount"], len(internal))
        self.assertEqual(references["testFileCount"], len(tests))
        self.assertEqual(references["externalApplicationFileCount"], len(external))
        self.assertEqual(
            references["totalFileCount"],
            len(internal) + len(tests) + len(external),
        )
        self.assertEqual(len(internal), len(set(internal)))
        self.assertEqual(len(tests), len(set(tests)))
        self.assertEqual(len(external), len(set(external)))

    def test_stage_one_migration_snapshot_has_no_legacy_python_imports(self) -> None:
        """阶段 1 完成后，生产代码和测试不得再依赖三个旧 Chat 命名空间。"""
        post_migration = self.reference_baseline["postMigration"]
        self.assertEqual(0, post_migration["legacyServicesChatReferenceCount"])
        self.assertEqual(0, post_migration["legacyGlobalChatPortReferenceCount"])
        self.assertEqual(
            0,
            post_migration["legacyAnythingllmChatAdapterReferenceCount"],
        )

        forbidden_imports = (
            ".".join(("app", "services", "chat")),
            ".".join(("app", "ports", "chat")),
            ".".join(("app", "integrations", "anythingllm", "chat_gateway")),
            ".".join(("app", "integrations", "anythingllm", "chat_factory")),
        )
        violations: list[str] = []
        for source_root in (_REPOSITORY_ROOT / "app", _REPOSITORY_ROOT / "tests"):
            for path in source_root.rglob("*.py"):
                source = path.read_text(encoding="utf-8")
                for forbidden in forbidden_imports:
                    if forbidden in source:
                        violations.append(
                            f"{path.relative_to(_REPOSITORY_ROOT)}:{forbidden}"
                        )
        self.assertEqual([], violations)

    def test_parser_continues_after_text_close_until_finalization(self) -> None:
        """修复后必须越过文本 close，继续读取完整来源终态。"""
        normal_events = [
            SSEEvent(data=json.dumps(row["event"], ensure_ascii=False))
            for row in self.fixture_rows
            if row["scenario"] == "normal_sources"
        ]
        transport = MagicMock()
        # 使用生成器而不是 list_iterator，保持与 Transport 返回值一致的可关闭协议。
        transport.stream_sse.return_value = (event for event in normal_events)
        client = AnythingLLMThreadClient(transport)

        chunks = list(
            client.stream(
                "workspace-redacted",
                "thread-redacted",
                "问题",
                mode="query",
            )
        )

        self.assertEqual(2, len(chunks))
        self.assertIsInstance(chunks[0], AnythingLLMTextDelta)
        self.assertEqual("第一段", chunks[0].content)
        self.assertIsInstance(chunks[1], AnythingLLMFinalization)
        self.assertEqual(4, len(chunks[1].sources))

    def test_target_routes_are_implemented(self) -> None:
        """阶段 6 后五个目标路由必须存在于生产 Blueprint。"""
        route_source = _ROUTE_SOURCE_PATH.read_text(encoding="utf-8")
        for suffix in ("", "/title", "/history", "/abort", "/delete"):
            with self.subTest(suffix=suffix):
                self.assertIn(
                    f'"/llm/weaponry-chat{suffix}"',
                    route_source,
                )

    def test_authoritative_document_matches_frozen_headers_and_strict_name_rule(
        self,
    ) -> None:
        """权威文档必须持续包含本轮最后确认的两个公开决定。"""
        document = _INTERFACE_DOC_PATH.read_text(encoding="utf-8")
        self.assertIn("`Cache-Control` | `no-cache`", document)
        self.assertIn("`X-Accel-Buffering` | `no`", document)
        self.assertIn("也不回退为 `fileName`", document)
        self.assertIn("删除开头完整的供应商 Metadata 包装", document)


if __name__ == "__main__":
    unittest.main()
