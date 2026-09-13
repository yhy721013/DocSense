from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from docsense_license import (
    LICENSE_PRODUCT,
    LICENSE_SCHEMA_VERSION,
    LicenseValidationError,
    create_signed_license,
    verify_license_blob,
)


logger = logging.getLogger(__name__)


def _aware_timestamp(value: str, field_name: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{field_name} 必须是 ISO 8601 时间，例如 2027-01-01T00:00:00+08:00"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError(f"{field_name} 必须显式包含时区")
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _existing_file(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"文件不存在: {path}")
    return path


def _output_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _ensure_writable_output(path: Path, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"输出文件已存在，如需覆盖请使用 --force: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    try:
        private_key = serialization.load_pem_private_key(
            path.read_bytes(),
            password=None,
        )
    except (OSError, TypeError, ValueError) as exc:
        raise ValueError(f"无法读取 Ed25519 PEM 私钥: {path}") from exc
    if not isinstance(private_key, Ed25519PrivateKey):
        raise ValueError("私钥必须是 Ed25519 私钥")
    return private_key


def _generate_key(args: argparse.Namespace) -> int:
    if args.private_key == args.public_key:
        raise ValueError("私钥和公钥必须使用不同的输出路径")
    _ensure_writable_output(args.private_key, args.force)
    _ensure_writable_output(args.public_key, args.force)
    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    args.private_key.write_bytes(private_bytes)
    os.chmod(args.private_key, 0o600)
    args.public_key.write_bytes(public_bytes)
    os.chmod(args.public_key, 0o644)
    logger.info("已生成 Ed25519 私钥: %s", args.private_key)
    logger.info("已生成 Ed25519 公钥: %s", args.public_key)
    logger.warning("私钥不得放入 DocSense 部署包或提交到代码仓库")
    return 0


def _issue(args: argparse.Namespace) -> int:
    expires_at = _aware_timestamp(args.expires_at, "--expires-at")
    issued_at = datetime.now(timezone.utc)
    license_id = args.license_id.strip()
    customer = args.customer.strip()
    if not license_id:
        raise ValueError("--license-id 不能为空")
    if not customer:
        raise ValueError("--customer 不能为空")
    not_before = (
        _aware_timestamp(args.not_before, "--not-before")
        if args.not_before
        else None
    )
    if expires_at <= issued_at:
        raise ValueError("--expires-at 必须晚于当前时间")
    if not_before is not None and expires_at <= not_before:
        raise ValueError("--expires-at 必须晚于 --not-before")

    payload = {
        "version": LICENSE_SCHEMA_VERSION,
        "product": LICENSE_PRODUCT,
        "license_id": license_id,
        "customer": customer,
        "issued_at": _utc_text(issued_at),
        "expires_at": _utc_text(expires_at),
    }
    if not_before is not None:
        payload["not_before"] = _utc_text(not_before)

    _ensure_writable_output(args.output, args.force)
    blob = create_signed_license(payload, _load_private_key(args.private_key))
    args.output.write_bytes(blob)
    logger.info("已签发二进制授权文件: %s", args.output)
    logger.info("授权到期时间: %s", payload["expires_at"])
    return 0


def _inspect(args: argparse.Namespace) -> int:
    claims = verify_license_blob(
        args.license.read_bytes(),
        _load_private_key(args.private_key).public_key(),
        product=LICENSE_PRODUCT,
    )
    sys.stdout.write(json.dumps(dict(claims.raw), ensure_ascii=False, indent=2) + "\n")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DocSense 离线授权签发工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    key_parser = subparsers.add_parser("generate-key", help="生成一对 Ed25519 密钥")
    key_parser.add_argument("--private-key", required=True, type=_output_path)
    key_parser.add_argument("--public-key", required=True, type=_output_path)
    key_parser.add_argument("--force", action="store_true", help="覆盖已存在的输出文件")
    key_parser.set_defaults(handler=_generate_key)

    issue_parser = subparsers.add_parser("issue", help="签发一个二进制 .license 文件")
    issue_parser.add_argument("--private-key", required=True, type=_existing_file)
    issue_parser.add_argument("--output", required=True, type=_output_path)
    issue_parser.add_argument("--license-id", required=True)
    issue_parser.add_argument("--customer", required=True)
    issue_parser.add_argument("--expires-at", required=True)
    issue_parser.add_argument("--not-before")
    issue_parser.add_argument("--force", action="store_true", help="覆盖已存在的输出文件")
    issue_parser.set_defaults(handler=_issue)

    inspect_parser = subparsers.add_parser("inspect", help="验签并显示授权内容")
    inspect_parser.add_argument("--license", required=True, type=_existing_file)
    inspect_parser.add_argument("--private-key", required=True, type=_existing_file)
    inspect_parser.set_defaults(handler=_inspect)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)s [%(name)s:%(lineno)d] %(message)s",
    )
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (
        argparse.ArgumentTypeError,
        LicenseValidationError,
        OSError,
        ValueError,
    ) as exc:
        logger.error("授权操作失败: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
