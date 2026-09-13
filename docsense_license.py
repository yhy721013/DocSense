from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


LICENSE_PRODUCT = "DocSense"
LICENSE_SCHEMA_VERSION = 1
LICENSE_MAGIC = b"DSLIC\x00\x01\x00"
LICENSE_SIGNATURE_SIZE = 64
LICENSE_MAX_PAYLOAD_SIZE = 64 * 1024
EMBEDDED_PUBLIC_KEY_RAW = bytes.fromhex(
    "0ad83742a29d3293a6b180de3b7b98513a7cd12d46eda9d7ecb87b963311165a"
)
_HEADER = struct.Struct(">8sI")


class LicenseValidationError(RuntimeError):
    code = "LICENSE_INVALID"
    public_message = "DocSense 授权无效，请联系管理员更新授权文件"


class LicenseConfigurationError(LicenseValidationError):
    code = "LICENSE_CONFIGURATION_ERROR"
    public_message = "DocSense 授权配置不完整，请联系管理员"


class LicenseFormatError(LicenseValidationError):
    code = "LICENSE_FORMAT_ERROR"


class LicenseSignatureError(LicenseValidationError):
    code = "LICENSE_SIGNATURE_ERROR"


class LicenseExpiredError(LicenseValidationError):
    code = "LICENSE_EXPIRED"
    public_message = "DocSense 授权已过期，请联系管理员更新授权文件"


class LicenseNotYetValidError(LicenseValidationError):
    code = "LICENSE_NOT_YET_VALID"
    public_message = "DocSense 授权尚未生效，请检查系统时间或联系管理员"


class LicenseProductMismatchError(LicenseValidationError):
    code = "LICENSE_PRODUCT_MISMATCH"


@dataclass(frozen=True)
class LicenseConfig:
    required: bool = False
    license_file: Path | None = None
    product: str = LICENSE_PRODUCT


@dataclass(frozen=True)
class LicenseClaims:
    license_id: str
    product: str
    customer: str
    issued_at: datetime
    expires_at: datetime
    not_before: datetime | None
    raw: Mapping[str, Any]


