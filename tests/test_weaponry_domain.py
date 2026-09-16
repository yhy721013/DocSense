"""阶段 1D-1：武器谱不可变 DTO、Prompt、来源与 TABLE 纯规则测试。"""

from __future__ import annotations

import unittest

from app.modules.weaponry.domain import (
    FORCED_EMPTY_FIELD_NAMES,
    AuxiliaryGuidance,
    EvidenceCandidate,
    EvidenceSelectionResult,
    ExtractionPrompt,
    FrozenJsonObject,
    MergedTableRow,
    ParsedTableRow,
    SelectedEvidence,
    SourceNameMapping,
    TableRowResult,
    WeaponryAnalyseDataSource,
    WeaponryDocumentSnapshot,
    WeaponryDomainValidationError,
    WeaponryExecutionIdentity,
    WeaponryFieldResult,
    WeaponryFieldSpecification,
    WeaponryResult,
    assemble_table_rows,
    build_forced_empty_result,
    build_table_empty_fallback_result,
    build_input_extraction_prompt,
    build_table_extraction_prompt,
    external_processing_specification,
    is_forced_empty_field_name,
    normalize_architecture_id_value,
    normalize_evidence_rows,
    merge_table_rows,
    normalize_table_cell_value,
    parse_table_json_rows,
)


def _selected(
    candidate_id: str,
    text: str,
    *,
    document_key: str = "doc-a",
    provider_rank: int = 1,
) -> SelectedEvidence:
    return SelectedEvidence(
        candidate_id=candidate_id,
        document_key=document_key,
        text=text,
        provider_rank=provider_rank,
        provider_score=0.9,
        score_profile_id="test-profile-v1",
        score_mode="score",
        original_index=provider_rank - 1,
    )


def _input_specification() -> WeaponryFieldSpecification:
    return WeaponryFieldSpecification.from_mapping(
        {
            "templateClassifyId": 1772442376645740,
            "fieldName": "舰级名称",
            "fieldType": "INPUT",
            "fieldDescription": "提取装备所属舰级的正式名称，不要与单舰名称混淆",
            "futureExtension": {"ordered": [1, {"enabled": True}]},
            "analyseData": "",
            "analyseDataSource": [],
        }
    )


def _table_specification() -> WeaponryFieldSpecification:
    return WeaponryFieldSpecification.from_mapping(
        {
            "templateClassifyId": 1772442376645741,
            "fieldName": "雷达设备",
            "fieldType": "TABLE",
            "fieldDescription": "按雷达型号逐行提取，不合并不同设备",
            "tableFieldList": [
                [
                    {
                        "fieldName": "型号",
                        "fieldType": "INPUT",
                        "fieldDescription": "雷达的正式型号",
                        "futureColumnKey": "keep-me",
                    },
                    {
                        "fieldName": "用途",
                        "fieldType": "INPUT",
                        "fieldDescription": "搜索、跟踪或火控用途",
                    },
                ]
            ],
        }
    )


