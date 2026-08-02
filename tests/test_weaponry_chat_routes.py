"""知识谱系独立对话的 Web 合同与路由验收。"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest

from app import create_app
from app.adapters.web import (
    WeaponryChatRequestValidationError,
    parse_weaponry_chat_history_query,
    parse_weaponry_chat_post,
)
from app.modules.chat.ports import ChatSourceEvidence
from app.modules.chat.domain.identity import WeaponryChatIdentity
from tests.test_chat import _build_test_services


def _sse_events(raw_body: str) -> list[tuple[str, dict]]:
    """将路由返回的 SSE 拆为可精确断言的事件序列。"""
    events: list[tuple[str, dict]] = []
    for block in raw_body.strip().split("\n\n"):
        lines = block.splitlines()
        if len(lines) != 2:
            raise AssertionError(f"invalid SSE block: {block!r}")
        events.append(
            (
                lines[0].removeprefix("event: "),
                json.loads(lines[1].removeprefix("data: ")),
            )
        )
    return events


class WeaponryChatParserTests(unittest.TestCase):
    def test_post_normalizes_architecture_string_but_requires_numeric_user_id(
        self,
    ) -> None:
        request_model = parse_weaponry_chat_post(
            {
                "businessType": "weaponryChat",
                "params": {
                    "userId": 7,
                    "architectureId": "0009",
                    "message": "  问题  ",
                },
            },
            require_message=True,
        )
        self.assertEqual(7, request_model.identity.user_id)
        self.assertEqual(9, request_model.identity.architecture_id)
        self.assertEqual("问题", request_model.message)

        with self.assertRaisesRegex(
            WeaponryChatRequestValidationError,
            "userId必须为",
        ):
            parse_weaponry_chat_post(
                {
                    "businessType": "weaponryChat",
                    "params": {
                        "userId": "7",
                        "architectureId": 9,
                        "message": "问题",
                    },
                },
                require_message=True,
            )

    def test_post_rejects_non_object_unknown_and_legacy_fields(self) -> None:
        cases = (
            ([], "请求体必须为JSON对象"),
            (
                {"businessType": "weaponryChat", "params": {}, "extra": 1},
                "请求包含未知字段",
            ),
            (
                {
                    "businessType": "weaponryChat",
                    "params": {
                        "userId": 1,
                        "architectureId": 2,
                        "message": "x",
                        "chatId": 3,
                    },
                },
                "请求包含未知字段",
            ),
        )
        for payload, error in cases:
            with self.subTest(error=error):
                with self.assertRaisesRegex(
                    WeaponryChatRequestValidationError,
                    error,
                ):
                    parse_weaponry_chat_post(payload, require_message=True)

    def test_history_accepts_leading_zero_and_rejects_duplicate_or_unknown(self) -> None:
        identity = parse_weaponry_chat_history_query(
            (("userId", "0007"), ("architectureId", "0009"))
        )
        self.assertEqual((7, 9), (identity.user_id, identity.architecture_id))

        with self.assertRaisesRegex(
            WeaponryChatRequestValidationError,
            "Query参数不能重复",
        ):
            parse_weaponry_chat_history_query(
                (
                    ("userId", "7"),
                    ("userId", "8"),
                    ("architectureId", "9"),
                )
            )
        with self.assertRaisesRegex(
            WeaponryChatRequestValidationError,
            "请求包含未知字段",
        ):
            parse_weaponry_chat_history_query(
                (("userId", "7"), ("architectureId", "9"), ("x", "1"))
            )


class WeaponryChatRouteAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp = self._tempdir.__enter__()
        self.document_id = "weaponry-doc"
        self.source_key = (
            "docsense_ref:"
            + hashlib.sha256(self.document_id.encode()).hexdigest()[:32]
        )
        self.source_content = "  原文首行\n原文尾行  "
        self.services = _build_test_services(
            self.tmp,
            stream_sources=(
                ChatSourceEvidence(
                    content=self.source_content,
                    structured_source_key=self.source_key,
                ),
            ),
        )
        self.services.kb_service.save_document_record(
            "internal.pdf",
            9,
            self.document_id,
            f"custom-documents/{self.document_id}.json",
            original_name="原始文件.pdf",
            ingested_file_name="internal.pdf",
            metadata={"docSource": self.source_key},
        )
        self.app = create_app(services=self.services)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    @staticmethod
    def _post_payload(*, message: str | None = None) -> dict:
        params: dict = {"userId": 7, "architectureId": "0009"}
        if message is not None:
            params["message"] = message
        return {"businessType": "weaponryChat", "params": params}

    def test_send_history_title_abort_delete_and_recreate_contract(self) -> None:
        send = self.client.post(
            "/llm/weaponry-chat",
            json=self._post_payload(message="请回答"),
        )
        self.assertEqual(200, send.status_code)
        self.assertTrue(send.content_type.startswith("text/event-stream"))
        self.assertEqual("no-cache", send.headers["Cache-Control"])
        self.assertEqual("no", send.headers["X-Accel-Buffering"])
        events = _sse_events(send.get_data(as_text=True))
        self.assertEqual(
            ["chatInfo", "textChunk", "textChunk", "sourceChunks", "done"],
            [event_type for event_type, _ in events],
        )
        self.assertEqual(
            {"userId": 7, "architectureId": 9, "isNewChat": True},
            events[0][1],
        )
        expected_chunks = [
            {
                "content": self.source_content,
                "fileName": "internal.pdf",
                "originalFileName": "原始文件.pdf",
            }
        ]
        self.assertEqual(
            {"userId": 7, "architectureId": 9, "chunks": expected_chunks},
            events[-2][1],
        )
        self.assertEqual(
            {"userId": 7, "architectureId": 9},
            events[-1][1],
        )

        history = self.client.get(
            "/llm/weaponry-chat/history?userId=0007&architectureId=0009"
        )
        self.assertEqual(200, history.status_code)
        messages = history.get_json()
        self.assertIsInstance(messages, list)
        self.assertEqual(2, len(messages))
        self.assertNotIn("chunks", messages[0])
        self.assertEqual(expected_chunks, messages[1]["chunks"])

        title = self.client.post(
            "/llm/weaponry-chat/title",
            json=self._post_payload(),
        )
        self.assertEqual(200, title.status_code)
        self.assertEqual(7, title.get_json()["userId"])
        self.assertEqual(9, title.get_json()["architectureId"])

        abort = self.client.post(
            "/llm/weaponry-chat/abort",
            json=self._post_payload(),
        )
        self.assertEqual(
            {
                "userId": 7,
                "architectureId": 9,
                "aborted": False,
                "msg": "当前无进行中的流式响应",
            },
            abort.get_json(),
        )

        deleted = self.client.post(
            "/llm/weaponry-chat/delete",
            json=self._post_payload(),
        )
        self.assertEqual(200, deleted.status_code)
        self.assertEqual(
            {
                "userId": 7,
                "architectureId": 9,
                "deleted": True,
                "msg": "对话已删除",
            },
            deleted.get_json(),
        )
        repeated = self.client.post(
            "/llm/weaponry-chat/delete",
            json=self._post_payload(),
        )
        self.assertEqual(404, repeated.status_code)
        self.assertEqual({"error": "对话不存在"}, repeated.get_json())

        recreated = self.client.post(
            "/llm/weaponry-chat",
            json=self._post_payload(message="重建后的问题"),
        )
        recreated_events = _sse_events(recreated.get_data(as_text=True))
        self.assertTrue(recreated_events[0][1]["isNewChat"])

    def test_route_rejects_unknown_fields_duplicate_query_and_old_mode(self) -> None:
        unknown = self._post_payload(message="x")
        unknown["params"]["fileNames"] = []
        response = self.client.post("/llm/weaponry-chat", json=unknown)
        self.assertEqual(400, response.status_code)
        self.assertEqual({"error": "请求包含未知字段"}, response.get_json())

        duplicate = self.client.get(
            "/llm/weaponry-chat/history?userId=7&userId=8&architectureId=9"
        )
        self.assertEqual(400, duplicate.status_code)
        self.assertEqual({"error": "Query参数不能重复"}, duplicate.get_json())

        old_mode = self.client.post(
            "/llm/chat",
            json={
                "businessType": "chat",
                "params": {
                    "chatId": 99,
                    "architectureId": 9,
                    "message": "x",
                },
            },
        )
        self.assertEqual(400, old_mode.status_code)

    def test_application_logs_do_not_expose_weaponry_request_or_source_data(
        self,
    ) -> None:
        """应用日志不得泄漏业务用户、问答正文、文件身份、来源键、URL 或远端引用。"""

        secret_user_id = 9_007_199_254_740_991
        secret_message = "phase8-private-message-body"
        secret_chunk = " phase8-private-source\r\nΩ "
        secret_file_name = "phase8-private-file.pdf"
        secret_original_name = "阶段八私密原名.pdf"
        secret_source_key = (
            "docsense_ref:"
            + hashlib.sha256(b"phase8-private-document").hexdigest()[:32]
        )
        secret_url = "https://private.example.invalid/source/document.json"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as private_tmp:
            services = _build_test_services(
                private_tmp,
                stream_sources=(
                    ChatSourceEvidence(
                        content=secret_chunk,
                        structured_source_key=secret_source_key,
                    ),
                ),
            )
            services.kb_service.save_document_record(
                secret_file_name,
                88,
                "phase8-private-document",
                secret_url,
                original_name=secret_original_name,
                ingested_file_name=secret_file_name,
                metadata={"docSource": secret_source_key},
            )
            client = create_app(services=services).test_client()

            with self.assertLogs("app", level="INFO") as captured:
                response = client.post(
                    "/llm/weaponry-chat",
                    json={
                        "businessType": "weaponryChat",
                        "params": {
                            "userId": secret_user_id,
                            "architectureId": 88,
                            "message": secret_message,
                        },
                    },
                )
                self.assertEqual(200, response.status_code)
                response.get_data()

        combined_logs = "\n".join(captured.output)
        forbidden_values = (
            str(secret_user_id),
            secret_message,
            secret_chunk,
            secret_file_name,
            secret_original_name,
            secret_source_key,
            secret_url,
            "context:1",
            "conversation:1",
            "document:phase8-private-document",
        )
        for forbidden_value in forbidden_values:
            with self.subTest(forbidden_value=forbidden_value):
                self.assertNotIn(forbidden_value, combined_logs)

    def test_unbounded_source_and_history_projection_preserves_controlled_sample(
        self,
    ) -> None:
        """在不建立业务上限的前提下，完整保留受控的大来源集合及历史响应。"""

        source_count = 64
        content_chars = 4_096
        architecture_id = 91
        source_records: list[tuple[str, str, str, str]] = []
        stream_sources: list[ChatSourceEvidence] = []
        for index in range(source_count):
            document_id = f"phase8-capacity-{index:03d}"
            source_key = (
                "docsense_ref:"
                + hashlib.sha256(document_id.encode()).hexdigest()[:32]
            )
            content = f" {index:03d}-" + ("Ω" * content_chars) + "\r\n "
            file_name = f"phase8-capacity-{index:03d}.pdf"
            original_name = f"阶段八容量样例-{index:03d}.pdf"
            source_records.append(
                (document_id, source_key, file_name, original_name)
            )
            stream_sources.append(
                ChatSourceEvidence(
                    content=content,
                    structured_source_key=source_key,
                )
            )

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as capacity_tmp:
            services = _build_test_services(
                capacity_tmp,
                max_files_per_request=source_count,
                stream_sources=tuple(stream_sources),
            )
            for document_id, source_key, file_name, original_name in source_records:
                services.kb_service.save_document_record(
                    file_name,
                    architecture_id,
                    document_id,
                    f"custom-documents/{document_id}.json",
                    original_name=original_name,
                    ingested_file_name=file_name,
                    metadata={"docSource": source_key},
                )
            client = create_app(services=services).test_client()
            response = client.post(
                "/llm/weaponry-chat",
                json={
                    "businessType": "weaponryChat",
                    "params": {
                        "userId": 64,
                        "architectureId": architecture_id,
                        "message": "验证受控的大来源集合",
                    },
                },
            )
            self.assertEqual(200, response.status_code)
            events = _sse_events(response.get_data(as_text=True))
            source_payload = next(
                payload
                for event_type, payload in events
                if event_type == "sourceChunks"
            )
            history = client.get(
                "/llm/weaponry-chat/history?userId=64&architectureId=91"
            )

        self.assertEqual(source_count, len(source_payload["chunks"]))
        self.assertEqual(stream_sources[0].content, source_payload["chunks"][0]["content"])
        self.assertEqual(stream_sources[-1].content, source_payload["chunks"][-1]["content"])
        self.assertEqual(200, history.status_code)
        history_payload = history.get_json()
        self.assertEqual(source_payload["chunks"], history_payload[1]["chunks"])
        self.assertGreater(len(history.data), source_count * content_chars)

    def test_title_lease_blocks_title_send_and_delete_with_frozen_errors(self) -> None:
        first = self.client.post(
            "/llm/weaponry-chat",
            json=self._post_payload(message="初始问题"),
        )
        self.assertEqual(200, first.status_code)
        first.get_data()
        resolution = self.services.chat_store.identities.resolve_active(
            WeaponryChatIdentity(user_id=7, architecture_id=9)
        )
        self.assertIsNotNone(resolution)
        assert resolution is not None
        lease_id = (
            f"chat:{resolution.conversation_id}:temporary_thread:test-held"
        )
        self.services.chat_store.resource_leases.begin(
            lease_id=lease_id,
            conversation_id=resolution.conversation_id,
            resource_type="thread",
            require_active_session=True,
            require_exclusive_title=True,
        )
        try:
            title = self.client.post(
                "/llm/weaponry-chat/title",
                json=self._post_payload(),
            )
            self.assertEqual(409, title.status_code)
            self.assertEqual(
                {"error": "当前对话暂不可用于标题生成"},
                title.get_json(),
            )

            send = self.client.post(
                "/llm/weaponry-chat",
                json=self._post_payload(message="被标题占用阻止"),
            )
            self.assertEqual(409, send.status_code)
            self.assertEqual({"error": "当前对话暂不可用"}, send.get_json())

            deleted = self.client.post(
                "/llm/weaponry-chat/delete",
                json=self._post_payload(),
            )
            self.assertEqual(409, deleted.status_code)
            self.assertEqual(
                {
                    "userId": 7,
                    "architectureId": 9,
                    "deleted": False,
                    "error": "当前对话正在生成标题，请稍后重试",
                },
                deleted.get_json(),
            )
        finally:
            self.services.chat_store.resource_leases.mark_closed(lease_id)


if __name__ == "__main__":
    unittest.main()
