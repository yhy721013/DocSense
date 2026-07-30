"""文件分析 RAG 上传命名的纯领域策略。

本模块只处理业务名称选择、合法性校验和确定性文件名派生，不访问文件系统、数据库或
AnythingLLM。公开请求中的 ``fileName`` / ``originalFileName`` 保持原有语义；这里生成的
值仅用于内部 RAG 上传描述符，不能反向写回公开请求或 Callback。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import ClassVar, Mapping

from .errors import AnalysisContractError


RAG_NAMING_SOURCE_ORIGINAL_FILE_NAME = "original_file_name"
RAG_NAMING_SOURCE_FILE_NAME_FALLBACK = "business_file_name_fallback"
RAG_REPRESENTATION_MARKDOWN = "markdown"
RAG_REPRESENTATION_PDF = "pdf"
RAG_TRANSPORT_NAME_MAX_UTF8_BYTES = 255

_ALLOWED_NAMING_SOURCES = frozenset(
    {
        RAG_NAMING_SOURCE_ORIGINAL_FILE_NAME,
        RAG_NAMING_SOURCE_FILE_NAME_FALLBACK,
    }
)
_FORBIDDEN_FILE_NAME_CHARACTERS = frozenset('/\\<>:"|?*')
_WINDOWS_RESERVED_STEMS = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
        # Windows 也把带 ¹/²/³ 的 COM/LPT 名称视为设备名，离线迁移时必须一致拒绝。
        "com¹",
        "com²",
        "com³",
        "lpt¹",
        "lpt²",
        "lpt³",
    }
)
_REPRESENTATION_SUFFIXES = {
    RAG_REPRESENTATION_MARKDOWN: ".md",
    RAG_REPRESENTATION_PDF: ".pdf",
}


class RagNameValidationError(ValueError):
    """命名候选无法安全、无损地用于 multipart filename。"""

    def __init__(self, reason_code: str, value: object) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.value_length = len(value) if isinstance(value, str) else 0
        digest_source = (
            value.encode("utf-8", errors="surrogatepass")
            if isinstance(value, str)
            else type(value).__name__.encode("ascii", errors="replace")
        )
        self.value_digest = hashlib.sha256(digest_source).hexdigest()


@dataclass(frozen=True)
class SelectedRagBusinessName:
    """从公开字段中选出的、尚未校验的业务命名候选。"""

    value: str
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not self.value.strip():
            raise AnalysisContractError("RAG 业务命名候选必须是非空 str")
        if self.source not in _ALLOWED_NAMING_SOURCES:
            raise AnalysisContractError("RAG 业务命名来源不受支持")


def select_rag_business_name(
    original_file_name: object,
    file_name: object,
) -> SelectedRagBusinessName:
    """按冻结规则选择 display title 与传输名的共同候选。

    ``originalFileName`` 缺失时调用方传入 ``None``。缺失、``null``、空字符串或仅空白
    都走兼容回退；其他非字符串值明确拒绝，避免把 JSON 数组/对象的 Python 字符串表示
    当成文件名。选中字符串不会被 trim、归一化、替换或截断。
    """

    if original_file_name is None or (
        isinstance(original_file_name, str) and not original_file_name.strip()
    ):
        if not isinstance(file_name, str) or not file_name.strip():
            raise RagNameValidationError("fallback_file_name_not_string", file_name)
        return SelectedRagBusinessName(
            value=file_name,
            source=RAG_NAMING_SOURCE_FILE_NAME_FALLBACK,
        )
    if not isinstance(original_file_name, str):
        raise RagNameValidationError(
            "original_file_name_not_string",
            original_file_name,
        )
    return SelectedRagBusinessName(
        value=original_file_name,
        source=RAG_NAMING_SOURCE_ORIGINAL_FILE_NAME,
    )


def validate_rag_transport_name_candidate(value: object) -> str:
    """校验候选可在 Windows、HTTP multipart 与离线迁移中无损使用。

    校验只作判断，不返回“安全化”后的另一个名称。任何需要替换、裁剪或 Unicode
    归一化才能使用的值都会被拒绝，从而保持业务原名与展示标题严格一致。
    """

    if not isinstance(value, str):
        raise RagNameValidationError("candidate_not_string", value)
    if not value or not value.strip():
        raise RagNameValidationError("candidate_empty", value)
    if value in {".", ".."}:
        raise RagNameValidationError("relative_path_name", value)
    if any(character in _FORBIDDEN_FILE_NAME_CHARACTERS for character in value):
        raise RagNameValidationError("forbidden_character", value)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise RagNameValidationError("control_character", value)
    if value.endswith((" ", ".")):
        raise RagNameValidationError("trailing_space_or_dot", value)

    stem = _remove_last_suffix(value)
    if not stem:
        raise RagNameValidationError("empty_stem", value)
    windows_device_stem = value.split(".", 1)[0].casefold()
    if windows_device_stem in _WINDOWS_RESERVED_STEMS:
        raise RagNameValidationError("windows_reserved_name", value)

    for representation in _REPRESENTATION_SUFFIXES:
        derived = _derive_without_revalidation(value, representation)
        try:
            derived_bytes = derived.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise RagNameValidationError("invalid_unicode", value) from exc
        if len(derived_bytes) > RAG_TRANSPORT_NAME_MAX_UTF8_BYTES:
            raise RagNameValidationError("derived_name_too_long", value)
    return value


def derive_rag_transport_file_name(
    candidate: object,
    representation: str,
) -> str:
    """依据实际上传表示替换最后一个后缀，绝不通过内容或本地路径猜测。"""

    validated = validate_rag_transport_name_candidate(candidate)
    if representation not in _REPRESENTATION_SUFFIXES:
        raise AnalysisContractError("RAG 上传表示类型不受支持")
    return _derive_without_revalidation(validated, representation)


def _remove_last_suffix(value: str) -> str:
    """仅移除最后一个后缀；无后缀名称保持完整主干。"""

    if "." not in value:
        return value
    return value.rsplit(".", 1)[0]


def _derive_without_revalidation(candidate: str, representation: str) -> str:
    suffix = _REPRESENTATION_SUFFIXES[representation]
    return f"{_remove_last_suffix(candidate)}{suffix}"


@dataclass(frozen=True)
class AnalysisRagNamingSnapshot:
    """受理时冻结、与具体 RAG Provider 无关的命名事实。

    P1 同时持久化 Markdown/PDF 两种允许表示的传输名；P3 只有在实际表示确定后才选择
    唯一名称并写入最终上传描述符，避免受理层提前耦合 OCR/MinerU 的运行结果。
    """

    display_title: str
    naming_source: str
    candidate_sha256: str
    markdown_transport_file_name: str
    pdf_transport_file_name: str

    EXPECTED_KEYS: ClassVar[frozenset[str]] = frozenset(
        {
            "display_title",
            "naming_source",
            "candidate_sha256",
            "markdown_transport_file_name",
            "pdf_transport_file_name",
        }
    )

    def __post_init__(self) -> None:
        candidate = validate_rag_transport_name_candidate(self.display_title)
        if self.naming_source not in _ALLOWED_NAMING_SOURCES:
            raise AnalysisContractError("rag_naming.naming_source 不受支持")
        expected_digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        if self.candidate_sha256 != expected_digest:
            raise AnalysisContractError("rag_naming.candidate_sha256 与标题不一致")
        if self.markdown_transport_file_name != derive_rag_transport_file_name(
            candidate,
            RAG_REPRESENTATION_MARKDOWN,
        ):
            raise AnalysisContractError("rag_naming Markdown 传输名不一致")
        if self.pdf_transport_file_name != derive_rag_transport_file_name(
            candidate,
            RAG_REPRESENTATION_PDF,
        ):
            raise AnalysisContractError("rag_naming PDF 传输名不一致")

    @classmethod
    def from_public_names(
        cls,
        *,
        original_file_name: object,
        file_name: object,
    ) -> "AnalysisRagNamingSnapshot":
        selected = select_rag_business_name(original_file_name, file_name)
        candidate = validate_rag_transport_name_candidate(selected.value)
        return cls(
            display_title=candidate,
            naming_source=selected.source,
            candidate_sha256=hashlib.sha256(candidate.encode("utf-8")).hexdigest(),
            markdown_transport_file_name=derive_rag_transport_file_name(
                candidate,
                RAG_REPRESENTATION_MARKDOWN,
            ),
            pdf_transport_file_name=derive_rag_transport_file_name(
                candidate,
                RAG_REPRESENTATION_PDF,
            ),
        )

    def transport_file_name_for(self, representation: str) -> str:
        """供 P3 在表示类型确定后选择唯一的最终传输名。"""

        if representation == RAG_REPRESENTATION_MARKDOWN:
            return self.markdown_transport_file_name
        if representation == RAG_REPRESENTATION_PDF:
            return self.pdf_transport_file_name
        raise AnalysisContractError("RAG 上传表示类型不受支持")

    def to_dict(self) -> dict[str, str]:
        return {
            "display_title": self.display_title,
            "naming_source": self.naming_source,
            "candidate_sha256": self.candidate_sha256,
            "markdown_transport_file_name": self.markdown_transport_file_name,
            "pdf_transport_file_name": self.pdf_transport_file_name,
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
    ) -> "AnalysisRagNamingSnapshot":
        if not isinstance(value, Mapping):
            raise AnalysisContractError("rag_naming 必须是 Mapping")
        if any(not isinstance(key, str) for key in value):
            raise AnalysisContractError("rag_naming 字段名必须是 str")
        actual_keys = frozenset(value)
        if actual_keys != cls.EXPECTED_KEYS:
            missing = sorted(cls.EXPECTED_KEYS - actual_keys)
            unknown = sorted(actual_keys - cls.EXPECTED_KEYS)
            raise AnalysisContractError(
                f"rag_naming 键集合不匹配: missing={missing} unknown={unknown}"
            )
        return cls(
            display_title=value["display_title"],  # type: ignore[arg-type]
            naming_source=value["naming_source"],  # type: ignore[arg-type]
            candidate_sha256=value["candidate_sha256"],  # type: ignore[arg-type]
            markdown_transport_file_name=value[  # type: ignore[arg-type]
                "markdown_transport_file_name"
            ],
            pdf_transport_file_name=value["pdf_transport_file_name"],  # type: ignore[arg-type]
        )


__all__ = (
    "AnalysisRagNamingSnapshot",
    "RAG_NAMING_SOURCE_FILE_NAME_FALLBACK",
    "RAG_NAMING_SOURCE_ORIGINAL_FILE_NAME",
    "RAG_REPRESENTATION_MARKDOWN",
    "RAG_REPRESENTATION_PDF",
    "RAG_TRANSPORT_NAME_MAX_UTF8_BYTES",
    "RagNameValidationError",
    "SelectedRagBusinessName",
    "derive_rag_transport_file_name",
    "select_rag_business_name",
    "validate_rag_transport_name_candidate",
)
