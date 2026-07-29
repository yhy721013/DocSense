"""与框架、供应商和文件系统无关的 Translation 值对象。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from app.modules.document_processing.domain import (
    ArtifactRef,
    DocumentRepresentation,
)
from app.modules.tasks.domain import TaskId


def _required(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 不能为空")
    return value.strip()


def _canonical_json(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            dict(value),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Translation Profile 参数必须可稳定 JSON 序列化") from exc


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


class TranslationMode(str, Enum):
    """翻译引擎的明确模式，不使用含糊的 fast 布尔值作为领域语义。"""

    MACHINE = "machine"
    LLM = "llm"


class TranslationFailurePolicy(str, Enum):
    """单个翻译单元失败时的冻结策略。"""

    FAIL_DOCUMENT = "fail_document"
    PLACEHOLDER = "placeholder"


@dataclass(frozen=True, slots=True)
class TranslationProfile:
    """冻结引擎、Renderer 和失败策略。"""

    engine_id: str
    engine_fingerprint: str
    renderer_id: str
    renderer_fingerprint: str
    mode: TranslationMode
    failure_policy: TranslationFailurePolicy
    parameters_json: str = "{}"
    profile_id: str = ""

    def __post_init__(self) -> None:
        for name in (
            "engine_id",
            "engine_fingerprint",
            "renderer_id",
            "renderer_fingerprint",
        ):
            object.__setattr__(self, name, _required(getattr(self, name), name))
        if not isinstance(self.mode, TranslationMode):
            raise TypeError("mode 必须是 TranslationMode")
        if not isinstance(self.failure_policy, TranslationFailurePolicy):
            raise TypeError("failure_policy 必须是 TranslationFailurePolicy")
        try:
            parameters = json.loads(self.parameters_json)
        except json.JSONDecodeError as exc:
            raise ValueError("parameters_json 必须是合法 JSON") from exc
        if not isinstance(parameters, dict):
            raise ValueError("parameters_json 顶层必须是对象")
        canonical = _canonical_json(parameters)
        object.__setattr__(self, "parameters_json", canonical)
        expected = _digest(
            {
                "engineFingerprint": self.engine_fingerprint,
                "engineId": self.engine_id,
                "failurePolicy": self.failure_policy.value,
                "mode": self.mode.value,
                "parameters": parameters,
                "rendererFingerprint": self.renderer_fingerprint,
                "rendererId": self.renderer_id,
            }
        )
        if self.profile_id and self.profile_id.lower() != expected:
            raise ValueError("profile_id 与 Translation Profile 内容不一致")
        object.__setattr__(self, "profile_id", expected)

    @classmethod
    def create(
        cls,
        *,
        engine_id: str,
        engine_fingerprint: str,
        renderer_id: str,
        renderer_fingerprint: str,
        mode: TranslationMode,
        failure_policy: TranslationFailurePolicy,
        parameters: Mapping[str, Any] | None = None,
    ) -> "TranslationProfile":
        return cls(
            engine_id=engine_id,
            engine_fingerprint=engine_fingerprint,
            renderer_id=renderer_id,
            renderer_fingerprint=renderer_fingerprint,
            mode=mode,
            failure_policy=failure_policy,
            parameters_json=_canonical_json(parameters or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "engineFingerprint": self.engine_fingerprint,
            "engineId": self.engine_id,
            "failurePolicy": self.failure_policy.value,
            "mode": self.mode.value,
            "parameters": json.loads(self.parameters_json),
            "profileId": self.profile_id,
            "rendererFingerprint": self.renderer_fingerprint,
            "rendererId": self.renderer_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TranslationProfile":
        expected = {
            "engineFingerprint",
            "engineId",
            "failurePolicy",
            "mode",
            "parameters",
            "profileId",
            "rendererFingerprint",
            "rendererId",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("TranslationProfile 字段集合不合法")
        try:
            mode = TranslationMode(value["mode"])
            failure_policy = TranslationFailurePolicy(
                value["failurePolicy"]
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("TranslationProfile 枚举值不合法") from exc
        return cls(
            engine_id=value["engineId"],
            engine_fingerprint=value["engineFingerprint"],
            renderer_id=value["rendererId"],
            renderer_fingerprint=value["rendererFingerprint"],
            mode=mode,
            failure_policy=failure_policy,
            parameters_json=_canonical_json(value["parameters"]),
            profile_id=value["profileId"],
        )


@dataclass(frozen=True, slots=True)
class TranslationRequest:
    """翻译已准备 Artifact；不允许携带任何格式转换选择。"""

    task_id: TaskId
    prepared_artifact: ArtifactRef
    target_language: str
    item_limit: int
    profile: TranslationProfile
    trace_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        if not isinstance(self.prepared_artifact, ArtifactRef):
            raise TypeError("prepared_artifact 必须是 ArtifactRef")
        if self.prepared_artifact.task_id != self.task_id:
            raise ValueError("prepared_artifact 不属于当前 task")
        if self.prepared_artifact.representation not in {
            DocumentRepresentation.MARKDOWN,
            DocumentRepresentation.TEXT,
        }:
            raise ValueError("Translation 只接受 prepared Markdown/Text Artifact")
        object.__setattr__(
            self,
            "target_language",
            _required(self.target_language, "target_language"),
        )
        if (
            isinstance(self.item_limit, bool)
            or not isinstance(self.item_limit, int)
            or self.item_limit < 0
        ):
            raise ValueError("item_limit 必须是非负整数；0 表示全部")
        if not isinstance(self.profile, TranslationProfile):
            raise TypeError("profile 必须是 TranslationProfile")
        object.__setattr__(self, "trace_id", _required(self.trace_id, "trace_id"))

    @property
    def translation_key(self) -> str:
        return _digest(
            {
                "itemLimit": self.item_limit,
                "preparedArtifactId": self.prepared_artifact.artifact_id,
                "preparedSha256": self.prepared_artifact.metadata.sha256,
                "profileId": self.profile.profile_id,
                "targetLanguage": self.target_language,
                "taskId": self.task_id.value,
            }
        )


@dataclass(frozen=True, slots=True)
class TranslationUnit:
    """一个有序、不可变的翻译单元。"""

    ordinal: int
    source_text: str
    translated_text: str
    translated: bool
    failed: bool = False

    def __post_init__(self) -> None:
        if (
            isinstance(self.ordinal, bool)
            or not isinstance(self.ordinal, int)
            or self.ordinal <= 0
        ):
            raise ValueError("ordinal 必须是正整数")
        if not isinstance(self.source_text, str) or not isinstance(
            self.translated_text,
            str,
        ):
            raise TypeError("TranslationUnit 文本必须是 str")
        if not isinstance(self.translated, bool) or not isinstance(
            self.failed,
            bool,
        ):
            raise TypeError("translated/failed 必须是 bool")


@dataclass(frozen=True, slots=True)
class RenderedTranslation:
    """Renderer 生成、尚未由业务 Presenter 投影的不可变结果。"""

    bilingual_html: str
    monolingual_html: str

    def __post_init__(self) -> None:
        if not isinstance(self.bilingual_html, str) or not isinstance(
            self.monolingual_html,
            str,
        ):
            raise TypeError("Renderer 输出必须是 str")
        if not self.bilingual_html.strip() or not self.monolingual_html.strip():
            raise ValueError("Renderer 输出不能为空")


@dataclass(frozen=True, slots=True)
class TranslationResult:
    translation_key: str
    rendered: RenderedTranslation
    units: tuple[TranslationUnit, ...]
    translated_count: int
    failed_count: int

    def __post_init__(self) -> None:
        if len(self.translation_key) != 64:
            raise ValueError("translation_key 必须是 SHA-256")
        if not isinstance(self.rendered, RenderedTranslation):
            raise TypeError("rendered 必须是 RenderedTranslation")
        object.__setattr__(self, "units", tuple(self.units))
        if any(not isinstance(item, TranslationUnit) for item in self.units):
            raise TypeError("units 只能包含 TranslationUnit")
        if self.translated_count < 0 or self.failed_count < 0:
            raise ValueError("结果计数不得为负")
        if self.translated_count != sum(item.translated for item in self.units):
            raise ValueError("translated_count 与 units 不一致")
        if self.failed_count != sum(item.failed for item in self.units):
            raise ValueError("failed_count 与 units 不一致")


__all__ = [
    "RenderedTranslation",
    "TranslationFailurePolicy",
    "TranslationMode",
    "TranslationProfile",
    "TranslationRequest",
    "TranslationResult",
    "TranslationUnit",
]
