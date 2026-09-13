from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import create_app
from docsense_license import (
    LICENSE_PRODUCT,
    LICENSE_SCHEMA_VERSION,
    LicenseConfig,
    LicenseConfigurationError,
    LicenseExpiredError,
    LicenseGuard,
    LicenseProductMismatchError,
    LicenseSignatureError,
    create_signed_license,
    verify_license_blob,
)


class OfflineLicenseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.private_key = Ed25519PrivateKey.generate()
        self.public_key = self.private_key.public_key()
        self.issued_at = datetime(2026, 8, 24, 0, 0, tzinfo=timezone.utc)
        self.expires_at = self.issued_at + timedelta(days=30)

    def _payload(self, **overrides):
        payload = {
            "version": LICENSE_SCHEMA_VERSION,
            "product": LICENSE_PRODUCT,
            "license_id": "test-license-001",
            "customer": "离线测试客户",
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }
        payload.update(overrides)
        return payload

    def _blob(self, **overrides) -> bytes:
        return create_signed_license(self._payload(**overrides), self.private_key)

    def test_valid_signed_binary_license_returns_claims(self) -> None:
        claims = verify_license_blob(
            self._blob(),
            self.public_key,
            now=self.issued_at + timedelta(days=1),
        )

        self.assertEqual(claims.license_id, "test-license-001")
        self.assertEqual(claims.customer, "离线测试客户")
        self.assertEqual(claims.expires_at, self.expires_at)

    def test_signature_tampering_is_rejected(self) -> None:
        tampered = bytearray(self._blob())
        tampered[-1] ^= 0x01

        with self.assertRaises(LicenseSignatureError):
            verify_license_blob(
                bytes(tampered),
                self.public_key,
                now=self.issued_at + timedelta(days=1),
            )

    def test_expiration_is_exclusive_at_exact_timestamp(self) -> None:
        with self.assertRaises(LicenseExpiredError):
            verify_license_blob(
                self._blob(),
                self.public_key,
                now=self.expires_at,
            )

    def test_license_for_another_product_is_rejected(self) -> None:
        with self.assertRaises(LicenseProductMismatchError):
            verify_license_blob(
                self._blob(product="OtherProduct"),
                self.public_key,
                now=self.issued_at + timedelta(days=1),
            )

    def test_required_guard_rejects_missing_paths(self) -> None:
        guard = LicenseGuard(LicenseConfig(required=True))

        with self.assertRaises(LicenseConfigurationError):
            guard.validate()

    def test_running_guard_rejects_after_expiration(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temporary_dir:
            root = Path(temporary_dir)
            license_file = root / "customer.license"
            license_file.write_bytes(self._blob())
            current_time = [self.issued_at + timedelta(days=1)]
            guard = LicenseGuard(
                LicenseConfig(
                    required=True,
                    license_file=license_file,
                ),
                now_factory=lambda: current_time[0],
                public_key=self.public_key,
            )
            self.assertEqual(guard.validate().license_id, "test-license-001")
            current_time[0] = self.expires_at
            with self.assertRaises(LicenseExpiredError):
                guard.validate()

    def test_expired_license_only_blocks_llm_features(self) -> None:
        guard = Mock()
        guard.validate.side_effect = LicenseExpiredError()
        app = create_app(services=Mock(), license_guard=guard)
        client = app.test_client()

        llm_response = client.post("/llm/chat", json={})
        ordinary_response = client.get("/debug/chat")

        self.assertEqual(llm_response.status_code, 403)
        self.assertEqual(llm_response.data, b"")
        self.assertEqual(ordinary_response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
