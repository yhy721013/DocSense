"""Legacy Office 通用处理 profile 的稳定构造规则。"""

from __future__ import annotations

import hashlib
import json

from app.modules.document_processing.domain import (
    DocumentRepresentation,
    ProcessingProfile,
)


LEGACY_OFFICE_PROCESSOR_ID = "libreoffice-legacy-office"
_TARGET_SUFFIXES = {
    ".doc": ".docx",
    ".ppt": ".pptx",
    ".xls": ".xlsx",
}
_TARGET_MEDIA_TYPES = {
    ".doc": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".ppt": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xls": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def normalize_legacy_suffix(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("source_suffix 必须是 str")
    normalized = value.strip().lower()
    if not normalized.startswith("."):
        normalized = f".{normalized}"
    if normalized not in _TARGET_SUFFIXES:
        raise ValueError("source_suffix 不是受支持的 Legacy Office 格式")
    return normalized


def target_suffix_for(source_suffix: object) -> str:
    return _TARGET_SUFFIXES[normalize_legacy_suffix(source_suffix)]


def target_media_type_for(source_suffix: object) -> str:
    return _TARGET_MEDIA_TYPES[normalize_legacy_suffix(source_suffix)]


def create_legacy_office_profile(
    *,
    source_suffix: str,
    libreoffice_version: str,
    policy_fingerprint: str,
) -> ProcessingProfile:
    """冻结输入格式、LibreOffice 实际版本和部署策略指纹。"""

    source = normalize_legacy_suffix(source_suffix)
    version = str(libreoffice_version).strip()
    policy = str(policy_fingerprint).strip()
    if not version:
        raise ValueError("libreoffice_version 不能为空")
    if not policy:
        raise ValueError("policy_fingerprint 不能为空")
    fingerprint_payload = json.dumps(
        {
            "libreofficeVersion": version,
            "policyFingerprint": policy,
            "sourceSuffix": source,
            "targetSuffix": _TARGET_SUFFIXES[source],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    processor_fingerprint = hashlib.sha256(fingerprint_payload).hexdigest()
    return ProcessingProfile.create(
        processor_id=LEGACY_OFFICE_PROCESSOR_ID,
        processor_fingerprint=processor_fingerprint,
        target_representation=DocumentRepresentation.OOXML,
        parameters={
            "libreofficeVersion": version,
            "policyFingerprint": policy,
            "sourceSuffix": source,
            "targetSuffix": _TARGET_SUFFIXES[source],
        },
    )


__all__ = [
    "LEGACY_OFFICE_PROCESSOR_ID",
    "create_legacy_office_profile",
    "normalize_legacy_suffix",
    "target_media_type_for",
    "target_suffix_for",
]
