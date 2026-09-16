import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app.services.core.progress_hub import LLMProgressHub
from app.services.llm_service.report_service import build_report_callback_payload, ensure_report_html
from app.services.llm_service.task_service import LLMTaskService
from app.services.utils.word_extractor import extract_text_from_word
from tests import workspace_tempdir


class LLMReportServiceTests(unittest.TestCase):
    def _write_minimal_docx(self, path: Path, body_xml: str) -> None:
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(
                "[Content_Types].xml",
                """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>""",
            )
            archive.writestr(
                "word/document.xml",
                f"""<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>{body_xml}</w:body>
</w:document>""",
            )

    def test_ensure_report_html_wraps_plain_text(self):
        html = ensure_report_html("报告正文")
        self.assertIn("<div", html)
        self.assertIn("报告正文", html)

    def test_build_report_callback_payload_uses_fixed_success_message(self):
        payload = build_report_callback_payload(132, "<div>ok</div>", status="1")
        self.assertEqual(payload["msg"], "生成成功")

    def test_extract_text_from_word_reads_paragraphs_and_table_cells(self):
        with workspace_tempdir() as tmp:
            docx_path = Path(tmp) / "template.docx"
            self._write_minimal_docx(
                docx_path,
                """
  <w:p><w:r><w:t>报告标题</w:t></w:r></w:p>
  <w:tbl>
    <w:tr>
      <w:tc><w:p><w:r><w:t>章节</w:t></w:r></w:p></w:tc>
      <w:tc><w:p><w:r><w:t>要求</w:t></w:r></w:p></w:tc>
    </w:tr>
  </w:tbl>
""",
            )

            text = extract_text_from_word(str(docx_path))

        self.assertIn("报告标题", text)
        self.assertIn("章节", text)
        self.assertIn("要求", text)

    @patch("app.services.llm_service.report_service.post_callback_payload", return_value=True)
    @patch("app.services.llm_service.report_service.extract_text_from_word", return_value="模板大纲文本")
    @patch("app.services.llm_service.report_service.run_anythingllm_rag", return_value="<section>报告内容</section>")
    @patch("app.services.llm_service.report_service.prepare_upload_files")
    @patch("app.services.llm_service.report_service.normalize_file_for_llm")
    @patch("app.services.llm_service.report_service.download_to_temp_file")
    def test_run_report_task_normalizes_mhtml_before_prepare_upload_files(
        self,
        mock_download,
        mock_normalize,
        mock_prepare,
        mock_rag,
        _mock_extract,
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
                        "templateOutline": "http://127.0.0.1:8000/template.docx",
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
        rag_arguments = mock_rag.call_args.kwargs
        self.assertEqual("report-132", rag_arguments["thread_name"])
        self.assertRegex(
            rag_arguments["workspace_name"],
            r"^llm-report-132-\d+$",
        )

    @patch("app.services.llm_service.report_service.post_callback_payload", return_value=True)
    @patch("app.services.llm_service.report_service.extract_text_from_word", return_value="模板大纲文本")
    @patch("app.services.llm_service.report_service.run_anythingllm_rag", return_value="<section>报告内容</section>")
    @patch("app.services.llm_service.report_service.prepare_upload_files")
    @patch("app.services.llm_service.report_service.normalize_file_for_llm", side_effect=RuntimeError("boom"))
    @patch("app.services.llm_service.report_service.download_to_temp_file")
    def test_run_report_task_falls_back_to_original_file_when_mhtml_normalization_fails(
        self,
        mock_download,
        _mock_normalize,
        mock_prepare,
        _mock_rag,
        _mock_extract,
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
                        "templateOutline": "http://127.0.0.1:8000/template.docx",
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

    @patch("app.services.llm_service.report_service.post_callback_payload", return_value=True)
    @patch("app.services.llm_service.report_service.extract_text_from_word", return_value="模板大纲文本")
    @patch("app.services.llm_service.report_service.run_anythingllm_rag", return_value="<section>报告内容</section>")
    @patch("app.services.llm_service.report_service.prepare_upload_files")
    @patch("app.services.llm_service.report_service.download_to_temp_file")
    def test_run_report_task_marks_success(self, mock_download, mock_prepare, _mock_rag, _mock_extract, _mock_callback):
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
                        "templateOutline": "http://127.0.0.1:8000/template.docx",
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
            self.assertEqual(
                [0.15, 0.25, 0.35, 1.0],
                [event["data"]["progress"] for event in events],
            )

    @patch("app.services.llm_service.report_service.post_callback_payload", return_value=True)
    @patch("app.services.llm_service.report_service.extract_text_from_word", return_value="模板大纲文本")
    @patch("app.services.llm_service.report_service.run_anythingllm_rag", return_value=None)
    @patch("app.services.llm_service.report_service.prepare_upload_files")
    @patch("app.services.llm_service.report_service.normalize_file_for_llm", side_effect=lambda path: path)
    @patch("app.services.llm_service.report_service.download_to_temp_file")
    def test_run_report_task_keeps_empty_rag_result_success_and_logs_quality_signal(
        self,
        mock_download,
        _mock_normalize,
        mock_prepare,
        _mock_rag,
        _mock_extract,
        mock_callback,
    ):
        """空模型结果仍成功，但必须留下不进入公开载荷的结构化质量信号。"""

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
                        "filePathList": ["http://127.0.0.1:8000/sample.txt"],
                        "templateDesc": "模板",
                        "templateOutline": "http://127.0.0.1:8000/template.docx",
                        "requirement": "要求",
                    }
                ],
            }
            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task_service.create_report_task(132, request_payload)

            from app.services.llm_service.report_service import run_report_task

            with self.assertLogs(
                "app.services.llm_service.report_service",
                level="WARNING",
            ) as captured:
                run_report_task(
                    task_service=task_service,
                    progress_hub=LLMProgressHub(),
                    request_payload=request_payload,
                    download_root=tmp,
                    callback_url="http://127.0.0.1:9000/llm/callback",
                    callback_timeout=5,
                )

            task = task_service.get_task("report", "132")

        self.assertIsNotNone(task)
        assert task is not None
        self.assertEqual("1", task["status"])
        self.assertEqual("生成成功", task["result_payload"]["msg"])
        self.assertEqual(
            '<div class="report-content"><pre></pre></div>',
            task["result_payload"]["data"]["details"],
        )
        self.assertTrue(
            any("empty_rag_result=true" in message for message in captured.output)
        )
        callback_payload = mock_callback.call_args.args[1]
        self.assertEqual("1", callback_payload["data"]["status"])
        self.assertNotIn("empty_rag_result", callback_payload)

    @patch("app.services.llm_service.report_service.post_callback_payload", return_value=True)
    @patch("app.services.llm_service.report_service.extract_text_from_word", return_value="Word模板中的大纲")
    @patch("app.services.llm_service.report_service.build_report_prompt", return_value="report prompt")
    @patch("app.services.llm_service.report_service.run_anythingllm_rag", return_value="<section>报告内容</section>")
    @patch("app.services.llm_service.report_service.prepare_upload_files")
    @patch("app.services.llm_service.report_service.normalize_file_for_llm", side_effect=lambda path: path)
    @patch("app.services.llm_service.report_service.download_to_temp_file")
    def test_run_report_task_uses_extracted_word_template_text_in_prompt(
        self,
        mock_download,
        mock_normalize,
        mock_prepare,
        _mock_rag,
        mock_build_prompt,
        _mock_extract,
        _mock_callback,
    ):
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
                        "filePathList": ["http://127.0.0.1:8000/sample.txt"],
                        "templateDesc": "模板",
                        "templateOutline": "http://127.0.0.1:8000/template.docx",
                        "requirement": "要求",
                    }
                ],
            }

            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task_service.create_report_task(132, request_payload)

            from app.services.llm_service.report_service import run_report_task

            run_report_task(
                task_service=task_service,
                progress_hub=LLMProgressHub(),
                request_payload=request_payload,
                download_root=tmp,
                callback_url="http://127.0.0.1:9000/llm/callback",
                callback_timeout=5,
            )

        self.assertEqual(mock_normalize.call_count, 1)
        prompt_params = mock_build_prompt.call_args.args[0]
        self.assertEqual(prompt_params["templateOutline"], "Word模板中的大纲")

    @patch("app.services.llm_service.report_service.run_anythingllm_rag")
    @patch("app.services.llm_service.report_service.extract_text_from_word", return_value="")
    @patch("app.services.llm_service.report_service.prepare_upload_files")
    @patch("app.services.llm_service.report_service.normalize_file_for_llm", side_effect=lambda path: path)
    @patch("app.services.llm_service.report_service.download_to_temp_file")
    def test_run_report_task_fails_when_word_template_has_no_text(
        self,
        mock_download,
        _mock_normalize,
        mock_prepare,
        _mock_extract,
        mock_rag,
    ):
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
                        "filePathList": ["http://127.0.0.1:8000/sample.txt"],
                        "templateDesc": "模板",
                        "templateOutline": "http://127.0.0.1:8000/template.docx",
                        "requirement": "要求",
                    }
                ],
            }

            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task_service.create_report_task(132, request_payload)

            from app.services.llm_service.report_service import run_report_task

            run_report_task(
                task_service=task_service,
                progress_hub=LLMProgressHub(),
                request_payload=request_payload,
                download_root=tmp,
                callback_url="",
                callback_timeout=5,
            )
            task = task_service.get_task("report", "132")

        self.assertEqual(task["status"], "2")
        self.assertEqual(task["result_payload"]["msg"], "生成失败")
        mock_rag.assert_not_called()
