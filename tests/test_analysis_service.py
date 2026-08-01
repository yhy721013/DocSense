import unittest
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from app.ports import (
    KnowledgeIndexDocumentReleasedError,
    KnowledgeIndexRetentionRequiredError,
    RagPromptKind,
    RagSource,
)
from app.modules.document_processing import LegacyOfficeConversionError
from app.modules.analysis.domain.callback_payloads import build_file_callback_payload
from app.modules.analysis.domain.classification_rules import (
    _resolve_analysis_architecture_id,
    _unique_visible_equipment_identifier_parent,
    _validate_topk_architecture_id,
)
from app.modules.analysis.domain.errors import (
    ArchitectureContractError,
    DataStandardParentContractError,
)
from app.modules.analysis.domain.models import DEFAULT_ARCHITECTURE_OPTIONS
from app.modules.analysis.domain.prompts import build_file_analysis_prompt
from app.modules.analysis.domain.result_mapping import (
    _first_data_standard_leaf_id,
    map_analysis_result,
    map_analysis_result as map_stage1f_analysis_result,
    resolve_storage_architecture_id,
)
from app.modules.analysis.domain.architecture_tree import build_architecture_tree_index
from app.services.core.progress_hub import LLMProgressHub
from app.services.llm_service.task_service import LLMTaskService
from tests import workspace_tempdir
from tests.fakes.knowledge_index import (
    FakeKnowledgeIndexFactory,
    FakeKnowledgeIndexPort,
)
from tests.fakes.rag import (
    FakeDocumentRagFactory,
    FakeDocumentRagSession,
    FakeRagOutcome,
)


class _OfficePreparation:
    def __init__(
        self,
        *,
        original_path: Path,
        prepared_path: Path,
        converted: bool,
    ) -> None:
        self.original_path = original_path
        self.prepared_path = prepared_path
        self.converted = converted
        self.source_suffix = original_path.suffix.lower()
        self.target_suffix = prepared_path.suffix.lower()
        self.libreoffice_version = (
            "LibreOffice 26.2.5.2" if converted else None
        )
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.closed = True
        if self.converted:
            self.prepared_path.unlink(missing_ok=True)


