"""阶段 0 对外契约黄金资产的离线校验。

本模块只验证已经取得确认的目标契约和当前回调/SSE Presenter，不调用路由中的
后台线程，也不连接 AnythingLLM、模型、回调服务器或生产数据库。每个波次完成后，
黄金资产的 current 和 implementationStatus 必须同步推进，不能继续把已实现行为标成 pending。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any

from app.presenters.chat_stream import format_sse_event
from app.services.llm_service.analysis_service import build_file_callback_payload
from app.services.llm_service.report_service import (
    build_report_callback_payload,
    ensure_report_html,
)
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

    def test_task_submission_success_responses_and_wave_status_are_explicit(self) -> None:
        """三类受理接口均已切换为批准的 202 严格空响应。"""

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

        expected_status = {
            "/llm/analysis": "implemented",
            "/llm/generate-report": "implemented",
            "/llm/weaponry": "implemented",
        }
        for item in self.contract["taskSubmissions"]:
            with self.subTest(path=item["path"]):
                self.assertEqual(202, item["success"]["status"])
                self.assertEqual("", item["success"]["body"])
                self.assertEqual(
                    expected_status[item["path"]],
                    item["implementationStatus"],
                )

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

    def test_report_current_has_reached_approved_target(self) -> None:
        """1C-6 完成后 current 必须精确推进到批准的 202/409/Dispatcher 目标。"""

        self.assertEqual(3, self.contract["schemaVersion"])
        baseline = self.contract["reportGenerationBaseline"]
        self.assertEqual(202, baseline["current"]["success"]["status"])
        self.assertEqual("", baseline["current"]["success"]["body"])
        self.assertEqual(202, baseline["target"]["success"]["status"])
        self.assertEqual("", baseline["target"]["success"]["body"])
        self.assertEqual(409, baseline["current"]["activeDuplicate"]["status"])
        self.assertEqual(409, baseline["target"]["activeDuplicate"]["status"])
        self.assertEqual(
            "persistent_backlog_and_bounded_wakeup",
            baseline["current"]["dispatch"],
        )
        self.assertEqual(
            "persistent_backlog_and_bounded_wakeup",
            baseline["target"]["dispatch"],
        )
        self.assertEqual(
            [0.0, 0.15, 0.25, 0.35, 1.0],
            baseline["current"]["progressValues"],
        )
        self.assertEqual(
            {
                "container": "non_empty_array",
                "nonObjectElements": "reject_entire_request_http_400",
                "topLevelNonObject": "reject_http_400",
                "filePathListElementTypes": (
                    "non_empty_strings_or_reject_http_400"
                ),
                "filePathListElementErrorTemplate": (
                    "filePathList中第{index}项不是有效字符串"
                ),
                "templateDescAndRequirement": (
                    "legacy_compatible_string_conversion"
                ),
            },
            baseline["current"]["paramsPolicy"],
        )
        self.assertEqual(
            "请求体必须是JSON对象",
            baseline["current"]["validationErrors"]["requestBody"],
        )
        self.assertEqual(
            "params元素必须是对象",
            baseline["current"]["validationErrors"]["paramsElement"],
        )

    def test_report_id_input_policy_accepts_integer_strings_with_128_digit_limit(self) -> None:
        self.assertEqual("2026-07-25", self.contract["updatedAt"])
        self.assertEqual(
            {
                "acceptedJsonTypes": ["integer", "string"],
                "stringFormat": "optional_sign_and_decimal_digits_after_trim",
                "businessRange": "absolute_value_less_than_10_power_128",
                "maxDecimalDigits": 128,
                "digitCountIncludesLeadingZeros": True,
                "digitCountExcludesOptionalSign": True,
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
        self.assertEqual(
            ["file", "report", "weaponry"],
            check_task["documentedBusinessTypes"],
        )
        self.assertEqual(
            "all-params-parse-and-normalize-before-any-callback",
            check_task["validationBeforeSideEffects"],
        )
        self.assertEqual(
            "normalized-reportId-first-occurrence",
            check_task["sameRequestDeduplication"]["report"],
        )
        self.assertEqual(
            "normalized-architectureId-first-occurrence",
            check_task["sameRequestDeduplication"]["weaponry"],
        )
        self.assertEqual(
            ["file", "report", "weaponry"],
            check_task["explicitUnknownRecovery"]["businessTypes"],
        )
        self.assertFalse(
            check_task["explicitUnknownRecovery"]["automaticWorkerRetry"]
        )
        self.assertEqual(
            "original-params-count-before-deduplication",
            check_task["responseCardinalityBasis"],
        )
        self.assertEqual("implemented", check_task["implementationStatus"])
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
        self.assertEqual(["file", "report", "weaponry"], progress["publicBusinessTypes"])
        self.assertEqual("public_contract_active", progress["weaponryProgressStatus"])
        self.assertEqual(
            "release_all_connection_subscriptions",
            progress["closeBehavior"],
        )
        for request in progress["requestExamples"]:
            with self.subTest(business_type=request["businessType"]):
                self.assertNotIn("action", request)
                self.assertGreaterEqual(len(request["params"]), 1)
        weaponry_message = next(
            message
            for message in progress["serverMessageExamples"]
            if message["businessType"] == "weaponry"
        )
        self.assertIs(int, type(weaponry_message["data"]["architectureId"]))

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
            callbacks["reportSuccess"],
            build_report_callback_payload(
                132,
                "<div>报告内容</div>",
                status="1",
            ),
        )
        self.assertEqual(
            callbacks["reportEmptySuccess"],
            build_report_callback_payload(
                132,
                ensure_report_html(None),
                status="1",
            ),
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
