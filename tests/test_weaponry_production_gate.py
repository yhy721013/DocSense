"""阶段 1D-7 前真实供应商能力门禁与应用就绪度验收。"""

from __future__ import annotations

import json
import io
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

from app.modules.weaponry.adapters import (
    WEAPONRY_PRODUCTION_ATTESTATION_SCHEMA,
    build_weaponry_production_attestation,
    evaluate_weaponry_production_gate,
)
from tests.offline_application import build_offline_application_services
from tests import workspace_tempdir
from scripts.check_weaponry_production_gate import main as check_gate_main


_PROFILE_ID = "weaponry-production-v2-test"
_FINGERPRINTS = {
    "provider": "provider-test",
    "embedding": "embedding-test",
    "documentProcessing": "processing-test",
    "extractionModel": "model-test",
}


_VERIFIED_AT = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc)


def _evidence() -> dict[str, object]:
    return {
        "scoreRankProtocol": {
            "passed": True,
            "candidateCount": 8,
            "validScoreCount": 8,
            "validRankCount": 8,
            "scoreMode": "score",
        },
        "sourceIdentity": {
            "passed": True,
            "sourceCount": 8,
            "resolvedCount": 8,
            "ambiguousCount": 0,
            "outOfScopeCount": 0,
            "identityFields": ["url"],
        },
        "emptyWorkspaceIsolation": {
            "passed": True,
            "documentCountBefore": 0,
            "documentCountAfter": 0,
        },
        "providedEvidenceIsolation": {
            "passed": True,
            "requestDocumentIdCount": 0,
            "responseSourceCount": 0,
            "responseChars": 32,
            "nonceMatched": True,
        },
        "resourceCleanup": {
            "passed": True,
            "temporaryWorkspaceCount": 2,
            "deletedWorkspaceCount": 2,
            "baselineSnapshotRestored": True,
            "existingResourcesModified": False,
        },
    }


def _attestation() -> dict[str, object]:
    return build_weaponry_production_attestation(
        profile_id=_PROFILE_ID,
        fingerprints=_FINGERPRINTS,
        environment="isolated-production-like-test",
        evidence=_evidence(),
        verified_at=_VERIFIED_AT,
        valid_for_seconds=86_400,
    )