class WeaponryImmutableModelTests(unittest.TestCase):
    def test_architecture_id_normalization_is_shared_and_bounded(self) -> None:
        self.assertEqual(10502, normalize_architecture_id_value(10502))
        self.assertEqual(10502, normalize_architecture_id_value("00010502"))

        invalid_values = (
            True,
            0,
            -1,
            1.0,
            " 10502",
            "+10502",
            "1.0",
            "９",
            "9" * 10_000,
        )
        for value in invalid_values:
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaises(WeaponryDomainValidationError):
                    normalize_architecture_id_value(value)

    def test_success_result_cannot_exist_without_any_field_object(self) -> None:
        with self.assertRaisesRegex(
            WeaponryDomainValidationError,
            "成功结果必须携带字段结果",
        ):
            WeaponryResult(
                identity=WeaponryExecutionIdentity("task-empty-success", 1),
                status="2",
                fields=(),
            )

    def test_field_template_is_deep_frozen_and_preserves_unknown_key_order(self) -> None:
        raw = {
            "templateClassifyId": 1,
            "fieldName": "舰级名称",
            "fieldType": "INPUT",
            "fieldDescription": "正式舰级",
            "futureExtension": {"ordered": [1, {"enabled": True}]},
        }
        specification = WeaponryFieldSpecification.from_mapping(raw)

        raw["fieldName"] = "被外部修改"
        raw["futureExtension"]["ordered"][1]["enabled"] = False
        first_projection = specification.template.to_dict()
        first_projection["futureExtension"]["ordered"].append("污染副本")
        second_projection = specification.template.to_dict()

        self.assertEqual("舰级名称", specification.field_name)
        self.assertEqual("舰级名称", second_projection["fieldName"])
        self.assertTrue(second_projection["futureExtension"]["ordered"][1]["enabled"])
        self.assertEqual(tuple(raw.keys()), tuple(second_projection.keys()))

    def test_strict_json_freeze_rejects_non_finite_numbers(self) -> None:
        with self.assertRaisesRegex(WeaponryDomainValidationError, "NaN"):
            FrozenJsonObject.from_mapping(
                {"fieldName": "字段", "score": float("nan")},
                name="field",
            )
        with self.assertRaisesRegex(WeaponryDomainValidationError, "已经冻结"):
            FrozenJsonObject((("unsafe", []),))  # type: ignore[arg-type]

    def test_execution_and_document_identity_are_immutable_and_bounded(self) -> None:
        identity = WeaponryExecutionIdentity(
            task_id="task-001",
            architecture_id=10502,
        )
        document = WeaponryDocumentSnapshot(
            sequence_no=1,
            document_key="doc-1",
            file_name="hash.pdf",
            original_name=" 甲方原值.pdf ",
            ingested_file_name="parsed.retrieval.md",
            source_architecture_id=7,
            external_document_ref="custom-documents/doc.json",
        )

        self.assertEqual("10502", identity.business_key)
        self.assertEqual(" 甲方原值.pdf ", document.original_name)
        with self.assertRaisesRegex(WeaponryDomainValidationError, "sequence_no"):
            WeaponryDocumentSnapshot(
                sequence_no=0,
                document_key="doc-1",
                file_name="hash.pdf",
                original_name="原名.pdf",
                ingested_file_name="parsed.md",
                source_architecture_id=7,
                external_document_ref="ref",
            )
        with self.assertRaisesRegex(
            WeaponryDomainValidationError,
            "source_architecture_id",
        ):
            WeaponryDocumentSnapshot(
                sequence_no=1,
                document_key="doc-1",
                file_name="hash.pdf",
                original_name="原名.pdf",
                ingested_file_name="parsed.md",
                source_architecture_id=9_223_372_036_854_775_808,
                external_document_ref="ref",
            )

    def test_field_description_and_unknown_keys_survive_result_projection(self) -> None:
        specification = _input_specification()
        result = WeaponryFieldResult(
            specification=specification,
            analyse_data="尼米兹级",
            sources=(
                WeaponryAnalyseDataSource(
                    content="该舰属于尼米兹级",
                    source="资料.pdf",
                    occurred_at="2026-07-18 12:00:00",
                    file_name="hash.pdf",
                    rows=("该舰是尼米兹级航空母舰。",),
                    translation="",
                ),
            ),
        ).to_public_dict()

        self.assertEqual(
            "提取装备所属舰级的正式名称，不要与单舰名称混淆",
            result["fieldDescription"],
        )
        self.assertEqual(
            {"ordered": [1, {"enabled": True}]},
            result["futureExtension"],
        )
        self.assertEqual("尼米兹级", result["analyseData"])

    def test_field_specification_rejects_non_string_field_name(self) -> None:
        """领域层拒绝数字字段名，避免把接口脏数据静默转换为另一种语义。"""

        with self.assertRaises(WeaponryDomainValidationError):
            WeaponryFieldSpecification.from_mapping(
                {"fieldName": 123, "fieldType": "INPUT"}
            )

        with self.assertRaises(WeaponryDomainValidationError):
            WeaponryFieldSpecification.from_mapping(
                {
                    "fieldName": "装备表",
                    "fieldType": "TABLE",
                    "tableFieldList": [[{"fieldName": 123}]],
                }
            )

        for invalid_field in (
            {"fieldName": "舰级名称"},
            {"fieldName": "舰级名称", "fieldType": "input"},
            {
                "fieldName": "装备表",
                "fieldType": "TABLE",
                "tableFieldList": [[]],
            },
            {
                "fieldName": "装备表",
                "fieldType": "TABLE",
                "tableFieldList": [[{"fieldName": "型号"}]],
            },
        ):
            with self.subTest(invalid_field=invalid_field):
                with self.assertRaises(WeaponryDomainValidationError):
                    WeaponryFieldSpecification.from_mapping(invalid_field)

    def test_direct_prompt_and_selection_result_freeze_sequence_inputs(self) -> None:
        """frozen dataclass 也必须复制调用方传入的可变列表。"""

        evidence_ids = ["candidate-1"]
        rows = ["目标证据"]
        prompt = ExtractionPrompt(
            text="仅基于目标证据抽取。",
            field_type="INPUT",
            document_key="doc-a",
            evidence_ids=evidence_ids,  # type: ignore[arg-type]
            rows=rows,  # type: ignore[arg-type]
        )
        result = EvidenceSelectionResult(selected=[], rejected=[])  # type: ignore[arg-type]
        evidence_ids.append("candidate-2")
        rows.append("污染证据")

        self.assertEqual(("candidate-1",), prompt.evidence_ids)
        self.assertEqual(("目标证据",), prompt.rows)
        self.assertEqual((), result.selected)
        self.assertEqual((), result.rejected)


