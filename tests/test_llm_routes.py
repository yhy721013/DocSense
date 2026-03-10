import unittest

from app import create_app


class LLMRouteValidationTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()

    def test_analysis_rejects_invalid_business_type(self):
        response = self.client.post("/llm/analysis", json={"businessType": "wrong", "params": [{}]})
        self.assertEqual(response.status_code, 400)

    def test_generate_report_rejects_missing_params(self):
        response = self.client.post("/llm/generate-report", json={"businessType": "report"})
        self.assertEqual(response.status_code, 400)
