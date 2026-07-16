"""阶段 0 对外契约黄金资产的离线校验。

本模块只验证已经取得确认的目标契约和当前回调/SSE Presenter，不调用路由中的
后台线程，也不连接 AnythingLLM、模型、回调服务器或生产数据库。目标 HTTP 行为
尚未进入对应实施波次，因此这里不会拿当前路由输出冒充目标已经实现。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from app.presenters.chat_stream import format_sse_event
from app.services.llm_service.analysis_service import build_file_callback_payload
from app.services.llm_service.report_service import build_report_callback_payload
from app.services.llm_service.weaponry_service import (
    _build_weaponry_callback_payload,
)


_CONTRACT_PATH = Path(__file__).with_name("contracts") / "stage0_contracts.json"


class Stage0ContractAssetTests(unittest.TestCase):
    """防止后续重构意外扩张或回退已确认的契约调整。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.contract: dict[str, Any] = json.loads(
            _CONTRACT_PATH.read_text(encoding="utf-8")
        )

    def test_task_submission_success_responses_are_empty_and_pending(self) -> None:
        """三类受理接口只删除成功体，实施前必须显式标记 pending。"""

        expected = {
            "/llm/analysis": "1F",
            "/llm/generate-report": "1C",
            "/llm/weaponry": "1D",
        }
        observed = {
            item["path"]: item["implementationWave"]
            for item in self.contract["taskSubmissions"]
        }
        self.assertEqual(expected, observed)

        for item in self.contract["taskSubmissions"]:
            with self.subTest(path=item["path"]):
                self.assertEqual(202, item["success"]["status"])
                self.assertEqual("", item["success"]["body"])
                self.assertEqual("pending", item["implementationStatus"])

    def test_report_active_duplicate_keeps_confirmed_409_error(self) -> None:
        report = next(
            item
            for item in self.contract["taskSubmissions"]
            if item["name"] == "report"
        )
        self.assertEqual(
            {"status": 409, "body": {"error": "任务正在处理中"}},
            report["activeDuplicate"],
        )

    def test_report_id_input_policy_accepts_integer_strings_without_range_limit(self) -> None:
        self.assertEqual("2026-07-16", self.contract["updatedAt"])
        self.assertEqual(
            {
                "acceptedJsonTypes": ["integer", "string"],
                "stringFormat": "optional_sign_and_decimal_digits_after_trim",
                "businessRange": "no_application_level_min_or_max",
                "canonicalization": "same_integer_value_is_same_business_key",
                "invalidHttpStatus": 400,
                "invalidProgressBehavior": "error_message_and_keep_connection",
                "serverOutputType": "json_number",
            },
            self.contract["reportIdInputPolicy"],
        )

    def test_check_task_success_is_empty_but_recovery_side_effect_remains(self) -> None:
        check_task = self.contract["checkTask"]
        self.assertEqual(200, check_task["success"]["status"])
        self.assertEqual("", check_task["success"]["body"])
        self.assertEqual(
            "may_recover_terminal_callback",
            check_task["success"]["sideEffect"],
        )
        self.assertEqual(400, check_task["invalidRequestStatus"])
        self.assertEqual(
            {
                "requiredType": "object",
                "onInvalid": "reject_entire_request",
                "status": 400,
            },
            check_task["paramsElementPolicy"],
        )
        self.assertEqual(404, check_task["singleMissing"]["status"])
        self.assertEqual(["file", "report"], check_task["documentedBusinessTypes"])
        self.assertEqual("1B", check_task["implementationWave"])

    def test_progress_target_has_no_explicit_action_or_ack(self) -> None:
        progress = self.contract["progress"]
        self.assertFalse(progress["explicitActions"])
        self.assertFalse(progress["ackMessages"])
        self.assertEqual(
            {
                "requiredType": "object",
                "onInvalid": "reject_entire_message",
                "response": "error_message",
                "connection": "keep_open",
            },
            progress["paramsElementPolicy"],
        )
        self.assertEqual(
            {
                "onPresent": "reject_entire_message",
                "response": "error_message",
                "connection": "keep_open",
                "ack": False,
            },
            progress["explicitActionPolicy"],
        )
        self.assertEqual(["file", "report"], progress["publicBusinessTypes"])
        self.assertEqual(
            "release_all_connection_subscriptions",
            progress["closeBehavior"],
        )
        for request in progress["requestExamples"]:
            with self.subTest(business_type=request["businessType"]):
                self.assertNotIn("action", request)
                self.assertGreaterEqual(len(request["params"]), 1)

    def test_callback_builders_match_frozen_payloads(self) -> None:
        callbacks = self.contract["callbacks"]
        self.assertEqual(
            callbacks["fileFailure"],
            build_file_callback_payload("示例资料.pdf", {}, status="3"),
        )
        self.assertEqual(
            callbacks["reportFailure"],
            build_report_callback_payload(132, "", status="2"),
        )
        self.assertEqual(
            callbacks["weaponryFailure"],
            _build_weaponry_callback_payload(132, [], status="3"),
        )

    def test_chat_sse_wire_examples_match_presenter(self) -> None:
        for frame in self.contract["chatSseFrames"]:
            with self.subTest(event_type=frame["eventType"]):
                self.assertEqual(
                    frame["wire"],
                    format_sse_event(frame["eventType"], frame["data"]),
                )


if __name__ == "__main__":
    unittest.main()