class WeaponryForcedEmptyFieldPolicyTests(unittest.TestCase):
    def test_forced_empty_names_use_trimmed_exact_matching(self) -> None:
        self.assertEqual(
            {
                "装备编号",
                "一级分类",
                "二级分类",
                "三级分类",
                "四级分类",
            },
            set(FORCED_EMPTY_FIELD_NAMES),
        )
        for field_name in FORCED_EMPTY_FIELD_NAMES:
            with self.subTest(field_name=field_name):
                self.assertTrue(is_forced_empty_field_name(field_name))
                self.assertTrue(
                    is_forced_empty_field_name(f" \t{field_name}\n")
                )

        for field_name in (
            "装备编号说明",
            "主装备编号",
            "一级分类名称",
            "五级分类",
            "",
            None,
            123,
        ):
            with self.subTest(field_name=field_name):
                self.assertFalse(is_forced_empty_field_name(field_name))

    def test_mixed_table_external_specification_contains_only_normal_columns(
        self,
    ) -> None:
        specification = WeaponryFieldSpecification.from_mapping(
            {
                "templateClassifyId": 1772442376645742,
                "fieldName": "装备明细",
                "fieldType": "TABLE",
                "futureExtension": {"preserved": True},
                "tableFieldList": [
                    [
                        {
                            "fieldName": " 装备编号 ",
                            "fieldType": "INPUT",
                            "futureColumnKey": "forced",
                        },
                        {
                            "fieldName": "型号",
                            "fieldType": "INPUT",
                            "futureColumnKey": "normal",
                        },
                    ],
                    [
                        {
                            "fieldName": "一级分类",
                            "fieldType": "INPUT",
                        },
                        {
                            "fieldName": "用途",
                            "fieldType": "INPUT",
                        },
                    ],
                ],
            }
        )

        narrowed = external_processing_specification(specification)

        self.assertIsNotNone(narrowed)
        assert narrowed is not None
        self.assertEqual(("型号", "用途"), tuple(
            column.field_name for column in narrowed.columns
        ))
        narrowed_template = narrowed.template.to_dict()
        self.assertEqual({"preserved": True}, narrowed_template["futureExtension"])
        self.assertEqual(
            [["型号"], ["用途"]],
            [
                [cell["fieldName"] for cell in row]
                for row in narrowed_template["tableFieldList"]
            ],
        )
        normal_specification = _table_specification()
        self.assertIs(
            normal_specification,
            external_processing_specification(normal_specification),
        )

    def test_fully_forced_fields_build_standard_empty_public_results(self) -> None:
        input_specification = WeaponryFieldSpecification.from_mapping(
            {
                "fieldName": " 装备编号 ",
                "fieldType": "INPUT",
                "analyseData": "调用方脏值不会进入受理，但领域结果必须覆盖",
                "analyseDataSource": [{"content": "脏来源"}],
            }
        )
        input_result = build_forced_empty_result(
            input_specification
        ).to_public_dict()
        self.assertEqual("", input_result["analyseData"])
        self.assertEqual(
            {
                "content": "",
                "source": "",
                "time": "",
                "fileName": "",
                "rows": [],
                "translate": "",
            },
            input_result["analyseDataSource"][0],
        )

        table_specification = WeaponryFieldSpecification.from_mapping(
            {
                "fieldName": "分类信息",
                "fieldType": "TABLE",
                "tableFieldList": [
                    [
                        {
                            "fieldName": field_name,
                            "fieldType": "INPUT",
                            "futureColumnKey": index,
                        }
                        for index, field_name in enumerate(
                            (
                                "装备编号",
                                "一级分类",
                                "二级分类",
                                "三级分类",
                                "四级分类",
                            ),
                            start=1,
                        )
                    ]
                ],
            }
        )
        table_result = build_forced_empty_result(
            table_specification
        ).to_public_dict()

        self.assertIsNone(external_processing_specification(table_specification))
        self.assertEqual(1, len(table_result["tableFieldList"]))
        for index, cell in enumerate(table_result["tableFieldList"][0], start=1):
            with self.subTest(index=index):
                self.assertEqual(index, cell["futureColumnKey"])
                self.assertEqual("", cell["analyseData"])
                self.assertEqual(
                    {
                        "content": "",
                        "source": "",
                        "time": "",
                        "fileName": "",
                        "rows": [],
                        "translate": "",
                    },
                    cell["analyseDataSource"][0],
                )

    def test_final_table_assembly_overrides_polluted_forced_cells(self) -> None:
        specification = WeaponryFieldSpecification.from_mapping(
            {
                "fieldName": "装备明细",
                "fieldType": "TABLE",
                "tableFieldList": [
                    [
                        {
                            "fieldName": "装备编号",
                            "fieldType": "INPUT",
                        },
                        {
                            "fieldName": "型号",
                            "fieldType": "INPUT",
                        },
                    ]
                ],
            }
        )
        polluted_source = WeaponryAnalyseDataSource(
            content="MALICIOUS-001",
            source="模型污染.pdf",
            occurred_at="",
            file_name="polluted.pdf",
            rows=("不应进入最终保留字段",),
            translation="polluted",
        )
        normal_source = WeaponryAnalyseDataSource(
            content="AN/SPY-1",
            source="手册.pdf",
            occurred_at="",
            file_name="manual.pdf",
            rows=("AN/SPY-1 雷达",),
            translation="",
        )
        assembled = assemble_table_rows(
            (
                MergedTableRow(
                    values=(
                        ("装备编号", "MALICIOUS-001"),
                        ("型号", "AN/SPY-1"),
                    ),
                    sources=(
                        ("装备编号", (polluted_source,)),
                        ("型号", (normal_source,)),
                    ),
                ),
            ),
            specification,
        )

        self.assertEqual(1, len(assembled))
        forced_cell, normal_cell = assembled[0]
        self.assertEqual("", forced_cell.analyse_data)
        self.assertEqual(
            (WeaponryAnalyseDataSource.empty(),),
            forced_cell.sources,
        )
        self.assertEqual("AN/SPY-1", normal_cell.analyse_data)
        self.assertEqual((normal_source,), normal_cell.sources)

    def test_mixed_table_empty_fallback_preserves_full_columns_and_source_semantics(
        self,
    ) -> None:
        specification = WeaponryFieldSpecification.from_mapping(
            {
                "fieldName": "装备明细",
                "fieldType": "TABLE",
                "tableFieldList": [
                    [
                        {
                            "fieldName": "装备编号",
                            "fieldType": "INPUT",
                            "futureColumnKey": "forced",
                        },
                        {
                            "fieldName": "型号",
                            "fieldType": "INPUT",
                            "futureColumnKey": "normal",
                        },
                    ]
                ],
            }
        )

        public_result = build_table_empty_fallback_result(
            specification
        ).to_public_dict()
        forced_cell, normal_cell = public_result["tableFieldList"][0]

        self.assertEqual(["装备编号", "型号"], [
            forced_cell["fieldName"],
            normal_cell["fieldName"],
        ])
        self.assertEqual("forced", forced_cell["futureColumnKey"])
        self.assertEqual("", forced_cell["analyseData"])
        self.assertEqual(
            [WeaponryAnalyseDataSource.empty().to_public_dict()],
            forced_cell["analyseDataSource"],
        )
        self.assertEqual("normal", normal_cell["futureColumnKey"])
        self.assertEqual("", normal_cell["analyseData"])
        self.assertEqual([], normal_cell["analyseDataSource"])