class _RecordingOfficePreparer:
    def __init__(
        self,
        converted_paths: dict[str, Path] | None = None,
        *,
        fail_suffixes: set[str] | None = None,
    ) -> None:
        self.converted_paths = converted_paths or {}
        self.fail_suffixes = {
            item.lower() for item in (fail_suffixes or set())
        }
        self.calls: list[tuple[Path, str]] = []
        self.results: list[_OfficePreparation] = []

    def prepare(self, source_path, *, job_id: str):
        source = Path(source_path)
        self.calls.append((source, job_id))
        if source.suffix.lower() in self.fail_suffixes:
            raise LegacyOfficeConversionError("test_conversion_failure")
        prepared = self.converted_paths.get(source.suffix.lower(), source)
        result = _OfficePreparation(
            original_path=source,
            prepared_path=prepared,
            converted=prepared != source,
        )
        self.results.append(result)
        return result


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
                    "source": "简氏防务",
                }
            },
            request_params,
        )

        self.assertEqual(result["fileDataItem"]["score"], 85)

    def test_map_analysis_result_composes_path_and_summary_keywords(self):
        summary = (
            "该报告介绍航空母舰的核动力推进、飞行甲板、舰载机运用和数据链技术。"
        )
        result = map_analysis_result(
            {
                "fileDataItem": {
                    "summary": summary,
                    "keyword": (
                        "海军装备, 航空母舰, CVN-78, 基础数据, "
                        "核动力推进, 飞行甲板, 舰载机运用, 数据链技术, 摘要外词"
                    ),
                }
            },
            {
                "fileName": "sample.txt",
                "architectureList": [
                    {
                        "id": 1,
                        "name": "装备/体系",
                    },
                    {
                        "id": 2,
                        "parentId": 1,
                        "name": "海军装备",
                    },
                    {
                        "id": 3,
                        "parentId": 2,
                        "name": "航空母舰",
                    },
                    {
                        "id": 10,
                        "parentId": 3,
                        "name": "CVN-78",
                        "pathName": "甲方不透明展示/不得拆分",
                    }
                ],
            },
            resolved_architecture_id=10,
        )

        self.assertEqual(
            result["fileDataItem"]["keyword"],
            (
                "CVN-78, 航空母舰, 海军装备, 装备/体系, "
                "核动力推进, 飞行甲板, 舰载机运用, 数据链技术"
            ),
        )
        self.assertNotIn("不得拆分", result["fileDataItem"]["keyword"])
        self.assertNotIn("摘要外词", result["fileDataItem"]["keyword"])
        self.assertNotIn("基础数据", result["fileDataItem"]["keyword"])

    def test_map_analysis_result_uses_source_backed_keyword_to_reach_minimum(self):
        result = map_analysis_result(
            {
                "fileDataItem": {
                    "summary": "本标准规定质量管理体系和装备质量相关要求。",
                    "keyword": (
                        "质量管理体系, 军用标准, GJB 9001C, 装备质量, 产品设计"
                    ),
                }
            },
            {
                "fileName": "gjb.pdf",
                "architectureList": [
                    {"id": 1, "name": "数据标准"},
                    {"id": 2, "parentId": 1, "name": "军用软件"},
                ],
            },
            original_text=(
                "本文件为国家军用标准 GJB 9001C―2017，规定质量管理体系要求。"
            ),
            resolved_architecture_id=2,
        )

        self.assertEqual(
            result["fileDataItem"]["keyword"],
            "军用软件, 数据标准, 质量管理体系, 装备质量, 军用标准",
        )

    def test_map_analysis_result_normalizes_related_technology_string(self):
        result = map_analysis_result(
            {
                "fileDataItem": {
                    "relatedTechnology": [
                        "雷达技术",
                        "数据融合",
                        "雷达技术",
                        "卫星通信",
                        "数据链技术",
                        "量子通信",
                    ],
                }
            },
            {
                "fileName": "sample.txt",
                "architectureList": [{"id": 10, "name": "测试"}],
            },
            original_text="正文明确介绍雷达技术、数据融合、卫星通信和数据链技术。",
        )

        self.assertEqual(
            result["fileDataItem"]["relatedTechnology"],
            "雷达技术, 数据融合, 卫星通信, 数据链技术",
        )
        self.assertNotIn("量子通信", result["fileDataItem"]["relatedTechnology"])

    def test_map_analysis_result_accepts_chinese_technology_with_english_evidence(self):
        result = map_analysis_result(
                {
                    "fileDataItem": {
                        "relatedTechnology": (
                            "电磁弹射系统, 先进拦阻系统, 分布式孔径系统"
                        ),
                        "relatedTechnologyEvidence": [
                            {
                                "nameZh": "电磁弹射系统",
                                "sourceTerm": (
                                    "Electromagnetic Aircraft Launch Systems (EMALS)"
                                ),
                            },
                            {
                                "nameZh": "先进拦阻系统",
                                "sourceTerm": "Advanced Arresting Gear (AAG)",
                            },
                            {
                                "nameZh": "分布式孔径系统",
                                "sourceTerm": "Distributed Aperture System",
                            },
                        ],
                    }
                },
                {
                    "fileName": "ford.pdf",
                    "architectureList": [{"id": 10, "name": "测试"}],
                },
                original_text=(
                    "The ship uses Electromagnetic Aircraft Launch Systems (EMALS) "
                    "and Advanced Arresting Gear (AAG)."
                ),
        )

        self.assertEqual(
            result["fileDataItem"]["relatedTechnology"],
            "电磁弹射系统, 先进拦阻系统",
        )
        self.assertNotIn(
            "relatedTechnologyEvidence",
            result["fileDataItem"],
        )

    def test_map_analysis_result_retains_related_technology_without_evidence_text(self):
        result = map_analysis_result(
            {"fileDataItem": {"relatedTechnology": "雷达技术"}},
            {
                "fileName": "sample.bin",
                "architectureList": [{"id": 10, "name": "测试"}],
            },
        )

        self.assertEqual(result["fileDataItem"]["relatedTechnology"], "雷达技术")

    def test_map_analysis_result_forces_score_55_when_source_is_unknown(self):
        request_params = {
            "fileName": "sample.txt",
            "architectureList": [{"id": 10, "name": "测试"}],
        }

        result = map_analysis_result(
            {
                "fileDataItem": {
                    "score": 95,
                    "source": "未明确数据来源",
                }
            },
            request_params,
        )

        self.assertEqual(result["fileDataItem"]["source"], "未明确数据来源")
        self.assertEqual(result["fileDataItem"]["score"], 55)

    def test_map_analysis_result_replaces_exact_known_legacy_internal_source(self):
        internal_name = "prepared-0123456789abcdef0123456789abcdef.docx"
        result = map_stage1f_analysis_result(
            {
                "fileDataItem": {
                    "score": 95,
                    "source": internal_name,
                }
            },
            {
                "fileName": "customer-hash.doc",
                "originalFileName": "甲方原始名称.doc",
                "architectureList": [{"id": 10, "name": "测试"}],
            },
            internal_prepared_basename=internal_name,
        )

        self.assertEqual(
            result["fileDataItem"]["source"],
            "甲方原始名称.doc",
        )

    def test_map_analysis_result_keeps_unrelated_source_with_internal_name_known(self):
        internal_name = "prepared-0123456789abcdef0123456789abcdef.docx"
        result = map_stage1f_analysis_result(
            {
                "fileDataItem": {
                    "score": 95,
                    "source": "简氏防务",
                }
            },
            {
                "fileName": "customer-hash.doc",
                "originalFileName": "甲方原始名称.doc",
                "architectureList": [{"id": 10, "name": "测试"}],
            },
            internal_prepared_basename=internal_name,
        )

        self.assertEqual(result["fileDataItem"]["source"], "简氏防务")

    def test_map_analysis_result_defaults_missing_source_to_unknown_and_forces_score_55(self):
        request_params = {
            "fileName": "sample.txt",
            "architectureList": [{"id": 10, "name": "测试"}],
        }

        result = map_analysis_result(
            {"fileDataItem": {"score": 85}},
            request_params,
        )

        self.assertEqual(result["fileDataItem"]["source"], "未明确数据来源")
        self.assertEqual(result["fileDataItem"]["score"], 55)

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
        self.assertIn("source 为“未明确数据来源”时，score 必须且只能输出 55", prompt)
        self.assertIn("禁止在这种情况下输出 95、85、75 或 65", prompt)
        self.assertIn("候选包含“公开”则输出“公开”", prompt)
        self.assertIn("keyword 必须输出 5 至 10 个关键词", prompt)
        self.assertIn("分类路径关键词排在前面，内容关键词排在后面", prompt)
        self.assertIn("内容关键词数量不得少于 max(2, 5-C)", prompt)
        self.assertIn("relatedTechnologyEvidence", prompt)
        self.assertIn("没有合格技术时输出空字符串", prompt)
        self.assertNotIn("固定输出 10 个关键词", prompt)
        self.assertNotIn("至少 10 个关键词", prompt)
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

    def test_topk_contract_accepts_finite_boundary_parent_but_rejects_true_root(self):
        finite_tree = [
            {"id": 10, "name": "有限边界父节点", "parentId": 999},
            {"id": 11, "name": "边界叶甲", "parentId": 10},
            {"id": 12, "name": "边界叶乙", "parentId": 10},
        ]
        finite_index = build_architecture_tree_index(finite_tree)

        self.assertEqual(
            _validate_topk_architecture_id(
                10,
                visible_ids={10, 11, 12},
                tree_index=finite_index,
                architecture_list=finite_tree,
            ),
            10,
        )

        true_root_tree = [
            {"id": 20, "name": "真实根", "parentId": None},
            {"id": 21, "name": "根下叶子", "parentId": 20},
        ]
        with self.assertRaisesRegex(ArchitectureContractError, "根节点"):
            _validate_topk_architecture_id(
                20,
                visible_ids={20, 21},
                tree_index=build_architecture_tree_index(true_root_tree),
                architecture_list=true_root_tree,
            )

    def test_finite_boundary_data_standard_parent_remains_forbidden(self):
        architecture_list = [
            {"id": 30, "name": "数据标准", "parentId": 999},
            {"id": 31, "name": "通用要求", "parentId": 30},
        ]
        with self.assertRaises(DataStandardParentContractError):
            _validate_topk_architecture_id(
                30,
                visible_ids={30, 31},
                tree_index=build_architecture_tree_index(architecture_list),
                architecture_list=architecture_list,
            )

    def test_filename_constraint_matches_finite_boundary_equipment_parent(self):
        detail_kinds = (
            "基础数据",
            "战技指标",
            "运用数据",
            "效能数据",
            "模型数据",
            "目特数据",
            "声像数据",
        )
        architecture_list = [
            {"id": 40, "name": "CVN-78", "parentId": 999},
            *(
                {
                    "id": 41 + offset,
                    "name": f"CVN-78-{kind}",
                    "parentId": 40,
                }
                for offset, kind in enumerate(detail_kinds)
            ),
        ]
        tree_index = build_architecture_tree_index(architecture_list)

        self.assertEqual(
            _unique_visible_equipment_identifier_parent(
                file_name="Gerald R Ford CVN-78.pdf",
                original_name="CVN 78 class.pdf",
                visible_ids={node["id"] for node in architecture_list},
                tree_index=tree_index,
                architecture_list=architecture_list,
            ),
            40,
        )
