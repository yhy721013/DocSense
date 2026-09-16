"""武器谱 I/O 端口共享的调用身份与无敏感信息结果类型。

本文件只描述 Application 与 Adapter 之间必须共同遵守的稳定事实。真实 workspace、
thread、HTTP response 或供应商 metadata 均不得进入这些 DTO；Adapter 需要把它们转换为
不透明引用或结构化结果后再返回。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.modules.tasks.domain import TaskId


def required_text(value: object, *, name: str) -> str:
    """规范化非空文本；错误信息只包含字段名，不回显业务正文。"""

    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} 不能为空")
    return normalized


def optional_text(value: object, *, name: str) -> str:
    """规范化允许为空的文本。"""

    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是 str")
    return value.strip()


def positive_int(value: object, *, name: str) -> int:
    """拒绝 ``bool`` 的严格正整数校验。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} 必须是正整数")
    return value


def non_negative_int(value: object, *, name: str) -> int:
    """拒绝 ``bool`` 的严格非负整数校验。"""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} 必须是非负整数")
    return value


def sha256_digest(value: object, *, name: str) -> str:
    """校验不含正文的 SHA-256 小写十六进制摘要。"""

    digest = required_text(value, name=name).lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError(f"{name} 必须是 SHA-256 小写十六进制摘要")
    return digest


def text_tuple(
    value: object,
    *,
    name: str,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    """冻结有序文本集合，并拒绝重复身份。"""

    if not isinstance(value, (tuple, list)):
        raise TypeError(f"{name} 必须是有序文本序列")
    normalized = tuple(required_text(item, name=f"{name} item") for item in value)
    if not allow_empty and not normalized:
        raise ValueError(f"{name} 不能为空")
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"{name} 不能包含重复项")
    return normalized


class WeaponryOperation(str, Enum):
    """会被审计的武器谱外部操作。"""

    TARGET_RETRIEVAL = "target_retrieval"
    AUXILIARY_GUIDANCE = "auxiliary_guidance"
    EVIDENCE_EXTRACTION = "evidence_extraction"
    TRANSLATION = "translation"


@dataclass(frozen=True)
class WeaponryCallIdentity:
    """一次可重试外部调用的稳定逻辑身份。

    ``call_id`` 不包含 attempt，使同一逻辑调用重试时保持稳定；``attempt_no`` 单独递增。
    来源级操作必须携带 ``document_sequence``，字段范围操作则固定使用 ``dscope``。
    """

    task_id: TaskId
    field_sequence: int
    document_sequence: int | None
    operation: WeaponryOperation
    attempt_no: int = 1
    item_sequence: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        positive_int(self.field_sequence, name="field_sequence")
        positive_int(self.attempt_no, name="attempt_no")
        if not isinstance(self.operation, WeaponryOperation):
            raise TypeError("operation 必须是 WeaponryOperation")

        source_scoped = self.operation in {
            WeaponryOperation.EVIDENCE_EXTRACTION,
            WeaponryOperation.TRANSLATION,
        }
        if source_scoped:
            positive_int(self.document_sequence, name="document_sequence")
        elif self.document_sequence is not None:
            raise ValueError("字段范围操作的 document_sequence 必须是 None")

        # Translation 是来源级能力，但 TABLE 的同一来源可能包含多行、多列非空值。
        # 如果只使用 field/document/operation，多个单元格会共享同一审计键，导致后一个
        # 翻译被错误识别为前一个调用的重复完成。item_sequence 只为翻译提供来源内的
        # 稳定子项身份；Extraction 等操作仍保持原有逻辑调用粒度。
        if self.operation is WeaponryOperation.TRANSLATION:
            positive_int(self.item_sequence, name="item_sequence")
        elif self.item_sequence is not None:
            raise ValueError("非翻译操作的 item_sequence 必须是 None")

    @property
    def call_id(self) -> str:
        """返回计划冻结的、不会因重试而漂移的逻辑调用 ID。"""

        document_part = (
            str(self.document_sequence)
            if self.document_sequence is not None
            else "scope"
        )
        item_part = (
            f":i{self.item_sequence}"
            if self.item_sequence is not None
            else ""
        )
        return (
            f"weaponry:{self.task_id.value}:f{self.field_sequence}:"
            f"d{document_part}:{self.operation.value}{item_part}"
        )

    @property
    def attempt_key(self) -> str:
        """返回审计幂等键；同一逻辑调用的不同重试不会碰撞。"""

        return f"{self.call_id}:a{self.attempt_no}"


@dataclass(frozen=True)
class IdempotentOperationResult:
    """close/cleanup 等幂等操作的通用结果。"""

    success: bool
    already_applied: bool = False
    error_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.success, bool):
            raise TypeError("success 必须是 bool")
        if not isinstance(self.already_applied, bool):
            raise TypeError("already_applied 必须是 bool")
        error_code = optional_text(self.error_code, name="error_code")
        if self.success and error_code:
            raise ValueError("成功结果不得携带 error_code")
        if not self.success and not error_code:
            raise ValueError("失败结果必须携带 error_code")
        if self.already_applied and not self.success:
            raise ValueError("already_applied 只能用于成功结果")
        object.__setattr__(self, "error_code", error_code)


__all__ = [
    "IdempotentOperationResult",
    "WeaponryCallIdentity",
    "WeaponryOperation",
]
