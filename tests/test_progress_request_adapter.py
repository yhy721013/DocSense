"""阶段 1B-2 Progress Flask 请求适配器契约测试。"""

from __future__ import annotations

import unittest

from app.adapters.web.flask import (
    ProgressRequestValidationError,
    parse_progress_subscription,
)


class ProgressRequestAdapterTests(unittest.TestCase):
    def test_file_batch_preserves_order_whitespace_and_duplicates(self) -> None:
        request = parse_progress_subscription(
            {
                "businessType": "file",
                "params": [
                    {"fileName": " b.pdf "},
                    {"fileName": "a.pdf"},
                    {"fileName": "b.pdf"},
                ],
            }
        )

        self.assertEqual(
            ["b.pdf", "a.pdf", "b.pdf"],
            [item.business_key for item in request.ordered_keys],
        )

    def test_report_integer_and_integer_strings_share_one_canonical_key(self) -> None:
        values_and_expected = (
            (132, "132"),
            ("132", "132"),
            ("00132", "132"),
            (" +00132 ", "132"),
            ("-000132", "-132"),
            ("9" * 128, "9" * 128),
        )

        for value, expected in values_and_expected:
            with self.subTest(value=value):
                report = parse_progress_subscription(
                    {
                        "businessType": "report",
                        "params": [{"reportId": value}],
                    }
                )
                self.assertEqual(
                    expected,
                    report.ordered_keys[0].business_key,
                )

    def test_report_rejects_values_that_are_not_integers(self) -> None:
        for invalid in (True, False, 132.0, "132.0", "not-an-integer", "", [], {}):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    ProgressRequestValidationError,
                    "reportId必须是整数或整数字符串",
                ):
                    parse_progress_subscription(
                        {
                            "businessType": "report",
                            "params": [{"reportId": invalid}],
                        }
                    )

    def test_report_rejects_more_than_128_decimal_digits(self) -> None:
        with self.assertRaisesRegex(
            ProgressRequestValidationError,
            "reportId不能超过128位十进制数字",
        ):
            parse_progress_subscription(
                {
                    "businessType": "report",
                    "params": [{"reportId": "9" * 129}],
                }
            )

    def test_weaponry_integer_forms_share_the_approved_canonical_key(self) -> None:
        for value in (10502, "10502", "00010502"):
            with self.subTest(value=value):
                weaponry = parse_progress_subscription(
                    {
                        "businessType": "weaponry",
                        "params": [{"architectureId": value}],
                    }
                )

                self.assertEqual("10502", weaponry.ordered_keys[0].business_key)

    def test_weaponry_rejects_every_unapproved_architecture_id_form(self) -> None:
        invalid_values = (
            True,
            1.0,
            " 1 ",
            "+1",
            "1.0",
            0,
            -1,
            [],
            {},
            9_223_372_036_854_775_808,
        )
        for invalid in invalid_values:
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    ProgressRequestValidationError,
                    "architectureId必须为1到9223372036854775807之间的正整数",
                ):
                    parse_progress_subscription(
                        {
                            "businessType": "weaponry",
                            "params": [{"architectureId": invalid}],
                        }
                    )

    def test_any_explicit_action_is_rejected_even_when_empty(self) -> None:
        for action in ("subscribe", "query", "unsubscribe", "", None):
            with self.subTest(action=action):
                with self.assertRaisesRegex(
                    ProgressRequestValidationError,
                    "action",
                ):
                    parse_progress_subscription(
                        {
                            "action": action,
                            "businessType": "file",
                            "params": [{"fileName": "a.pdf"}],
                        }
                    )

    def test_any_non_object_param_rejects_entire_message(self) -> None:
        for invalid in (None, "bad", 1, [], True):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(
                    ProgressRequestValidationError,
                    "params元素必须是对象",
                ):
                    parse_progress_subscription(
                        {
                            "businessType": "file",
                            "params": [
                                {"fileName": "must-not-subscribe.pdf"},
                                invalid,
                            ],
                        }
                    )

    def test_invalid_root_type_business_type_params_and_keys_are_rejected(self) -> None:
        cases = (
            ([], "订阅消息格式无效"),
            ({"businessType": "unknown", "params": [{}]}, "businessType无效"),
            ({"businessType": "file", "params": []}, "params不能为空"),
            ({"businessType": "file", "params": [{}]}, "fileName不能为空"),
            ({"businessType": "report", "params": [{}]}, "reportId不能为空"),
            (
                {"businessType": "weaponry", "params": [{}]},
                "architectureId不能为空",
            ),
        )
        for payload, message in cases:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(ProgressRequestValidationError, message):
                    parse_progress_subscription(payload)


if __name__ == "__main__":
    unittest.main()
