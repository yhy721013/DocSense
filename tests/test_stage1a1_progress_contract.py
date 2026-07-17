"""阶段 1A-1 基线在 1B-2 切换后的 Progress 路由契约测试。

测试使用可控 Fake WebSocket、临时 SQLite 和进程内 Hub，不建立真实网络连接、不启动
``run.py``，也不访问 AnythingLLM、模型服务或外部回调地址。
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from collections import deque
from pathlib import Path
from typing import Any, Iterable

from app import create_app
from tests import workspace_tempdir
from tests.offline_application import build_offline_application_services


_TARGET_CONTRACT_PATH = (
    Path(__file__).with_name("contracts") / "stage0_contracts.json"
)


class _FakeWebSocket:
    """模拟 simple-websocket 的超时 receive、连接状态和单写入 send。"""

    def __init__(
        self,
        raw_messages: Iterable[str | bytes] = (),
        *,
        auto_close: bool,
        fail_send_after: int | None = None,
    ) -> None:
        self._condition = threading.Condition()
        self._raw_messages = deque(raw_messages)
        self._auto_close = auto_close
        self._fail_send_after = fail_send_after
        self.connected = True
        self.sent_messages: list[dict[str, Any]] = []
        self.send_thread_ids: list[int] = []
        self.send_attempts = 0

    def receive(self, timeout: float | None = None) -> str | bytes | None:
        with self._condition:
            if not self._raw_messages and self.connected and not self._auto_close:
                self._condition.wait(timeout=timeout)
            if self._raw_messages:
                return self._raw_messages.popleft()
            if self._auto_close:
                self.connected = False
            return None

    def send(self, raw_message: str) -> None:
        with self._condition:
            attempt = self.send_attempts
            self.send_attempts += 1
            if self._fail_send_after is not None and attempt >= self._fail_send_after:
                raise RuntimeError("fake websocket send failed")
            self.sent_messages.append(json.loads(raw_message))
            self.send_thread_ids.append(threading.get_ident())
            self._condition.notify_all()

    def push(self, payload: dict[str, Any] | str | bytes) -> None:
        raw: str | bytes
        if isinstance(payload, dict):
            raw = json.dumps(payload, ensure_ascii=False)
        else:
            raw = payload
        with self._condition:
            self._raw_messages.append(raw)
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self.connected = False
            self._condition.notify_all()

    def wait_for_messages(self, count: int, *, timeout: float = 3.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while len(self.sent_messages) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            return True


class ProgressRouteContractTests(unittest.TestCase):
    """验证批准后的无 action 协议、顺序、隔离和连接生命周期。"""

    def setUp(self) -> None:
        self._tempdir = workspace_tempdir()
        self.runtime_directory = self._tempdir.__enter__()
        self.services = build_offline_application_services(self.runtime_directory)
        self.task_service = self.services.task_service
        self.progress_hub = self.services.progress_hub
        self.app = create_app(services=self.services)
        registered_view = self.app.view_functions["__flask_sock.llm_progress"]
        self.route_handler = registered_view.__wrapped__
        self._live_connections: list[tuple[_FakeWebSocket, threading.Thread]] = []

    def tearDown(self) -> None:
        for websocket, thread in self._live_connections:
            websocket.close()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "Progress Fake WebSocket 线程未退出")
        self._tempdir.__exit__(None, None, None)

    def _run_websocket(
        self,
        *raw_messages: str | bytes,
        fail_send_after: int | None = None,
    ) -> _FakeWebSocket:
        websocket = _FakeWebSocket(
            raw_messages,
            auto_close=True,
            fail_send_after=fail_send_after,
        )
        with self.app.app_context():
            self.route_handler(websocket)
        return websocket

    def _start_websocket(self) -> tuple[_FakeWebSocket, threading.Thread, list[BaseException]]:
        websocket = _FakeWebSocket(auto_close=False)
        errors: list[BaseException] = []

        def run() -> None:
            try:
                with self.app.app_context():
                    self.route_handler(websocket)
            except BaseException as exc:  # 测试线程必须把异常交回主线程断言。
                errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self._live_connections.append((websocket, thread))
        return websocket, thread, errors

    def test_invalid_json_returns_error_then_connection_accepts_valid_message(self) -> None:
        self.task_service.create_file_task(
            "valid-after-json.pdf",
            {"businessType": "file"},
            status="1",
        )
        valid = json.dumps(
            {
                "businessType": "file",
                "params": [{"fileName": "valid-after-json.pdf"}],
            },
            ensure_ascii=False,
        )

        websocket = self._run_websocket("{not-json", valid)

        self.assertEqual(
            {"type": "error", "message": "订阅消息不是合法JSON"},
            websocket.sent_messages[0],
        )
        self.assertEqual("valid-after-json.pdf", websocket.sent_messages[1]["data"]["fileName"])

    def test_explicit_action_is_rejected_without_ack_and_connection_stays_open(self) -> None:
        self.task_service.create_file_task(
            "after-action.pdf",
            {"businessType": "file"},
            status="1",
        )
        action = json.dumps(
            {
                "action": "query",
                "businessType": "file",
                "params": [{"fileName": "after-action.pdf"}],
            }
        )
        valid = json.dumps(
            {
                "businessType": "file",
                "params": [{"fileName": "after-action.pdf"}],
            }
        )

        websocket = self._run_websocket(action, valid)

        self.assertEqual("error", websocket.sent_messages[0]["type"])
        self.assertIn("action", websocket.sent_messages[0]["message"])
        self.assertEqual("file", websocket.sent_messages[1]["businessType"])
        self.assertTrue(all(item.get("type") != "ack" for item in websocket.sent_messages))

    def test_invalid_report_id_returns_error_then_large_integer_string_subscribes(self) -> None:
        """reportId 转换失败只拒绝当前帧，且不施加 64 位业务范围限制。"""

        report_id = 10**80 + 132
        self.task_service.create_report_task(
            report_id,
            {
                "businessType": "report",
                "params": [{"reportId": report_id}],
            },
        )
        invalid = json.dumps(
            {
                "businessType": "report",
                "params": [{"reportId": "132.0"}],
            }
        )
        valid = json.dumps(
            {
                "businessType": "report",
                "params": [{"reportId": f"+000{report_id}"}],
            }
        )

        websocket = self._run_websocket(invalid, valid)

        self.assertEqual(
            {
                "type": "error",
                "message": "reportId必须是整数或整数字符串",
            },
            websocket.sent_messages[0],
        )
        self.assertEqual("report", websocket.sent_messages[1]["businessType"])
        self.assertEqual(
            report_id,
            websocket.sent_messages[1]["data"]["reportId"],
        )
        self.assertIs(
            int,
            type(websocket.sent_messages[1]["data"]["reportId"]),
        )

    def test_overlong_report_id_returns_error_and_keeps_connection(self) -> None:
        self.task_service.create_report_task(
            132,
            {"businessType": "report", "params": [{"reportId": 132}]},
        )
        overlong = json.dumps(
            {
                "businessType": "report",
                "params": [{"reportId": "9" * 129}],
            }
        )
        valid = json.dumps(
            {
                "businessType": "report",
                "params": [{"reportId": 132}],
            }
        )

        websocket = self._run_websocket(overlong, valid)

        self.assertEqual(
            {
                "type": "error",
                "message": "reportId不能超过128位十进制数字",
            },
            websocket.sent_messages[0],
        )
        self.assertEqual("report", websocket.sent_messages[1]["businessType"])

    def test_mixed_params_rejects_entire_message_without_partial_subscription(self) -> None:
        websocket, _, errors = self._start_websocket()
        websocket.push(
            {
                "businessType": "file",
                "params": [{"fileName": "blocked.pdf"}, None],
            }
        )
        self.assertTrue(websocket.wait_for_messages(1))
        self.assertEqual("error", websocket.sent_messages[0]["type"])

        self.progress_hub.publish(
            "file",
            "blocked.pdf",
            {
                "businessType": "file",
                "data": {"fileName": "blocked.pdf", "progress": 0.5},
            },
        )
        time.sleep(0.25)
        self.assertEqual(1, len(websocket.sent_messages))

        websocket.push(
            {
                "businessType": "file",
                "params": [{"fileName": "blocked.pdf"}],
            }
        )
        self.assertTrue(websocket.wait_for_messages(2))
        self.assertEqual(0.5, websocket.sent_messages[1]["data"]["progress"])
        self.assertEqual([], errors)

    def test_batch_current_snapshots_keep_request_order_and_duplicate_positions(self) -> None:
        for name, progress in (("a.pdf", 0.2), ("b.pdf", 0.4)):
            self.task_service.create_file_task(
                name,
                {"businessType": "file"},
                status="1",
            )
            self.task_service.update_task_progress(
                "file",
                name,
                progress=progress,
                message="处理中",
                status="1",
            )
        raw = json.dumps(
            {
                "businessType": "file",
                "params": [
                    {"fileName": "b.pdf"},
                    {"fileName": "a.pdf"},
                    {"fileName": "b.pdf"},
                ],
            }
        )

        websocket = self._run_websocket(raw)

        self.assertEqual(
            ["b.pdf", "a.pdf", "b.pdf"],
            [item["data"]["fileName"] for item in websocket.sent_messages],
        )
        self.assertEqual(
            [0.4, 0.2, 0.4],
            [item["data"]["progress"] for item in websocket.sent_messages],
        )
        self.assertTrue(all("type" not in item for item in websocket.sent_messages))

    def test_hub_latest_wins_and_progress_is_normalized(self) -> None:
        self.task_service.create_file_task(
            "latest.pdf",
            {"businessType": "file"},
            status="1",
        )
        self.task_service.update_task_progress(
            "file",
            "latest.pdf",
            progress=0.15,
            message="数据库快照",
            status="1",
        )
        self.progress_hub.publish(
            "file",
            "latest.pdf",
            {
                "businessType": "file",
                "data": {"fileName": "latest.pdf", "progress": 0.28000000004},
            },
        )

        websocket = self._run_websocket(
            json.dumps(
                {
                    "businessType": "file",
                    "params": [{"fileName": "latest.pdf"}],
                }
            )
        )

        self.assertEqual(0.28, websocket.sent_messages[0]["data"]["progress"])
        self.assertEqual(0.28, self.progress_hub.get_latest("file", "latest.pdf")["data"]["progress"])

    def test_missing_report_and_weaponry_public_value_types_are_preserved(self) -> None:
        missing = self._run_websocket(
            json.dumps(
                {
                    "businessType": "file",
                    "params": [{"fileName": "missing.pdf"}],
                }
            )
        )
        self.assertEqual(
            {
                "progress": 0.0,
                "fileName": "missing.pdf",
                "exists": False,
            },
            missing.sent_messages[0]["data"],
        )

        self.task_service.create_report_task(132, {"businessType": "report"})
        report = self._run_websocket(
            json.dumps(
                {"businessType": "report", "params": [{"reportId": 132}]}
            )
        )
        self.assertIs(int, type(report.sent_messages[0]["data"]["reportId"]))

        self.task_service.create_weaponry_task(
            10502,
            {"businessType": "weaponry", "params": {"architectureId": 10502}},
        )
        weaponry = self._run_websocket(
            json.dumps(
                {
                    "businessType": "weaponry",
                    "params": [{"architectureId": 10502}],
                }
            )
        )
        self.assertIs(str, type(weaponry.sent_messages[0]["data"]["architectureId"]))

    def test_live_notification_is_sent_only_by_connection_route_thread(self) -> None:
        task = self.task_service.create_file_task(
            "live.pdf",
            {"businessType": "file"},
            status="1",
        )
        websocket, thread, errors = self._start_websocket()
        websocket.push(
            {"businessType": "file", "params": [{"fileName": "live.pdf"}]}
        )
        self.assertTrue(websocket.wait_for_messages(1))

        publisher_thread_id = threading.get_ident()
        self.progress_hub.publish(
            "file",
            "live.pdf",
            {
                "businessType": "file",
                "data": {"fileName": "live.pdf", "progress": 0.75},
            },
            task_id=task["execution_id"],
        )
        self.assertTrue(websocket.wait_for_messages(2))

        self.assertEqual(0.75, websocket.sent_messages[1]["data"]["progress"])
        self.assertEqual({thread.ident}, set(websocket.send_thread_ids))
        self.assertNotIn(publisher_thread_id, websocket.send_thread_ids)
        self.assertEqual([], errors)

    def test_two_connections_same_key_are_isolated_when_one_closes(self) -> None:
        task = self.task_service.create_file_task(
            "shared.pdf",
            {"businessType": "file"},
            status="1",
        )
        first, first_thread, first_errors = self._start_websocket()
        second, _, second_errors = self._start_websocket()
        request = {
            "businessType": "file",
            "params": [{"fileName": "shared.pdf"}],
        }
        first.push(request)
        second.push(request)
        self.assertTrue(first.wait_for_messages(1))
        self.assertTrue(second.wait_for_messages(1))

        self.progress_hub.publish(
            "file",
            "shared.pdf",
            {
                "businessType": "file",
                "data": {"fileName": "shared.pdf", "progress": 0.5},
            },
            task_id=task["execution_id"],
        )
        self.assertTrue(first.wait_for_messages(2))
        self.assertTrue(second.wait_for_messages(2))

        first.close()
        first_thread.join(timeout=5)
        self.assertFalse(first_thread.is_alive())
        first_count = len(first.sent_messages)
        self.progress_hub.publish(
            "file",
            "shared.pdf",
            {
                "businessType": "file",
                "data": {"fileName": "shared.pdf", "progress": 0.8},
            },
            task_id=task["execution_id"],
        )
        self.assertTrue(second.wait_for_messages(3))
        time.sleep(0.15)

        self.assertEqual(first_count, len(first.sent_messages))
        self.assertEqual(0.8, second.sent_messages[-1]["data"]["progress"])
        self.assertEqual([], first_errors)
        self.assertEqual([], second_errors)

    def test_initial_send_failure_aborts_barrier_and_releases_subscription(self) -> None:
        task = self.task_service.create_file_task(
            "send-failure.pdf",
            {"businessType": "file"},
            status="1",
        )
        raw = json.dumps(
            {
                "businessType": "file",
                "params": [{"fileName": "send-failure.pdf"}],
            }
        )

        websocket = _FakeWebSocket(
            (raw,),
            auto_close=True,
            fail_send_after=0,
        )
        with self.assertRaisesRegex(RuntimeError, "send failed"):
            with self.app.app_context():
                self.route_handler(websocket)

        attempts_after_close = websocket.send_attempts
        self.progress_hub.publish(
            "file",
            "send-failure.pdf",
            {
                "businessType": "file",
                "data": {"fileName": "send-failure.pdf", "progress": 0.9},
            },
            task_id=task["execution_id"],
        )
        self.assertEqual(attempts_after_close, websocket.send_attempts)


class ProgressApprovedTargetContractTests(unittest.TestCase):
    """验证阶段 0 冻结资产已标记为 1B-2 实现完成。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(
            _TARGET_CONTRACT_PATH.read_text(encoding="utf-8")
        )["progress"]

    def test_target_only_accepts_no_action_subscriptions_and_has_no_ack(self) -> None:
        self.assertFalse(self.contract["explicitActions"])
        self.assertFalse(self.contract["ackMessages"])
        self.assertEqual("implemented", self.contract["implementationStatus"])
        self.assertEqual("1B-2", self.contract["implementationWave"])
        for request in self.contract["requestExamples"]:
            self.assertNotIn("action", request)

    def test_target_public_types_strict_params_and_close_cleanup_remain_frozen(self) -> None:
        self.assertEqual(["file", "report"], self.contract["publicBusinessTypes"])
        self.assertEqual(
            "reject_entire_message",
            self.contract["paramsElementPolicy"]["onInvalid"],
        )
        self.assertEqual("keep_open", self.contract["explicitActionPolicy"]["connection"])
        self.assertFalse(self.contract["explicitActionPolicy"]["ack"])
        self.assertEqual(
            "release_all_connection_subscriptions",
            self.contract["closeBehavior"],
        )


if __name__ == "__main__":
    unittest.main()
