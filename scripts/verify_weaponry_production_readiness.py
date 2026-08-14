"""使用隔离临时资源验证 Weaponry 真实供应商协议并生成 production attestation。

本工具只读取既有 workspace/文档；远端写操作严格限定为两个带随机执行标识的临时
workspace、其中一个临时 thread，以及把一份既有全局文档绑定到临时检索 workspace。
finally 会删除两个临时 workspace，并重新读取全量快照证明既有 workspace/文档未改变。
任何清理结果不确定都会失败，禁止生成 attestation。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Mapping, Sequence
import uuid

from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env", override=False)

from app.integrations.anythingllm.models import (  # noqa: E402
    AnythingLLMSource,
    normalize_document_location_key,
    normalize_document_ref,
)
from app.integrations.anythingllm.policies import (  # noqa: E402
    document_rag_workspace_settings,
)
from app.modules.weaponry.adapters import (  # noqa: E402
    AnythingLLMWeaponryClientFactory,
    WeaponryAnythingLLMClients,
    build_weaponry_production_attestation,
    build_weaponry_runtime_policies,
    load_weaponry_runtime_config,
    normalize_anythingllm_source_url_ref,
    resolve_anythingllm_source_document_key,
)
from app.services.core.config import load_anythingllm_config  # noqa: E402


logger = logging.getLogger("scripts.verify_weaponry_production_readiness")
_WORKSPACE_PREFIX = "docsense-weaponry-readiness-"


class WeaponryProductionVerificationError(RuntimeError):
    """真实协议、资源所有权或清理证明不满足生产门禁。"""


def _snapshot(workspaces: Any, *, user_id: int) -> tuple[tuple[object, ...], ...]:
    rows: list[tuple[object, ...]] = []
    for workspace in workspaces.list_workspaces(user_id=user_id):
        documents = workspaces.list_documents(workspace.slug, user_id=user_id)
        rows.append(
            (
                workspace.slug,
                workspace.id,
                tuple(sorted((item.location, item.id) for item in documents)),
            )
        )
    return tuple(sorted(rows))


def _find_source_document(
    workspaces: Any,
    *,
    user_id: int,
    ingested_ref_by_location: Mapping[str, str],
) -> tuple[object, object, str]:
    """确定性选择一份具有本地权威入库谱系的已绑定文档。"""

    for workspace in sorted(
        workspaces.list_workspaces(user_id=user_id),
        key=lambda item: item.slug,
    ):
        documents = workspaces.list_documents(workspace.slug, user_id=user_id)
        for document in sorted(documents, key=lambda item: item.location):
            location = normalize_document_location_key(document.location)
            ingested_ref = ingested_ref_by_location.get(location)
            if ingested_ref:
                return workspace, document, ingested_ref
    raise WeaponryProductionVerificationError(
        "AnythingLLM 中没有与本地知识库谱系精确对应的既有文档"
    )


def load_ingested_identity_catalog(
    database_path: Path,
) -> dict[str, str]:
    """只读加载 ``doc_path -> ingested_file_name``，不初始化或迁移数据库。

    production attestation 必须证明生产 Adapter 真正使用的本地权威谱系，不能从供应商
    title 或本次 vector-search 响应反推期望值，否则验证会退化成自我证明。
    """

    path = database_path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"知识库数据库不存在或不是文件: {path}")
    connection = sqlite3.connect(
        f"file:{path.as_posix()}?mode=ro",
        uri=True,
        timeout=5.0,
    )
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT doc_path, ingested_file_name FROM documents"
        ).fetchall()
    except sqlite3.Error as exc:
        raise WeaponryProductionVerificationError(
            "无法只读加载知识库文档身份谱系"
        ) from exc
    finally:
        connection.close()

    catalog: dict[str, str] = {}
    for row in rows:
        location = normalize_document_location_key(str(row["doc_path"] or ""))
        ingested_ref = normalize_document_ref(
            str(row["ingested_file_name"] or "")
        )
        if not location or not ingested_ref:
            continue
        existing = catalog.get(location)
        if existing is not None and existing != ingested_ref:
            raise WeaponryProductionVerificationError(
                "知识库同一文档位置对应多个入库文件身份"
            )
        catalog[location] = ingested_ref
    if not catalog:
        raise WeaponryProductionVerificationError(
            "知识库中没有可用于来源验证的完整文档身份谱系"
        )
    return catalog


def _wait_for_sources(
    workspaces: Any,
    *,
    workspace_slug: str,
    query: str,
    top_n: int,
    user_id: int,
    timeout_seconds: float,
) -> list[AnythingLLMSource]:
    deadline = time.monotonic() + timeout_seconds
    while True:
        sources = workspaces.vector_search(
            workspace_slug,
            query,
            top_n=top_n,
            score_threshold=0.0,
            user_id=user_id,
        )
        if sources:
            return sources
        if time.monotonic() >= deadline:
            raise WeaponryProductionVerificationError(
                "临时检索 workspace 在限定时间内没有返回 Candidate"
            )
        time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))


def _identity_fields(
    source: AnythingLLMSource,
    *,
    location_keys: set[str],
    provider_ids: set[str],
    ingested_refs: set[str],
) -> set[str]:
    fields: set[str] = set()
    metadata = source.metadata or {}
    for key in ("location", "docpath", "docPath", "sourceDocument", "url"):
        value = metadata.get(key)
        if value is not None and normalize_document_location_key(str(value)) in location_keys:
            fields.add(key)
    if source.url and normalize_document_location_key(source.url) in location_keys:
        fields.add("url")
    for value in (metadata.get("url"), source.url):
        if normalize_anythingllm_source_url_ref(value) in ingested_refs:
            fields.add("url")
    for key in ("docId", "documentId"):
        if str(metadata.get(key) or "").strip() in provider_ids:
            fields.add(key)
    if source.id and source.id in provider_ids:
        fields.add("source.id")
    return fields


def _delete_owned_workspaces(
    workspaces: Any,
    *,
    owned_names: Sequence[str],
    baseline_slugs: set[str],
    user_id: int,
) -> int:
    """只按随机完整名称恢复本次 workspace；与基线重名时拒绝取得删除权。"""

    deleted = 0
    current = workspaces.list_workspaces(user_id=user_id)
    for name in owned_names:
        matches = [item for item in current if item.name == name]
        if len(matches) > 1:
            raise WeaponryProductionVerificationError(
                "发现多个同名临时 workspace，无法证明唯一删除权"
            )
        if not matches:
            continue
        workspace = matches[0]
        if workspace.slug in baseline_slugs:
            raise WeaponryProductionVerificationError(
                "临时 workspace 名称与执行前资源冲突，禁止删除"
            )
        workspaces.delete_workspace(workspace.slug, user_id=user_id)
        deleted += 1
        current = workspaces.list_workspaces(user_id=user_id)
    return deleted


def verify_and_build_attestation(
    client: WeaponryAnythingLLMClients,
    *,
    environment: str,
    user_id: int,
    top_n: int,
    readiness_timeout_seconds: float,
    valid_for_seconds: float,
    ingested_ref_by_location: Mapping[str, str],
) -> dict[str, object]:
    """完成四项真实检查、清理临时资源并返回已绑定证据的 Schema v2 证明。"""

    infrastructure = load_weaponry_runtime_config()
    policies = build_weaponry_runtime_policies(infrastructure)
    token = uuid.uuid4().hex
    retrieval_name = f"{_WORKSPACE_PREFIX}retrieval-{token}"
    extraction_name = f"{_WORKSPACE_PREFIX}extraction-{token}"
    owned_names = (retrieval_name, extraction_name)
    baseline = _snapshot(client.workspaces, user_id=user_id)
    baseline_slugs = {str(item[0]) for item in baseline}
    source_workspace, source_document, trusted_ingested_ref = _find_source_document(
        client.workspaces,
        user_id=user_id,
        ingested_ref_by_location=ingested_ref_by_location,
    )
    del source_workspace  # 只需全局文档位置；禁止修改来源 workspace。

    retrieval_slug = ""
    extraction_slug = ""
    thread_slug = ""
    created_count = 0
    deleted_count = 0
    evidence: dict[str, object] = {}
    operation_error: BaseException | None = None
    try:
        retrieval_workspace = client.workspaces.create_workspace(
            retrieval_name,
            settings=document_rag_workspace_settings(),
            user_id=user_id,
        )
        if retrieval_workspace.slug in baseline_slugs:
            raise WeaponryProductionVerificationError("检索临时 workspace 与基线冲突")
        retrieval_slug = retrieval_workspace.slug
        created_count += 1
        client.workspaces.update_embeddings(
            retrieval_slug,
            adds=[source_document.location],
            user_id=user_id,
        )
        bound_documents = client.workspaces.list_documents(
            retrieval_slug,
            user_id=user_id,
        )
        if len(bound_documents) != 1:
            raise WeaponryProductionVerificationError(
                "检索临时 workspace 没有保持严格单文档绑定"
            )
        bound = bound_documents[0]
        location_key = normalize_document_location_key(bound.location)
        location_map = {location_key: "readiness-document"}
        provider_ids = {
            str(bound.id or "").strip(),
            str(bound.raw_document_id or "").strip(),
        }
        provider_ids.discard("")
        provider_id_map = {item: "readiness-document" for item in provider_ids}
        ingested_ref_map = {trusted_ingested_ref: "readiness-document"}
        query = str(bound.title or "").strip() or "文档内容"
        sources = _wait_for_sources(
            client.workspaces,
            workspace_slug=retrieval_slug,
            query=query,
            top_n=top_n,
            user_id=user_id,
            timeout_seconds=readiness_timeout_seconds,
        )
        resolved = 0
        ambiguous = 0
        identity_fields: set[str] = set()
        for source in sources:
            try:
                resolved_key = resolve_anythingllm_source_document_key(
                    source,
                    document_key_by_location=location_map,
                    document_key_by_provider_id=provider_id_map,
                    document_key_by_ingested_ref=ingested_ref_map,
                )
            except Exception:
                ambiguous += 1
                continue
            if resolved_key == "readiness-document":
                resolved += 1
            identity_fields.update(
                _identity_fields(
                    source,
                    location_keys={location_key},
                    provider_ids=provider_ids,
                    ingested_refs={trusted_ingested_ref},
                )
            )
        score_presence = [item.score_present for item in sources]
        score_mode = "score" if sources and all(score_presence) else "rank"
        valid_score_count = sum(
            item.score_present and item.score_valid for item in sources
        )
        mixed_score_mode = any(score_presence) and not all(score_presence)
        evidence["scoreRankProtocol"] = {
            "passed": not mixed_score_mode
            and (
                valid_score_count == len(sources)
                if score_mode == "score"
                else valid_score_count == 0
            ),
            "candidateCount": len(sources),
            "validScoreCount": valid_score_count,
            "validRankCount": len(sources),
            "scoreMode": score_mode,
        }
        evidence["sourceIdentity"] = {
            "passed": resolved == len(sources) and ambiguous == 0,
            "sourceCount": len(sources),
            "resolvedCount": resolved,
            "ambiguousCount": ambiguous,
            "outOfScopeCount": 0,
            "identityFields": sorted(identity_fields),
        }

        extraction_workspace = client.workspaces.create_workspace(
            extraction_name,
            user_id=user_id,
        )
        if extraction_workspace.slug in baseline_slugs:
            raise WeaponryProductionVerificationError("抽取临时 workspace 与基线冲突")
        extraction_slug = extraction_workspace.slug
        created_count += 1
        before_documents = client.workspaces.list_documents(
            extraction_slug,
            user_id=user_id,
        )
        nonce = f"DOCSENSE_READINESS_{token.upper()}"
        thread = client.threads.create_thread(
            extraction_slug,
            f"readiness-{token}",
            user_id=user_id,
        )
        thread_slug = thread.slug
        answer = client.threads.ask(
            extraction_slug,
            thread_slug,
            (
                "这是隔离生产就绪验证。不要访问知识库，只输出下一行标识，不要解释：\n"
                f"{nonce}"
            ),
            mode="chat",
            user_id=user_id,
            document_ids=(),
        )
        after_documents = client.workspaces.list_documents(
            extraction_slug,
            user_id=user_id,
        )
        evidence["emptyWorkspaceIsolation"] = {
            "passed": not before_documents and not after_documents,
            "documentCountBefore": len(before_documents),
            "documentCountAfter": len(after_documents),
        }
        evidence["providedEvidenceIsolation"] = {
            "passed": not answer.sources and nonce in answer.text,
            "requestDocumentIdCount": 0,
            "responseSourceCount": len(answer.sources),
            "responseChars": len(answer.text),
            "nonceMatched": nonce in answer.text,
        }
    except BaseException as exc:
        operation_error = exc
    finally:
        if extraction_slug and thread_slug:
            try:
                client.threads.delete_thread(
                    extraction_slug,
                    thread_slug,
                    user_id=user_id,
                )
            except Exception:
                # workspace 删除会级联清理 thread；不能在这里提前掩盖主错误，最终仍以
                # workspace 不存在和基线恢复作为删除完成的权威判断。
                logger.warning("临时验证 thread 单独删除未确认，将由 workspace 级联清理")
        deleted_count = _delete_owned_workspaces(
            client.workspaces,
            owned_names=owned_names,
            baseline_slugs=baseline_slugs,
            user_id=user_id,
        )

    final_snapshot = _snapshot(client.workspaces, user_id=user_id)
    baseline_restored = final_snapshot == baseline
    evidence["resourceCleanup"] = {
        "passed": created_count == 2
        and deleted_count == created_count
        and baseline_restored,
        "temporaryWorkspaceCount": created_count,
        "deletedWorkspaceCount": deleted_count,
        "baselineSnapshotRestored": baseline_restored,
        "existingResourcesModified": not baseline_restored,
    }
    if not baseline_restored or deleted_count != created_count:
        raise WeaponryProductionVerificationError(
            "临时资源清理后未恢复执行前 AnythingLLM 资源快照"
        ) from operation_error
    if operation_error is not None:
        raise operation_error

    return build_weaponry_production_attestation(
        profile_id=policies.evidence_selection.profile_id,
        fingerprints={
            "provider": infrastructure.provider_fingerprint,
            "embedding": infrastructure.embedding_fingerprint,
            "documentProcessing": infrastructure.document_processing_fingerprint,
            "extractionModel": infrastructure.extraction_model_fingerprint,
        },
        environment=environment,
        evidence=evidence,
        verified_at=datetime.now(timezone.utc),
        valid_for_seconds=valid_for_seconds,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="验证 Weaponry 真实供应商协议并生成有时效的 production attestation"
    )
    parser.add_argument("--environment", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument("--top-n", type=int, default=8)
    parser.add_argument("--readiness-timeout-seconds", type=float, default=90.0)
    parser.add_argument("--valid-for-hours", type=float, default=24.0)
    runtime_dir = Path(
        os.getenv("DOCSENSE_RUNTIME_DIR", str(ROOT / ".runtime"))
    ).expanduser()
    parser.add_argument(
        "--knowledge-db-path",
        type=Path,
        default=Path(
            os.getenv(
                "DOCSENSE_KNOWLEDGE_BASE_DB",
                os.getenv(
                    "KNOWLEDGE_BASE_DB_PATH",
                    str(runtime_dir / "knowledge_base.sqlite3"),
                ),
            )
        ),
        help="只读知识库 SQLite；用于绑定 doc_path 与 ingested_file_name。",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.user_id < 1:
        raise ValueError("user-id 必须是正整数")
    if args.top_n < 1 or args.top_n > 100:
        raise ValueError("top-n 必须是 1~100 的整数")
    if args.readiness_timeout_seconds <= 0 or args.readiness_timeout_seconds > 600:
        raise ValueError("readiness-timeout-seconds 必须大于 0 且不超过 600")
    if args.valid_for_hours <= 0 or args.valid_for_hours > 168:
        raise ValueError("valid-for-hours 必须大于 0 且不超过 168")

    identity_catalog = load_ingested_identity_catalog(args.knowledge_db_path)
    factory = AnythingLLMWeaponryClientFactory(load_anythingllm_config())
    with factory.create() as client:
        attestation = verify_and_build_attestation(
            client,
            environment=args.environment,
            user_id=args.user_id,
            top_n=args.top_n,
            readiness_timeout_seconds=args.readiness_timeout_seconds,
            valid_for_seconds=args.valid_for_hours * 3600.0,
            ingested_ref_by_location=identity_catalog,
        )
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    # 证明文件会被应用启动门禁并发读取，必须先完整落盘再原子替换，不能让半写 JSON
    # 短暂关闭生产 readiness。临时文件与目标位于同一目录，保证 ``os.replace`` 不跨卷。
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(attestation, ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    logger.info(
        "Weaponry production attestation 已生成: output=%s schema=%s",
        output,
        attestation["schemaVersion"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
