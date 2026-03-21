import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.services.core import LLMProgressHub
from app.services.llm_service.analysis_service import build_file_callback_payload, map_analysis_result
from app.prompts.llm_prompts import build_file_analysis_prompt
from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir


class LLMAnalysisServiceTests(unittest.TestCase):
    def test_map_analysis_result_keeps_translation_fields_blank(self):
        result = map_analysis_result(
            parsed_result={"summary": "摘要", "language": "中文", "score": 3.6},
            request_params={
                "fileName": "demo.pdf",
                "country": [{"key": "02", "value": "美国"}],
                "channel": [{"key": "01", "value": "装发"}],
                "maturity": [{"key": "02", "value": "阶段成果"}],
                "format": [{"key": "03", "value": "文档类"}],
                "architectureList": [{"id": 10, "name": "测试"}],
            },
        )
        self.assertEqual(result["fileDataItem"]["documentTranslationOne"], "")
        self.assertEqual(result["fileDataItem"]["documentTranslationTwo"], "")

    def test_build_file_callback_payload_uses_fixed_success_message(self):
        payload = build_file_callback_payload("demo.pdf", {"summary": "摘要"}, status="2")
        self.assertEqual(payload["msg"], "解析成功")

    def test_map_analysis_result_supports_current_chinese_object_response(self):
        request_params = {
            "fileName": "sample.txt",
            "country": [{"key": "02", "value": "美国"}],
            "channel": [{"key": "02", "value": "装发"}],
            "maturity": [{"key": "02", "value": "阶段成果"}],
            "format": [{"key": "03", "value": "文档类"}],
            "architectureList": [{"id": 1768464916588441, "name": "测试"}],
        }
        parsed_result = {
            "领域体系": {
                "id": 1768464916588441,
                "name": "测试",
            },
            "国家": {"value": "美国", "key": "02"},
            "渠道": {"value": "装发", "key": "02"},
            "成熟度": {"value": "阶段成果", "key": "02"},
            "格式": {"value": "文档类", "key": "03"},
            "资料年代": "2025-08-25",
            "摘要": "达里尔·考德尔正式担任美国海军作战部长。",
            "原文链接": "https://www.navy.mil/example",
            "语种": "中英双语",
            "文件概述": "美国海军人事任命新闻。",
        }

        result = map_analysis_result(parsed_result, request_params, original_text="demo text")

        self.assertEqual(result["country"], "美国")
        self.assertEqual(result["channel"], "装发")
        self.assertEqual(result["maturity"], "阶段成果")
        self.assertEqual(result["format"], "文档类")
        self.assertEqual(result["architectureId"], 1768464916588441)
        self.assertEqual(result["fileDataItem"]["dataTime"], "2025-08-25")
        self.assertEqual(result["fileDataItem"]["summary"], "达里尔·考德尔正式担任美国海军作战部长。")
        self.assertEqual(result["fileDataItem"]["originalLink"], "https://www.navy.mil/example")
        self.assertEqual(result["fileDataItem"]["language"], "中英双语")
        self.assertEqual(result["fileDataItem"]["documentOverview"], "美国海军人事任命新闻。")

    def test_map_analysis_result_rejects_out_of_range_country(self):
        request_params = {
            "fileName": "demo.txt",
            "country": [{"key": "02", "value": "美国"}],
        }

        result = map_analysis_result({"country": "俄罗斯"}, request_params)

        self.assertEqual(result["country"], "")

    def test_map_analysis_result_uses_default_ranges_when_request_missing(self):
        result = map_analysis_result(
            {"国家": {"value": "美国", "key": "02"}},
            {"fileName": "demo.txt"},
        )

        self.assertEqual(result["country"], "美国")

    def test_build_file_analysis_prompt_requires_protocol_schema(self):
        prompt = build_file_analysis_prompt(
            {
                "architectureList": [{"id": 1, "name": "测试"}],
                "country": [{"key": "02", "value": "美国"}],
                "channel": [{"key": "02", "value": "装发"}],
                "maturity": [{"key": "02", "value": "阶段成果"}],
                "format": [{"key": "03", "value": "文档类"}],
            }
        )

        self.assertIn('"country"', prompt)
        self.assertIn('"architectureId"', prompt)
        self.assertIn('"fileDataItem"', prompt)
        self.assertIn("不要直接原样返回候选对象", prompt)

    def test_build_file_analysis_prompt_uses_default_ranges_when_missing(self):
        prompt = build_file_analysis_prompt({"fileName": "demo.txt"})
        self.assertIn('"音频类"', prompt)
        self.assertIn('"文档类"', prompt)
        self.assertIn('"图片类"', prompt)
        self.assertIn('"军事基地"', prompt)

    def test_build_file_analysis_prompt_uses_explicit_ranges_over_defaults(self):
        prompt = build_file_analysis_prompt(
            {
                "fileName": "demo.txt",
                "country": [{"key": "99", "value": "德国"}],
                "format": [{"key": "88", "value": "数据库类"}],
            }
        )
        self.assertIn('"德国"', prompt)
        self.assertIn('"数据库类"', prompt)
        self.assertNotIn('"美国"', prompt)
        self.assertNotIn('"文档类"', prompt)

    def test_build_file_analysis_prompt_includes_architecture_classification_rules(self):
        prompt = build_file_analysis_prompt({"fileName": "demo.txt"})

        self.assertIn("军事基地：", prompt)
        self.assertIn("作战指挥：", prompt)
        self.assertIn("组织机构", prompt)
        self.assertIn("必须从领域体系候选中选择一个最可能的节点", prompt)
        self.assertIn("只有当文档内容与所有除'其他'项以外的候选领域都明显无关时才输出 1", prompt)

    def test_map_analysis_result_falls_back_to_original_text_for_obvious_fields(self):
        original_text = (
            "标题\n"
            "达里尔·考德尔正式担任美国海军作战部长\n\n"
            "内容\n"
            "【美国海军网2025年8月25日报道】8月25日，达里尔·考德尔海军上将在美国华盛顿特区正式就任第34任海军作战部长。\n\n"
            "原文链接\n"
            "https://www.navy.mil/example\n\n"
            "原文\n"
            "Caudle Takes Helm as 34th Chief of Naval Operations\n"
            "25 August 2025\n"
        )
        request_params = {
            "fileName": "sample.txt",
            "country": [{"key": "02", "value": "美国"}],
            "channel": [{"key": "02", "value": "装发"}],
            "maturity": [{"key": "02", "value": "阶段成果"}],
            "format": [{"key": "03", "value": "文档类"}],
            "architectureList": [{"id": 1768464916588441, "name": "测试"}],
        }

        result = map_analysis_result({}, request_params, original_text=original_text)

        self.assertEqual(result["country"], "美国")
        self.assertEqual(result["fileDataItem"]["dataTime"], "2025-08-25")
        self.assertEqual(result["fileDataItem"]["source"], "美国海军网")
        self.assertEqual(result["fileDataItem"]["originalLink"], "https://www.navy.mil/example")
        self.assertEqual(result["fileDataItem"]["language"], "中英双语")
        self.assertEqual(result["fileDataItem"]["summary"], "达里尔·考德尔正式担任美国海军作战部长")
        self.assertEqual(result["fileDataItem"]["documentOverview"], "达里尔·考德尔正式担任美国海军作战部长")

    def test_map_analysis_result_matches_architecture_by_path_name(self):
        request_params = {
            "fileName": "sample.txt",
            "architectureList": [
                {"id": 105, "name": "作战指挥", "pathName": "作战指挥"},
                {"id": 10502, "name": "组织机构", "pathName": "作战指挥/组织机构"},
            ],
        }

        result = map_analysis_result({"领域体系名称": "作战指挥/组织机构"}, request_params)

        self.assertEqual(result["architectureId"], 10502)

    def test_map_analysis_result_matches_architecture_by_nested_name(self):
        request_params = {
            "fileName": "sample.txt",
            "architectureList": [
                {"id": 105, "name": "作战指挥", "pathName": "作战指挥"},
                {"id": 10502, "name": "组织机构", "pathName": "作战指挥/组织机构"},
            ],
        }

        result = map_analysis_result({"领域体系": {"name": "组织机构"}}, request_params)

        self.assertEqual(result["architectureId"], 10502)

    @patch("app.services.llm_analysis_service.enrich_with_translations", side_effect=lambda mapped_result, *_args, **_kwargs: mapped_result)
    @patch("app.services.llm_analysis_service.post_callback_payload", return_value=True)
    @patch("app.services.llm_analysis_service.pipeline_process_file_with_rag", return_value='{"summary":"摘要","language":"中文","score":3.6}')
    @patch("app.services.llm_analysis_service.normalize_file_for_llm")
    @patch("app.services.llm_analysis_service.download_to_temp_file")
    def test_run_file_analysis_task_normalizes_mhtml_before_rag(
        self,
        mock_download,
        mock_normalize,
        _mock_pipeline,
        _mock_callback,
        _mock_enrich,
    ):
        with workspace_tempdir() as tmp:
            sample = Path(tmp) / "sample.mhtml"
            sample.write_text("mhtml", encoding="utf-8")
            normalized = Path(tmp) / "sample.mhtml.normalized.md"
            normalized.write_text("标题\nHello MHTML", encoding="utf-8")
            mock_download.return_value = str(sample)
            mock_normalize.return_value = str(normalized)

            request_payload = {
                "businessType": "file",
                "params": [
                    {
                        "fileName": "sample.mhtml",
                        "filePath": "http://127.0.0.1:8000/sample.mhtml",
                        "enableFullTranslation": False,
                        "country": [],
                        "channel": [],
                        "maturity": [],
                        "format": [],
                        "architectureList": [],
                    }
                ],
            }

            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task_service.create_file_task("sample.mhtml", request_payload)
            hub = LLMProgressHub()

            from app.services.llm_service.analysis_service import run_file_analysis_task

            run_file_analysis_task(
                task_service=task_service,
                kb_service=Mock(),
                progress_hub=hub,
                request_payload=request_payload,
                download_root=tmp,
                callback_url="http://127.0.0.1:9000/llm/callback",
                callback_timeout=5,
            )

        mock_normalize.assert_called_once_with(str(sample))
        self.assertEqual(_mock_pipeline.call_args.kwargs["file_path"], str(normalized))

    @patch("app.services.llm_analysis_service.enrich_with_translations", side_effect=lambda mapped_result, *_args, **_kwargs: mapped_result)
    @patch("app.services.llm_analysis_service.post_callback_payload", return_value=True)
    @patch("app.services.llm_analysis_service.pipeline_process_file_with_rag", return_value='{"summary":"摘要","language":"中文","score":3.6}')
    @patch("app.services.llm_analysis_service.normalize_file_for_llm", side_effect=RuntimeError("boom"))
    @patch("app.services.llm_analysis_service.download_to_temp_file")
    def test_run_file_analysis_task_falls_back_to_original_file_when_mhtml_normalization_fails(
        self,
        mock_download,
        _mock_normalize,
        _mock_pipeline,
        _mock_callback,
        _mock_enrich,
    ):
        with workspace_tempdir() as tmp:
            sample = Path(tmp) / "sample.mhtml"
            sample.write_text("mhtml", encoding="utf-8")
            mock_download.return_value = str(sample)

            request_payload = {
                "businessType": "file",
                "params": [
                    {
                        "fileName": "sample.mhtml",
                        "filePath": "http://127.0.0.1:8000/sample.mhtml",
                        "enableFullTranslation": False,
                        "country": [],
                        "channel": [],
                        "maturity": [],
                        "format": [],
                        "architectureList": [],
                    }
                ],
            }

            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task_service.create_file_task("sample.mhtml", request_payload)
            hub = LLMProgressHub()

            from app.services.llm_service.analysis_service import run_file_analysis_task

            run_file_analysis_task(
                task_service=task_service,
                kb_service=Mock(),
                progress_hub=hub,
                request_payload=request_payload,
                download_root=tmp,
                callback_url="http://127.0.0.1:9000/llm/callback",
                callback_timeout=5,
            )

        self.assertEqual(_mock_pipeline.call_args.kwargs["file_path"], str(sample))

    @patch("app.services.llm_analysis_service.post_callback_payload", return_value=True)
    @patch("app.services.llm_analysis_service.pipeline_process_file_with_rag", return_value='{"summary":"摘要","language":"中文","score":3.6}')
    @patch("app.services.llm_analysis_service.enrich_with_translations", side_effect=lambda mapped_result, *_args, **_kwargs: mapped_result)
    @patch("app.services.llm_analysis_service.download_to_temp_file")
    def test_run_file_analysis_task_marks_success(self, mock_download, _mock_enrich, _mock_pipeline, _mock_callback):
        with workspace_tempdir() as tmp:
            sample = Path(tmp) / "sample.txt"
            sample.write_text("sample", encoding="utf-8")
            mock_download.return_value = str(sample)

            request_payload = {
                "businessType": "file",
                "params": [
                    {
                        "fileName": "sample.txt",
                        "filePath": "http://127.0.0.1:8000/sample.txt",
                        "enableFullTranslation": False,
                        "country": [],
                        "channel": [],
                        "maturity": [],
                        "format": [],
                        "architectureList": [],
                    }
                ],
            }

            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task_service.create_file_task("sample.txt", request_payload)
            hub = LLMProgressHub()
            events = []
            hub.subscribe("file", "sample.txt", events.append)

            from app.services.llm_service.analysis_service import run_file_analysis_task

            run_file_analysis_task(
                task_service=task_service,
                kb_service=Mock(),
                progress_hub=hub,
                request_payload=request_payload,
                download_root=tmp,
                callback_url="http://127.0.0.1:9000/llm/callback",
                callback_timeout=5,
            )

            task = task_service.get_task("file", "sample.txt")
            self.assertIsNotNone(task)
            self.assertEqual(task["status"], "2")
            self.assertEqual(task["callback_status"], "success")
            self.assertEqual(task["result_payload"]["msg"], "解析成功")
            self.assertEqual(events[-1]["data"]["progress"], 1.0)

    @patch("app.services.llm_analysis_service.run_file_analysis_task")
    def test_run_file_analysis_batch_processes_files_in_order(self, mock_run_single):
        with workspace_tempdir() as tmp:
            request_payload = {
                "businessType": "file",
                "params": [
                    {
                        "fileName": "a.txt",
                        "filePath": "http://127.0.0.1:8000/a.txt",
                    },
                    {
                        "fileName": "b.txt",
                        "filePath": "http://127.0.0.1:8000/b.txt",
                    },
                ],
            }

            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task_service.create_file_task("a.txt", {"businessType": "file", "params": [request_payload["params"][0]]}, status="1")
            task_service.create_file_task("b.txt", {"businessType": "file", "params": [request_payload["params"][1]]}, status="0")
            hub = LLMProgressHub()
            transitions = []

            def capture_transition(*, task_service, request_payload, **kwargs):
                current = request_payload["params"][0]["fileName"]
                status_a = task_service.get_task("file", "a.txt")["status"]
                status_b = task_service.get_task("file", "b.txt")["status"]
                transitions.append((current, status_a, status_b))
                task_service.mark_business_result("file", current, {"ok": True}, status="2", message="完成")

            mock_run_single.side_effect = capture_transition

            from app.services.llm_service.analysis_service import run_file_analysis_batch_task

            run_file_analysis_batch_task(
                task_service=task_service,
                kb_service=Mock(),
                progress_hub=hub,
                request_payload=request_payload,
                download_root=tmp,
                callback_url="http://127.0.0.1:9000/llm/callback",
                callback_timeout=5,
            )

            self.assertEqual(
                transitions,
                [
                    ("a.txt", "1", "0"),
                    ("b.txt", "2", "1"),
                ],
            )
