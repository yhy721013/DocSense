"""阶段 1D-2 武器谱框架无关请求 Parser 契约测试。"""

from __future__ import annotations

from copy import deepcopy
import math
import unittest

from app.adapters.web.flask.weaponry_requests import (
    WeaponryRequestValidationError,
    parse_weaponry_request,
)
from app.modules.weaponry.domain import MAX_ARCHITECTURE_ID


def _input_field(**overrides: object) -> dict[str, object]:
    field: dict[str, object] = {
        "templateClassifyId": 1772442376645740,
        "fieldName": "舰级名称",
        "fieldType": "INPUT",
        "fieldDescription": "提取正式舰级，不要与单舰名称混淆",
        "analyseData": "",
        "analyseDataSource": [],
        "extension": {"nested": [1, True, None, {"order": "kept"}]},
    }
    field.update(overrides)
    return field


def _table_field(**overrides: object) -> dict[str, object]:
    field: dict[str, object] = {
        "templateClassifyId": 1772442376645741,
        "fieldName": "雷达设备",
        "fieldType": "TABLE",
        "fieldDescription": "按雷达型号逐行提取",
        "tableFieldList": [
            [
                {
                    "fieldName": "型号",
                    "fieldType": "INPUT",
                    "fieldDescription": "雷达正式型号",
                    "analyseData": None,
                    "analyseDataSource": [],
                },
                {
                    "fieldName": "用途",
                    "fieldType": "INPUT",
                    "fieldDescription": "搜索、跟踪或火控用途",
                },
            ]
        ],
    }
    field.update(overrides)
    return field


def _valid_payload() -> dict[str, object]:
    return {
        "businessType": "weaponry",
        "params": {
            "status": {"legacy": ["ignored", 7]},
            "architectureId": "00042",
            "filePathList": [
                "http://files.local/download/A.pdf?token=secret#fragment",
                "folder\\b%20name.pdf",
                "a.PDF",
            ],
            "weaponryTemplateFieldList": [_input_field(), _table_field()],
            "unknownParam": {"preserve": [3, 2, 1]},
        },
        "unknownRoot": ["preserved"],
    }


class WeaponryRequestAdapterSuccessTests(unittest.TestCase):
    def test_duplicate_table_column_names_keep_legacy_first_column_behavior(self) -> None:
        payload = _valid_payload()
        table_field = payload["params"]["weaponryTemplateFieldList"][1]  # type: ignore[index]
        first_column = table_field["tableFieldList"][0][0]  # type: ignore[index]
        table_field["tableFieldList"][0].append(dict(first_column))  # type: ignore[index]

        parsed = parse_weaponry_request(payload)

        # 内部列规范沿用“同名列仅保留首次出现”，原始模板投影仍完整保留调用方输入。
        self.assertEqual(2, len(parsed.fields[1].columns))
        self.assertEqual(
            3,
            len(parsed.fields[1].template.to_dict()["tableFieldList"][0]),
        )

    def test_valid_request_normalizes_only_internal_identity_and_file_scope(self) -> None:
        payload = _valid_payload()
        original = deepcopy(payload)

        parsed = parse_weaponry_request(payload)

        self.assertEqual(42, parsed.architecture_id)
        self.assertEqual("42", parsed.business_key)
        self.assertEqual(("A.pdf", "b name.pdf"), parsed.selected_file_names)
        self.assertEqual(("INPUT", "TABLE"), tuple(item.field_type for item in parsed.fields))
        self.assertEqual(original, payload)
        self.assertEqual(original, parsed.request_payload.to_dict())
        self.assertEqual(original["params"], parsed.params.to_dict())
        self.assertEqual(
            "00042",
            parsed.request_payload.to_dict()["params"]["architectureId"],
        )

    def test_parser_snapshots_do_not_share_mutable_state_with_request_or_thawed_copy(self) -> None:
        payload = _valid_payload()
        parsed = parse_weaponry_request(payload)

        payload["params"]["weaponryTemplateFieldList"][0]["fieldName"] = "已污染"  # type: ignore[index]
        first_copy = parsed.request_payload.to_dict()
        first_copy["params"]["weaponryTemplateFieldList"][0]["fieldName"] = "二次污染"
        second_copy = parsed.request_payload.to_dict()

        self.assertEqual(
            "舰级名称",
            second_copy["params"]["weaponryTemplateFieldList"][0]["fieldName"],
        )
        self.assertEqual("舰级名称", parsed.fields[0].field_name)

    def test_missing_empty_and_null_file_scope_all_mean_category_freeze(self) -> None:
        for marker in ("missing", "empty", "null"):
            payload = _valid_payload()
            params = payload["params"]
            assert isinstance(params, dict)
            if marker == "missing":
                params.pop("filePathList")
            elif marker == "empty":
                params["filePathList"] = []
            else:
                params["filePathList"] = None

            with self.subTest(marker=marker):
                self.assertEqual((), parse_weaponry_request(payload).selected_file_names)

    def test_architecture_id_accepts_only_approved_integer_forms(self) -> None:
        valid_cases = (
            (1, 1),
            ("0001", 1),
            ("0" * 5000 + "1", 1),
            (MAX_ARCHITECTURE_ID, MAX_ARCHITECTURE_ID),
            (str(MAX_ARCHITECTURE_ID), MAX_ARCHITECTURE_ID),
        )
        for raw, expected in valid_cases:
            payload = _valid_payload()
            payload["params"]["architectureId"] = raw  # type: ignore[index]
            with self.subTest(raw=raw):
                self.assertEqual(expected, parse_weaponry_request(payload).architecture_id)


