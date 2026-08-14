"""Report 最终 Artifact 与 Task 终态共用的稳定身份算法。"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any

from app.modules.report.ports import ReportArtifactRef


def report_artifact_result_ref(
    artifact: ReportArtifactRef | Mapping[str, Any],
) -> str:
    """生成终态和资源记录共同校验的 Canonical Artifact 引用。

    这是 Application 的确定性身份算法，不读取文件、数据库或网络。Port 只保留不透明
    Artifact DTO，避免把 Canonical JSON 与摘要实现错误地下沉到抽象端口层。
    """

    if isinstance(artifact, ReportArtifactRef):
        payload: Mapping[str, Any] = {
            "task_id": artifact.task_id.value,
            "artifact_id": artifact.artifact_id,
            "category": artifact.category.value,
            "sequence_no": artifact.sequence_no,
            "size_bytes": artifact.size_bytes,
            "checksum": artifact.checksum,
        }
    elif isinstance(artifact, Mapping):
        payload = artifact
    else:
        raise TypeError("artifact 必须是 ReportArtifactRef 或 Mapping")
    try:
        canonical = json.dumps(
            dict(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Artifact 引用必须可编码为 Canonical JSON") from exc
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"report-artifact:v1:{digest}"


__all__ = ["report_artifact_result_ref"]
