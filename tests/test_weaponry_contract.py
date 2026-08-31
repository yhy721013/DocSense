"""阶段 1D-1：纯领域回调黄金样例和模式 1 删除证据。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.modules.weaponry.domain import (
    DeprecatedWeaponryModeError,
    WeaponryAnalyseDataSource,
    WeaponryExecutionIdentity,
    WeaponryFieldResult,
    WeaponryFieldSpecification,
    WeaponryResult,
    WeaponryTableCellResult,
    resolve_legacy_extraction_strategy,
)


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "tests" / "contracts" / "stage1d_weaponry_contracts.json"


def _source(value: dict) -> WeaponryAnalyseDataSource:
    return WeaponryAnalyseDataSource(
        content=value["content"],
        source=value["source"],
        occurred_at=value["time"],
        file_name=value["fileName"],
        rows=tuple(value["rows"]),
        translation=value["translate"],
    )


class WeaponryCallbackGoldenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    def test_input_success_callback_matches_frozen_golden_payload(self) -> None:
        expected = self.contract["goldenCallbacks"]["inputSuccess"]
        raw_field = expected["data"]["weaponryTemplateFieldList"][0]
        field = WeaponryFieldResult(
            specification=WeaponryFieldSpecification.from_mapping(raw_field),
            analyse_data=raw_field["analyseData"],
            sources=tuple(_source(item) for item in raw_field["analyseDataSource"]),
        )
        result = WeaponryResult(
            identity=WeaponryExecutionIdentity("task-input", 10502),
            status="2",
            fields=(field,),
            message="解析成功",
        )

        self.assertEqual(expected, result.to_callback().to_public_dict())

    def test_table_success_callback_matches_frozen_golden_payload(self) -> None:
        expected = self.contract["goldenCallbacks"]["tableSuccess"]
        raw_field = expected["data"]["weaponryTemplateFieldList"][0]
        specification = WeaponryFieldSpecification.from_mapping(raw_field)
        rows = []
        for raw_row in raw_field["tableFieldList"]:
            cells = []
            for index, raw_cell in enumerate(raw_row):
                cells.append(
                    WeaponryTableCellResult(
                        specification=specification.columns[index],
                        analyse_data=raw_cell["analyseData"],
                        sources=tuple(
                            _source(item) for item in raw_cell["analyseDataSource"]
                        ),
                    )
                )
            rows.append(tuple(cells))
        result = WeaponryResult(
            identity=WeaponryExecutionIdentity("task-table", 10502),
            status="2",
            fields=(
                WeaponryFieldResult(
                    specification=specification,
                    table_rows=tuple(rows),
                ),
            ),
            message="解析成功",
        )

        self.assertEqual(expected, result.to_callback().to_public_dict())

    def test_failure_callback_matches_frozen_golden_payload(self) -> None:
        expected = self.contract["goldenCallbacks"]["failure"]
        result = WeaponryResult(
            identity=WeaponryExecutionIdentity("task-failed", 10502),
            status="3",
            message="解析失败",
        )

        self.assertEqual(expected, result.to_callback().to_public_dict())


class WeaponryModeOneDeletionTests(unittest.TestCase):
    def test_only_legacy_mode_two_can_resolve(self) -> None:
        for value in (None, "", "2", " 2 "):
            with self.subTest(value=value):
                self.assertEqual(
                    "file-aggregate-v1",
                    resolve_legacy_extraction_strategy(value),
                )
        with self.assertRaisesRegex(DeprecatedWeaponryModeError, "已废弃"):
            resolve_legacy_extraction_strategy("1")

    def test_production_sources_no_longer_contain_mode_one_execution_branch(self) -> None:
        legacy_service = (
            ROOT / "app" / "services" / "llm_service" / "weaponry_service.py"
        )
        prompt_source = (
            ROOT / "app" / "services" / "core" / "prompts.py"
        ).read_text(encoding="utf-8")

        # 1G-5A4 已物理删除旧 Worker；不存在的文件比检查旧文件内部不含分支更强，
        # 同时保留 Prompt 公共工具不得重新引入模式 1 的永久门禁。
        self.assertFalse(legacy_service.exists())
        for forbidden in (
            "build_chunk_based_field_prompt",
            "build_multi_chunk_based_field_prompt",
            "build_input_field_prompt",
            "build_table_column_prompt",
            "build_table_extraction_prompt",
            "_build_terms_rule_part",
            "_map_source_to_analyse_data_source",
            "_build_analyse_data_sources",
            "if analyse_mode",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, prompt_source)


if __name__ == "__main__":
    unittest.main()
