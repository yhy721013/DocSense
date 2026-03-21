import unittest
from pathlib import Path
from unittest.mock import patch

from app.services.core import LLMProgressHub
from app.services.llm_service.report_service import build_report_callback_payload, ensure_report_html
from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir


class LLMReportServiceTests(unittest.TestCase):
    def test_ensure_report_html_wraps_plain_text(self):
        html = ensure_report_html("报告正文")
        self.assertIn("<div", html)
        self.assertIn("报告正文", html)

    def test_build_report_callback_payload_uses_fixed_success_message(self):
        payload = build_report_callback_payload(132, "<div>ok</div>", status="1")
        self.assertEqual(payload["msg"], "生成成功")

    @patch("app.services.llm_report_service.post_callback_payload", return_value=True)
    @patch("app.services.llm_report_service.run_anythingllm_rag", return_value="<section>报告内容</section>")
    @patch("app.services.llm_report_service.prepare_upload_files")
    @patch("app.services.llm_report_service.normalize_file_for_llm")
    @patch("app.services.llm_report_service.download_to_temp_file")
    def test_run_report_task_normalizes_mhtml_before_prepare_upload_files(
        self,
        mock_download,
        mock_normalize,
        mock_prepare,
        _mock_rag,
        _mock_callback,
    ):
        with workspace_tempdir() as tmp:
            sample = Path(tmp) / "sample.mhtml"
            sample.write_text("mhtml", encoding="utf-8")
            normalized = Path(tmp) / "sample.mhtml.normalized.md"
            normalized.write_text("Hello MHTML", encoding="utf-8")
            mock_download.return_value = str(sample)
            mock_normalize.return_value = str(normalized)
            mock_prepare.return_value = [str(normalized)]

            request_payload = {
                "businessType": "report",
                "params": [
                    {
                        "reportId": 132,
                        "filePathList": [
                            "http://127.0.0.1:8000/sample.mhtml",
                        ],
                        "templateDesc": "模板",
                        "templateOutline": "大纲",
                        "requirement": "要求",
                    }
                ],
            }

            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task_service.create_report_task(132, request_payload)
            hub = LLMProgressHub()

            from app.services.llm_service.report_service import run_report_task

            run_report_task(
                task_service=task_service,
                progress_hub=hub,
                request_payload=request_payload,
                download_root=tmp,
                callback_url="http://127.0.0.1:9000/llm/callback",
                callback_timeout=5,
            )

        mock_normalize.assert_called_once_with(str(sample))
        mock_prepare.assert_called_once_with(str(normalized))

    @patch("app.services.llm_report_service.post_callback_payload", return_value=True)
    @patch("app.services.llm_report_service.run_anythingllm_rag", return_value="<section>报告内容</section>")
    @patch("app.services.llm_report_service.prepare_upload_files")
    @patch("app.services.llm_report_service.normalize_file_for_llm", side_effect=RuntimeError("boom"))
    @patch("app.services.llm_report_service.download_to_temp_file")
    def test_run_report_task_falls_back_to_original_file_when_mhtml_normalization_fails(
        self,
        mock_download,
        _mock_normalize,
        mock_prepare,
        _mock_rag,
        _mock_callback,
    ):
        with workspace_tempdir() as tmp:
            sample = Path(tmp) / "sample.mhtml"
            sample.write_text("mhtml", encoding="utf-8")
            mock_download.return_value = str(sample)
            mock_prepare.return_value = [str(sample)]

            request_payload = {
                "businessType": "report",
                "params": [
                    {
                        "reportId": 132,
                        "filePathList": [
                            "http://127.0.0.1:8000/sample.mhtml",
                        ],
                        "templateDesc": "模板",
                        "templateOutline": "大纲",
                        "requirement": "要求",
                    }
                ],
            }

            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task_service.create_report_task(132, request_payload)
            hub = LLMProgressHub()

            from app.services.llm_service.report_service import run_report_task

            run_report_task(
                task_service=task_service,
                progress_hub=hub,
                request_payload=request_payload,
                download_root=tmp,
                callback_url="http://127.0.0.1:9000/llm/callback",
                callback_timeout=5,
            )

        mock_prepare.assert_called_once_with(str(sample))

    @patch("app.services.llm_report_service.post_callback_payload", return_value=True)
    @patch("app.services.llm_report_service.run_anythingllm_rag", return_value="<section>报告内容</section>")
    @patch("app.services.llm_report_service.prepare_upload_files")
    @patch("app.services.llm_report_service.download_to_temp_file")
    def test_run_report_task_marks_success(self, mock_download, mock_prepare, _mock_rag, _mock_callback):
        with workspace_tempdir() as tmp:
            sample = Path(tmp) / "sample.txt"
            sample.write_text("sample", encoding="utf-8")
            mock_download.return_value = str(sample)
            mock_prepare.return_value = [str(sample)]

            request_payload = {
                "businessType": "report",
                "params": [
                    {
                        "reportId": 132,
                        "filePathList": [
                            "http://127.0.0.1:8000/sample.txt",
                            "http://127.0.0.1:8000/sample.txt"
                        ],
                        "templateDesc": "模板",
                        "templateOutline": "大纲",
                        "requirement": "要求",
                    }
                ],
            }

            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task_service.create_report_task(132, request_payload)
            hub = LLMProgressHub()
            events = []
            hub.subscribe("report", "132", events.append)

            from app.services.llm_service.report_service import run_report_task

            run_report_task(
                task_service=task_service,
                progress_hub=hub,
                request_payload=request_payload,
                download_root=tmp,
                callback_url="http://127.0.0.1:9000/llm/callback",
                callback_timeout=5,
            )

            task = task_service.get_task("report", "132")
            self.assertIsNotNone(task)
            self.assertEqual(task["status"], "1")
            self.assertEqual(task["callback_status"], "success")
            self.assertEqual(task["result_payload"]["msg"], "生成成功")
            self.assertEqual(events[-1]["data"]["progress"], 1.0)
