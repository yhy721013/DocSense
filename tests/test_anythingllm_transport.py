"""AnythingLLM HTTP 传输层的离线单元测试。

本测试模块只使用 ``MagicMock`` 模拟 ``requests.Session`` 和响应，不访问网络，也不要求
AnythingLLM、数据库服务或项目主进程处于运行状态。测试重点覆盖传输层的稳定契约：
请求参数规范化、异常分类、敏感信息脱敏、SSE 分帧以及所有成功和失败路径上的资源释放。
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import MagicMock

import requests

from app.integrations.anythingllm.errors import (
    AnythingLLMConnectionError,
    AnythingLLMHTTPError,
    AnythingLLMProtocolError,
    AnythingLLMTimeoutError,
    AnythingLLMTransportClosedError,
)
from app.integrations.anythingllm.transport import AnythingLLMTransport, SSEEvent


def _response(
    *,
    status_code: int = 200,
    json_value: Any = None,
    text: str = "",
    json_error: Exception | None = None,
) -> MagicMock:
    """构造只包含传输层测试所需行为的响应替身。

    参数:
        status_code: 模拟的 HTTP 状态码。
        json_value: ``response.json()`` 正常返回的对象。
        text: 用于空正文判断、协议错误和安全摘要测试的文本正文。
        json_error: 非空时配置为 ``response.json()`` 抛出的异常。

    返回:
        可记录 ``json``、``iter_lines`` 和 ``close`` 调用情况的 ``MagicMock``。
    """
    response = MagicMock()
    response.status_code = status_code
    response.text = text
    if json_error is not None:
        response.json.side_effect = json_error
    else:
        response.json.return_value = json_value
    return response


class AnythingLLMTransportTests(unittest.TestCase):
    """验证传输层公开契约及关键失败路径。

    每个测试使用独立的会话替身和传输对象，避免调用记录、关闭状态或响应副作用在测试
    之间传播。真实网络行为由 ``requests`` 自身保证，本模块只验证项目封装逻辑。
    """

    def setUp(self) -> None:
        """为每个用例创建独立会话替身和标准配置传输对象。"""
        self.session = MagicMock()
        self.transport = AnythingLLMTransport(
            base_url="http://anythingllm.local/api/v1/",
            api_key="super-secret-key",
            timeout=30,
            session=self.session,
        )

    def tearDown(self) -> None:
        """幂等关闭传输对象，确保失败用例也不会遗留会话资源。"""
        self.transport.close()

    def test_get_json_returns_decoded_value_and_sets_common_headers(self) -> None:
        """GET 应返回解码结果、注入公共请求头并在返回前关闭响应。"""
        response = _response(json_value={"ok": True})
        self.session.request.return_value = response

        result = self.transport.get_json("/health", user_id=7, params={"full": "1"})

        self.assertEqual(result, {"ok": True})
        self.assertIsNot(result, response)
        self.session.request.assert_called_once_with(
            "GET",
            "http://anythingllm.local/api/v1/health",
            timeout=30,
            headers={
                "Accept": "application/json",
                "Authorization": "Bearer super-secret-key",
                "Content-Type": "application/json",
                "X-AnythingLLM-User-Id": "7",
            },
            params={"full": "1"},
        )
        response.close.assert_called_once_with()

    def test_post_and_delete_json_send_payloads(self) -> None:
        """POST 与 DELETE 应使用正确方法和 URL，并原样传递 JSON 请求体。"""
        post_response = _response(json_value={"created": True})
        delete_response = _response(json_value={"deleted": True})
        self.session.request.side_effect = [post_response, delete_response]

        self.assertEqual(
            self.transport.post_json("items", {"name": "one"}),
            {"created": True},
        )
        self.assertEqual(
            self.transport.delete_json("items/1", {"force": True}),
            {"deleted": True},
        )

        post_call, delete_call = self.session.request.call_args_list
        self.assertEqual(post_call.args[:2], ("POST", "http://anythingllm.local/api/v1/items"))
        self.assertEqual(post_call.kwargs["json"], {"name": "one"})
        self.assertEqual(
            delete_call.args[:2],
            ("DELETE", "http://anythingllm.local/api/v1/items/1"),
        )
        self.assertEqual(delete_call.kwargs["json"], {"force": True})

    def test_delete_json_can_explicitly_accept_empty_success_body(self) -> None:
        """显式允许空正文时，204 响应应返回 None 且不得尝试 JSON 解码。"""
        response = _response(json_error=ValueError("empty"), text="")
        response.status_code = 204
        self.session.request.return_value = response

        result = self.transport.delete_json("items/1", allow_empty=True)

        self.assertIsNone(result)
        response.json.assert_not_called()
        response.close.assert_called_once_with()

    def test_successful_non_json_response_is_protocol_error(self) -> None:
        """2xx 只代表 HTTP 成功；无效 JSON 必须转换为带摘要的协议异常。"""
        response = _response(
            status_code=200,
            text="upstream returned html",
            json_error=ValueError("not json"),
        )
        self.session.request.return_value = response

        with self.assertRaises(AnythingLLMProtocolError) as caught:
            self.transport.get_json("health")

        self.assertEqual(caught.exception.status_code, 200)
        self.assertEqual(caught.exception.response_summary, "upstream returned html")
        response.close.assert_called_once_with()

    def test_http_errors_have_stable_status_and_sanitized_bounded_summary(
        self,
    ) -> None:
        """典型错误状态必须保留状态码，同时脱敏并限制响应摘要长度。"""
        for status_code in (400, 404, 500):
            with self.subTest(status_code=status_code):
                session = MagicMock()
                response = _response(
                    status_code=status_code,
                    text=(
                        'Authorization: Bearer another-secret '
                        'api_key=super-secret-key '
                        + ("x" * 800)
                    ),
                )
                session.request.return_value = response
                transport = AnythingLLMTransport(
                    base_url="http://anythingllm.local/api/v1",
                    api_key="super-secret-key",
                    timeout=10,
                    session=session,
                    max_error_body_chars=128,
                )
                try:
                    with self.assertRaises(AnythingLLMHTTPError) as caught:
                        transport.get_json("failure?token=query-secret")
                finally:
                    transport.close()

                error = caught.exception
                rendered = str(error)
                self.assertEqual(error.status_code, status_code)
                self.assertNotIn("super-secret-key", rendered)
                self.assertNotIn("another-secret", rendered)
                self.assertNotIn("query-secret", rendered)
                self.assertIn("<redacted>", rendered)
                self.assertLessEqual(len(error.response_summary), 142)
                response.close.assert_called_once_with()

    def test_timeout_is_translated_without_leaking_original_exception(self) -> None:
        """requests 超时应映射为稳定异常，公开消息不得包含原始密钥。"""
        self.session.request.side_effect = requests.Timeout(
            "Bearer super-secret-key should not escape"
        )

        with self.assertRaises(AnythingLLMTimeoutError) as caught:
            self.transport.get_json("slow")

        self.assertEqual(caught.exception.code, "timeout")
        self.assertNotIn("super-secret-key", str(caught.exception))

    def test_request_exception_is_translated_to_connection_error(self) -> None:
        """非超时请求异常应映射为连接异常并保留安全的方法上下文。"""
        self.session.request.side_effect = requests.ConnectionError("connection refused")

        with self.assertRaises(AnythingLLMConnectionError) as caught:
            self.transport.get_json("health")

        self.assertEqual(caught.exception.code, "connection_error")
        self.assertEqual(caught.exception.method, "GET")

    def test_multipart_does_not_override_requests_boundary_content_type(self) -> None:
        """multipart 请求不得手动设置内容类型，以便 requests 正确生成 boundary。"""
        response = _response(json_value={"documents": [{"id": "doc-1"}]})
        self.session.request.return_value = response
        files = {"file": ("demo.txt", b"content")}

        result = self.transport.post_multipart(
            "document/upload",
            files=files,
            data={"folder": "demo"},
            user_id=1,
        )

        self.assertEqual(result["documents"][0]["id"], "doc-1")
        kwargs = self.session.request.call_args.kwargs
        self.assertEqual(kwargs["files"], files)
        self.assertEqual(kwargs["data"], {"folder": "demo"})
        self.assertEqual(kwargs["headers"]["X-AnythingLLM-User-Id"], "1")
        self.assertNotIn("Content-Type", kwargs["headers"])
        response.close.assert_called_once_with()

    def test_sse_parses_frames_and_closes_response(self) -> None:
        """SSE 应合并 data 行、继承事件 ID、忽略注释并关闭完整消费的响应。"""
        response = _response(status_code=200)
        response.iter_lines.return_value = iter(
            [
                ": keepalive",
                "id: event-1",
                "event: textResponseChunk",
                'data: {"text":"你"}',
                'data: {"text":"好"}',
                "retry: 1500",
                "",
                "event: done",
                "data: close",
            ]
        )
        self.session.request.return_value = response

        events = list(
            self.transport.stream_sse(
                "workspace/example/stream",
                {"message": "hello"},
                user_id=9,
            )
        )

        self.assertEqual(
            events,
            [
                SSEEvent(
                    data='{"text":"你"}\n{"text":"好"}',
                    event="textResponseChunk",
                    event_id="event-1",
                    retry=1500,
                ),
                SSEEvent(data="close", event="done", event_id="event-1"),
            ],
        )
        kwargs = self.session.request.call_args.kwargs
        self.assertTrue(kwargs["stream"])
        self.assertEqual(kwargs["headers"]["Accept"], "text/event-stream")
        self.assertEqual(kwargs["headers"]["X-AnythingLLM-User-Id"], "9")
        self.assertEqual(response.encoding, "utf-8")
        response.iter_lines.assert_called_once_with(decode_unicode=True, chunk_size=1)
        response.close.assert_called_once_with()

    def test_closing_sse_generator_early_releases_response(self) -> None:
        """调用方提前关闭 SSE 生成器时也必须立即释放底层流式响应。"""
        response = _response(status_code=200)
        response.iter_lines.return_value = iter(["data: first", "", "data: second"])
        self.session.request.return_value = response

        events = self.transport.stream_sse("events", {"message": "hello"})
        self.assertEqual(next(events), SSEEvent(data="first"))
        events.close()

        response.close.assert_called_once_with()

    def test_sse_timeout_is_translated_and_releases_response(self) -> None:
        """读取 SSE 流超时应转换异常，并通过 finally 关闭已建立的响应。"""
        response = _response(status_code=200)
        response.iter_lines.side_effect = requests.Timeout("stream timeout")
        self.session.request.return_value = response

        with self.assertRaises(AnythingLLMTimeoutError) as caught:
            list(self.transport.stream_sse("events", {"message": "hello"}))

        self.assertEqual(caught.exception.method, "POST")
        response.close.assert_called_once_with()

    def test_close_is_idempotent_and_blocks_future_requests(self) -> None:
        """close 必须幂等，关闭后的请求必须在访问会话前确定性失败。"""
        self.transport.close()
        self.transport.close()

        self.session.close.assert_called_once_with()
        with self.assertRaises(AnythingLLMTransportClosedError):
            self.transport.get_json("health")
        self.session.request.assert_not_called()

    def test_context_manager_closes_session(self) -> None:
        """上下文管理器退出时应关闭其拥有的会话。"""
        session = MagicMock()
        with AnythingLLMTransport(
            base_url="http://anythingllm.local/api/v1",
            api_key="key",
            timeout=None,
            session=session,
        ):
            pass

        session.close.assert_called_once_with()

    def test_absolute_request_path_is_rejected(self) -> None:
        """拒绝绝对请求 URL，防止调用方绕过已配置的 AnythingLLM 主机。"""
        with self.assertRaises(ValueError):
            self.transport.get_json("https://untrusted.example/path")
        self.session.request.assert_not_called()

    def test_parent_directory_request_path_is_rejected(self) -> None:
        """拒绝上级目录片段，防止请求路径逃逸已配置的 API 根目录。"""
        with self.assertRaises(ValueError):
            self.transport.get_json("../admin")
        self.session.request.assert_not_called()

    def test_base_url_must_be_absolute_and_must_not_contain_credentials(
        self,
    ) -> None:
        """根地址必须绝对且结构单一，不允许嵌入凭据、查询参数或片段。"""
        invalid_base_urls = (
            "anythingllm.local/api/v1",
            "http://user:secret@host/api/v1",
            "http://anythingllm.local/api/v1?token=secret",
            "http://anythingllm.local/api/v1#fragment",
        )
        for base_url in invalid_base_urls:
            with self.subTest(base_url=base_url):
                with self.assertRaises(ValueError):
                    AnythingLLMTransport(
                        base_url=base_url,
                        api_key="key",
                        timeout=10,
                        session=MagicMock(),
                    )

    def test_api_key_and_timeout_are_validated_before_creating_session(self) -> None:
        """空密钥和非正超时必须在默认会话创建前快速失败。"""
        invalid_settings = (
            {"api_key": "", "timeout": 10},
            {"api_key": "key", "timeout": 0},
            {"api_key": "key", "timeout": -1},
        )
        for settings in invalid_settings:
            with self.subTest(settings=settings):
                with self.assertRaises(ValueError):
                    AnythingLLMTransport(
                        base_url="http://anythingllm.local/api/v1",
                        **settings,
                    )


if __name__ == "__main__":
    unittest.main()
