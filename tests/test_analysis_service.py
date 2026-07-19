import unittest
import json
from pathlib import Path
from unittest.mock import Mock, patch

from app.ports import (
    KnowledgeIndexDocumentReleasedError,
    KnowledgeIndexRetentionRequiredError,
    RagPromptKind,
    RagSource,
)
from app.services.core.progress_hub import LLMProgressHub
from app.services.llm_service.analysis_service import (
    DataStandardParentContractError,
    DEFAULT_ARCHITECTURE_OPTIONS,
    _first_data_standard_leaf_id,
    _resolve_analysis_architecture_id,
    build_file_callback_payload,
    map_analysis_result,
    resolve_storage_architecture_id,
)
from app.services.core.prompts import build_file_analysis_prompt
from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir
from tests.fakes.knowledge_index import (
    FakeKnowledgeIndexFactory,
    FakeKnowledgeIndexPort,
)
from tests.fakes.rag import FakeDocumentRagFactory, FakeRagOutcome


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

    def test_map_analysis_result_rejects_out_of_range_channel_maturity_format(self):
        request_params = {
            "fileName": "demo.txt",
            "channel": [{"key": "02", "value": "装发"}],
            "maturity": [{"key": "02", "value": "阶段成果"}],
            "security": [{"key": "02", "value": "公开"}],
            "format": [{"key": "03", "value": "文档类"}],
        }

        result = map_analysis_result(
            {
                "channel": "未知渠道",
                "maturity": "未知成熟度",
                "security": "绝密",
                "format": "未知格式",
            },
            request_params,
        )

        self.assertEqual(result["channel"], "")
        self.assertEqual(result["maturity"], "")
        self.assertEqual(result["security"], "公开")
        self.assertEqual(result["format"], "")

    def test_map_analysis_result_keeps_channel_empty_without_request_candidates(self):
        result = map_analysis_result(
            {"channel": "装发"},
            {"fileName": "demo.txt", "channel": []},
        )

        self.assertEqual(result["channel"], "")

    def test_map_analysis_result_resolves_security_from_candidate_range(self):
        request_params = {
            "fileName": "demo.txt",
            "security": [
                {"key": "02", "value": "公开"},
                {"key": "03", "value": "秘密"},
            ],
        }

        result = map_analysis_result({"security": "秘密"}, request_params)

        self.assertEqual(result["security"], "秘密")

    def test_map_analysis_result_infers_security_from_opening_text(self):
        request_params = {
            "fileName": "demo.txt",
            "security": [
                {"key": "02", "value": "公开"},
                {"key": "03", "value": "秘密"},
            ],
        }
        original_text = "密级：秘密\n文件编号：ABC-001\n正文内容。"

        result = map_analysis_result({}, request_params, original_text=original_text)

        self.assertEqual(result["security"], "秘密")

    def test_map_analysis_result_defaults_security_to_public_when_missing(self):
        result = map_analysis_result({}, {"fileName": "demo.txt"}, original_text="普通正文内容。")

        self.assertEqual(result["security"], "公开")

    def test_map_analysis_result_matches_options_after_normalization(self):
        request_params = {
            "fileName": "demo.txt",
            "channel": [{"key": "02", "value": "装发"}],
            "maturity": [{"key": "02", "value": "阶段成果"}],
            "format": [{"key": "03", "value": "文档类"}],
        }

        result = map_analysis_result(
            {
                "channel": "  装 发  ",
                "maturity": "０２",
                "format": " 文档类 ",
            },
            request_params,
        )

        self.assertEqual(result["channel"], "装发")
        self.assertEqual(result["maturity"], "阶段成果")
        self.assertEqual(result["format"], "文档类")

    def test_map_analysis_result_forces_data_format_to_resolved_format(self):
        request_params = {
            "fileName": "demo.txt",
            "format": [{"key": "03", "value": "文档类"}],
        }

        result = map_analysis_result(
            {
                "format": " 文档类 ",
                "fileDataItem": {
                    "dataFormat": "PDF报告",
                },
            },
            request_params,
        )

        self.assertEqual(result["format"], "文档类")
        self.assertEqual(result["fileDataItem"]["dataFormat"], "文档类")

    def test_map_analysis_result_uses_data_format_as_format_fallback(self):
        request_params = {
            "fileName": "demo.txt",
            "format": [{"key": "03", "value": "文档类"}],
        }

        result = map_analysis_result(
            {
                "fileDataItem": {
                    "dataFormat": "03",
                },
            },
            request_params,
        )

        self.assertEqual(result["format"], "文档类")
        self.assertEqual(result["fileDataItem"]["dataFormat"], "文档类")

    def test_map_analysis_result_falls_back_architecture_to_one_when_not_matched(self):
        request_params = {
            "fileName": "sample.txt",
            "architectureList": [
                {"id": 105, "name": "作战指挥", "pathName": "作战指挥"},
                {"id": 10502, "name": "组织机构", "pathName": "作战指挥/组织机构"},
            ],
        }

        result = map_analysis_result({"architectureId": 999999}, request_params)

        self.assertEqual(result["architectureId"], 1)

    def test_map_analysis_result_routes_gjb_content_to_general_requirement_leaf(self):
        request_params = {
            "fileName": "sample.txt",
            "originalFileName": "GJB 9001C-2017.pdf",
            "architectureList": [
                {
                    "id": 201,
                    "name": "条令条例",
                    "parentId": None,
                    "path": "201",
                    "pathName": "条令条例",
                    "remark": "军事条令、条例、制度类文件。",
                },
                {
                    "id": 202,
                    "name": "数据标准",
                    "parentId": None,
                    "path": "202",
                    "pathName": "数据标准",
                    "remark": "国家军用标准、GJB、技术标准和数据规范。",
                },
                {
                    "id": 203,
                    "name": "军用软件标准",
                    "parentId": 202,
                    "path": "202/203",
                    "pathName": "数据标准/军用软件标准",
                    "remark": "军用软件相关标准。",
                },
                {
                    "id": 204,
                    "name": "建模与仿真标准",
                    "parentId": 202,
                    "path": "202/204",
                    "pathName": "数据标准/建模与仿真标准",
                    "remark": "建模与仿真相关标准。",
                },
                {
                    "id": 205,
                    "name": "通用要求标准",
                    "parentId": 202,
                    "path": "202/205",
                    "pathName": "数据标准/通用要求标准",
                    "remark": "质量管理及综合性标准要求。",
                },
            ],
        }

        result = map_analysis_result(
            {"architectureId": 201},
            request_params,
            original_text="本文档为 GJB 9001C-2017 质量管理体系要求，属于国家军用标准。",
        )

        # GJB 兜底必须跳过父节点并定向选择“通用要求”，不再依赖请求顺序。
        self.assertEqual(result["architectureId"], 205)

    def test_resolve_architecture_keeps_normal_parent_but_rejects_data_standard_parent(self):
        """普通父节点可用，数据标准父节点必须转入叶子兜底路径。"""
        request_params = {
            "architectureList": [
                {"id": 211, "name": "普通父节点", "parentId": None},
                {"id": 212, "name": "普通子节点", "parentId": 211},
                {"id": 213, "name": "数据标准", "parentId": None},
                {"id": 214, "name": "军用软件标准", "parentId": 213},
                {"id": 215, "name": "通用要求标准", "parentId": 213},
            ]
        }

        self.assertEqual(
            _resolve_analysis_architecture_id({"architectureId": 211}, request_params),
            211,
        )
        self.assertEqual(_first_data_standard_leaf_id(request_params["architectureList"]), 215)
        with self.assertRaises(DataStandardParentContractError):
            _resolve_analysis_architecture_id({"architectureId": 213}, request_params)

    def test_map_analysis_result_returns_standard_fields_when_architecture_matches_standard_range(self):
        request_params = {
            "fileName": "sample.txt",
            "architectureList": [
                {"id": 202, "name": "数据标准", "path": "202", "pathName": "数据标准"},
            ],
            "architectureStandardList": [
                {"id": 202, "name": "数据标准", "path": "202", "pathName": "数据标准"},
            ],
        }
        parsed_result = {
            "architectureId": 202,
            "fileDataItem": {
                "militaryName": "GJB 9001C-2017 质量管理体系要求",
                "num": "GJB 9001C-2017",
                "startTime": "2017年5月18日",
                "implTime": "2017/7/1",
                "approvalDept": "中央军委装备发展部",
            },
        }

        result = map_analysis_result(parsed_result, request_params)

        self.assertEqual(result["fileDataItem"]["militaryName"], "GJB 9001C-2017 质量管理体系要求")
        self.assertEqual(result["fileDataItem"]["num"], "GJB 9001C-2017")
        self.assertEqual(result["fileDataItem"]["startTime"], "2017-05-18")
        self.assertEqual(result["fileDataItem"]["implTime"], "2017-07-01")
        self.assertEqual(result["fileDataItem"]["approvalDept"], "中央军委装备发展部")

    def test_map_analysis_result_returns_standard_fields_for_descendant_architecture(self):
        request_params = {
            "fileName": "sample.txt",
            "architectureList": [
                {"id": 202, "name": "数据标准", "path": "202", "pathName": "数据标准"},
                {"id": 203, "name": "国家军用标准", "parentId": 202, "path": "202/203", "pathName": "数据标准/国家军用标准"},
            ],
            "architectureStandardList": [
                {"id": 202, "name": "数据标准", "path": "202", "pathName": "数据标准"},
            ],
        }
        parsed_result = {
            "architectureId": 203,
            "国军标名称": "GJB 1234-2020 测试标准",
            "编号": "GJB 1234-2020",
            "发布时间": "2020-03-04",
            "实施时间": "2020.05.06",
            "批准部门": "批准部门",
        }

        result = map_analysis_result(parsed_result, request_params)

        self.assertEqual(result["architectureId"], 203)
        self.assertEqual(result["fileDataItem"]["militaryName"], "GJB 1234-2020 测试标准")
        self.assertEqual(result["fileDataItem"]["num"], "GJB 1234-2020")
        self.assertEqual(result["fileDataItem"]["startTime"], "2020-03-04")
        self.assertEqual(result["fileDataItem"]["implTime"], "2020-05-06")
        self.assertEqual(result["fileDataItem"]["approvalDept"], "批准部门")

    def test_map_analysis_result_uses_resolved_architecture_for_child_only_standard_range(self):
        request_params = {
            "fileName": "sample.txt",
            "originalFileName": "GJB 9001C-2017.pdf",
            "architectureList": [
                {"id": 202, "name": "数据标准", "path": "202", "pathName": "数据标准"},
                {
                    "id": 203,
                    "name": "通用要求标准",
                    "parentId": 202,
                    "path": "202/203",
                    "pathName": "数据标准/通用要求标准",
                },
                {"id": 301, "name": "水面装备", "path": "301", "pathName": "装备目标/水面装备"},
            ],
            "architectureStandardList": [
                {
                    "id": 203,
                    "name": "通用要求标准",
                    "parentId": 202,
                    "path": "202/203",
                    "pathName": "数据标准/通用要求标准",
                },
            ],
        }
        original_text = (
            "GJB 9001C-2017 质量管理体系要求\n"
            "国军标名称：GJB 9001C-2017 质量管理体系要求\n"
            "编号：GJB 9001C-2017\n"
            "发布时间：2017年5月18日\n"
            "实施时间：2017年7月1日\n"
            "批准部门：中央军委装备发展部\n"
        )

        result = map_analysis_result(
            {"architectureId": 203},
            request_params,
            original_text=original_text,
            resolved_architecture_id=203,
        )

        self.assertEqual(result["architectureId"], 203)
        self.assertEqual(result["fileDataItem"]["militaryName"], "GJB 9001C-2017 质量管理体系要求")
        self.assertEqual(result["fileDataItem"]["num"], "GJB 9001C-2017")
        self.assertEqual(result["fileDataItem"]["startTime"], "2017-05-18")
        self.assertEqual(result["fileDataItem"]["implTime"], "2017-07-01")
        self.assertEqual(result["fileDataItem"]["approvalDept"], "中央军委装备发展部")

    def test_map_analysis_result_omits_standard_fields_when_architecture_not_in_standard_range(self):
        request_params = {
            "fileName": "sample.txt",
            "architectureList": [
                {"id": 201, "name": "条令条例", "path": "201", "pathName": "条令条例"},
                {"id": 202, "name": "数据标准", "path": "202", "pathName": "数据标准"},
            ],
            "architectureStandardList": [
                {"id": 202, "name": "数据标准", "path": "202", "pathName": "数据标准"},
            ],
        }
        parsed_result = {
            "architectureId": 201,
            "fileDataItem": {
                "militaryName": "不应返回",
                "num": "GJB 0000",
                "startTime": "2020-01-01",
                "implTime": "2020-02-01",
                "approvalDept": "批准部门",
            },
        }

        result = map_analysis_result(parsed_result, request_params)

        for field in ("militaryName", "num", "startTime", "implTime", "approvalDept"):
            self.assertNotIn(field, result["fileDataItem"])

    def test_map_analysis_result_returns_blank_standard_fields_when_missing(self):
        request_params = {
            "fileName": "sample.txt",
            "architectureList": [{"id": 202, "name": "数据标准"}],
            "architectureStandardList": [{"id": 202, "name": "数据标准"}],
        }

        result = map_analysis_result({"architectureId": 202}, request_params)

        for field in ("militaryName", "num", "startTime", "implTime", "approvalDept"):
            self.assertIn(field, result["fileDataItem"])
            self.assertEqual(result["fileDataItem"][field], "")

    def test_map_analysis_result_falls_back_to_original_text_for_standard_fields(self):
        request_params = {
            "fileName": "sample.txt",
            "architectureList": [{"id": 202, "name": "数据标准"}],
            "architectureStandardList": [{"id": 202, "name": "数据标准"}],
        }
        original_text = (
            "GJB 9001C-2017 质量管理体系要求\n"
            "国军标名称：GJB 9001C-2017 质量管理体系要求\n"
            "编号：GJB 9001C-2017\n"
            "发布时间：2017年5月18日\n"
            "实施时间：2017年7月1日\n"
            "批准部门：中央军委装备发展部\n"
        )

        result = map_analysis_result({"architectureId": 202}, request_params, original_text=original_text)

        self.assertEqual(result["fileDataItem"]["militaryName"], "GJB 9001C-2017 质量管理体系要求")
        self.assertEqual(result["fileDataItem"]["num"], "GJB 9001C-2017")
        self.assertEqual(result["fileDataItem"]["startTime"], "2017-05-18")
        self.assertEqual(result["fileDataItem"]["implTime"], "2017-07-01")
        self.assertEqual(result["fileDataItem"]["approvalDept"], "中央军委装备发展部")

    def test_map_analysis_result_uses_only_architecture_candidate_when_single_node(self):
        request_params = {
            "fileName": "sample.txt",
            "architectureList": [
                {
                    "id": 1768464916588441,
                    "name": "测试",
                    "parentId": None,
                    "path": "1768464916588441",
                    "pathName": "测试",
                }
            ],
        }

        result = map_analysis_result({"summary": "摘要"}, request_params)

        self.assertEqual(result["architectureId"], 1768464916588441)

    def test_map_analysis_result_normalizes_score_to_protocol_discrete_values(self):
        request_params = {
            "fileName": "sample.txt",
            "architectureList": [{"id": 10, "name": "测试"}],
        }

        result = map_analysis_result(
            {
                "fileDataItem": {
                    "score": "85",
                }
            },
            request_params,
        )

        self.assertEqual(result["fileDataItem"]["score"], 85)

    def test_map_analysis_result_falls_back_score_to_55_when_missing_or_invalid(self):
        request_params = {
            "fileName": "sample.txt",
            "architectureList": [{"id": 10, "name": "测试"}],
        }

        missing = map_analysis_result({}, request_params)
        invalid = map_analysis_result({"score": 80}, request_params)

        self.assertEqual(missing["fileDataItem"]["score"], 55)
        self.assertEqual(invalid["fileDataItem"]["score"], 55)

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
        self.assertIn('"security"', prompt)
        self.assertNotIn('"secrets"', prompt)
        self.assertIn('"architectureId"', prompt)
        self.assertIn('"fileDataItem"', prompt)
        self.assertIn('"originalText"', prompt)
        self.assertIn("不要直接原样返回候选对象", prompt)
        self.assertIn("输出前自检清单", prompt)
        self.assertIn("无法匹配时输出空字符串", prompt)
        self.assertIn("parentId 表示父节点 id", prompt)
        self.assertIn("architectureList 只包含 id, name, parentId, path, pathName", prompt)

    def test_build_file_analysis_prompt_includes_original_file_name_and_architecture_remark(self):
        prompt = build_file_analysis_prompt(
            {
                "fileName": "storage-name.pdf",
                "originalFileName": "GJB 9001C-2017 质量管理体系要求.pdf",
                "architectureList": [
                    {
                        "id": 202,
                        "name": "数据标准",
                        "parentId": None,
                        "path": "202",
                        "pathName": "数据标准",
                        "remark": "国家军用标准、GJB、技术标准和数据规范。",
                    }
                ],
            }
        )

        self.assertIn("originalFileName", prompt)
        self.assertIn("GJB 9001C-2017 质量管理体系要求.pdf", prompt)
        self.assertIn("remark", prompt)
        self.assertIn("国家军用标准、GJB、技术标准和数据规范。", prompt)

    def test_build_file_analysis_prompt_includes_standard_fields_when_standard_range_present(self):
        prompt = build_file_analysis_prompt(
            {
                "fileName": "storage-name.pdf",
                "architectureList": [
                    {"id": 201, "name": "条令条例"},
                    {"id": 202, "name": "数据标准"},
                ],
                "architectureStandardList": [
                    {"id": 202, "name": "数据标准", "remark": "标准化资料。"},
                ],
            }
        )

        self.assertIn('"militaryName"', prompt)
        self.assertIn('"num"', prompt)
        self.assertIn('"startTime"', prompt)
        self.assertIn('"implTime"', prompt)
        self.assertIn('"approvalDept"', prompt)
        self.assertIn("数据标准额外解析范围", prompt)
        self.assertIn("yyyy-MM-dd", prompt)

    def test_build_file_analysis_prompt_omits_standard_fields_when_standard_range_missing(self):
        prompt = build_file_analysis_prompt({"fileName": "demo.txt"})

        self.assertNotIn('"militaryName"', prompt)
        self.assertNotIn('"approvalDept"', prompt)
        self.assertNotIn("数据标准额外解析范围", prompt)

    def test_build_file_analysis_prompt_uses_default_ranges_when_missing(self):
        prompt = build_file_analysis_prompt({"fileName": "demo.txt"})
        channel_options_line = next(
            line for line in prompt.splitlines() if line.startswith("渠道候选:")
        )

        self.assertIn('"音频类"', prompt)
        self.assertIn('"文档类"', prompt)
        self.assertIn('"图片类"', prompt)
        self.assertIn('"军事基地"', prompt)
        self.assertEqual(channel_options_line, "渠道候选: []")
        self.assertIn(
            "当 channel 候选为空时，channel 输出空字符串",
            prompt,
        )

    def test_build_file_analysis_prompt_uses_explicit_ranges_over_defaults(self):
        prompt = build_file_analysis_prompt(
            {
                "fileName": "demo.txt",
                "country": [{"key": "99", "value": "德国"}],
                "channel": [{"key": "98", "value": "公开发布"}],
                "format": [{"key": "88", "value": "数据库类"}],
                "security": [{"key": "03", "value": "秘密"}],
            }
        )
        lines = prompt.splitlines()
        country_options_line = next(line for line in lines if line.startswith("国家候选:"))
        channel_options_line = next(line for line in lines if line.startswith("渠道候选:"))
        format_options_line = next(line for line in lines if line.startswith("格式候选:"))
        security_options_line = next(line for line in lines if line.startswith("密级候选:"))

        self.assertIn('"德国"', country_options_line)
        self.assertNotIn('"美国"', country_options_line)
        self.assertIn('"公开发布"', channel_options_line)
        self.assertNotIn('"装发"', channel_options_line)
        self.assertIn('"数据库类"', format_options_line)
        self.assertNotIn('"文档类"', format_options_line)
        self.assertIn('"秘密"', security_options_line)
        self.assertNotIn('"公开"', security_options_line)

    def test_build_file_analysis_prompt_includes_architecture_classification_rules(self):
        prompt = build_file_analysis_prompt({"fileName": "demo.txt"})

        self.assertIn("领域体系候选:", prompt)
        self.assertIn("密级候选:", prompt)
        self.assertIn('"name": "军事基地"', prompt)
        self.assertIn('"name": "作战指挥"', prompt)
        self.assertIn('"pathName": "作战指挥/组织机构"', prompt)
        self.assertIn("architectureId 只能输出候选 architectureList 中的叶子 id 数字", prompt)
        self.assertIn("fileDataItem.dataFormat 必须与顶层 format 完全一致", prompt)
        self.assertIn("无法匹配时输出空字符串", prompt)
        self.assertIn("当 architectureList 只有一个节点时", prompt)
        self.assertIn("分类到最底层的叶子节点", prompt)
        self.assertIn("不得默认选择「战技指标」", prompt)
        self.assertIn("score 必须且只能输出以下 5 个整数值", prompt)
        self.assertIn("候选包含“公开”则输出“公开”", prompt)
        self.assertIn("由至少 10 个关键词构成", prompt)
        self.assertIn("GJB", prompt)
        self.assertIn("数据标准", prompt)

    def test_default_architecture_options_use_current_protocol_shape(self):
        for item in DEFAULT_ARCHITECTURE_OPTIONS:
            self.assertIn("parentId", item)
            self.assertIn("remark", item)
            self.assertNotIn("level", item)
            self.assertNotIn("sort", item)

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

    def test_resolve_storage_architecture_id_routes_weaponry_detail_categories_to_parent(self):
        for index, suffix in enumerate(("基础数据", "战技指标", "运用数据", "效能数据"), start=1):
            architecture_list = [
                {"id": 680, "name": "CVN68", "parentId": 60, "path": "60/680"},
                {
                    "id": 6800 + index,
                    "name": f"CVN68-{suffix}",
                    "parentId": 680,
                    "path": f"60/680/{6800 + index}",
                },
            ]

            self.assertEqual(
                resolve_storage_architecture_id(6800 + index, architecture_list),
                680,
            )

    def test_resolve_storage_architecture_id_supports_hyphens_in_weaponry_name(self):
        architecture_list = [
            {"id": 35, "name": "F-35", "parentId": 3, "path": "3/35"},
            {"id": 351, "name": "F-35-战技指标", "parentId": 35, "path": "3/35/351"},
        ]

        self.assertEqual(resolve_storage_architecture_id(351, architecture_list), 35)

    def test_resolve_storage_architecture_id_uses_parent_id_when_parent_node_is_not_in_range(self):
        architecture_list = [
            {"id": 6801, "name": "CVN68-基础数据", "parentId": 680, "path": "60/680/6801"},
        ]

        self.assertEqual(resolve_storage_architecture_id(6801, architecture_list), 680)

    def test_resolve_storage_architecture_id_keeps_original_for_non_matching_category(self):
        architecture_list = [
            {"id": 680, "name": "CVN68", "parentId": 60},
            {"id": 6801, "name": "CVN68-基础数据库", "parentId": 680},
        ]

        self.assertEqual(resolve_storage_architecture_id(6801, architecture_list), 6801)

    def test_resolve_storage_architecture_id_keeps_original_without_reliable_weaponry_parent(self):
        missing_parent_id = [
            {"id": 6801, "name": "CVN68-基础数据", "parentId": None},
        ]
        mismatched_parent = [
            {"id": 999, "name": "其他装备", "parentId": None},
            {"id": 6801, "name": "CVN68-基础数据", "parentId": 999, "path": "999/6801"},
        ]

        self.assertEqual(resolve_storage_architecture_id(6801, missing_parent_id), 6801)
        self.assertEqual(resolve_storage_architecture_id(6801, mismatched_parent), 6801)

    @patch("app.services.llm_service.analysis_service.run_file_analysis_task")
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
            filename_constraint_modes = []

            def capture_transition(*, task_service, request_payload, **kwargs):
                current = request_payload["params"][0]["fileName"]
                status_a = task_service.get_task("file", "a.txt")["status"]
                status_b = task_service.get_task("file", "b.txt")["status"]
                transitions.append((current, status_a, status_b))
                filename_constraint_modes.append(
                    kwargs["analysis_filename_constraint_mode"]
                )
                task_service.mark_business_result("file", current, {"ok": True}, status="2", message="完成")

            mock_run_single.side_effect = capture_transition

            from app.services.llm_service.analysis_service import run_file_analysis_batch_task

            run_file_analysis_batch_task(
                task_service=task_service,
                progress_hub=hub,
                request_payload=request_payload,
                download_root=tmp,
                callback_url="http://127.0.0.1:9000/llm/callback",
                callback_timeout=5,
                document_rag_factory=Mock(),
                knowledge_index_factory=Mock(),
                analysis_filename_constraint_mode="scope_guard",
            )

            self.assertEqual(
                transitions,
                [
                    ("a.txt", "1", "0"),
                    ("b.txt", "2", "1"),
                ],
            )
            self.assertEqual(
                filename_constraint_modes,
                ["scope_guard", "scope_guard"],
            )

    @staticmethod
    def _stage9_model_response(file_name: str, architecture_id: int | str) -> str:
        """生成满足阶段 9 顶层契约的最小严格 JSON 回答。"""
        return json.dumps(
            {
                "country": "",
                "channel": "",
                "maturity": "",
                "security": "",
                "format": "",
                "architectureId": architecture_id,
                "fileDataItem": {
                    "fileName": file_name,
                    "dataFormat": "",
                    "summary": "阶段 9 测试摘要",
                    "keyword": "测试",
                    "score": 55,
                    "source": "未明确数据来源",
                },
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _stage9_request(file_name: str, architecture_list: list[dict]) -> dict:
        """构造一个不依赖真实后台服务的文件分析请求。"""
        return {
            "businessType": "file",
            "params": [
                {
                    "fileName": file_name,
                    "originalFileName": file_name,
                    "filePath": f"https://example.invalid/{file_name}",
                    "enableFullTranslation": False,
                    "architectureList": architecture_list,
                }
            ],
        }

    @staticmethod
    def _run_stage9_task(
            *,
            task_service: LLMTaskService,
            request_payload: dict,
            download_root: str,
            document_rag_factory: FakeDocumentRagFactory,
            knowledge_index_factory: FakeKnowledgeIndexFactory,
    ) -> None:
        """在文件下载、归一化和翻译边界使用纯内存替身执行阶段 9 编排。"""
        file_name = request_payload["params"][0]["fileName"]
        local_file = str(Path(download_root) / file_name)
        with (
            patch(
                "app.services.llm_service.analysis_service.download_to_temp_file",
                return_value=local_file,
            ),
            patch(
                "app.services.llm_service.analysis_service.normalize_file_for_llm",
                side_effect=lambda path: path,
            ),
            patch(
                "app.services.llm_service.analysis_service.prepare_analysis_file_for_upload",
                side_effect=lambda path, *_args: path,
            ),
            patch(
                "app.services.llm_service.analysis_service.enrich_with_translations",
                side_effect=lambda result, *_args, **_kwargs: result,
            ),
        ):
            from app.services.llm_service.analysis_service import run_file_analysis_task

            run_file_analysis_task(
                task_service=task_service,
                progress_hub=LLMProgressHub(),
                request_payload=request_payload,
                download_root=download_root,
                callback_url="",
                callback_timeout=5,
                document_rag_factory=document_rag_factory,
                knowledge_index_factory=knowledge_index_factory,
                analysis_classification_mode="legacy",
            )

    def test_stage9_success_audits_then_transfers_prepared_document(self):
        """成功路径应审计一次上传所得文档，并在转交后保留全局实体。"""
        with workspace_tempdir() as tmp:
            file_name = "stage9.txt"
            Path(tmp, file_name).write_text("stage 9", encoding="utf-8")
            request_payload = self._stage9_request(
                file_name,
                [{"id": 9001, "name": "阶段九分类", "parentId": 8999}],
            )
            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task_service.create_file_task(file_name, request_payload)
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text=self._stage9_model_response(file_name, 9001),
                        sources=(
                            RagSource(
                                document_ref="document:stage9",
                                text="阶段 9 来源证据",
                            ),
                        ),
                    )
                ]
            )
            knowledge_factory = FakeKnowledgeIndexFactory()

            self._run_stage9_task(
                task_service=task_service,
                request_payload=request_payload,
                download_root=tmp,
                document_rag_factory=rag_factory,
                knowledge_index_factory=knowledge_factory,
            )

            task = task_service.get_task("file", file_name)
            interactions = task_service.get_llm_interactions("file", file_name)
            attempts = task_service.get_llm_interaction_attempts(interactions[0]["id"])
            lifecycle = task_service.get_llm_interaction_lifecycle_events(
                interactions[0]["id"]
            )
            leases = task_service.rag_resource_leases.list_open()

        self.assertEqual(task["status"], "2")
        self.assertEqual(task["callback_status"], "skipped")
        self.assertEqual(task["result_payload"]["data"]["country"], "")
        self.assertEqual(task["result_payload"]["data"]["channel"], "")
        self.assertEqual(task["result_payload"]["data"]["maturity"], "")
        self.assertEqual(task["result_payload"]["data"]["security"], "公开")
        self.assertNotIn("secrets", task["result_payload"]["data"])
        self.assertEqual(task["result_payload"]["data"]["format"], "")
        self.assertEqual([item["prompt_kind"] for item in attempts], ["analysis"])
        self.assertTrue(all(item["query_mode"] == "query" for item in attempts))
        self.assertEqual(interactions[0]["workspace_cleanup_status"], "deleted")
        self.assertEqual(lifecycle[-1]["operation"], "context_delete")
        self.assertTrue(rag_factory.ports[0].sessions[0].retain_document_on_close)
        self.assertEqual(len(knowledge_factory.ports), 1)
        self.assertEqual(leases, [])

    def test_stage9_non_architecture_field_violations_are_mapped_without_failure(self):
        """普通字段缺失、未知、越界或不一致时应由 mapper 宽松处理。"""
        with workspace_tempdir() as tmp:
            file_name = "relaxed-fields.txt"
            Path(tmp, file_name).write_text("宽松字段测试", encoding="utf-8")
            request_payload = self._stage9_request(
                file_name,
                [
                    {"id": 9050, "name": "根节点", "parentId": None},
                    {"id": 9051, "name": "父节点", "parentId": 9050},
                    {"id": 9052, "name": "子节点", "parentId": 9051},
                ],
            )
            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task_service.create_file_task(file_name, request_payload)
            source = RagSource(document_ref="document:relaxed", text="宽松字段来源")
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text=json.dumps(
                            {
                                "architectureId": 9051,
                                "country": {"value": "美国"},
                                "channel": "候选外渠道",
                                "format": "文档类",
                                "fileDataItem": {
                                    "dataFormat": "图片类",
                                    "summary": "宽松映射摘要",
                                },
                                "unexpectedField": "ignored",
                            },
                            ensure_ascii=False,
                        ),
                        sources=(source,),
                    )
                ]
            )
            knowledge_factory = FakeKnowledgeIndexFactory()

            self._run_stage9_task(
                task_service=task_service,
                request_payload=request_payload,
                download_root=tmp,
                document_rag_factory=rag_factory,
                knowledge_index_factory=knowledge_factory,
            )
            task = task_service.get_task("file", file_name)
            interaction = task_service.get_llm_interactions("file", file_name)[0]
            attempts = task_service.get_llm_interaction_attempts(interaction["id"])

        data = task["result_payload"]["data"]
        self.assertEqual(task["status"], "2")
        self.assertEqual(data["architectureId"], 9051)
        self.assertEqual(data["country"], "美国")
        self.assertEqual(data["channel"], "")
        self.assertEqual(data["maturity"], "")
        self.assertEqual(data["security"], "公开")
        self.assertEqual(data["format"], "文档类")
        self.assertEqual(data["fileDataItem"]["dataFormat"], "文档类")
        self.assertNotIn("unexpectedField", data)
        self.assertEqual([item["prompt_kind"] for item in attempts], ["analysis"])
        self.assertEqual(len(knowledge_factory.ports), 1)

    def test_stage9_non_object_file_data_item_is_mapped_without_failure(self):
        """fileDataItem 类型错误不再触发普通字段合同失败。"""
        with workspace_tempdir() as tmp:
            file_name = "relaxed-file-item.txt"
            Path(tmp, file_name).write_text("宽松详细字段测试", encoding="utf-8")
            request_payload = self._stage9_request(
                file_name,
                [
                    {"id": 9061, "name": "候选一", "parentId": 9060},
                    {"id": 9062, "name": "候选二", "parentId": 9060},
                ],
            )
            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task_service.create_file_task(file_name, request_payload)
            source = RagSource(document_ref="document:file-item", text="详细字段来源")
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text=json.dumps(
                            {
                                "architectureId": 9062,
                                "fileDataItem": ["不是对象"],
                            },
                            ensure_ascii=False,
                        ),
                        sources=(source,),
                    )
                ]
            )

            self._run_stage9_task(
                task_service=task_service,
                request_payload=request_payload,
                download_root=tmp,
                document_rag_factory=rag_factory,
                knowledge_index_factory=FakeKnowledgeIndexFactory(),
            )
            task = task_service.get_task("file", file_name)

        self.assertEqual(task["status"], "2")
        self.assertEqual(task["result_payload"]["data"]["architectureId"], 9062)
        self.assertIsInstance(task["result_payload"]["data"]["fileDataItem"], dict)
        self.assertEqual(task["result_payload"]["data"]["fileDataItem"]["score"], 55)

    def test_stage9_valid_model_architecture_id_wins_over_gjb_heuristic(self):
        """模型已返回合法候选时，GJB 正文不得覆盖该分类。"""
        with workspace_tempdir() as tmp:
            file_name = "valid-model-id.txt"
            Path(tmp, file_name).write_text("GJB 9001C-2017 国家军用标准", encoding="utf-8")
            request_payload = self._stage9_request(
                file_name,
                [
                    {"id": 9071, "name": "普通候选", "parentId": 9070},
                    {"id": 9072, "name": "数据标准", "parentId": None},
                    {
                        "id": 9073,
                        "name": "军用软件标准",
                        "parentId": 9072,
                        "pathName": "数据标准/军用软件标准",
                    },
                ],
            )
            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task_service.create_file_task(file_name, request_payload)
            source = RagSource(document_ref="document:model-first", text="分类来源")
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text=self._stage9_model_response(file_name, 9071),
                        sources=(source,),
                    )
                ]
            )

            self._run_stage9_task(
                task_service=task_service,
                request_payload=request_payload,
                download_root=tmp,
                document_rag_factory=rag_factory,
                knowledge_index_factory=FakeKnowledgeIndexFactory(),
            )
            task = task_service.get_task("file", file_name)
            interaction = task_service.get_llm_interactions("file", file_name)[0]
            attempts = task_service.get_llm_interaction_attempts(interaction["id"])

        self.assertEqual(task["status"], "2")
        self.assertEqual(task["result_payload"]["data"]["architectureId"], 9071)
        self.assertEqual([item["prompt_kind"] for item in attempts], ["analysis"])

    def test_stage9_invalid_model_architecture_uses_general_gjb_leaf_before_repair(self):
        """数字字符串不合法时，应按 GJB 正文定向命中“通用要求”。"""
        with workspace_tempdir() as tmp:
            file_name = "gjb-fallback.txt"
            Path(tmp, file_name).write_text("本文件为 GJB 9001C-2017 国家军用标准。", encoding="utf-8")
            request_payload = self._stage9_request(
                file_name,
                [
                    {"id": 9081, "name": "普通候选", "parentId": None},
                    {"id": 9082, "name": "数据标准", "parentId": None},
                    {
                        "id": 9083,
                        "name": "军用软件标准",
                        "parentId": 9082,
                        "pathName": "数据标准/军用软件标准",
                    },
                    {
                        "id": 9084,
                        "name": "建模与仿真标准",
                        "parentId": 9082,
                        "pathName": "数据标准/建模与仿真标准",
                    },
                    {
                        "id": 9085,
                        "name": "通用要求标准",
                        "parentId": 9082,
                        "pathName": "数据标准/通用要求标准",
                    },
                ],
            )
            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task_service.create_file_task(file_name, request_payload)
            source = RagSource(document_ref="document:gjb", text="GJB 来源")
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text=self._stage9_model_response(file_name, "9081"),
                        sources=(source,),
                    )
                ]
            )

            self._run_stage9_task(
                task_service=task_service,
                request_payload=request_payload,
                download_root=tmp,
                document_rag_factory=rag_factory,
                knowledge_index_factory=FakeKnowledgeIndexFactory(),
            )
            task = task_service.get_task("file", file_name)
            interaction = task_service.get_llm_interactions("file", file_name)[0]
            attempts = task_service.get_llm_interaction_attempts(interaction["id"])

        self.assertEqual(task["status"], "2")
        self.assertEqual(task["result_payload"]["data"]["architectureId"], 9085)
        self.assertEqual([item["prompt_kind"] for item in attempts], ["analysis"])

    def test_stage9_data_standard_parent_falls_back_to_general_leaf_without_gjb_text(self):
        """数据标准父节点即使没有 GJB 关键词，也必须定向兜底到通用要求。"""
        with workspace_tempdir() as tmp:
            file_name = "data-standard-parent.txt"
            Path(tmp, file_name).write_text("数据标准父节点分类测试", encoding="utf-8")
            request_payload = self._stage9_request(
                file_name,
                [
                    {"id": 9091, "name": "普通候选", "parentId": None},
                    {"id": 9092, "name": "数据标准", "parentId": None},
                    {
                        "id": 9093,
                        "name": "军用软件标准",
                        "parentId": 9092,
                        "pathName": "数据标准/军用软件标准",
                    },
                    {
                        "id": 9094,
                        "name": "建模与仿真标准",
                        "parentId": 9092,
                        "pathName": "数据标准/建模与仿真标准",
                    },
                    {
                        "id": 9095,
                        "name": "通用要求标准",
                        "parentId": 9092,
                        "pathName": "数据标准/通用要求标准",
                    },
                ],
            )
            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task_service.create_file_task(file_name, request_payload)
            source = RagSource(document_ref="document:data-standard-parent", text="分类来源")
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text=self._stage9_model_response(file_name, 9092),
                        sources=(source,),
                    )
                ]
            )

            self._run_stage9_task(
                task_service=task_service,
                request_payload=request_payload,
                download_root=tmp,
                document_rag_factory=rag_factory,
                knowledge_index_factory=FakeKnowledgeIndexFactory(),
            )
            task = task_service.get_task("file", file_name)
            interaction = task_service.get_llm_interactions("file", file_name)[0]
            attempts = task_service.get_llm_interaction_attempts(interaction["id"])

        self.assertEqual(task["status"], "2")
        self.assertEqual(task["result_payload"]["data"]["architectureId"], 9095)
        self.assertEqual([item["prompt_kind"] for item in attempts], ["analysis"])

    def test_stage9_repair_cannot_store_data_standard_parent(self):
        """分类修复再次返回数据标准父节点时，任务不得成功入库。"""
        with workspace_tempdir() as tmp:
            file_name = "repair-data-standard-parent.txt"
            Path(tmp, file_name).write_text("普通分类文本", encoding="utf-8")
            request_payload = self._stage9_request(
                file_name,
                [
                    {"id": 9095, "name": "普通候选", "parentId": None},
                    {"id": 9096, "name": "数据标准", "parentId": None},
                    {
                        "id": 9097,
                        "name": "军用软件标准",
                        "parentId": 9096,
                        "pathName": "数据标准/军用软件标准",
                    },
                ],
            )
            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task_service.create_file_task(file_name, request_payload)
            source = RagSource(document_ref="document:repair-data-standard-parent", text="分类来源")
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text=self._stage9_model_response(file_name, ""),
                        sources=(source,),
                    )
                ],
                ask_outcomes=[
                    FakeRagOutcome(
                        text='{"architectureId":9096}',
                        sources=(source,),
                    )
                ],
            )
            knowledge_factory = FakeKnowledgeIndexFactory()

            self._run_stage9_task(
                task_service=task_service,
                request_payload=request_payload,
                download_root=tmp,
                document_rag_factory=rag_factory,
                knowledge_index_factory=knowledge_factory,
            )
            task = task_service.get_task("file", file_name)
            interaction = task_service.get_llm_interactions("file", file_name)[0]
            attempts = task_service.get_llm_interaction_attempts(interaction["id"])

        self.assertEqual(task["status"], "3")
        self.assertEqual(len(knowledge_factory.ports), 0)
        self.assertEqual(
            [item["prompt_kind"] for item in attempts],
            [RagPromptKind.ANALYSIS.value, RagPromptKind.ARCHITECTURE_REPAIR.value],
        )

    def test_stage9_architecture_repair_has_separate_audit_attempt(self):
        """多候选缺少 architectureId 时可修复为请求中的父节点。"""
        with workspace_tempdir() as tmp:
            file_name = "architecture-repair.txt"
            Path(tmp, file_name).write_text("architecture", encoding="utf-8")
            request_payload = self._stage9_request(
                file_name,
                [
                    {"id": 9100, "name": "根节点", "parentId": None},
                    {"id": 9101, "name": "候选一", "parentId": 9100},
                    {"id": 9102, "name": "候选二", "parentId": 9101},
                ],
            )
            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task_service.create_file_task(file_name, request_payload)
            source = RagSource(document_ref="document:repair", text="分类证据")
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text=self._stage9_model_response(file_name, ""),
                        sources=(source,),
                    )
                ],
                ask_outcomes=[
                    FakeRagOutcome(
                        text='{"architectureId":9101}',
                        sources=(source,),
                    )
                ],
            )

            self._run_stage9_task(
                task_service=task_service,
                request_payload=request_payload,
                download_root=tmp,
                document_rag_factory=rag_factory,
                knowledge_index_factory=FakeKnowledgeIndexFactory(),
            )
            interaction = task_service.get_llm_interactions("file", file_name)[0]
            attempts = task_service.get_llm_interaction_attempts(interaction["id"])
            task = task_service.get_task("file", file_name)

        self.assertEqual(task["status"], "2")
        self.assertEqual(task["result_payload"]["data"]["architectureId"], 9101)
        self.assertEqual(
            [item["prompt_kind"] for item in attempts],
            [RagPromptKind.ANALYSIS.value, RagPromptKind.ARCHITECTURE_REPAIR.value],
        )

    def test_stage9_invalid_architecture_repair_fails_without_knowledge_transfer(self):
        """二次修复仍越界时任务失败，且不得创建永久知识库 Port。"""
        with workspace_tempdir() as tmp:
            file_name = "architecture-repair-failure.txt"
            Path(tmp, file_name).write_text("普通分类文本", encoding="utf-8")
            request_payload = self._stage9_request(
                file_name,
                [
                    {"id": 9151, "name": "候选一", "parentId": None},
                    {"id": 9152, "name": "候选二", "parentId": None},
                ],
            )
            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task_service.create_file_task(file_name, request_payload)
            source = RagSource(document_ref="document:repair-failure", text="分类来源")
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text=self._stage9_model_response(file_name, 9999),
                        sources=(source,),
                    )
                ],
                ask_outcomes=[
                    FakeRagOutcome(
                        text='{"architectureId":9999}',
                        sources=(source,),
                    )
                ],
            )
            knowledge_factory = FakeKnowledgeIndexFactory()

            self._run_stage9_task(
                task_service=task_service,
                request_payload=request_payload,
                download_root=tmp,
                document_rag_factory=rag_factory,
                knowledge_index_factory=knowledge_factory,
            )
            task = task_service.get_task("file", file_name)
            interaction = task_service.get_llm_interactions("file", file_name)[0]
            attempts = task_service.get_llm_interaction_attempts(interaction["id"])

        self.assertEqual(task["status"], "3")
        self.assertEqual(task["result_payload"]["data"]["status"], "3")
        self.assertEqual(len(knowledge_factory.ports), 0)
        self.assertEqual(
            [item["prompt_kind"] for item in attempts],
            [RagPromptKind.ANALYSIS.value, RagPromptKind.ARCHITECTURE_REPAIR.value],
        )

    def test_stage9_json_repair_has_separate_audit_attempt(self):
        """首次回答语法不合法时只执行一次 JSON_REPAIR，并审计两次调用。"""
        with workspace_tempdir() as tmp:
            file_name = "json-repair.txt"
            Path(tmp, file_name).write_text("json repair", encoding="utf-8")
            request_payload = self._stage9_request(
                file_name,
                [{"id": 9201, "name": "唯一候选", "parentId": 9200}],
            )
            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task_service.create_file_task(file_name, request_payload)
            source = RagSource(document_ref="document:json", text="JSON 来源")
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[FakeRagOutcome(text="```json\n{bad}\n```", sources=(source,))],
                ask_outcomes=[
                    FakeRagOutcome(
                        text=self._stage9_model_response(file_name, 9201),
                        sources=(source,),
                    )
                ],
            )

            self._run_stage9_task(
                task_service=task_service,
                request_payload=request_payload,
                download_root=tmp,
                document_rag_factory=rag_factory,
                knowledge_index_factory=FakeKnowledgeIndexFactory(),
            )
            interaction = task_service.get_llm_interactions("file", file_name)[0]
            attempts = task_service.get_llm_interaction_attempts(interaction["id"])

        self.assertEqual(
            [item["prompt_kind"] for item in attempts],
            [RagPromptKind.ANALYSIS.value, RagPromptKind.JSON_REPAIR.value],
        )

    def test_stage9_audit_failure_preserves_session_and_blocks_downstream_work(self):
        """原子审计失败必须保留 RAG 现场，并阻断知识库、翻译和成功回调。"""
        with workspace_tempdir() as tmp:
            file_name = "audit-failure.txt"
            Path(tmp, file_name).write_text("audit", encoding="utf-8")
            request_payload = self._stage9_request(
                file_name,
                [{"id": 9301, "name": "审计候选", "parentId": 9300}],
            )
            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task_service.create_file_task(file_name, request_payload)
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text=self._stage9_model_response(file_name, 9301),
                        sources=(RagSource(document_ref="document:audit", text="证据"),),
                    )
                ]
            )
            knowledge_factory = FakeKnowledgeIndexFactory()
            with patch.object(
                task_service,
                "create_llm_interaction_with_trace",
                side_effect=OSError("audit unavailable"),
            ), patch(
                "app.services.llm_service.analysis_service.enrich_with_translations"
            ) as mock_translation:
                self._run_stage9_task(
                    task_service=task_service,
                    request_payload=request_payload,
                    download_root=tmp,
                    document_rag_factory=rag_factory,
                    knowledge_index_factory=knowledge_factory,
                )
            task = task_service.get_task("file", file_name)
            open_leases = task_service.rag_resource_leases.list_open()

        self.assertEqual(task["status"], "3")
        self.assertIsNone(rag_factory.ports[0].sessions[0].retain_document_on_close)
        self.assertEqual(len(knowledge_factory.ports), 0)
        mock_translation.assert_not_called()
        self.assertEqual(open_leases[0].status, "audit_failed")

    def test_stage9_retention_required_error_keeps_global_document(self):
        """永久集合可能已接管文档时，即使 store 抛错也必须保留全局实体。"""
        with workspace_tempdir() as tmp:
            file_name = "retention-required.txt"
            Path(tmp, file_name).write_text("retention", encoding="utf-8")
            request_payload = self._stage9_request(
                file_name,
                [{"id": 9401, "name": "保留候选", "parentId": 9400}],
            )
            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task_service.create_file_task(file_name, request_payload)
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text=self._stage9_model_response(file_name, 9401),
                        sources=(RagSource(document_ref="document:retain", text="证据"),),
                    )
                ]
            )
            with patch.object(
                FakeKnowledgeIndexPort,
                "store_prepared_document",
                side_effect=KnowledgeIndexRetentionRequiredError("需要人工恢复"),
            ):
                self._run_stage9_task(
                    task_service=task_service,
                    request_payload=request_payload,
                    download_root=tmp,
                    document_rag_factory=rag_factory,
                    knowledge_index_factory=FakeKnowledgeIndexFactory(),
                )
            task = task_service.get_task("file", file_name)

        self.assertEqual(task["status"], "3")
        self.assertTrue(rag_factory.ports[0].sessions[0].retain_document_on_close)

    def test_stage9_confirmed_compensation_deletes_untransferred_document(self):
        """Gateway 明确完成补偿时，失败路径应请求 Session 永久删除全局文档。"""
        with workspace_tempdir() as tmp:
            file_name = "released-document.txt"
            Path(tmp, file_name).write_text("released", encoding="utf-8")
            request_payload = self._stage9_request(
                file_name,
                [{"id": 9451, "name": "补偿候选", "parentId": 9450}],
            )
            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task_service.create_file_task(file_name, request_payload)
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text=self._stage9_model_response(file_name, 9451),
                        sources=(RagSource(document_ref="document:release", text="证据"),),
                    )
                ]
            )
            with patch.object(
                FakeKnowledgeIndexPort,
                "store_prepared_document",
                side_effect=KnowledgeIndexDocumentReleasedError("集合补偿已完成"),
            ):
                self._run_stage9_task(
                    task_service=task_service,
                    request_payload=request_payload,
                    download_root=tmp,
                    document_rag_factory=rag_factory,
                    knowledge_index_factory=FakeKnowledgeIndexFactory(),
                )
            task = task_service.get_task("file", file_name)
            interaction = task_service.get_llm_interactions("file", file_name)[0]
            lifecycle = task_service.get_llm_interaction_lifecycle_events(
                interaction["id"]
            )

        self.assertEqual(task["status"], "3")
        self.assertFalse(rag_factory.ports[0].sessions[0].retain_document_on_close)
        self.assertIn(
            "global_document_delete",
            [event["operation"] for event in lifecycle],
        )

    def test_stage9_cleanup_failure_keeps_resource_lease_open(self):
        """关闭失败应写入审计并保留可巡检租约，不能伪装成资源已关闭。"""
        with workspace_tempdir() as tmp:
            file_name = "cleanup-failure.txt"
            Path(tmp, file_name).write_text("cleanup", encoding="utf-8")
            request_payload = self._stage9_request(
                file_name,
                [{"id": 9471, "name": "清理候选", "parentId": 9470}],
            )
            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task_service.create_file_task(file_name, request_payload)
            rag_factory = FakeDocumentRagFactory(
                cleanup_error_message="删除隔离上下文失败",
                analyse_outcomes=[
                    FakeRagOutcome(
                        text=self._stage9_model_response(file_name, 9471),
                        sources=(RagSource(document_ref="document:cleanup", text="证据"),),
                    )
                ],
            )

            self._run_stage9_task(
                task_service=task_service,
                request_payload=request_payload,
                download_root=tmp,
                document_rag_factory=rag_factory,
                knowledge_index_factory=FakeKnowledgeIndexFactory(),
            )
            task = task_service.get_task("file", file_name)
            interaction = task_service.get_llm_interactions("file", file_name)[0]
            open_leases = task_service.rag_resource_leases.list_open()

        self.assertEqual(task["status"], "2")
        self.assertEqual(interaction["workspace_cleanup_status"], "failed")
        self.assertEqual(open_leases[0].status, "audited")
        self.assertEqual(open_leases[0].last_error, "删除隔离上下文失败")

    def test_stage9_batch_uses_independent_rag_and_knowledge_leases(self):
        """批量任务必须为每个文件创建独立 Port，不能复用有状态 Session。"""
        with workspace_tempdir() as tmp:
            file_names = ("batch-a.txt", "batch-b.txt")
            architecture_ids = (9501, 9502)
            for file_name in file_names:
                Path(tmp, file_name).write_text(file_name, encoding="utf-8")
            request_payload = {
                "businessType": "file",
                "params": [
                    self._stage9_request(
                        file_name,
                        [{"id": architecture_id, "name": f"批量候选{architecture_id}", "parentId": 9500}],
                    )["params"][0]
                    for file_name, architecture_id in zip(file_names, architecture_ids)
                ],
            }
            task_service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            for params in request_payload["params"]:
                task_service.create_file_task(
                    params["fileName"],
                    {"businessType": "file", "params": [params]},
                    status="1" if params["fileName"] == file_names[0] else "0",
                )
            source = RagSource(document_ref="document:batch", text="批量来源")
            rag_factory = FakeDocumentRagFactory(
                analyse_outcomes=[
                    FakeRagOutcome(
                        text=self._stage9_model_response("ignored.txt", ""),
                        sources=(source,),
                    )
                ]
            )
            knowledge_factory = FakeKnowledgeIndexFactory()
            with (
                patch(
                    "app.services.llm_service.analysis_service.download_to_temp_file",
                    side_effect=lambda _url, file_name, *_args, **_kwargs: str(
                        Path(tmp, file_name)
                    ),
                ),
                patch(
                    "app.services.llm_service.analysis_service.normalize_file_for_llm",
                    side_effect=lambda path: path,
                ),
                patch(
                    "app.services.llm_service.analysis_service.prepare_analysis_file_for_upload",
                    side_effect=lambda path, *_args: path,
                ),
                patch(
                    "app.services.llm_service.analysis_service.enrich_with_translations",
                    side_effect=lambda result, *_args, **_kwargs: result,
                ),
            ):
                from app.services.llm_service.analysis_service import (
                    run_file_analysis_batch_task,
                )

                run_file_analysis_batch_task(
                    task_service=task_service,
                    progress_hub=LLMProgressHub(),
                    request_payload=request_payload,
                    download_root=tmp,
                    callback_url="",
                    callback_timeout=5,
                    document_rag_factory=rag_factory,
                    knowledge_index_factory=knowledge_factory,
                    analysis_classification_mode="legacy",
                )

            tasks = [task_service.get_task("file", name) for name in file_names]

        self.assertTrue(all(task["status"] == "2" for task in tasks))
        self.assertEqual(len(rag_factory.ports), 2)
        self.assertEqual(len(knowledge_factory.ports), 2)
        self.assertTrue(
            all(
                port.sessions[0].retain_document_on_close
                for port in rag_factory.ports
            )
        )
        self.assertEqual(rag_factory.active_leases, 0)
        self.assertEqual(knowledge_factory.active_leases, 0)