def _parse_timestamp(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise LicenseFormatError(f"授权字段 {field_name} 必须是带时区的 ISO 8601 时间")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise LicenseFormatError(
            f"授权字段 {field_name} 不是有效的 ISO 8601 时间"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LicenseFormatError(f"授权字段 {field_name} 必须显式包含时区")
    return parsed.astimezone(timezone.utc)


def _required_string(payload: Mapping[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise LicenseFormatError(f"授权字段 {field_name} 必须是非空字符串")
    return value.strip()


def canonical_payload_bytes(payload: Mapping[str, Any]) -> bytes:
    try:
        encoded = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise LicenseFormatError("授权载荷不能序列化为 JSON") from exc
    if not encoded or len(encoded) > LICENSE_MAX_PAYLOAD_SIZE:
        raise LicenseFormatError("授权载荷大小不合法")
    return encoded


def create_signed_license(
    payload: Mapping[str, Any],
    private_key: Ed25519PrivateKey,
) -> bytes:
    payload_bytes = canonical_payload_bytes(payload)
    header = _HEADER.pack(LICENSE_MAGIC, len(payload_bytes))
    signed_content = header + payload_bytes
    signature = private_key.sign(signed_content)
    return signed_content + signature


def _decode_envelope(blob: bytes) -> tuple[bytes, bytes, Mapping[str, Any]]:
    minimum_size = _HEADER.size + LICENSE_SIGNATURE_SIZE + 2
    if len(blob) < minimum_size:
        raise LicenseFormatError("授权文件过短或已损坏")
    magic, payload_size = _HEADER.unpack(blob[: _HEADER.size])
    if magic != LICENSE_MAGIC:
        raise LicenseFormatError("授权文件标识或版本不受支持")
    if payload_size < 2 or payload_size > LICENSE_MAX_PAYLOAD_SIZE:
        raise LicenseFormatError("授权载荷长度不合法")
    expected_size = _HEADER.size + payload_size + LICENSE_SIGNATURE_SIZE
    if len(blob) != expected_size:
        raise LicenseFormatError("授权文件长度与封装声明不一致")

    signed_content = blob[: _HEADER.size + payload_size]
    signature = blob[-LICENSE_SIGNATURE_SIZE:]
    payload_bytes = blob[_HEADER.size : _HEADER.size + payload_size]
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LicenseFormatError("授权载荷不是有效的 UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise LicenseFormatError("授权载荷必须是 JSON 对象")
    return signed_content, signature, payload


def load_embedded_public_key() -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(EMBEDDED_PUBLIC_KEY_RAW)


def verify_license_blob(
    blob: bytes,
    public_key: Ed25519PublicKey,
    *,
    product: str = LICENSE_PRODUCT,
    now: datetime | None = None,
) -> LicenseClaims:
    signed_content, signature, payload = _decode_envelope(blob)
    try:
        public_key.verify(signature, signed_content)
    except InvalidSignature as exc:
        raise LicenseSignatureError("授权签名校验失败，文件可能已被修改") from exc

    schema_version = payload.get("version")
    if schema_version != LICENSE_SCHEMA_VERSION:
        raise LicenseFormatError(f"不支持的授权载荷版本: {schema_version!r}")
    actual_product = _required_string(payload, "product")
    if actual_product != product:
        raise LicenseProductMismatchError(
            f"授权产品不匹配: expected={product!r}, actual={actual_product!r}"
        )

    license_id = _required_string(payload, "license_id")
    customer = _required_string(payload, "customer")
    issued_at = _parse_timestamp(payload.get("issued_at"), "issued_at")
    expires_at = _parse_timestamp(payload.get("expires_at"), "expires_at")
    raw_not_before = payload.get("not_before")
    not_before = (
        _parse_timestamp(raw_not_before, "not_before")
        if raw_not_before is not None
        else None
    )
    if expires_at <= issued_at:
        raise LicenseFormatError("expires_at 必须晚于 issued_at")
    if not_before is not None and expires_at <= not_before:
        raise LicenseFormatError("expires_at 必须晚于 not_before")

    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise ValueError("now 必须是带时区的 datetime")
    checked_at = checked_at.astimezone(timezone.utc)
    if not_before is not None and checked_at < not_before:
        raise LicenseNotYetValidError(
            f"授权尚未生效: not_before={not_before.isoformat()}"
        )
    if checked_at >= expires_at:
        raise LicenseExpiredError(f"授权已过期: expires_at={expires_at.isoformat()}")

    return LicenseClaims(
        license_id=license_id,
        product=actual_product,
        customer=customer,
        issued_at=issued_at,
        expires_at=expires_at,
        not_before=not_before,
        raw=payload,
    )


def verify_license_file(
    license_file: Path,
    *,
    public_key: Ed25519PublicKey | None = None,
    product: str = LICENSE_PRODUCT,
    now: datetime | None = None,
) -> LicenseClaims:
    try:
        blob = license_file.read_bytes()
    except OSError as exc:
        raise LicenseValidationError(f"无法读取授权文件: {license_file}") from exc
    resolved_public_key = public_key or load_embedded_public_key()
    return verify_license_blob(blob, resolved_public_key, product=product, now=now)


class LicenseGuard:
    def __init__(
        self,
        config: LicenseConfig,
        *,
        now_factory: Callable[[], datetime] | None = None,
        public_key: Ed25519PublicKey | None = None,
    ) -> None:
        self._config = config
        self._now_factory = now_factory or (lambda: datetime.now(timezone.utc))
        self._public_key = public_key

    @property
    def required(self) -> bool:
        return self._config.required

    def validate(self) -> LicenseClaims | None:
        if not self._config.required:
            return None
        if self._config.license_file is None:
            raise LicenseConfigurationError(
                "DOCSENSE_LICENSE_REQUIRED=true 时必须配置 DOCSENSE_LICENSE_FILE"
            )
        return verify_license_file(
            self._config.license_file,
            public_key=self._public_key,
            product=self._config.product,
            now=self._now_factory(),
        )
