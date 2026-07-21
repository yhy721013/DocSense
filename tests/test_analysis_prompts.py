import json
import unittest
from types import SimpleNamespace

from app.services.core.prompts import (
    ANALYSIS_ENUM_FIELD_MAX_ITEMS,
    ANALYSIS_ENUM_ITEM_MAX_CHARS,
    ANALYSIS_KEYWORD_COUNT,
    ANALYSIS_KEYWORD_MAX_CHARS,
    ANALYSIS_RESPONSE_MAX_CHARS,
    ANALYSIS_SUMMARY_MAX_CHARS,
    build_architecture_classification_prompt,
    build_architecture_repair_prompt,
    build_architecture_reselect_prompt,
    build_data_standard_classification_prompt,
    build_file_analysis_prompt,
    build_file_extraction_prompt,
)


class AnalysisPromptSplitTests(unittest.TestCase):
    @staticmethod
    def _reselect_candidates() -> list[dict]:
        parent_path = "海军装备/航空母舰/CVN-68"
        candidates = [
            {
                "id": "6800",
                "pathName": parent_path,
                "nodeType": "parent",
                "remark": "CVN-68 单舰资料。",
            }
        ]
        for index, detail_name in enumerate(
            (
                "基础数据",
                "战技指标",
                "运用数据",
                "效能数据",
                "编制数据",
                "保障数据",
                "部署数据",
            ),
            start=1,
        ):
            candidates.append(
                {
                    "id": 6800 + index,
                    "pathName": f"{parent_path}/{detail_name}",
                    "nodeType": "leaf",
                }
            )
        return candidates

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

    def test_data_standard_prompt_injects_semantic_cards_and_general_fallback_rule(self):
        prompt = build_data_standard_classification_prompt(
            {
                "fileName": "storage.pdf",
                "originalFileName": "GJB 9001C-2017.pdf",
            },
            [
                {
                    "id": 101,
                    "pathName": "数据标准/术语与定义",
                    "nodeType": "leaf",
                },
                {
                    "id": 102,
                    "pathName": "数据标准/通用要求标准",
                    "nodeType": "leaf",
                },
            ],
            standard_context={
                "standardNumber": "GJB 9001C-2017",
                "standardTitle": "质量管理体系要求",
                "documentKind": "standard_body",
                "evidenceSources": ["originalFileName", "coverIdentifier"],
            },
        )

        candidate_json = prompt.split("【数据标准叶节点候选】\n", 1)[1].strip()
        candidates = json.loads(candidate_json)
        self.assertTrue(all(item.get("remark") for item in candidates))
        self.assertIn("质量管理体系要求", prompt)
        self.assertIn("固定章节", prompt)
        self.assertIn("选择候选中的“通用要求”", prompt)
        self.assertIn("普通标准中的固定章节不算", candidates[0]["remark"])

    def test_data_standard_prompt_rejects_unknown_leaf(self):
        with self.assertRaisesRegex(ValueError, "只允许六类"):
            build_data_standard_classification_prompt(
                {},
                [
                    {
                        "id": 999,
                        "pathName": "数据标准/未知类别",
                        "nodeType": "leaf",
                    }
                ],
                standard_context={},
            )

    def test_scope_rules_are_only_enabled_with_explicit_context(self):
        candidates = [
            {
                "id": 100,
                "pathName": "装备目标/水面装备/测试舰级",
                "nodeType": "parent",
            }
        ]
        legacy_prompt = build_architecture_classification_prompt({}, candidates)
        scope_prompt = build_architecture_classification_prompt(
            {},
            candidates,
            classification_context={
                "title": "Test (DDG 51 Flight III) class",
                "primaryIdentifier": "ddg51",
                "qualifier": "Flight III",
                "scopeKind": "flight",
                "matchedScopeParentId": 100,
            },
        )

        self.assertIn("证据足以支持叶子候选时", legacy_prompt)
        self.assertNotIn("Fleetlist", legacy_prompt)
        self.assertNotIn("serverExtractedClassificationContext", legacy_prompt)
        self.assertIn("Fleetlist", scope_prompt)
        self.assertIn("Flight、Block、批次限定词", scope_prompt)
        self.assertIn("dominantDetailKind=technical_specifications", scope_prompt)
        self.assertIn("Contents 中的普通章节", scope_prompt)
        self.assertIn("serverExtractedClassificationContext", scope_prompt)

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

    def test_analysis_prompts_bound_repeat_prone_fields_to_mapper_contract(self):
        legacy_prompt = build_file_analysis_prompt({"fileName": "legacy.pdf"})
        extraction_prompt = build_file_extraction_prompt(
            {"fileName": "split.pdf"},
            resolved_architecture_id=101,
        )

        for prompt in (legacy_prompt, extraction_prompt):
            with self.subTest(prompt_kind=prompt.splitlines()[0]):
                self.assertIn(
                    f"keyword 必须固定输出 {ANALYSIS_KEYWORD_COUNT} 个关键词",
                    prompt,
                )
                self.assertIn(
                    "关键词之间允许语义相近、同义或内容重叠",
                    prompt,
                )
                self.assertIn(
                    "每一项都必须与文档主题、主要对象或关键内容有明确且较强的相关性",
                    prompt,
                )
                self.assertIn(
                    "不得为凑数量添加与文档相关性不大的词",
                    prompt,
                )
                self.assertIn(
                    f"每个关键词不超过 {ANALYSIS_KEYWORD_MAX_CHARS} 个字符",
                    prompt,
                )
                self.assertIn(
                    "associatedEquipment、relatedTechnology、equipmentModel",
                    prompt,
                )
                self.assertIn(
                    f"每个字段最多 {ANALYSIS_ENUM_FIELD_MAX_ITEMS} 个互不重复的条目",
                    prompt,
                )
                self.assertIn(
                    f"每个条目不超过 {ANALYSIS_ENUM_ITEM_MAX_CHARS} 个字符",
                    prompt,
                )
                self.assertIn(
                    f"summary 不超过 {ANALYSIS_SUMMARY_MAX_CHARS} 个字符",
                    prompt,
                )
                self.assertIn(
                    f"完整 JSON 对象不超过 {ANALYSIS_RESPONSE_MAX_CHARS} 个字符",
                    prompt,
                )
                self.assertIn("禁止循环枚举或按编号规律补造实体", prompt)
                self.assertNotIn("至少 10 个关键词", prompt)
                self.assertNotIn("互不重复的关键词", prompt)

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

    def test_architecture_reselect_uses_only_confirmed_identity_and_family_candidates(self):
        prompt = build_architecture_reselect_prompt(
            {
                "architectureId": 999,
                "country": "不应进入重选上下文",
            },
            {
                "identifier": "CVN-68",
                "matchedParentId": "6800",
                "matchedParentPath": "海军装备/航空母舰/CVN-68",
                "evidenceSources": ["originalFileName", "title"],
            },
            self._reselect_candidates(),
        )

        context = json.loads(
            prompt.split("【已确认身份上下文】\n", 1)[1].splitlines()[0]
        )
        candidates = json.loads(prompt.split("【受限候选】\n", 1)[1].strip())
        self.assertEqual(
            context,
            {
                "identifier": "CVN-68",
                "matchedParentId": 6800,
                "matchedParentPath": "海军装备/航空母舰/CVN-68",
                "evidenceSources": ["originalFileName", "title"],
            },
        )
        self.assertEqual(8, len(candidates))
        self.assertEqual("parent", candidates[0]["nodeType"])
        self.assertTrue(all(item["nodeType"] == "leaf" for item in candidates[1:]))
        self.assertTrue(all(isinstance(item["id"], int) for item in candidates))
        self.assertIn('{"architectureId":999}', prompt)
        self.assertIn("叶子证据不足", prompt)
        self.assertIn("必须选择 parent", prompt)
        self.assertIn("architectureId 输出 null", prompt)
        self.assertIn('{"architectureId": null}', prompt)
        self.assertNotIn("不应进入重选上下文", prompt)

    def test_architecture_reselect_rejects_unconfirmed_or_cross_branch_context(self):
        candidates = self._reselect_candidates()
        valid_context = {
            "identifier": "CVN-68",
            "matchedParentId": 6800,
            "matchedParentPath": "海军装备/航空母舰/CVN-68",
            "evidenceSources": ["originalFileName", "title"],
        }

        invalid_cases = (
            (
                {**valid_context, "body": "正文不得进入身份上下文"},
                candidates,
                "不允许的字段",
            ),
            (
                {**valid_context, "evidenceSources": ["originalFileName"]},
                candidates,
                "两个独立身份凭据来源",
            ),
            (
                {**valid_context, "matchedParentId": 6900},
                candidates,
                "与 parent candidate 不一致",
            ),
            (
                valid_context,
                [
                    *candidates[:-1],
                    {
                        "id": 6807,
                        "pathName": "海军装备/航空母舰/CVN-69/部署数据",
                        "nodeType": "leaf",
                    },
                ],
                "直接子节点",
            ),
        )
        for context, candidate_set, message in invalid_cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    build_architecture_reselect_prompt(
                        {"architectureId": 999},
                        context,
                        candidate_set,
                    )

        with self.assertRaisesRegex(ValueError, "恰好包含"):
            build_architecture_reselect_prompt(
                {"architectureId": 999},
                valid_context,
                candidates[:-1],
            )

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
