"""Schema v2 生产 Selection profile 的确定性构造器。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from app.modules.weaponry.domain import (
    EVIDENCE_DEDUP_STRATEGY,
    EVIDENCE_RANKING_STRATEGY,
    EVIDENCE_SCORE_PROTOCOL,
    EVIDENCE_SCORE_SEMANTICS,
    RETRIEVAL_QUERY_VERSION,
    EvidenceSelectionPolicy,
)


@dataclass(frozen=True)
class WeaponryProductionSelectionProfileConfig:
    """部署装配必须显式提供的供应商和内容处理指纹。

    本对象不读取环境变量，也不猜 AnythingLLM/embedding 版本。配置装配层可以读取环境，
    但必须把最终值显式传入并随 execution 编码；排队任务重试时只读取快照。
    """

    provider_fingerprint: str
    embedding_fingerprint: str
    document_processing_fingerprint: str
    input_candidate_top_n: int = 8
    table_candidate_top_n: int = 16
    reject_reference_like: bool = True


def build_weaponry_production_selection_policy(
    config: WeaponryProductionSelectionProfileConfig,
) -> EvidenceSelectionPolicy:
    """由完整运行清单生成稳定 profile_id 和唯一 Schema v2 Policy。"""

    if not isinstance(config, WeaponryProductionSelectionProfileConfig):
        raise TypeError("config 必须是 WeaponryProductionSelectionProfileConfig")
    manifest = {
        "provider_fingerprint": config.provider_fingerprint,
        "embedding_fingerprint": config.embedding_fingerprint,
        "document_processing_fingerprint": config.document_processing_fingerprint,
        "query_version": RETRIEVAL_QUERY_VERSION,
        "score_semantics": EVIDENCE_SCORE_SEMANTICS,
        "score_protocol": EVIDENCE_SCORE_PROTOCOL,
        "ranking_strategy": EVIDENCE_RANKING_STRATEGY,
        "input_candidate_top_n": config.input_candidate_top_n,
        "table_candidate_top_n": config.table_candidate_top_n,
        "dedup_strategy": EVIDENCE_DEDUP_STRATEGY,
        "reject_reference_like": config.reject_reference_like,
    }
    encoded = json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    profile_id = f"weaponry-production-v2-{hashlib.sha256(encoded).hexdigest()[:24]}"
    return EvidenceSelectionPolicy(profile_id=profile_id, **manifest)


__all__ = [
    "WeaponryProductionSelectionProfileConfig",
    "build_weaponry_production_selection_policy",
]
