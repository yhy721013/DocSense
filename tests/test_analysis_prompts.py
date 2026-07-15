import json
import unittest
from types import SimpleNamespace

from app.services.core.prompts import (
    build_architecture_classification_prompt,
    build_architecture_repair_prompt,
    build_file_analysis_prompt,
    build_file_extraction_prompt,
)


class AnalysisPromptSplitTests(unittest.TestCase):
    def test_classification_prompt_uses_only_model_candidate_projection(self):
        prompt = build_architecture_classification_prompt(
            {
                "fileName": "storage.pdf",
                "originalFileName": "CVN 78 class.pdf",
            },
            [
                {
                    "id": 101,
                    "name": "基础数据",
                    "parentId": 100,
                    "path": "1/100/101",
                    "pathName": "海军装备/CVN-78/基础数据",
                    "nodeType": "leaf",
                    "remark": "装备固有属性。",
                    "internalScore": 0.98,
                },
                {
                    "id": 100,
                    "pathName": "海军装备/CVN-78",
                    "node_type": "parent",
                    "remark": "",
                },
            ],
        )

        candidate_json = prompt.split("【模型候选】\n", 1)[1].strip()
        self.assertEqual(
            json.loads(candidate_json),
            [
                {
                    "id": 101,
                    "pathName": "海军装备/CVN-78/基础数据",
                    "nodeType": "leaf",
                    "remark": "装备固有属性。",
                },
                {
                    "id": 100,
                    "pathName": "海军装备/CVN-78",
                    "nodeType": "parent",
                },
            ],
        )
        self.assertIn("CVN 78 class.pdf", prompt)
        self.assertIn('{"architectureId": null}', prompt)
        self.assertIn("JSON 数字", prompt)
        self.assertNotIn("parentId", candidate_json)
        self.assertNotIn('"name":', candidate_json)
        self.assertNotIn('"path":', candidate_json)
        self.assertNotIn("internalScore", candidate_json)

    def test_classification_prompt_rejects_empty_candidates(self):
        with self.assertRaisesRegex(ValueError, "不能为空"):
            build_architecture_classification_prompt({}, [])

    def test_candidate_projection_supports_dto_paths_and_truncates_remark(self):
        long_remark = "甲" * 512 + "不得进入提示词"
        candidates = [
            SimpleNamespace(
                id=101,
                path_name="海军装备/CVN-78/基础数据",
                node_type="leaf",
                remark=long_remark,
            ),
            SimpleNamespace(
                id=100,
                semantic_path="海军装备/CVN-78",
                node_type="parent",
                remark="父节点概述",
            ),
        ]

        classification_prompt = build_architecture_classification_prompt({}, candidates)
        classification_candidates = json.loads(
            classification_prompt.split("【模型候选】\n", 1)[1].strip()
        )
        repair_prompt = build_architecture_repair_prompt(
            {"architectureId": None},
            candidates,
            "证据不足",
        )
        repair_candidates = json.loads(
            next(
                line.removeprefix("允许候选: ")
                for line in repair_prompt.splitlines()
                if line.startswith("允许候选: ")
            )
        )

        for projected in (classification_candidates, repair_candidates):
            self.assertEqual(
                projected[0]["pathName"],
                "海军装备/CVN-78/基础数据",
            )
            self.assertEqual(projected[1]["pathName"], "海军装备/CVN-78")
            self.assertEqual(len(projected[0]["remark"]), 512)
            self.assertNotIn("不得进入提示词", projected[0]["remark"])

    def test_extraction_prompt_uses_confirmed_context_without_classification_output(self):
        prompt = build_file_extraction_prompt(
            {
                "fileName": "storage.pdf",
                "originalFileName": "CVN 78 class.pdf",
                "architectureList": [
                    {"id": 999, "pathName": "不得进入抽取提示词"},
                ],
                "architectureStandardList": [
                    {"id": 201, "pathName": "同样不得决定扩展字段"},
                ],
                "country": [{"key": "02", "value": "美国"}],
                "format": [{"key": "03", "value": "文档类"}],
            },
            resolved_architecture_id=101,
            resolved_architecture_path_name="海军装备/CVN-78/基础数据",
            resolved_architecture_node_type="leaf",
        )

        self.assertIn(
            '已确认领域分类（只读，不得修改或写入输出）: {"id":101,'
            '"pathName":"海军装备/CVN-78/基础数据","nodeType":"leaf"}',
            prompt,
        )
        self.assertIn('"country": ""', prompt)
        self.assertIn('"fileDataItem"', prompt)
        self.assertIn('"美国"', prompt)
        self.assertIn('"文档类"', prompt)
        self.assertNotIn("architectureList", prompt)
        self.assertNotIn('"architectureId"', prompt)
        self.assertNotIn("不得进入抽取提示词", prompt)
        self.assertNotIn("同样不得决定扩展字段", prompt)
        self.assertNotIn('"militaryName"', prompt)
        self.assertNotIn('"approvalDept"', prompt)

    def test_extraction_standard_schema_is_controlled_only_by_boolean(self):
        params_with_standard_range = {
            "fileName": "standard.pdf",
            "architectureStandardList": [{"id": 201, "name": "标准"}],
        }
        plain_prompt = build_file_extraction_prompt(
            params_with_standard_range,
            resolved_architecture_id=201,
            include_data_standard_fields=False,
        )
        standard_prompt = build_file_extraction_prompt(
            {"fileName": "standard.pdf"},
            resolved_architecture_id=201,
            include_data_standard_fields=True,
        )

        self.assertNotIn('"militaryName"', plain_prompt)
        self.assertNotIn('"approvalDept"', plain_prompt)
        for field in (
            "militaryName",
            "num",
            "startTime",
            "implTime",
            "approvalDept",
        ):
            self.assertIn(f'"{field}"', standard_prompt)
        self.assertNotIn("architectureStandardList", standard_prompt)

    def test_extraction_prompt_accepts_numeric_string_but_rejects_invalid_id(self):
        prompt = build_file_extraction_prompt(
            {"fileName": "demo.pdf"},
            resolved_architecture_id="621103438000",  # type: ignore[arg-type]
        )
        self.assertIn('{"id":621103438000,', prompt)

        for invalid in (
            True,
            0,
            -1,
            "not-a-number",
            "１２３",
            "١٢٣",
            "²",
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "正整数"):
                    build_file_extraction_prompt(
                        {"fileName": "demo.pdf"},
                        resolved_architecture_id=invalid,  # type: ignore[arg-type]
                    )

    def test_architecture_repair_reuses_minimal_candidates_and_allows_null(self):
        prompt = build_architecture_repair_prompt(
            {"architectureId": 999, "country": "不应进入修复上下文"},
            [
                {
                    "id": 101,
                    "name": "基础数据",
                    "parentId": 100,
                    "path": "1/100/101",
                    "pathName": "海军装备/CVN-78/基础数据",
                    "nodeType": "leaf",
                    "remark": "装备固有属性。",
                }
            ],
            "候选外 ID",
        )

        self.assertIn('"id":101', prompt)
        self.assertIn('"pathName":"海军装备/CVN-78/基础数据"', prompt)
        self.assertIn('"nodeType":"leaf"', prompt)
        self.assertIn('"remark":"装备固有属性。"', prompt)
        self.assertNotIn("parentId", prompt)
        self.assertNotIn('"name":', prompt)
        self.assertNotIn('"path":', prompt)
        self.assertNotIn("不应进入修复上下文", prompt)
        self.assertIn("数字或null", prompt)
        self.assertIn("证据不足时不要猜测，输出 null", prompt)

    def test_legacy_analysis_prompt_remains_available(self):
        prompt = build_file_analysis_prompt(
            {
                "fileName": "legacy.pdf",
                "architectureList": [{"id": 101, "name": "基础数据"}],
            }
        )

        self.assertIn("architectureList", prompt)
        self.assertIn('"architectureId"', prompt)
        self.assertIn("fileDataItem", prompt)


if __name__ == "__main__":
    unittest.main()