class WeaponrySourceAndPromptRuleTests(unittest.TestCase):
    def test_source_mapping_supports_provider_aliases_without_guessing_public_name(self) -> None:
        mapping = SourceNameMapping.from_aliases(
            original_aliases=(("custom-documents/task/parsed.md", "甲方原名.pdf"),),
            file_aliases=(("parsed.md", "hash.pdf"),),
            fallback_original_name="唯一原名.pdf",
            fallback_file_name="only-hash.pdf",
        )

        self.assertEqual("甲方原名.pdf", mapping.resolve_original_name("parsed.md"))
        self.assertEqual("hash.pdf", mapping.resolve_file_name("task/parsed.md"))
        self.assertEqual("唯一原名.pdf", mapping.resolve_original_name(""))
        self.assertEqual("unknown.pdf", mapping.resolve_file_name("unknown.pdf"))

    def test_source_mapping_rejects_one_alias_pointing_to_multiple_documents(self) -> None:
        with self.assertRaisesRegex(WeaponryDomainValidationError, "多个目标"):
            SourceNameMapping.from_aliases(
                original_aliases=(
                    ("task/parsed.md", "甲方原名A.pdf"),
                    ("parsed.md", "甲方原名B.pdf"),
                ),
                file_aliases=(),
            )

    def test_evidence_rows_drop_only_blanks_and_never_truncate(self) -> None:
        rows = normalize_evidence_rows(
            ("  第一条证据  ", "", "第二条证据", "第三条证据"),
        )

        self.assertEqual(("第一条证据", "第二条证据", "第三条证据"), rows)

    def test_input_prompt_accepts_only_selected_evidence_and_keeps_rows_exact(self) -> None:
        specification = _input_specification()
        selected = (
            _selected("c-1", "尼米兹号属于尼米兹级。"),
            _selected("c-2", "尼米兹级是核动力航空母舰舰级。", provider_rank=2),
        )
        prompt = build_input_extraction_prompt(
            specification,
            selected,
            guidance=(AuxiliaryGuidance("g-1", "舰级指同型舰艇的级别。"),),
        )

        self.assertEqual(tuple(item.text for item in selected), prompt.rows)
        self.assertIn(specification.field_description, prompt.text)
        self.assertIn("目标证据", prompt.text)
        self.assertIn("辅助语境只用于理解字段口径", prompt.text)
        self.assertNotIn("workspace", prompt.text.lower())

        candidate = EvidenceCandidate(
            candidate_id="raw-candidate",
            document_key="doc-a",
            text="尚未选择的候选",
            provider_rank=1,
            provider_score=0.9,
            provider_score_present=True,
            score_profile_id="test-profile-v1",
        )
        with self.assertRaisesRegex(WeaponryDomainValidationError, "SelectedEvidence"):
            build_input_extraction_prompt(specification, (candidate,))  # type: ignore[arg-type]

    def test_extraction_prompt_rejects_cross_document_evidence(self) -> None:
        with self.assertRaisesRegex(WeaponryDomainValidationError, "同一 document_key"):
            build_input_extraction_prompt(
                _input_specification(),
                (
                    _selected("a", "文档A证据", document_key="doc-a"),
                    _selected("b", "文档B证据", document_key="doc-b", provider_rank=2),
                ),
            )

    def test_table_prompt_contains_table_and_every_column_description(self) -> None:
        prompt = build_table_extraction_prompt(
            _table_specification(),
            (_selected("radar", "AN/SPY-1 用于搜索与跟踪。"),),
        )

        for fragment in (
            "雷达设备",
            "按雷达型号逐行提取",
            "型号",
            "雷达的正式型号",
            "用途",
            "搜索、跟踪或火控用途",
            "目标证据",
        ):
            self.assertIn(fragment, prompt.text)


