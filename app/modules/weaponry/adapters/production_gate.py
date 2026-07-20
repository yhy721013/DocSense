"""武器谱真实供应商能力证明的生成与只读校验。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Mapping


WEAPONRY_PRODUCTION_ATTESTATION_SCHEMA = (
    "docsense.weaponry.production-readiness.v2"
)
_REQUIRED_EVIDENCE_KEYS = frozenset(
    {
        "scoreRankProtocol",
        "sourceIdentity",
        "emptyWorkspaceIsolation",
        "providedEvidenceIsolation",
        "resourceCleanup",
    }
)
_ALLOWED_IDENTITY_FIELDS = frozenset(
    {
        "location",
        "docpath",
        "docPath",
        "sourceDocument",
        "url",
        "docId",
        "documentId",
        "source.id",
    }
)
_MAX_VALIDITY = timedelta(days=7)
_CLOCK_SKEW = timedelta(minutes=5)


@dataclass(frozen=True)
class WeaponryProductionGateSnapshot:
    """供组合根、部署脚本和测试读取的稳定门禁结果。"""

    ready: bool
    reason: str
    profile_id: str
    attestation_digest: str = ""
    verified_at: str = ""
    expires_at: str = ""


def build_weaponry_production_attestation(
    *,
    profile_id: str,
    fingerprints: Mapping[str, str],
    environment: str,
    evidence: Mapping[str, object],
    verified_at: datetime | None = None,
    valid_for_seconds: float = 86_400.0,
) -> dict[str, object]:
    """由真实联调工具生成 Schema v2 证明，并把摘要绑定到内嵌观测证据。

    该函数不会访问网络或写文件。调用方必须先完成隔离临时资源验证，再把脱敏指标作为
    ``evidence`` 传入。门禁会重新计算摘要并复核各项数量关系，不能再通过四个手写布尔值
    冒充真实结果。
    """

    normalized_profile = _required_text(profile_id, name="profile_id")
    normalized_fingerprints = _normalize_fingerprints(fingerprints)
    normalized_environment = _required_text(environment, name="environment")
    validity = _positive_finite(valid_for_seconds, name="valid_for_seconds")
    if validity > _MAX_VALIDITY.total_seconds():
        raise ValueError("valid_for_seconds 不能超过 7 天")
    normalized_evidence = _normalize_evidence(evidence)
    evidence_error = _evidence_error(normalized_evidence)
    if evidence_error is not None:
        raise ValueError(evidence_error)

    observed_at = verified_at or datetime.now(timezone.utc)
    observed_at = _aware_datetime(observed_at, name="verified_at")
    expires_at = observed_at + timedelta(seconds=validity)
    evidence_digest = hashlib.sha256(
        _canonical_json(normalized_evidence)
    ).hexdigest()
    return {
        "schemaVersion": WEAPONRY_PRODUCTION_ATTESTATION_SCHEMA,
        "profileId": normalized_profile,
        "fingerprints": normalized_fingerprints,
        "environment": normalized_environment,
        "verifiedAt": observed_at.isoformat(),
        "expiresAt": expires_at.isoformat(),
        "evidenceDigest": evidence_digest,
        "evidence": normalized_evidence,
    }


def evaluate_weaponry_production_gate(
    *,
    attestation_path: str | None,
    profile_id: str,
    fingerprints: Mapping[str, str],
    now: datetime | None = None,
) -> WeaponryProductionGateSnapshot:
    """校验受控环境生成的能力证明，不执行网络请求或修改供应商资源。

    缺失或无效证明返回 ``ready=False``，而不是让开发环境启动失败。生产启动命令必须读取
    该快照作为发布/流量就绪依据；不能用 Fake 测试、配置存在或进程存活替代真实证明。
    """

    normalized_profile = _required_text(profile_id, name="profile_id")
    expected_fingerprints = _normalize_fingerprints(fingerprints)
    if attestation_path is None or not str(attestation_path).strip():
        return WeaponryProductionGateSnapshot(
            False,
            "production_attestation_path_missing",
            normalized_profile,
        )

    path = Path(str(attestation_path).strip())
    try:
        raw = path.read_bytes()
    except OSError:
        return WeaponryProductionGateSnapshot(
            False,
            "production_attestation_unreadable",
            normalized_profile,
        )
    digest = hashlib.sha256(raw).hexdigest()
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-standard JSON number: {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return WeaponryProductionGateSnapshot(
            False,
            "production_attestation_invalid_json",
            normalized_profile,
            digest,
        )
    if not isinstance(payload, dict):
        return WeaponryProductionGateSnapshot(
            False,
            "production_attestation_not_object",
            normalized_profile,
            digest,
        )

    verified_text = str(payload.get("verifiedAt") or "").strip()
    expires_text = str(payload.get("expiresAt") or "").strip()
    if payload.get("schemaVersion") != WEAPONRY_PRODUCTION_ATTESTATION_SCHEMA:
        reason = "production_attestation_schema_mismatch"
    elif payload.get("profileId") != normalized_profile:
        reason = "production_attestation_profile_mismatch"
    elif payload.get("fingerprints") != expected_fingerprints:
        reason = "production_attestation_fingerprint_mismatch"
    elif not isinstance(payload.get("environment"), str) or not payload[
        "environment"
    ].strip():
        reason = "production_attestation_environment_missing"
    else:
        verified_at = _parse_aware_datetime(verified_text)
        expires_at = _parse_aware_datetime(expires_text)
        current = _aware_datetime(
            now or datetime.now(timezone.utc),
            name="now",
        )
        if verified_at is None:
            reason = "production_attestation_verified_at_invalid"
        elif expires_at is None:
            reason = "production_attestation_expires_at_invalid"
        elif expires_at <= verified_at:
            reason = "production_attestation_validity_invalid"
        elif expires_at - verified_at > _MAX_VALIDITY:
            reason = "production_attestation_validity_too_long"
        elif verified_at - current > _CLOCK_SKEW:
            reason = "production_attestation_verified_in_future"
        elif current > expires_at:
            reason = "production_attestation_expired"
        else:
            evidence = payload.get("evidence")
            if not isinstance(evidence, dict):
                reason = "production_attestation_evidence_missing"
            else:
                evidence_error = _evidence_error(evidence)
                if evidence_error is not None:
                    reason = evidence_error
                else:
                    expected_digest = hashlib.sha256(
                        _canonical_json(evidence)
                    ).hexdigest()
                    if payload.get("evidenceDigest") != expected_digest:
                        reason = "production_attestation_evidence_digest_mismatch"
                    else:
                        return WeaponryProductionGateSnapshot(
                            True,
                            "ready",
                            normalized_profile,
                            digest,
                            verified_text,
                            expires_text,
                        )
    return WeaponryProductionGateSnapshot(
        False,
        reason,
        normalized_profile,
        digest,
        verified_text,
        expires_text,
    )


def _evidence_error(evidence: Mapping[str, object]) -> str | None:
    if set(evidence) != _REQUIRED_EVIDENCE_KEYS:
        return "production_attestation_evidence_keys_invalid"
    score = _mapping(evidence.get("scoreRankProtocol"))
    identity = _mapping(evidence.get("sourceIdentity"))
    empty = _mapping(evidence.get("emptyWorkspaceIsolation"))
    provided = _mapping(evidence.get("providedEvidenceIsolation"))
    cleanup = _mapping(evidence.get("resourceCleanup"))
    if None in (score, identity, empty, provided, cleanup):
        return "production_attestation_evidence_record_invalid"

    assert score is not None
    candidate_count = _non_negative_int(score.get("candidateCount"))
    valid_score_count = _non_negative_int(score.get("validScoreCount"))
    valid_rank_count = _non_negative_int(score.get("validRankCount"))
    score_mode = score.get("scoreMode")
    if (
        score.get("passed") is not True
        or candidate_count is None
        or candidate_count < 1
        or valid_score_count is None
        or valid_rank_count != candidate_count
        or score_mode not in {"score", "rank"}
        or (score_mode == "score" and valid_score_count != candidate_count)
        or (score_mode == "rank" and valid_score_count != 0)
    ):
        return "production_attestation_score_rank_evidence_failed"

    assert identity is not None
    source_count = _non_negative_int(identity.get("sourceCount"))
    resolved_count = _non_negative_int(identity.get("resolvedCount"))
    ambiguous_count = _non_negative_int(identity.get("ambiguousCount"))
    out_of_scope_count = _non_negative_int(identity.get("outOfScopeCount"))
    identity_fields = identity.get("identityFields")
    if (
        identity.get("passed") is not True
        or source_count is None
        or source_count < 1
        or resolved_count != source_count
        or ambiguous_count != 0
        or out_of_scope_count != 0
        or not isinstance(identity_fields, list)
        or not identity_fields
        or any(
            not isinstance(item, str) or item not in _ALLOWED_IDENTITY_FIELDS
            for item in identity_fields
        )
    ):
        return "production_attestation_source_identity_evidence_failed"

    assert empty is not None
    if (
        empty.get("passed") is not True
        or _non_negative_int(empty.get("documentCountBefore")) != 0
        or _non_negative_int(empty.get("documentCountAfter")) != 0
    ):
        return "production_attestation_empty_workspace_evidence_failed"

    assert provided is not None
    response_chars = _non_negative_int(provided.get("responseChars"))
    if (
        provided.get("passed") is not True
        or _non_negative_int(provided.get("requestDocumentIdCount")) != 0
        or _non_negative_int(provided.get("responseSourceCount")) != 0
        or response_chars is None
        or response_chars < 1
        or provided.get("nonceMatched") is not True
    ):
        return "production_attestation_provided_evidence_evidence_failed"

    assert cleanup is not None
    created_count = _non_negative_int(cleanup.get("temporaryWorkspaceCount"))
    deleted_count = _non_negative_int(cleanup.get("deletedWorkspaceCount"))
    if (
        cleanup.get("passed") is not True
        or created_count is None
        or created_count < 2
        or deleted_count != created_count
        or cleanup.get("baselineSnapshotRestored") is not True
        or cleanup.get("existingResourcesModified") is not False
    ):
        return "production_attestation_resource_cleanup_evidence_failed"
    return None


def _normalize_evidence(value: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("evidence 必须是 Mapping")
    # JSON 往返同时完成深复制、字符串键检查和 NaN/Infinity 拒绝，防止调用方在证明生成后
    # 修改原始可变字典，或让非标准数字在不同运行时产生不同摘要。
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("evidence 必须是严格 JSON 对象") from exc
    if not isinstance(decoded, dict):
        raise ValueError("evidence 必须是严格 JSON 对象")
    return decoded


def _canonical_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _normalize_fingerprints(value: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError("fingerprints 必须是 Mapping")
    if any(not isinstance(key, str) for key in value):
        raise TypeError("fingerprints 的键必须是 str")
    if any(not isinstance(item, str) for item in value.values()):
        raise TypeError("fingerprints 的值必须是 str")
    normalized = {key: item.strip() for key, item in value.items()}
    if set(normalized) != {
        "provider",
        "embedding",
        "documentProcessing",
        "extractionModel",
    } or any(not item for item in normalized.values()):
        raise ValueError("fingerprints 必须包含四类非空生产能力指纹")
    return normalized


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须是非空 str")
    return value.strip()


def _positive_finite(value: object, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} 必须是数字")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{name} 必须是正有限数字")
    return normalized


def _aware_datetime(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} 必须是带时区 datetime")
    return value.astimezone(timezone.utc)


def _parse_aware_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _mapping(value: object) -> Mapping[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    return value


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


__all__ = [
    "WEAPONRY_PRODUCTION_ATTESTATION_SCHEMA",
    "WeaponryProductionGateSnapshot",
    "build_weaponry_production_attestation",
    "evaluate_weaponry_production_gate",
]