class WeaponryProductionGateTests(unittest.TestCase):
    def test_missing_attestation_is_machine_readable_not_ready(self) -> None:
        result = evaluate_weaponry_production_gate(
            attestation_path=None,
            profile_id=_PROFILE_ID,
            fingerprints=_FINGERPRINTS,
        )

        self.assertFalse(result.ready)
        self.assertEqual("production_attestation_path_missing", result.reason)

    def test_exact_profile_fingerprints_and_checks_are_required(self) -> None:
        with workspace_tempdir() as directory:
            path = Path(directory) / "attestation.json"
            path.write_text(
                json.dumps(_attestation(), ensure_ascii=False),
                encoding="utf-8",
            )
            ready = evaluate_weaponry_production_gate(
                attestation_path=str(path),
                profile_id=_PROFILE_ID,
                fingerprints=_FINGERPRINTS,
                now=_VERIFIED_AT + timedelta(hours=1),
            )

            changed = _attestation()
            changed["fingerprints"] = {
                **_FINGERPRINTS,
                "embedding": "different-embedding",
            }
            path.write_text(json.dumps(changed), encoding="utf-8")
            mismatch = evaluate_weaponry_production_gate(
                attestation_path=str(path),
                profile_id=_PROFILE_ID,
                fingerprints=_FINGERPRINTS,
                now=_VERIFIED_AT + timedelta(hours=1),
            )

        self.assertTrue(ready.ready)
        self.assertEqual("ready", ready.reason)
        self.assertFalse(mismatch.ready)
        self.assertEqual(
            "production_attestation_fingerprint_mismatch",
            mismatch.reason,
        )

    def test_tampered_embedded_evidence_cannot_reuse_old_digest(self) -> None:
        with workspace_tempdir() as directory:
            path = Path(directory) / "attestation.json"
            payload = _attestation()
            payload["evidence"]["sourceIdentity"]["resolvedCount"] = 7
            path.write_text(json.dumps(payload), encoding="utf-8")

            result = evaluate_weaponry_production_gate(
                attestation_path=str(path),
                profile_id=_PROFILE_ID,
                fingerprints=_FINGERPRINTS,
                now=_VERIFIED_AT + timedelta(hours=1),
            )

        self.assertFalse(result.ready)
        self.assertEqual(
            "production_attestation_source_identity_evidence_failed",
            result.reason,
        )

    def test_attestation_expires_and_rejects_unbounded_validity(self) -> None:
        with self.assertRaisesRegex(ValueError, "不能超过 7 天"):
            build_weaponry_production_attestation(
                profile_id=_PROFILE_ID,
                fingerprints=_FINGERPRINTS,
                environment="test",
                evidence=_evidence(),
                verified_at=_VERIFIED_AT,
                valid_for_seconds=8 * 24 * 60 * 60,
            )

        with workspace_tempdir() as directory:
            path = Path(directory) / "attestation.json"
            path.write_text(json.dumps(_attestation()), encoding="utf-8")
            expired = evaluate_weaponry_production_gate(
                attestation_path=str(path),
                profile_id=_PROFILE_ID,
                fingerprints=_FINGERPRINTS,
                now=_VERIFIED_AT + timedelta(days=2),
            )

        self.assertFalse(expired.ready)
        self.assertEqual("production_attestation_expired", expired.reason)

    def test_dispatchers_can_run_while_production_gate_remains_closed(self) -> None:
        """开发环境可启动，但内部就绪快照不得把它伪装成生产可接流量。"""

        with workspace_tempdir() as directory:
            services = build_offline_application_services(directory)
            try:
                services.start_background_services()
                snapshot = services.readiness_snapshot()
            finally:
                services.close()

        self.assertFalse(snapshot.lifecycle_ready)
        self.assertFalse(snapshot.production_gate_ready)
        self.assertFalse(snapshot.ready)
        self.assertIn(
            "weaponry_production_gate:production_attestation_path_missing",
            snapshot.reasons,
        )

    def test_production_mode_fails_fast_before_public_routes_can_start(self) -> None:
        with workspace_tempdir() as directory:
            services = build_offline_application_services(directory)
            try:
                weaponry = services.weaponry_services
                assert weaponry is not None
                required = replace(
                    weaponry,
                    config=replace(
                        weaponry.config,
                        production_gate_required=True,
                    ),
                )
                with self.assertRaisesRegex(
                    RuntimeError,
                    "production_attestation_path_missing",
                ):
                    replace(services, weaponry_services=required)
            finally:
                services.close()

    def test_gate_check_reports_invalid_configuration_without_leaking_value(self) -> None:
        """发布脚本配置失败时仍输出稳定 JSON，且不回显可能包含凭据的异常。"""

        output = io.StringIO()
        with patch(
            "scripts.check_weaponry_production_gate."
            "load_weaponry_infrastructure_config",
            side_effect=ValueError("sensitive-provider-detail"),
        ), patch("sys.stdout", output):
            exit_code = check_gate_main()

        payload = json.loads(output.getvalue())
        self.assertEqual(1, exit_code)
        self.assertFalse(payload["ready"])
        self.assertEqual(
            "production_gate_configuration_invalid",
            payload["reason"],
        )
        self.assertNotIn("sensitive-provider-detail", output.getvalue())

    def test_fingerprint_values_must_be_real_strings(self) -> None:
        with self.assertRaisesRegex(TypeError, "值必须是 str"):
            build_weaponry_production_attestation(
                profile_id=_PROFILE_ID,
                fingerprints={**_FINGERPRINTS, "provider": None},
                environment="test",
                evidence=_evidence(),
                verified_at=_VERIFIED_AT,
            )


if __name__ == "__main__":
    unittest.main()
