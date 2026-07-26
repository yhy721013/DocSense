"""术语规则目录的标准单位输出合同。"""

from __future__ import annotations

from pathlib import Path
import re
import unittest


TERMS_DIR = Path(__file__).resolve().parents[1] / "terms"
UNIT_CONTRACT_TEMPLATE = (
    "最终输出必须包含标准单位“{unit}”，数值与单位之间使用一个半角空格，"
    "禁止只输出纯数字。"
)
MULTI_VALUE_CONTRACT = (
    "多个并列值时，每个值都必须分别携带标准单位；"
    "数值范围只在整个范围末尾携带一次标准单位。"
)


def _frontmatter_unit(text: str) -> str:
    match = re.search(r'^unit: "(.*)"$', text, flags=re.MULTILINE)
    if match is None:
        raise AssertionError("规则卡缺少 frontmatter unit")
    return match.group(1)


def _card_value(text: str, label: str) -> str:
    match = re.search(
        rf"^- \*\*{re.escape(label)}\*\*：(.*)$",
        text,
        flags=re.MULTILINE,
    )
    if match is None:
        raise AssertionError(f"规则卡缺少“{label}”")
    return match.group(1).strip()


def _assert_value_uses_unit(
    test_case: unittest.TestCase,
    value: str,
    unit: str,
    *,
    message: str,
) -> None:
    if unit == "海里@节":
        test_case.assertIn(" 海里@", value, msg=message)
        test_case.assertIn(" 节", value, msg=message)
        return
    test_case.assertIn(f" {unit}", value, msg=message)


class TermsRuleUnitContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cards = {
            path: path.read_text(encoding="utf-8")
            for path in sorted(TERMS_DIR.glob("term_rule_*.md"))
        }
        cls.unit_cards = {
            path: (text, _frontmatter_unit(text))
            for path, text in cls.cards.items()
            if _frontmatter_unit(text)
        }
        cls.unitless_cards = {
            path: text
            for path, text in cls.cards.items()
            if not _frontmatter_unit(text)
        }

    def test_catalog_has_expected_unit_and_unitless_card_counts(self) -> None:
        self.assertEqual(61, len(self.cards))
        self.assertEqual(39, len(self.unit_cards))
        self.assertEqual(22, len(self.unitless_cards))

    def test_every_unit_card_declares_concrete_unit_and_spacing_contract(
        self,
    ) -> None:
        for path, (text, unit) in self.unit_cards.items():
            with self.subTest(path=path.name):
                self.assertEqual(unit, _card_value(text, "标准单位"))
                if unit == "海里@节":
                    self.assertIn(
                        "最终输出必须包含标准单位信息：原文同时给出航程和航速时"
                        "使用“海里@节”结构；仅给出航程时至少包含“海里”。"
                        "数值与对应单位之间使用一个半角空格，禁止只输出纯数字。",
                        text,
                    )
                else:
                    self.assertIn(
                        UNIT_CONTRACT_TEMPLATE.format(unit=unit),
                        text,
                    )
                self.assertIn(MULTI_VALUE_CONTRACT, text)
                output_format = _card_value(text, "输出格式")
                if unit == "海里@节":
                    self.assertIn(" 海里@", output_format)
                    self.assertIn(" 节", output_format)
                else:
                    self.assertIn(f" {unit}", output_format)

    def test_unitless_cards_do_not_receive_numeric_unit_contract(self) -> None:
        for path, text in self.unitless_cards.items():
            with self.subTest(path=path.name):
                self.assertEqual("无", _card_value(text, "标准单位"))
                self.assertNotIn("禁止只输出纯数字", text)
                self.assertNotIn(MULTI_VALUE_CONTRACT, text)

    def test_existing_nonempty_samples_include_the_standard_unit(self) -> None:
        for path, (text, unit) in self.unit_cards.items():
            sample = _card_value(text, "标准输出样例")
            if sample == "无":
                continue
            with self.subTest(path=path.name, sample=sample):
                _assert_value_uses_unit(
                    self,
                    sample,
                    unit,
                    message=f"{path.name} 的标准输出样例缺少标准单位",
                )

    def test_empty_samples_remain_unfabricated(self) -> None:
        empty_sample_cards = {
            path.name
            for path, (text, _unit) in self.unit_cards.items()
            if _card_value(text, "标准输出样例") == "无"
        }
        self.assertEqual(
            {
                "term_rule_0043_Total MPM Power or MCR.md",
                "term_rule_0048_Fuel Oil Capacity.md",
                "term_rule_0049_Fuel Consumption.md",
                "term_rule_0053_Cruise Speed.md",
                "term_rule_0054_Endurance.md",
            },
            empty_sample_cards,
        )

    def test_existing_source_examples_show_unit_bearing_output(self) -> None:
        for path, (text, unit) in self.unit_cards.items():
            example = _card_value(text, "原文到标准输出示例")
            if example == "无":
                continue
            with self.subTest(path=path.name, example=example):
                self.assertIn("→", example)
                output = example.rsplit("→", 1)[1].strip()
                _assert_value_uses_unit(
                    self,
                    output,
                    unit,
                    message=f"{path.name} 的转换示例输出缺少标准单位",
                )

    def test_special_unit_rules_match_the_approved_formats(self) -> None:
        cases = {
            "term_rule_0011_Unitary cost.md": (
                "**标准输出样例**：约 55 亿美元",
                "不强行换算或确定数值",
            ),
            "term_rule_0038_Sustained sortie generation rate.md": (
                "**标准输出样例**：160～220 架/日（30天，每天12小时）",
            ),
            "term_rule_0039_Surge sortie generation rate.md": (
                "**标准输出样例**：270～310 架/日（4天，每天24小时）",
            ),
            "term_rule_0047_Total Generator Power or Aggregate Rated Power.md": (
                "**标准输出样例**：8 兆瓦",
            ),
            "term_rule_0052_Maximum Speed.md": (
                "→ 30 节",
            ),
            "term_rule_0054_Endurance.md": (
                "**输出格式**：[航程数值] 海里@[航速数值] 节",
                "允许仅输出“[航程数值] 海里”",
            ),
            "term_rule_0061_Sound Intensity Value.md": (
                "**标准输出样例**：20 TS",
            ),
        }
        for file_name, expected_fragments in cases.items():
            text = self.cards[TERMS_DIR / file_name]
            for fragment in expected_fragments:
                with self.subTest(path=file_name, fragment=fragment):
                    self.assertIn(fragment, text)

        fuel_consumption = self.cards[
            TERMS_DIR / "term_rule_0049_Fuel Consumption.md"
        ]
        self.assertIn("标准单位**：kg/s", fuel_consumption)
        self.assertNotIn("单位：吨/天", fuel_consumption)

        for file_name in (
            "term_rule_0043_Total MPM Power or MCR.md",
            "term_rule_0047_Total Generator Power or Aggregate Rated Power.md",
        ):
            with self.subTest(path=file_name, behavior="force-conversion"):
                self.assertIn("强制换算", self.cards[TERMS_DIR / file_name])


if __name__ == "__main__":
    unittest.main()
