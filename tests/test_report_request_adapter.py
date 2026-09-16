"""报告生成 HTTP 入站适配器的无 Flask、无 I/O 契约测试。"""

from __future__ import annotations

import unittest

from app.adapters.web.flask.report_requests import (
    ReportRequestValidationError,
    parse_report_request,
)


def _valid_payload(report_id: object = 132) -> dict[str, object]:
    """构造包含额外兼容字段的标准请求，验证解析器不会静默删减数据。"""

    return {
        "businessType": "report",
        "traceExtension": {"source": "test"},
        "params": [
            {
                "reportId": report_id,
                "filePathList": [
                    "http://files.invalid/a.pdf",
                    " http://files.invalid/b.pdf ",
                ],
                "templateDesc": "模板说明",
                "templateOutline": "http://files.invalid/template.docx",
                "requirement": "生成报告",
                "extra": ["保留"],
            },
            {"futureExtension": True},
        ],
    }


class ReportRequestAdapterTests(unittest.TestCase):
    """验证已批准的整次请求原子校验和兼容副本。"""

    def test_valid_request_is_copied_and_report_id_is_normalized(self) -> None:
        huge_report_id = 10**100 + 132
        payload = _valid_payload(f" +000{huge_report_id} ")

        parsed = parse_report_request(payload)

        self.assertEqual(huge_report_id, parsed.report_id.value)
        self.assertEqual(str(huge_report_id), parsed.report_id.business_key)
        self.assertEqual(huge_report_id, parsed.params["reportId"])
        self.assertEqual(payload["traceExtension"], parsed.request_payload["traceExtension"])
        self.assertEqual(
            {"futureExtension": True},
            parsed.request_payload["params"][1],
        )
        # 合法 URL 的首尾空格保持原样；本轮只拒绝空白项，不擅自改变下载地址内容。
        self.assertEqual(
            " http://files.invalid/b.pdf ",
            parsed.params["filePathList"][1],
        )
        self.assertEqual(f" +000{huge_report_id} ", payload["params"][0]["reportId"])

        parsed.params["extra"].append("仅修改副本")
        self.assertEqual(["保留"], payload["params"][0]["extra"])

    def test_optional_prompt_fields_use_legacy_string_conversion(self) -> None:
        payload = _valid_payload()
        first = payload["params"][0]  # type: ignore[index]
        first["templateDesc"] = 123
        first["requirement"] = None

        parsed = parse_report_request(payload)

        self.assertEqual("123", parsed.params["templateDesc"])
        self.assertEqual("None", parsed.params["requirement"])
        # 入站对象仍保持原样，后台执行只持有隔离后的兼容文本快照。
        self.assertEqual(123, first["templateDesc"])
        self.assertIsNone(first["requirement"])

        missing = _valid_payload()
        del missing["params"][0]["templateDesc"]  # type: ignore[index]
        del missing["params"][0]["requirement"]  # type: ignore[index]
        parsed_missing = parse_report_request(missing)
        self.assertEqual("", parsed_missing.params["templateDesc"])
        self.assertEqual("", parsed_missing.params["requirement"])

    def test_parsed_request_maps_to_immutable_submission_without_new_parameters(self) -> None:
        parsed = parse_report_request(_valid_payload("+000132"))

        submission = parsed.to_submission(trace_id="trace-from-server")

        self.assertEqual(132, submission.report_id.public_value)
        self.assertEqual("132", submission.report_id.business_key)
        self.assertEqual(
            tuple(parsed.params["filePathList"]),
            submission.source_urls,
        )
        self.assertEqual(
            parsed.params["templateOutline"],
            submission.template_outline_url,
        )
        self.assertEqual("trace-from-server", submission.trace_id)

    def test_report_id_accepts_128_digits_and_rejects_129_digits(self) -> None:
        accepted = "9" * 128
        parsed = parse_report_request(_valid_payload(accepted))
        self.assertEqual(accepted, parsed.report_id.business_key)

        for invalid in ("9" * 129, int("9" * 129), "+" + "0" * 128 + "1"):
            with self.subTest(value_type=type(invalid).__name__):
                with self.assertRaisesRegex(
                    ReportRequestValidationError,
                    "^reportId不能超过128位十进制数字$",
                ):
                    parse_report_request(_valid_payload(invalid))

    def test_every_non_object_top_level_json_value_is_rejected(self) -> None:
        invalid_payloads = (
            None,
            [],
            [{"businessType": "report"}],
            "report",
            132,
            True,
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(
                    ReportRequestValidationError,
                    "^请求体必须是JSON对象$",
                ):
                    parse_report_request(payload)

    def test_any_non_object_params_element_rejects_entire_request(self) -> None:
        valid_first = _valid_payload()["params"][0]  # type: ignore[index]
        invalid_params_values = (
            [valid_first, "invalid"],
            ["invalid", valid_first],
            [valid_first, None],
            [valid_first, 132],
        )

        for params in invalid_params_values:
            with self.subTest(params=params):
                with self.assertRaisesRegex(
                    ReportRequestValidationError,
                    "^params元素必须是对象$",
                ):
                    parse_report_request(
                        {"businessType": "report", "params": params}
                    )

    def test_invalid_file_path_element_reports_one_based_index(self) -> None:
        invalid_values = (123, None, "", " \r\n\t ")
        for invalid_value in invalid_values:
            payload = _valid_payload()
            payload["params"][0]["filePathList"] = [  # type: ignore[index]
                "http://files.invalid/valid.pdf",
                invalid_value,
            ]

            with self.subTest(invalid_value=invalid_value):
                with self.assertRaisesRegex(
                    ReportRequestValidationError,
                    "^filePathList中第2项不是有效字符串$",
                ):
                    parse_report_request(payload)

    def test_existing_fixed_validation_errors_remain_unchanged(self) -> None:
        cases = (
            ({}, "businessType必须为report"),
            ({"businessType": "report"}, "params不能为空"),
            ({"businessType": "report", "params": [{}]}, "reportId不能为空"),
            (
                {"businessType": "report", "params": [{"reportId": "1.2"}]},
                "reportId必须是整数或整数字符串",
            ),
            (
                {
                    "businessType": "report",
                    "params": [{"reportId": 132, "filePathList": []}],
                },
                "filePathList不能为空",
            ),
            (
                {
                    "businessType": "report",
                    "params": [
                        {
                            "reportId": 132,
                            "filePathList": ["http://files.invalid/a.pdf"],
                        }
                    ],
                },
                "templateOutline不能为空",
            ),
        )

        for payload, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(
                    ReportRequestValidationError,
                    f"^{message}$",
                ):
                    parse_report_request(payload)


if __name__ == "__main__":
    unittest.main()