class WeaponryTableRuleTests(unittest.TestCase):
    def test_broad_type_column_never_merges_distinct_rows(self) -> None:
        specification = WeaponryFieldSpecification.from_mapping(
            {
                "templateClassifyId": 2,
                "fieldName": "设备清单",
                "fieldType": "TABLE",
                "tableFieldList": [
                    [
                        {"fieldName": "类型", "fieldType": "INPUT"},
                        {"fieldName": "用途", "fieldType": "INPUT"},
                    ]
                ],
            }
        )
        rows = (
            ParsedTableRow(
                row_key="",
                values=(("类型", "雷达"), ("用途", "搜索")),
            ),
            ParsedTableRow(
                row_key="",
                values=(("类型", "雷达"), ("用途", "火控")),
            ),
        )

        merged = merge_table_rows(
            tuple(
                TableRowResult(
                    row=row,
                    source_name=f"手册{index}.pdf",
                    file_name=f"{index}.pdf",
                    evidence_rows=(f"证据{index}",),
                )
                for index, row in enumerate(rows, start=1)
            ),
            specification,
        )

        self.assertEqual(2, len(merged))
        self.assertEqual(("搜索", "火控"), tuple(row.value_for("用途") for row in merged))

    def test_parse_merge_and_assemble_are_deterministic_and_preserve_column_extensions(self) -> None:
        specification = _table_specification()
        parsed = parse_table_json_rows(
            """```json
            {"rows":[
              {"__rowKey":"AN/SPY-1", "型号":"AN/SPY-1", "用途":"搜索与跟踪"},
              {"rowKey":"AN/SPS-49", "型号":"AN/SPS-49", "用途":null}
            ]}
            ```""",
            specification,
        )
        self.assertEqual(2, len(parsed))
        self.assertEqual("AN/SPY-1", parsed[0].get("型号"))

        merged = merge_table_rows(
            (
                TableRowResult(
                    row=parsed[0],
                    source_name="手册A.pdf",
                    file_name="a.pdf",
                    evidence_rows=("AN/SPY-1用于搜索。",),
                    translations=(("型号", ""), ("用途", "")),
                ),
                TableRowResult(
                    row=ParsedTableRow(
                        row_key="AN/SPY-1",
                        values=(("型号", "AN/SPY-1"), ("用途", "火控")),
                    ),
                    source_name="手册B.pdf",
                    file_name="b.pdf",
                    evidence_rows=("AN/SPY-1也用于火控。",),
                ),
                TableRowResult(
                    row=parsed[1],
                    source_name="手册A.pdf",
                    file_name="a.pdf",
                    evidence_rows=("AN/SPS-49为另一设备。",),
                ),
            ),
            specification,
        )
        assembled = assemble_table_rows(merged, specification)

        self.assertEqual(2, len(assembled))
        first_model = assembled[0][0].to_public_dict()
        first_usage = assembled[0][1].to_public_dict()
        self.assertEqual("keep-me", first_model["futureColumnKey"])
        self.assertEqual("搜索与跟踪", first_usage["analyseData"])
        self.assertEqual(2, len(first_usage["analyseDataSource"]))
        self.assertEqual(
            ["手册A.pdf", "手册B.pdf"],
            [item["source"] for item in first_usage["analyseDataSource"]],
        )

    def test_table_parser_rejects_non_json_and_normalizer_rejects_non_finite(self) -> None:
        self.assertEqual((), parse_table_json_rows("not-json", _table_specification()))
        self.assertEqual(
            (),
            parse_table_json_rows(
                '{"rows":[],"型号":"不应被当作数据行"}',
                _table_specification(),
            ),
        )
        self.assertEqual("", normalize_table_cell_value(float("inf")))

    def test_table_intermediate_models_reject_ambiguous_pair_containers(self) -> None:
        """重复列和无序映射不得在组装时被 ``dict`` 静默覆盖。"""

        parsed = ParsedTableRow(
            row_key="radar",
            values=(("型号", "AN/SPY-1"), ("用途", "搜索")),
        )
        with self.assertRaises(WeaponryDomainValidationError):
            TableRowResult(
                row=parsed,
                source_name="手册.pdf",
                file_name="a.pdf",
                evidence_rows=("证据",),
                translations={"型号": ""},  # type: ignore[arg-type]
            )

        with self.assertRaises(WeaponryDomainValidationError):
            MergedTableRow(
                values=(("型号", "AN/SPY-1"), ("型号", "AN/SPY-1")),
                sources=(("型号", (WeaponryAnalyseDataSource.empty(),)),),
            )


if __name__ == "__main__":
    unittest.main()
