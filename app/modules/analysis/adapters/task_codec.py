"""文件分析 Worker 输入的严格 JSON Codec。

此 Codec 在持久化边界写入 ``AnalysisTaskInputV4``，并严格兼容读取历史 V1/V2/V3；它不读取
数据库、不生成任务身份，也不尝试修复历史脏数据。未知 schema、缺少字段、额外字段以及
任务身份不一致都必须失败关闭，避免错误 payload 被错误的 Worker 重放。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from app.modules.analysis.domain.errors import AnalysisContractError
from app.modules.analysis.domain.task_inputs import (
    ANALYSIS_EFFECTIVE_RANGE_KEYS,
    ANALYSIS_TASK_INPUT_SCHEMA_VERSION,
    ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V1,
    ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V2,
    ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V3,
    AnalysisDocumentProcessingPolicySnapshot,
    AnalysisPolicySnapshot,
    AnalysisTaskInputV1,
    AnalysisTaskInputV2,
    AnalysisTaskInputV3,
    AnalysisTaskInputV4,
    FrozenJsonObject,
)
from app.modules.analysis.domain.rag_naming import (
    AnalysisRagNamingSnapshot,
    AnalysisRagNamingSnapshotV3,
)


logger = logging.getLogger(__name__)

_EFFECTIVE_RANGE_REF_MARKER = "$analysis_effective_range_ref"


class AnalysisTaskInputCodecError(ValueError):
    """持久化输入不符合当前 Analysis Codec 合同。"""


class AnalysisTaskInputCodec:
    """严格编解码 V1–V4 输入，保持公开 ``params`` 的未知字段和值语义。"""

    _V1_ENVELOPE_KEYS = (
        "schema_version",
        "task_id",
        "batch_id",
        "batch_sequence",
        "file_name",
        "original_file_name",
        "original_file_name_present",
        "file_path",
        "raw_params",
        "effective_ranges",
        "policy_snapshot",
        "accepted_at",
        "trace_id",
    )
    _V2_ENVELOPE_KEYS = _V1_ENVELOPE_KEYS + ("document_processing_policy",)
    _V3_ENVELOPE_KEYS = _V2_ENVELOPE_KEYS + ("rag_naming",)
    _V4_ENVELOPE_KEYS = _V3_ENVELOPE_KEYS
    # 保留历史私有测试入口；各版本合同使用上方显式 Envelope 字段集合。
    _ENVELOPE_KEYS = _V1_ENVELOPE_KEYS

    @classmethod
    def encode(cls, task_input: AnalysisTaskInputV1) -> dict[str, Any]:
        """投影为可持久化字典，返回值不与领域快照共享可变引用。"""

        if not isinstance(task_input, AnalysisTaskInputV1):
            raise TypeError("task_input 必须是 AnalysisTaskInputV1/V2/V3/V4")
        effective_ranges = task_input.effective_ranges.to_dict()
        raw_params = task_input.raw_params.to_dict()
        compacted_range_count = 0
        # 公开最新投影必须继续保存原始 params，不能改接口合同；execution 内部则可对与
        # effective_ranges 完全相同的范围做无损引用，避免大型 architectureList 在同一
        # 输入快照中重复两次。字段缺失、显式空值或规范化前后不一致时仍保留原值。
        for key in ANALYSIS_EFFECTIVE_RANGE_KEYS:
            if key in raw_params and raw_params[key] == effective_ranges[key]:
                raw_params[key] = {_EFFECTIVE_RANGE_REF_MARKER: key}
                compacted_range_count += 1
        payload = {
            "schema_version": task_input.schema_version,
            "task_id": task_input.task_id,
            "batch_id": task_input.batch_id,
            "batch_sequence": task_input.batch_sequence,
            "file_name": task_input.file_name,
            "original_file_name": task_input.original_file_name,
            "original_file_name_present": task_input.original_file_name_present,
            "file_path": task_input.file_path,
            "raw_params": raw_params,
            "effective_ranges": effective_ranges,
            "policy_snapshot": task_input.policy_snapshot.to_dict(),
            "accepted_at": task_input.accepted_at,
            "trace_id": task_input.trace_id,
        }
        if isinstance(task_input, AnalysisTaskInputV2):
            payload["document_processing_policy"] = (
                task_input.document_processing_policy.to_dict()
            )
        if isinstance(task_input, AnalysisTaskInputV3):
            payload["rag_naming"] = task_input.rag_naming.to_dict()
        logger.debug(
            "文件分析任务输入已编码: task_id=%s batch_id=%s batch_sequence=%d "
            "compacted_range_count=%d",
            task_input.task_id,
            task_input.batch_id,
            task_input.batch_sequence,
            compacted_range_count,
        )
        return payload

    @classmethod
    def encode_json(cls, task_input: AnalysisTaskInputV1) -> str:
        """生成紧凑 UTF-8 JSON 文本，并保留原始参数对象的插入顺序。"""

        serialized = json.dumps(
            cls.encode(task_input),
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        logger.debug(
            "文件分析任务输入 JSON 已编码: task_id=%s payload_chars=%d",
            task_input.task_id,
            len(serialized),
        )
        return serialized

    @classmethod
    def decode_json(
        cls,
        serialized: str,
        *,
        expected_task_id: str | None = None,
        expected_business_key: str | None = None,
        expected_batch_id: str | None = None,
    ) -> AnalysisTaskInputV1:
        """解析 JSON 后按与字典入口一致的严格身份边界恢复领域快照。"""

        if not isinstance(serialized, str):
            raise AnalysisTaskInputCodecError("serialized 必须是 str")
        try:
            payload = json.loads(
                serialized,
                object_pairs_hook=cls._decode_unique_object,
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise AnalysisTaskInputCodecError("任务输入 JSON 无法解析") from exc
        return cls.decode(
            payload,
            expected_task_id=expected_task_id,
            expected_business_key=expected_business_key,
            expected_batch_id=expected_batch_id,
        )

    @staticmethod
    def _decode_unique_object(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        """拒绝任意层级重复键，避免不同 JSON 实现对持久载荷产生歧义。"""

        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise AnalysisTaskInputCodecError(f"任务输入 JSON 包含重复键: {key}")
            result[key] = value
        return result

    @classmethod
    def decode(
        cls,
        payload: object,
        *,
        expected_task_id: str | None = None,
        expected_business_key: str | None = None,
        expected_batch_id: str | None = None,
    ) -> AnalysisTaskInputV1:
        """从单一 schema 版本的字典恢复输入，并验证持久化记录的归属。"""

        if not isinstance(payload, Mapping):
            raise AnalysisTaskInputCodecError("任务输入 payload 必须是对象")
        schema_version = payload.get("schema_version")
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version
            not in {
                ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V1,
                ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V2,
                ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V3,
                ANALYSIS_TASK_INPUT_SCHEMA_VERSION,
            }
        ):
            raise AnalysisTaskInputCodecError("不支持的文件分析任务输入 schema_version")
        cls._validate_envelope_keys(payload, schema_version=schema_version)
        try:
            effective_ranges = dict(
                cls._require_mapping(
                    payload["effective_ranges"],
                    "effective_ranges",
                )
            )
            raw_params = dict(
                cls._require_mapping(payload["raw_params"], "raw_params")
            )
            for key in ANALYSIS_EFFECTIVE_RANGE_KEYS:
                if raw_params.get(key) == {
                    _EFFECTIVE_RANGE_REF_MARKER: key
                }:
                    if key not in effective_ranges:
                        raise AnalysisTaskInputCodecError(
                            f"任务输入范围引用缺少目标: {key}"
                        )
                    # FrozenJsonObject 会再次深冻结；这里保持键位置不变并恢复调用方原值。
                    raw_params[key] = effective_ranges[key]
            common_fields = {
                "schema_version": schema_version,
                "task_id": payload["task_id"],
                "batch_id": payload["batch_id"],
                "batch_sequence": payload["batch_sequence"],
                "file_name": payload["file_name"],
                "original_file_name": payload["original_file_name"],
                "original_file_name_present": payload[
                    "original_file_name_present"
                ],
                "file_path": payload["file_path"],
                "raw_params": FrozenJsonObject.from_mapping(
                    raw_params,
                    name="raw_params",
                ),
                "effective_ranges": FrozenJsonObject.from_mapping(
                    effective_ranges,
                    name="effective_ranges",
                ),
                "policy_snapshot": AnalysisPolicySnapshot.from_mapping(
                    cls._require_mapping(
                        payload["policy_snapshot"],
                        "policy_snapshot",
                    ),
                    name="policy_snapshot",
                ),
                "accepted_at": payload["accepted_at"],
                "trace_id": payload["trace_id"],
            }
            if schema_version == ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V1:
                task_input = AnalysisTaskInputV1(**common_fields)  # type: ignore[arg-type]
            elif schema_version == ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V2:
                task_input = AnalysisTaskInputV2(
                    **common_fields,  # type: ignore[arg-type]
                    document_processing_policy=(
                        AnalysisDocumentProcessingPolicySnapshot.from_mapping(
                            cls._require_mapping(
                                payload["document_processing_policy"],
                                "document_processing_policy",
                            )
                        )
                    ),
                )
            elif schema_version == ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V3:
                task_input = AnalysisTaskInputV3(
                    **common_fields,  # type: ignore[arg-type]
                    document_processing_policy=(
                        AnalysisDocumentProcessingPolicySnapshot.from_mapping(
                            cls._require_mapping(
                                payload["document_processing_policy"],
                                "document_processing_policy",
                            )
                        )
                    ),
                    rag_naming=AnalysisRagNamingSnapshotV3.from_mapping(
                        cls._require_mapping(
                            payload["rag_naming"],
                            "rag_naming",
                        )
                    ),
                )
            else:
                task_input = AnalysisTaskInputV4(
                    **common_fields,  # type: ignore[arg-type]
                    document_processing_policy=(
                        AnalysisDocumentProcessingPolicySnapshot.from_mapping(
                            cls._require_mapping(
                                payload["document_processing_policy"],
                                "document_processing_policy",
                            )
                        )
                    ),
                    rag_naming=AnalysisRagNamingSnapshot.from_mapping(
                        cls._require_mapping(
                            payload["rag_naming"],
                            "rag_naming",
                        )
                    ),
                )
        except (AnalysisContractError, TypeError, KeyError, ValueError) as exc:
            raise AnalysisTaskInputCodecError("任务输入 payload 不符合版本合同") from exc

        cls._validate_expected_identity(
            task_input,
            expected_task_id=expected_task_id,
            expected_business_key=expected_business_key,
            expected_batch_id=expected_batch_id,
        )
        logger.debug(
            "文件分析任务输入已解码: task_id=%s batch_id=%s batch_sequence=%d",
            task_input.task_id,
            task_input.batch_id,
            task_input.batch_sequence,
        )
        return task_input

    @classmethod
    def _validate_envelope_keys(
        cls,
        payload: Mapping[str, object],
        *,
        schema_version: int,
    ) -> None:
        """拒绝 schema 演进误读，不能让未知字段被旧 Worker 静默丢弃。"""

        if any(not isinstance(key, str) for key in payload):
            raise AnalysisTaskInputCodecError("任务输入字段名必须全部是 str")
        actual_keys = frozenset(payload)
        if schema_version == ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V1:
            version_keys = cls._V1_ENVELOPE_KEYS
        elif schema_version == ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V2:
            version_keys = cls._V2_ENVELOPE_KEYS
        elif schema_version == ANALYSIS_TASK_INPUT_SCHEMA_VERSION_V3:
            version_keys = cls._V3_ENVELOPE_KEYS
        else:
            version_keys = cls._V4_ENVELOPE_KEYS
        expected_keys = frozenset(version_keys)
        if actual_keys == expected_keys:
            return
        missing = sorted(expected_keys - actual_keys)
        unknown = sorted(actual_keys - expected_keys)
        raise AnalysisTaskInputCodecError(
            f"任务输入字段不匹配: missing={missing} unknown={unknown}"
        )

    @staticmethod
    def _require_mapping(value: object, name: str) -> Mapping[str, object]:
        """在冻结前确保嵌套 JSON 载荷仍是对象而非数组或自定义类型。"""

        if not isinstance(value, Mapping):
            raise AnalysisTaskInputCodecError(f"{name} 必须是对象")
        return value

    @staticmethod
    def _validate_expected_identity(
        task_input: AnalysisTaskInputV1,
        *,
        expected_task_id: str | None,
        expected_business_key: str | None,
        expected_batch_id: str | None,
    ) -> None:
        """将数据库行键与 payload 再次比对，防止跨任务错投或错误重放。"""

        expected_values = (
            ("task_id", expected_task_id, task_input.task_id),
            ("business_key", expected_business_key, task_input.file_name),
            ("batch_id", expected_batch_id, task_input.batch_id),
        )
        for name, expected, actual in expected_values:
            if expected is None:
                continue
            if not isinstance(expected, str) or not expected.strip():
                raise AnalysisTaskInputCodecError(f"expected_{name} 必须是非空 str")
            if expected != actual:
                raise AnalysisTaskInputCodecError(
                    f"任务输入与 expected_{name} 不一致"
                )


__all__ = ("AnalysisTaskInputCodec", "AnalysisTaskInputCodecError")
