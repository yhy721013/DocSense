import unittest

from app.services.llm_report_service import build_report_callback_payload, ensure_report_html


class LLMReportServiceTests(unittest.TestCase):
    def test_ensure_report_html_wraps_plain_text(self):
        html = ensure_report_html("报告正文")
        self.assertIn("<div", html)
        self.assertIn("报告正文", html)

    def test_build_report_callback_payload_uses_fixed_success_message(self):
        payload = build_report_callback_payload(132, "<div>ok</div>", status="1")
        self.assertEqual(payload["msg"], "生成成功")
