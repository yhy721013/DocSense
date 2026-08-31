"""Debug Query 的框架无关行为、失败收敛与并发隔离测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import unittest

from app.modules.debug.application import (
    CallbackRecord,
    ChatAvailableFile,
    ChatDebugSession,
    LoadCallbackPreview,
    LoadChatDebugBootstrap,
)
from app.modules.debug.ports import CallbackRecordText, ChatDebugSnapshot


_RECORD = CallbackRecord(
    record_id="latest.json",
    file_name="latest.json",
    modified_at="2026-08-01T12:00:00",
    size_bytes=12,
)


class _CallbackHistoryFake:
    def __init__(self, *, text: str | None = "{}", error_kind: str | None = None):
        self.text = text
        self.error_kind = error_kind

    def list_records(self, *, limit: int):
        if limit != 50:
            raise AssertionError("Query 必须使用冻结的 50 条上限")
        return (_RECORD,)

    def find_record(self, record_id: str):
        return _RECORD if record_id == _RECORD.record_id else None

    def read_record(self, record_id: str):
        if record_id != _RECORD.record_id:
            raise AssertionError("不得读取未选中的记录")
        return CallbackRecordText(text=self.text, error_kind=self.error_kind)


class _ChatSnapshotFake:
    def read_snapshot(self):
        return ChatDebugSnapshot(
            sessions=(
                ChatDebugSession(
                    chat_id=10001,
                    file_names=("alpha.pdf",),
                    created_at="created",
                    updated_at="updated",
                ),
            ),
            available_files=(ChatAvailableFile("alpha.pdf", 7),),
            active_scope_member_count=1,
            workspace_binding_count=2,
        )


class _FailingChatSnapshotFake:
    def read_snapshot(self):
        raise RuntimeError("boom")


class DebugApplicationTests(unittest.TestCase):
    def test_callback_query_converges_all_content_states(self) -> None:
        cases = (
            ("{invalid", None, "回调文件不是合法 JSON"),
            ("[]", None, "回调文件根节点必须为对象"),
            (None, "io", "回调文件读取失败"),
            (None, "encoding", "回调文件读取失败"),
        )
        for text, error_kind, expected_message in cases:
            with self.subTest(expected_message=expected_message):
                result = LoadCallbackPreview(
                    _CallbackHistoryFake(text=text, error_kind=error_kind)
                ).execute()
                self.assertFalse(result.ok)
                self.assertEqual(expected_message, result.message)
                self.assertEqual(_RECORD, result.selected_record)

    def test_callback_query_rejects_missing_requested_record(self) -> None:
        result = LoadCallbackPreview(_CallbackHistoryFake()).execute(
            record="../latest.json"
        )
        self.assertFalse(result.ok)
        self.assertEqual("指定的回调历史记录不存在", result.message)
        self.assertIsNone(result.selected_record)

    def test_callback_payload_is_deeply_immutable_inside_application(self) -> None:
        """冻结 dataclass 不能只冻结第一层，否则嵌套 dict/list 仍会跨请求污染。"""

        result = LoadCallbackPreview(
            _CallbackHistoryFake(text='{"nested": {"values": [1, 2]}}')
        ).execute()
        self.assertTrue(result.ok)
        self.assertIsNotNone(result.payload)
        nested = result.payload["nested"]  # type: ignore[index]
        values = nested["values"]
        self.assertEqual((1, 2), values)
        with self.assertRaises(TypeError):
            result.payload["new"] = "forbidden"  # type: ignore[index]
        with self.assertRaises(AttributeError):
            values.append(3)

    def test_chat_query_converges_failure_to_stable_empty_result(self) -> None:
        result = LoadChatDebugBootstrap(_FailingChatSnapshotFake()).execute()
        self.assertFalse(result.ok)
        self.assertEqual((), result.sessions)
        self.assertEqual((), result.available_files)
        self.assertEqual("读取失败: boom", result.message)

    def test_fifty_concurrent_queries_do_not_share_mutable_results(self) -> None:
        """同一无状态 Query 可并发复用，结果只含不可变 tuple，不跨请求污染。"""

        query = LoadChatDebugBootstrap(_ChatSnapshotFake())
        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(lambda _: query.execute(), range(50)))

        self.assertEqual(50, len(results))
        self.assertTrue(all(result.ok for result in results))
        self.assertTrue(all(isinstance(result.sessions, tuple) for result in results))
        self.assertEqual({("alpha.pdf",)}, {result.sessions[0].file_names for result in results})


if __name__ == "__main__":
    unittest.main()