class WeaponryRequestAdapterFailureTests(unittest.TestCase):
    def assert_validation_error(self, payload: object, message: str) -> None:
        with self.assertRaises(WeaponryRequestValidationError) as context:
            parse_weaponry_request(payload)
        self.assertEqual(message, str(context.exception))

    def test_root_business_params_and_architecture_errors_are_exact(self) -> None:
        cases = (
            (None, "请求体必须是JSON对象"),
            ([], "请求体必须是JSON对象"),
            ({}, "businessType必须为weaponry"),
            ({"businessType": "report"}, "businessType必须为weaponry"),
            ({"businessType": "weaponry"}, "params不能为空"),
            ({"businessType": "weaponry", "params": []}, "params不能为空"),
            (
                {"businessType": "weaponry", "params": {}},
                "architectureId不能为空",
            ),
        )
        for payload, message in cases:
            with self.subTest(payload=payload):
                self.assert_validation_error(payload, message)

    def test_invalid_architecture_id_forms_are_rejected(self) -> None:
        invalid_values = (
            True,
            False,
            0,
            -1,
            1.5,
            "",
            " 1 ",
            "+1",
            "-1",
            "1e3",
            "１",
            str(MAX_ARCHITECTURE_ID + 1),
            "9" * 5000,
        )
        expected = (
            f"architectureId必须为1到{MAX_ARCHITECTURE_ID}之间的正整数"
        )
        for raw in invalid_values:
            payload = _valid_payload()
            payload["params"]["architectureId"] = raw  # type: ignore[index]
            with self.subTest(raw=raw):
                self.assert_validation_error(payload, expected)

    def test_file_path_errors_and_one_based_indexes_are_exact(self) -> None:
        payload = _valid_payload()
        payload["params"]["filePathList"] = "a.pdf"  # type: ignore[index]
        self.assert_validation_error(payload, "filePathList必须为数组")

        for value in ("", " \r\n ", None, 7, False):
            payload = _valid_payload()
            payload["params"]["filePathList"] = ["ok.pdf", value]  # type: ignore[index]
            with self.subTest(value=value):
                self.assert_validation_error(
                    payload,
                    "filePathList中第2项不是有效字符串",
                )

        for value in (
            "/",
            "http://files.local",
            "http://files.local/path/",
            "http://[invalid/path.pdf",
            "http://files.local/%ZZ.pdf",
            "http://files.local/%FF.pdf",
        ):
            payload = _valid_payload()
            payload["params"]["filePathList"] = [value]  # type: ignore[index]
            with self.subTest(value=value):
                self.assert_validation_error(
                    payload,
                    "filePathList中第1项无法提取文件名",
                )

    def test_top_level_field_shape_errors_are_exact_and_never_raise_type_error(self) -> None:
        invalid_cases = (
            (None, "weaponryTemplateFieldList中第1项必须为对象"),
            (
                _input_field(templateClassifyId=True),
                "weaponryTemplateFieldList中第1项templateClassifyId必须为整数",
            ),
            (
                _input_field(templateClassifyId="1"),
                "weaponryTemplateFieldList中第1项templateClassifyId必须为整数",
            ),
            (
                _input_field(fieldName=" \t "),
                "weaponryTemplateFieldList中第1项fieldName不能为空",
            ),
            (
                _input_field(fieldDescription=7),
                "weaponryTemplateFieldList中第1项fieldDescription必须为字符串",
            ),
            (
                _input_field(fieldType="input"),
                "weaponryTemplateFieldList中第1项fieldType必须为INPUT或TABLE",
            ),
            (
                _input_field(fieldType=[]),
                "weaponryTemplateFieldList中第1项fieldType必须为INPUT或TABLE",
            ),
        )
        for field, message in invalid_cases:
            payload = _valid_payload()
            payload["params"]["weaponryTemplateFieldList"] = [field]  # type: ignore[index]
            with self.subTest(message=message):
                self.assert_validation_error(payload, message)

    def test_only_explicitly_approved_analysis_empty_values_are_accepted(self) -> None:
        for key, value in (
            ("analyseData", False),
            ("analyseData", 0),
            ("analyseData", []),
            ("analyseData", " "),
            ("analyseDataSource", ""),
            ("analyseDataSource", {}),
            ("analyseDataSource", False),
        ):
            payload = _valid_payload()
            field = _input_field()
            field[key] = value
            payload["params"]["weaponryTemplateFieldList"] = [field]  # type: ignore[index]
            with self.subTest(key=key, value=value):
                self.assert_validation_error(
                    payload,
                    "analyseData和analyseDataSource必须清空",
                )

    def test_table_shape_errors_are_exact(self) -> None:
        cases = (
            (
                _table_field(tableFieldList=None),
                "weaponryTemplateFieldList中第1项tableFieldList必须为非空数组",
            ),
            (
                _table_field(tableFieldList=[]),
                "weaponryTemplateFieldList中第1项tableFieldList必须为非空数组",
            ),
            (
                _table_field(tableFieldList=[[]]),
                "tableFieldList中第1行必须为非空数组",
            ),
            (
                _table_field(tableFieldList=[[None]]),
                "tableFieldList中第1行第1项必须为对象",
            ),
            (
                _table_field(tableFieldList=[[{"fieldType": "INPUT"}]]),
                "tableFieldList中第1行第1项fieldName不能为空",
            ),
            (
                _table_field(
                    tableFieldList=[[
                        {"fieldName": "型号", "fieldType": "TABLE"}
                    ]]
                ),
                "tableFieldList中第1行第1项fieldType必须为INPUT",
            ),
            (
                _table_field(
                    tableFieldList=[[
                        {
                            "fieldName": "型号",
                            "fieldType": "INPUT",
                            "fieldDescription": 7,
                        }
                    ]]
                ),
                "tableFieldList中第1行第1项fieldDescription必须为字符串",
            ),
        )
        for field, message in cases:
            payload = _valid_payload()
            payload["params"]["weaponryTemplateFieldList"] = [field]  # type: ignore[index]
            with self.subTest(message=message):
                self.assert_validation_error(payload, message)

    def test_non_standard_json_values_are_rejected_before_persistence(self) -> None:
        for invalid in (math.nan, math.inf, -math.inf):
            payload = _valid_payload()
            payload["params"]["unknownParam"] = invalid  # type: ignore[index]
            with self.subTest(invalid=invalid):
                self.assert_validation_error(payload, "请求体必须是JSON对象")


if __name__ == "__main__":
    unittest.main()
