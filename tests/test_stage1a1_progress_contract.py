"""阶段 1A-1：``/llm/progress`` 当前基线与目标契约测试。

当前 WebSocket 适配层仍兼容显式 ``action`` 和 ack；已批准目标则只接受无 action
订阅，收到显式 action 时返回错误消息、保持连接且不发送 ack。本文件把当前与目标
分开命名和断言，防止迁移前把旧扩展误当成甲方契约，也防止迁移时误删快照顺序、
连接清理和订阅隔离能力。

测试通过 Fake WebSocket、临时 SQLite 和进程内 Hub 完成，不建立真实长连接，不连接
AnythingLLM、模型服务或外部回调地址，也不会启动 ``run.py``。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any, Iterable

from app import create_app
from app.blueprints.llm import (
    _handle_progress_command,
    _parse_progress_command,
)
from tests import workspace_tempdir
from tests.offline_application import build_offline_application_services


_TARGET_CONTRACT_PATH = (
    Path(__file__).with_name("contracts") / "stage0_contracts.json"
)


class _FakeWebSocket:
    """按预设顺序返回客户端帧，并把服务端 JSON 帧解析后留给断言。"""

    def __init__(self, raw_messages: Iterable[str | None]) -> None:
        self._raw_messages = iter(raw_messages)
        self.sent_messages: list[dict[str, Any]] = []

    def receive(self) -> str | None:
        return next(self._raw_messages, None)

    def send(self, raw_message: str) -> None:
        self.sent_messages.append(json.loads(raw_message))


class ProgressCurrentRouteContractTests(unittest.TestCase):
    """冻结波次 1B 切换前仍需保持的 Progress 数据与生命周期行为。"""

    def setUp(self) -> None:
        self._tempdir = workspace_tempdir()
        self.runtime_directory = self._tempdir.__enter__()
        self.services = build_offline_application_services(self.runtime_directory)
        self.task_service = self.services.task_service
        self.progress_hub = self.services.progress_hub
        self.app = create_app(services=self.services)

    def tearDown(self) -> None:
        self._tempdir.__exit__(None, None, None)

    def _run_websocket(self, *raw_messages: str | None) -> _FakeWebSocket:
        """在已注入离线容器的 Flask 上下文中直接执行 WebSocket 适配器。"""

        websocket = _FakeWebSocket(raw_messages)
        # Flask-Sock 的 ``route`` 装饰器不会把原函数绑定回模块符号，但会通过
        # ``functools.wraps`` 将它保留在已注册 Flask view 的 ``__wrapped__`` 中。
        # 这里调用该原函数，既能覆盖完整 receive/send/finally 流程，也不会创建
        # 真实网络连接或依赖 Werkzeug WebSocket environ。
        registered_view = self.app.view_functions["__flask_sock.llm_progress"]
        route_handler = registered_view.__wrapped__
        with self.app.app_context():
            route_handler(websocket)
        return websocket

    def _subscribe_without_action(
        self,
        payload: dict[str, Any],
        sent_messages: list[dict[str, Any]],
        subscriptions: dict[tuple[str, str], Any],
    ) -> None:
        """执行当前无 action 订阅路径；该请求形态也属于目标公开契约。"""

        command = _parse_progress_command(payload)
        _handle_progress_command(
            sent_messages.append,
            subscriptions,
            command,
            emit_ack=False,
            services=self.services,
        )

    def _release_subscriptions(
        self,
        subscriptions: dict[tuple[str, str], Any],
    ) -> None:
        """模拟连接 finally 清理，避免同一用例后续发布受到残留监听影响。"""

        for (business_type, business_key), callback in list(subscriptions.items()):
            self.progress_hub.unsubscribe(business_type, business_key, callback)
        subscriptions.clear()

    def test_invalid_json_returns_error_frame_and_connection_can_close(self) -> None:
        """语法非法的客户端帧只产生错误消息，不终止清理流程。"""

        websocket = self._run_websocket("{not-json", None)

        self.assertEqual(
            [{"type": "error", "message": "订阅消息不是合法JSON"}],
            websocket.sent_messages,
        )

    def test_parser_rejects_invalid_business_type_and_params(self) -> None:
        """非法业务类型、空 params、非数组和仅含无效元素均必须被拒绝。"""

        cases = (
            (
                {"businessType": "unknown", "params": [{}]},
                "businessType无效",
            ),
            ({"businessType": "file"}, "params不能为空"),
            ({"businessType": "file", "params": []}, "params不能为空"),
            ({"businessType": "file", "params": {}}, "params不能为空"),
            ({"businessType": "file", "params": [None]}, "params不能为空"),
        )

        for payload, error_message in cases:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ValueError, error_message):
                    _parse_progress_command(payload)

    def test_current_parser_filters_non_object_items_from_mixed_params(self) -> None:
        """记录旧混合数组过滤行为；1B 是否收紧必须另行确认。"""

        command = _parse_progress_command(
            {
                "businessType": "file",
                "params": [None, {"fileName": "valid.pdf"}, "ignored"],
            }
        )

        self.assertEqual([("file", "valid.pdf")], command["keys"])

    def test_parser_rejects_missing_business_key_for_each_current_type(self) -> None:
        """file/report 公开类型及 weaponry 内部兼容类型都校验自己的业务键。"""

        cases = (
            ("file", "fileName不能为空"),
            ("report", "reportId不能为空"),
            ("weaponry", "architectureId不能为空"),
        )

        for business_type, error_message in cases:
            with self.subTest(business_type=business_type):
                with self.assertRaisesRegex(ValueError, error_message):
                    _parse_progress_command(
                        {"businessType": business_type, "params": [{}]}
                    )

    def test_current_explicit_query_action_still_emits_snapshot_then_ack(self) -> None:
        """仅记录待 1B 删除的旧扩展，不能把 action/ack 升级为公开承诺。"""

        self.task_service.create_file_task(
            "legacy-action.pdf",
            {"businessType": "file"},
            status="1",
        )
        raw_message = json.dumps(
            {
                "action": "query",
                "businessType": "file",
                "params": [{"fileName": "legacy-action.pdf"}],
            },
            ensure_ascii=False,
        )

        websocket = self._run_websocket(raw_message, None)

        self.assertEqual(
            {
                "businessType": "file",
                "data": {"progress": 0.0, "fileName": "legacy-action.pdf"},
            },
            websocket.sent_messages[0],
        )
        self.assertEqual(
            {"type": "ack", "action": "query", "count": 1},
            websocket.sent_messages[1],
        )

    def test_no_action_batch_keeps_order_count_and_subscriptions_without_ack(self) -> None:
        """无 action 批量订阅按 params 顺序发快照，消息数与任务数一致。"""

        self.task_service.create_file_task(
            "a.pdf",
            {"businessType": "file"},
            status="1",
        )
        self.task_service.update_task_progress(
            "file",
            "a.pdf",
            progress=0.2,
            message="处理中",
            status="1",
        )
        self.task_service.create_file_task(
            "b.pdf",
            {"businessType": "file"},
            status="1",
        )
        self.task_service.update_task_progress(
            "file",
            "b.pdf",
            progress=0.4,
            message="处理中",
            status="1",
        )
        sent_messages: list[dict[str, Any]] = []
        subscriptions: dict[tuple[str, str], Any] = {}

        self._subscribe_without_action(
            {
                "businessType": "file",
                "params": [{"fileName": "b.pdf"}, {"fileName": "a.pdf"}],
            },
            sent_messages,
            subscriptions,
        )

        self.assertEqual(2, len(sent_messages))
        self.assertEqual(
            ["b.pdf", "a.pdf"],
            [message["data"]["fileName"] for message in sent_messages],
        )
        self.assertEqual([0.4, 0.2], [message["data"]["progress"] for message in sent_messages])
        self.assertEqual(
            [("file", "b.pdf"), ("file", "a.pdf")],
            list(subscriptions),
        )
        self.assertTrue(all("type" not in message for message in sent_messages))
        self._release_subscriptions(subscriptions)

    def test_hub_latest_value_wins_over_task_snapshot(self) -> None:
        """Hub 已有更新时不得回退到较旧的 SQLite 任务快照。"""

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
                "data": {"fileName": "latest.pdf", "progress": 0.85},
            },
        )
        sent_messages: list[dict[str, Any]] = []
        subscriptions: dict[tuple[str, str], Any] = {}

        self._subscribe_without_action(
            {
                "businessType": "file",
                "params": [{"fileName": "latest.pdf"}],
            },
            sent_messages,
            subscriptions,
        )

        self.assertEqual(1, len(sent_messages))
        self.assertEqual(0.85, sent_messages[0]["data"]["progress"])
        self._release_subscriptions(subscriptions)

    def test_missing_task_snapshot_marks_exists_false(self) -> None:
        """Hub 与任务库均无记录时仍返回业务键，并显式标记不存在。"""

        sent_messages: list[dict[str, Any]] = []
        subscriptions: dict[tuple[str, str], Any] = {}

        self._subscribe_without_action(
            {
                "businessType": "file",
                "params": [{"fileName": "missing.pdf"}],
            },
            sent_messages,
            subscriptions,
        )

        self.assertEqual(
            [
                {
                    "businessType": "file",
                    "data": {
                        "progress": 0.0,
                        "fileName": "missing.pdf",
                        "exists": False,
                    },
                }
            ],
            sent_messages,
        )
        self._release_subscriptions(subscriptions)

    def test_report_snapshots_preserve_integer_key_type_and_batch_count(self) -> None:
        """公开 report 快照必须按请求顺序输出 Long/JSON number 业务键。"""

        self.task_service.create_report_task(
            132,
            {"businessType": "report", "params": [{"reportId": 132}]},
        )
        self.task_service.create_report_task(
            133,
            {"businessType": "report", "params": [{"reportId": 133}]},
        )
        sent_messages: list[dict[str, Any]] = []
        subscriptions: dict[tuple[str, str], Any] = {}

        self._subscribe_without_action(
            {
                "businessType": "report",
                "params": [{"reportId": 133}, {"reportId": 132}],
            },
            sent_messages,
            subscriptions,
        )

        report_ids = [message["data"]["reportId"] for message in sent_messages]
        self.assertEqual([133, 132], report_ids)
        self.assertEqual(2, len(sent_messages))
        self.assertTrue(all(type(report_id) is int for report_id in report_ids))
        self._release_subscriptions(subscriptions)

    def test_weaponry_snapshot_remains_internal_compatibility_only(self) -> None:
        """记录当前 weaponry Progress 值类型，但不将其冻结为公开协议。"""

        self.task_service.create_weaponry_task(
            10502,
            {
                "businessType": "weaponry",
                "params": {"architectureId": 10502},
            },
        )
        sent_messages: list[dict[str, Any]] = []
        subscriptions: dict[tuple[str, str], Any] = {}

        self._subscribe_without_action(
            {
                "businessType": "weaponry",
                "params": [{"architectureId": 10502}],
            },
            sent_messages,
            subscriptions,
        )

        architecture_id = sent_messages[0]["data"]["architectureId"]
        self.assertEqual("10502", architecture_id)
        self.assertIs(str, type(architecture_id))
        self._release_subscriptions(subscriptions)

    def test_hub_normalizes_progress_before_snapshot_delivery(self) -> None:
        """浮点计算伪影必须在存储最新值和推送前统一归一化。"""

        self.progress_hub.publish(
            "file",
            "normalized.pdf",
            {
                "businessType": "file",
                "data": {
                    "fileName": "normalized.pdf",
                    "progress": 0.28000000004,
                },
            },
        )
        sent_messages: list[dict[str, Any]] = []
        subscriptions: dict[tuple[str, str], Any] = {}

        self._subscribe_without_action(
            {
                "businessType": "file",
                "params": [{"fileName": "normalized.pdf"}],
            },
            sent_messages,
            subscriptions,
        )

        self.assertEqual(0.28, sent_messages[0]["data"]["progress"])
        self.assertEqual(
            0.28,
            self.progress_hub.get_latest("file", "normalized.pdf")["data"][
                "progress"
            ],
        )
        self._release_subscriptions(subscriptions)

    def test_connection_close_releases_all_subscriptions_without_control_frame(self) -> None:
        """WebSocket 关闭后当前连接的全部回调必须移除，且不额外发送 ack。"""

        self.task_service.create_file_task(
            "close.pdf",
            {"businessType": "file"},
            status="1",
        )
        raw_message = json.dumps(
            {
                "businessType": "file",
                "params": [{"fileName": "close.pdf"}],
            },
            ensure_ascii=False,
        )

        websocket = self._run_websocket(raw_message, None)
        message_count_after_close = len(websocket.sent_messages)
        self.assertEqual(1, message_count_after_close)
        self.assertTrue(
            all("type" not in message for message in websocket.sent_messages)
        )

        # 如果 finally 清理失效，下列发布会再次调用已关闭连接的 send。
        self.progress_hub.publish(
            "file",
            "close.pdf",
            {
                "businessType": "file",
                "data": {"fileName": "close.pdf", "progress": 0.9},
            },
        )
        self.assertEqual(message_count_after_close, len(websocket.sent_messages))

    def test_two_connections_subscribing_same_task_are_isolated(self) -> None:
        """释放一个连接的回调不得误删另一个连接对同一任务的订阅。"""

        self.task_service.create_file_task(
            "shared.pdf",
            {"businessType": "file"},
            status="1",
        )
        payload = {
            "businessType": "file",
            "params": [{"fileName": "shared.pdf"}],
        }
        first_messages: list[dict[str, Any]] = []
        second_messages: list[dict[str, Any]] = []
        first_subscriptions: dict[tuple[str, str], Any] = {}
        second_subscriptions: dict[tuple[str, str], Any] = {}
        self._subscribe_without_action(payload, first_messages, first_subscriptions)
        self._subscribe_without_action(payload, second_messages, second_subscriptions)
        first_messages.clear()
        second_messages.clear()

        self.progress_hub.publish(
            "file",
            "shared.pdf",
            {
                "businessType": "file",
                "data": {"fileName": "shared.pdf", "progress": 0.5},
            },
        )
        self.assertEqual([0.5], [message["data"]["progress"] for message in first_messages])
        self.assertEqual([0.5], [message["data"]["progress"] for message in second_messages])

        self._release_subscriptions(first_subscriptions)
        first_messages.clear()
        second_messages.clear()
        self.progress_hub.publish(
            "file",
            "shared.pdf",
            {
                "businessType": "file",
                "data": {"fileName": "shared.pdf", "progress": 0.75},
            },
        )

        self.assertEqual([], first_messages)
        self.assertEqual([0.75], [message["data"]["progress"] for message in second_messages])
        self._release_subscriptions(second_subscriptions)


class ProgressApprovedTargetContractTests(unittest.TestCase):
    """冻结已获批准、但要到波次 1B 才切换的 Progress 目标协议。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(
            _TARGET_CONTRACT_PATH.read_text(encoding="utf-8")
        )["progress"]

    def test_target_only_accepts_no_action_subscriptions_and_has_no_ack(self) -> None:
        """目标协议删除旧 action/ack 扩展，但不新增替代控制参数。"""

        self.assertFalse(self.contract["explicitActions"])
        self.assertFalse(self.contract["ackMessages"])
        self.assertEqual(
            {
                "onPresent": "reject_entire_message",
                "response": "error_message",
                "connection": "keep_open",
                "ack": False,
            },
            self.contract["explicitActionPolicy"],
        )
        self.assertEqual("pending", self.contract["implementationStatus"])
        self.assertEqual("1B", self.contract["implementationWave"])
        for request in self.contract["requestExamples"]:
            with self.subTest(business_type=request["businessType"]):
                self.assertNotIn("action", request)

    def test_target_public_types_and_connection_cleanup_are_frozen(self) -> None:
        """公开类型仅 file/report，连接关闭必须释放该连接全部订阅。"""

        self.assertEqual(["file", "report"], self.contract["publicBusinessTypes"])
        self.assertEqual(
            {
                "requiredType": "object",
                "onInvalid": "reject_entire_message",
                "response": "error_message",
                "connection": "keep_open",
            },
            self.contract["paramsElementPolicy"],
        )
        self.assertEqual(
            "release_all_connection_subscriptions",
            self.contract["closeBehavior"],
        )
        self.assertEqual(
            "current_internal_extension_not_frozen_as_public_contract",
            self.contract["weaponryProgressStatus"],
        )


if __name__ == "__main__":
    unittest.main()
