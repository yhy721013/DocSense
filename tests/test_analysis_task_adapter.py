"""阶段 1F-2：文件分析任务快照与严格 Codec 的离线测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import copy
import json
import unittest

from app.modules.analysis.adapters import (
    AnalysisTaskInputCodec,
    AnalysisTaskInputCodecError,
)
from app.modules.analysis.domain.task_inputs import (
    ANALYSIS_TASK_INPUT_SCHEMA_VERSION,
    ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V2,
    ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V3,
    AnalysisDocumentProcessingPolicySnapshot,
    AnalysisPolicySnapshot,
    AnalysisSubmissionSnapshot,
    AnalysisTaskInputV1,
    AnalysisTaskInputV2,
    AnalysisTaskInputV3,
    AnalysisTaskInputV4,
)


def _params(
    file_name: str = " demo.txt",
    *,
    original_file_name: object = "原始 demo.txt",
    include_original_file_name: bool = True,
    extension_index: int = 1,
) -> dict[str, object]:
    """构造带未知扩展字段的公开请求项，验证 Codec 不会裁剪调用方数据。"""

    result: dict[str, object] = {
        "fileName": file_name,
        "filePath": f"https://example.invalid/files/{extension_index}.txt ",
        "unknownExtension": {
            "keepEmpty": "",
            "nested": [extension_index, {"index": extension_index}],
        },
    }
    if include_original_file_name:
        result["originalFileName"] = original_file_name
    return result


def _task_input(index: int = 1) -> AnalysisTaskInputV1:
    """组合可独立持久化和重放的完整 V1 输入。"""

    submission = AnalysisSubmissionSnapshot.from_request_params(
        _params(extension_index=index),
        policy_snapshot=AnalysisPolicySnapshot.default(),
    )
    return AnalysisTaskInputV1.from_submission(
        submission,
        task_id=f"analysis-task-{index}",
        batch_id=f"{index:032x}",
        # 每个夹具都属于独立批次，因此批内序号固定为 1；index 仅用于构造隔离身份。
        batch_sequence=1,
        accepted_at="2026-07-26T10:00:00+08:00",
        trace_id=f"trace-analysis-{index}",
    )


def _task_input_v2(
    index: int = 1,
    *,
    suffix: str = "xls",
) -> AnalysisTaskInputV2:
    """构造携带不可变文档处理策略的历史 V2 输入。"""

    params = _params(extension_index=index)
    params["filePath"] = f"https://example.invalid/files/{index}.{suffix}"
    submission = AnalysisSubmissionSnapshot.from_request_params(
        params,
        policy_snapshot=AnalysisPolicySnapshot.default(),
        document_processing_policy=(
            AnalysisDocumentProcessingPolicySnapshot.for_source(
                str(params["filePath"]),
                allowed_version_series="26.2",
            )
        ),
    )
    return AnalysisTaskInputV2.from_submission(
        submission,
        task_id=f"analysis-task-v2-{index}",
        batch_id=f"{index:032x}",
        batch_sequence=1,
        accepted_at="2026-07-28T10:00:00+08:00",
        trace_id=f"trace-analysis-v2-{index}",
    )


def _task_input_v3(
    index: int = 1,
    *,
    suffix: str = "xls",
) -> AnalysisTaskInputV3:
    """构造历史 V3 输入，冻结业务原名传输语义。"""

    params = _params(extension_index=index)
    params["filePath"] = f"https://example.invalid/files/{index}.{suffix}"
    submission = AnalysisSubmissionSnapshot.from_request_params(
        params,
        policy_snapshot=AnalysisPolicySnapshot.default(),
        document_processing_policy=(
            AnalysisDocumentProcessingPolicySnapshot.for_source(
                str(params["filePath"]),
                allowed_version_series="26.2",
            )
        ),
    )
    return AnalysisTaskInputV3.from_submission(
        submission,
        task_id=f"analysis-task-v3-{index}",
        batch_id=f"{index:032x}",
        batch_sequence=1,
        accepted_at="2026-07-30T10:00:00+08:00",
        trace_id=f"trace-analysis-v3-{index}",
    )


def _task_input_v4(
    index: int = 1,
    *,
    suffix: str = "xls",
) -> AnalysisTaskInputV4:
    """构造当前 V4 输入，冻结业务键传输名与原始展示标题。"""

    params = _params(extension_index=index)
    params["filePath"] = f"https://example.invalid/files/{index}.{suffix}"
    submission = AnalysisSubmissionSnapshot.from_request_params(
        params,
        policy_snapshot=AnalysisPolicySnapshot.default(),
        document_processing_policy=(
            AnalysisDocumentProcessingPolicySnapshot.for_source(
                str(params["filePath"]),
                allowed_version_series="26.2",
            )
        ),
    )
    return AnalysisTaskInputV4.from_submission(
        submission,
        task_id=f"analysis-task-v4-{index}",
        batch_id=f"{index:032x}",
        batch_sequence=1,
        accepted_at="2026-07-30T13:00:00+08:00",
        trace_id=f"trace-analysis-v4-{index}",
    )


class AnalysisTaskSnapshotTests(unittest.TestCase):
    """锁定受理时深冻结、名称语义与策略快照的边界。"""

    def test_submission_snapshot_deep_freezes_unknown_extensions(self) -> None:
        raw_params = _params()
        submission = AnalysisSubmissionSnapshot.from_request_params(
            raw_params,
            policy_snapshot=AnalysisPolicySnapshot.default(),
        )
        raw_params["unknownExtension"]["nested"][1]["index"] = 999  # type: ignore[index]
        raw_params["unknownExtension"]["new"] = "不应泄漏"  # type: ignore[index]

        frozen_params = submission.raw_params.to_dict()
        self.assertEqual(" demo.txt", frozen_params["fileName"])
        self.assertEqual(1, frozen_params["unknownExtension"]["nested"][1]["index"])  # type: ignore[index]
        self.assertNotIn("new", frozen_params["unknownExtension"])  # type: ignore[operator]
        self.assertEqual("demo.txt", submission.file_name)
        self.assertEqual("https://example.invalid/files/1.txt", submission.file_path)

    def test_processing_policy_recognizes_legacy_business_name_on_extensionless_url(self) -> None:
        """签名 URL 不带扩展名时，冻结的业务 fileName 仍必须触发本地转换。"""

        policy = AnalysisDocumentProcessingPolicySnapshot.for_source(
            "https://example.invalid/download?id=opaque-token",
            business_file_name="customer-hash.xls",
        )
        self.assertTrue(policy.legacy_office_required)

    def test_original_file_name_preserves_missing_empty_and_explicit_value_semantics(self) -> None:
        cases = (
            (_params(include_original_file_name=False), False, ""),
            (_params(original_file_name=""), True, ""),
            (_params(original_file_name=" 原始名称.txt"), True, " 原始名称.txt"),
        )
        for raw_params, expected_present, expected_value in cases:
            with self.subTest(expected_present=expected_present, expected_value=expected_value):
                submission = AnalysisSubmissionSnapshot.from_request_params(
                    raw_params,
                    policy_snapshot=AnalysisPolicySnapshot.default(),
                )
                self.assertEqual(expected_present, submission.original_file_name_present)
                self.assertEqual(expected_value, submission.original_file_name)

    def test_policy_snapshot_rejects_missing_and_unknown_fields(self) -> None:
        policy_payload = AnalysisPolicySnapshot.default().to_dict()
        missing = dict(policy_payload)
        missing.pop("max_model_calls")
        with self.assertRaisesRegex(ValueError, "键集合不匹配"):
            AnalysisPolicySnapshot.from_mapping(missing)

        unknown = dict(policy_payload, future_policy="forbidden")
        with self.assertRaisesRegex(ValueError, "键集合不匹配"):
            AnalysisPolicySnapshot.from_mapping(unknown)

        non_text_key = dict(policy_payload)
        non_text_key[1] = "forbidden"  # type: ignore[index]
        with self.assertRaisesRegex(ValueError, "键必须全部是 str"):
            AnalysisPolicySnapshot.from_mapping(non_text_key)


class AnalysisTaskInputCodecTests(unittest.TestCase):
    """锁定 V1 严格 envelope、身份比对和跨线程独立性。"""

    def test_round_trip_is_stable_and_preserves_raw_param_order(self) -> None:
        task_input = _task_input()
        first_json = AnalysisTaskInputCodec.encode_json(task_input)
        second_json = AnalysisTaskInputCodec.encode_json(task_input)
        self.assertEqual(first_json, second_json)

        payload = json.loads(first_json)
        self.assertEqual(
            list(AnalysisTaskInputCodec._ENVELOPE_KEYS),  # type: ignore[attr-defined]
            list(payload),
        )
        self.assertEqual(
            ["fileName", "filePath", "unknownExtension", "originalFileName"],
            list(payload["raw_params"]),
        )
        restored = AnalysisTaskInputCodec.decode_json(
            first_json,
            expected_task_id=task_input.task_id,
            expected_business_key=task_input.file_name,
            expected_batch_id=task_input.batch_id,
        )
        self.assertEqual(task_input, restored)
        self.assertEqual(
            task_input.raw_params.to_dict(),
            restored.raw_params.to_dict(),
        )

    def test_v2_round_trip_remains_readable_with_frozen_processing_policy(self) -> None:
        """历史 V2 可恢复，且重启解码不能读取新环境开关替换既有策略。"""

        task_input = _task_input_v2()
        payload = AnalysisTaskInputCodec.encode(task_input)
        restored = AnalysisTaskInputCodec.decode(payload)

        self.assertEqual(
            ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V2,
            payload["schema_version"],
        )
        self.assertEqual(
            list(AnalysisTaskInputCodec._V2_ENVELOPE_KEYS),  # type: ignore[attr-defined]
            list(payload),
        )
        self.assertIsInstance(restored, AnalysisTaskInputV2)
        assert isinstance(restored, AnalysisTaskInputV2)
        self.assertTrue(restored.document_processing_policy.legacy_office_required)
        self.assertEqual(
            "single-sheet-v1",
            restored.document_processing_policy.xlsx_sheet_policy,
        )
        self.assertEqual(task_input, restored)

    def test_v3_remains_readable_and_v4_uses_business_key_transport_name(self) -> None:
        """历史 V3 可恢复；当前 V4 按 fileName 派生且不改变公开原名。"""

        legacy_input = _task_input_v3()
        legacy_payload = AnalysisTaskInputCodec.encode(legacy_input)
        legacy_restored = AnalysisTaskInputCodec.decode(legacy_payload)
        self.assertEqual(
            ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V3,
            legacy_payload["schema_version"],
        )
        self.assertIsInstance(legacy_restored, AnalysisTaskInputV3)
        self.assertNotIsInstance(legacy_restored, AnalysisTaskInputV4)
        self.assertEqual(
            "原始 demo.md",
            legacy_restored.rag_naming.markdown_transport_file_name,
        )

        task_input = _task_input_v4()
        payload = AnalysisTaskInputCodec.encode(task_input)
        restored = AnalysisTaskInputCodec.decode(payload)

        self.assertEqual(ANALYSIS_TASK_INPUT_SCHEMA_VERSION, payload["schema_version"])
        self.assertEqual(
            list(AnalysisTaskInputCodec._V4_ENVELOPE_KEYS),  # type: ignore[attr-defined]
            list(payload),
        )
        self.assertIsInstance(restored, AnalysisTaskInputV4)
        assert isinstance(restored, AnalysisTaskInputV4)
        self.assertEqual("原始 demo.txt", restored.original_file_name)
        self.assertEqual("原始 demo.txt", restored.rag_naming.display_title)
        self.assertEqual(" demo.txt", restored.rag_naming.transport_name_candidate)
        self.assertEqual(" demo.md", restored.rag_naming.markdown_transport_file_name)
        self.assertEqual(" demo.pdf", restored.rag_naming.pdf_transport_file_name)
        self.assertEqual(
            _params()["originalFileName"],
            restored.raw_params.to_dict()["originalFileName"],
        )
        self.assertEqual(task_input, restored)

    def test_v1_v2_payloads_remain_readable_but_tampering_is_rejected(self) -> None:
        """历史 accepted V1 可恢复；V2 指纹或策略字段被改写时必须失败关闭。"""

        legacy_payload = AnalysisTaskInputCodec.encode(_task_input())
        self.assertIsInstance(AnalysisTaskInputCodec.decode(legacy_payload), AnalysisTaskInputV1)

        current_payload = AnalysisTaskInputCodec.encode(_task_input_v2())
        current_payload["document_processing_policy"][  # type: ignore[index]
            "xlsx_sheet_policy"
        ] = "multi-sheet-v2"
        with self.assertRaises(AnalysisTaskInputCodecError):
            AnalysisTaskInputCodec.decode(current_payload)

        naming_payload = AnalysisTaskInputCodec.encode(_task_input_v4())
        naming_payload["rag_naming"]["markdown_transport_file_name"] = "shadow.md"  # type: ignore[index]
        with self.assertRaises(AnalysisTaskInputCodecError):
            AnalysisTaskInputCodec.decode(naming_payload)

    def test_payload_and_decode_result_do_not_share_mutable_references(self) -> None:
        task_input = _task_input()
        payload = AnalysisTaskInputCodec.encode(task_input)
        restored = AnalysisTaskInputCodec.decode(payload)
        payload["raw_params"]["unknownExtension"]["nested"][1]["index"] = 500  # type: ignore[index]

        self.assertEqual(
            1,
            task_input.raw_params.to_dict()["unknownExtension"]["nested"][1]["index"],  # type: ignore[index]
        )
        self.assertEqual(
            1,
            restored.raw_params.to_dict()["unknownExtension"]["nested"][1]["index"],  # type: ignore[index]
        )

    def test_codec_deduplicates_equal_effective_ranges_losslessly(self) -> None:
        """大型显式范围只在 execution 内保存一次，解码后仍恢复原始 params。"""

        raw_params = _params()
        raw_params["architectureList"] = [
            {
                "id": index,
                "name": f"领域节点{index}",
                "parentId": None,
            }
            for index in range(1, 101)
        ]
        submission = AnalysisSubmissionSnapshot.from_request_params(
            raw_params,
            policy_snapshot=AnalysisPolicySnapshot.default(),
        )
        task_input = AnalysisTaskInputV1.from_submission(
            submission,
            task_id="analysis-task-range-dedup",
            batch_id="d" * 32,
            batch_sequence=1,
            accepted_at="2026-07-26T10:00:00+08:00",
            trace_id="trace-range-dedup",
        )

        payload = AnalysisTaskInputCodec.encode(task_input)
        self.assertEqual(
            {"$analysis_effective_range_ref": "architectureList"},
            payload["raw_params"]["architectureList"],
        )
        restored = AnalysisTaskInputCodec.decode(payload)

        self.assertEqual(task_input, restored)
        self.assertEqual(
            raw_params["architectureList"],
            restored.raw_params.to_dict()["architectureList"],
        )

    def test_decode_fails_closed_for_schema_envelope_and_identity_mismatch(self) -> None:
        task_input = _task_input()
        base_payload = AnalysisTaskInputCodec.encode(task_input)
        cases: tuple[tuple[str, dict[str, object], dict[str, str]], ...] = (
            (
                "unknown_schema",
                dict(base_payload, schema_version=99),
                {},
            ),
            (
                "unknown_field",
                dict(base_payload, future_field=True),
                {},
            ),
            (
                "missing_field",
                {key: value for key, value in base_payload.items() if key != "trace_id"},
                {},
            ),
            (
                "task_identity_mismatch",
                copy.deepcopy(base_payload),
                {"expected_task_id": "other-task"},
            ),
            (
                "business_identity_mismatch",
                copy.deepcopy(base_payload),
                {"expected_business_key": "other.txt"},
            ),
            (
                "batch_identity_mismatch",
                copy.deepcopy(base_payload),
                {"expected_batch_id": "f" * 32},
            ),
        )
        for case_name, payload, expected in cases:
            with self.subTest(case=case_name):
                with self.assertRaises(AnalysisTaskInputCodecError):
                    AnalysisTaskInputCodec.decode(payload, **expected)

        non_text_key_payload = copy.deepcopy(base_payload)
        non_text_key_payload[1] = "reserved"  # type: ignore[index]
        with self.assertRaises(AnalysisTaskInputCodecError):
            AnalysisTaskInputCodec.decode(non_text_key_payload)

    def test_decode_json_rejects_duplicate_keys_at_every_object_level(self) -> None:
        """重复键在不同 JSON 实现中含义不一，持久化边界必须直接拒绝。"""

        serialized = AnalysisTaskInputCodec.encode_json(_task_input())
        duplicate_envelope = serialized[:-1] + ',"trace_id":"duplicate"}'
        duplicate_nested = serialized.replace(
            '"fileName":" demo.txt",',
            '"fileName":" demo.txt","fileName":"shadow.txt",',
            1,
        )
        for payload in (duplicate_envelope, duplicate_nested):
            with self.subTest(payload=payload[-80:]):
                with self.assertRaisesRegex(
                    AnalysisTaskInputCodecError,
                    "无法解析",
                ):
                    AnalysisTaskInputCodec.decode_json(payload)

    def test_decode_rejects_empty_required_effective_range(self) -> None:
        """默认化后应为非空的范围若在存储中变空，说明快照已经损坏。"""

        payload = AnalysisTaskInputCodec.encode(_task_input())
        payload["effective_ranges"]["country"] = []
        with self.assertRaises(AnalysisTaskInputCodecError):
            AnalysisTaskInputCodec.decode(payload)

    def test_fifty_concurrent_codec_calls_have_no_cross_task_data(self) -> None:
        """并发只复用无状态 Codec，任一任务的未知字段不得串入另一任务。"""

        def encode_and_decode(index: int) -> tuple[str, int, str]:
            task_input = _task_input(index)
            serialized = AnalysisTaskInputCodec.encode_json(task_input)
            restored = AnalysisTaskInputCodec.decode_json(
                serialized,
                expected_task_id=task_input.task_id,
                expected_business_key=task_input.file_name,
                expected_batch_id=task_input.batch_id,
            )
            extension_index = restored.raw_params.to_dict()["unknownExtension"]["nested"][1]["index"]  # type: ignore[index]
            return restored.task_id, extension_index, restored.file_name

        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(encode_and_decode, range(1, 51)))

        self.assertEqual(50, len(results))
        self.assertEqual(
            {f"analysis-task-{index}" for index in range(1, 51)},
            {task_id for task_id, _, _ in results},
        )
        self.assertEqual(
            set(range(1, 51)),
            {extension_index for _, extension_index, _ in results},
        )
        self.assertEqual({"demo.txt"}, {file_name for _, _, file_name in results})


if __name__ == "__main__":
    unittest.main()
