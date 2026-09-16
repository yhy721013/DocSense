"""阶段 1D-2 武器谱 202/400/404/409 Presenter 契约测试。"""

from __future__ import annotations

import json
import unittest

from app.presenters.weaponry_submission import (
    WeaponrySubmissionResponsePresenter,
)


class WeaponrySubmissionPresenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.presenter = WeaponrySubmissionResponsePresenter()

    def test_success_is_strict_zero_byte_202(self) -> None:
        presentation = self.presenter.present_success()

        self.assertEqual(202, presentation.status_code)
        self.assertEqual(b"", presentation.body)
        self.assertIsNone(presentation.content_type)

    def test_conflict_keeps_approved_single_error_field(self) -> None:
        presentation = self.presenter.present_conflict()

        self.assertEqual(409, presentation.status_code)
        self.assertEqual(
            {"error": "任务正在处理中"},
            json.loads(presentation.body.decode("utf-8")),
        )
        self.assertEqual(
            "application/json; charset=utf-8",
            presentation.content_type,
        )

    def test_bad_request_and_not_found_do_not_add_internal_fields(self) -> None:
        cases = (
            (
                self.presenter.present_bad_request("filePathList必须为数组"),
                400,
                "filePathList必须为数组",
            ),
            (
                self.presenter.present_not_found(
                    "文件 missing.pdf 尚未解析，无法用于知识谱系解析"
                ),
                404,
                "文件 missing.pdf 尚未解析，无法用于知识谱系解析",
            ),
        )
        for presentation, status_code, message in cases:
            with self.subTest(status_code=status_code):
                self.assertEqual(status_code, presentation.status_code)
                self.assertEqual(
                    {"error": message},
                    json.loads(presentation.body.decode("utf-8")),
                )

    def test_empty_or_non_text_error_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.presenter.present_bad_request("   ")
        with self.assertRaises(TypeError):
            self.presenter.present_not_found(None)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
