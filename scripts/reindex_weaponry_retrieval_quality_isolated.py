"""在隔离 AnythingLLM workspace 中验证清洗副本的真实检索质量。

该工具只允许创建带随机执行标识的临时 workspace 和临时全局文档。既有 workspace、
既有文档和输入 JSON 始终只读；执行前后会对既有资源做完整快照比对。清理动作仅接受
本次创建接口返回、且文件名仍携带随机执行标识的资源位置，避免把同名或历史资源误判
为本次资源。

本工具服务于阶段 1D-0R 的一次性校准，不接管生产入库。当前历史原始 MHTML 已不存在，
因此输入是 AnythingLLM 已处理 JSON 中的 ``pageContent``；工具会删除可证明的 PDF 页眉、
页脚、纯引用标记和参考文献尾部。输出会明确记录该局限，禁止把结果冒充为
``mhtml-main-content-v1`` 的真实重建结论。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import tempfile
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.integrations.anythingllm.models import (  # noqa: E402
    AnythingLLMDocument,
    AnythingLLMSource,
    AnythingLLMWorkspace,
    normalize_document_path,
)
from app.integrations.anythingllm.policies import (  # noqa: E402
    document_rag_workspace_settings,
)
from app.modules.weaponry.adapters import (  # noqa: E402
    AnythingLLMWeaponryClientFactory,
)
from app.services.core.config import load_anythingllm_config  # noqa: E402
from scripts.calibrate_weaponry_retrieval_quality import (  # noqa: E402
    CalibrationQuery,
    _load_queries,
    _query_result,
)


logger = logging.getLogger("scripts.reindex_weaponry_retrieval_quality_isolated")

_WORKSPACE_NAME_PREFIX = "docsense-stage1d0r-calibration-"
_CITATION_ONLY_PATTERN = re.compile(
    r"^\s*\[(?:\d+)(?:\s*[-–,，]\s*\d+)*\]\s*$"
)
_PDF_TIMESTAMP_PATTERN = re.compile(
    r"^\s*\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}\s*$"
)
_PDF_FILE_URI_PATTERN = re.compile(
    r"^file:///.*?\.\.\.\d+/\d+(.*)$",
    flags=re.IGNORECASE,
)
_NUMBERED_REFERENCE_PATTERN = re.compile(r"^\s*(\d+)[.)]\s+\S")
_REFERENCE_SIGNAL_PATTERN = re.compile(
    r"(?:\bISBN\b|https?://|www\.|原始内容|存档于|\[(?:19|20)\d{2}[-/])",
    flags=re.IGNORECASE,
)


class IsolatedCalibrationError(RuntimeError):
    """隔离校准无法证明安全完成或完整清理时抛出。"""


@dataclass(frozen=True)
class ResourceSnapshot:
    """只在进程内保存的 AnythingLLM 资源快照。

    原始 slug、名称和文档位置不写入校准输出；外部报告只记录不可逆摘要与计数。
    """

    workspaces: tuple[tuple[str, str, str], ...]
    documents: tuple[tuple[str, str, str], ...]

    @property
    def digest(self) -> str:
        serialized = json.dumps(
            {"workspaces": self.workspaces, "documents": self.documents},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return _digest(serialized)


@dataclass
class OwnedResources:
    """本次 execution 唯一允许补偿删除的远端资源。"""

    execution_token: str
    workspace_name: str
    workspace_slug: str | None = None
    document_location: str | None = None


@dataclass(frozen=True)
class CleanedRetrievalCopy:
    """历史处理文本生成的临时检索副本及脱敏审计指标。"""

    text: str
    source_hash: str
    output_hash: str
    input_chars: int
    input_lines: int
    output_chars: int
    output_lines: int
    reference_start_line: int
    removed_citation_lines: int
    removed_header_lines: int
    recovered_page_suffix_lines: int


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _required_directory(value: Path, *, name: str) -> Path:
    resolved = value.expanduser().resolve()
    if not resolved.is_dir():
        raise ValueError(f"{name} 必须是已存在的目录")
    return resolved


def _load_processed_page_content(
    source_json: Path,
    *,
    storage_root: Path,
) -> tuple[str, str]:
    """只读加载 AnythingLLM 已处理 JSON，并限制来源必须位于文档存储目录。"""

    resolved = source_json.expanduser().resolve()
    allowed_root = (storage_root / "documents" / "custom-documents").resolve()
    if not resolved.is_file() or not _is_relative_to(resolved, allowed_root):
        raise ValueError("source-json 必须是 AnythingLLM custom-documents 下的既有 JSON")
    source_bytes = resolved.read_bytes()
    try:
        payload = json.loads(source_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("source-json 必须是 UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("source-json 根节点必须是对象")
    page_content = payload.get("pageContent")
    if not isinstance(page_content, str) or len(page_content.strip()) < 500:
        raise ValueError("source-json.pageContent 必须是至少 500 字符的正文")
    return page_content, hashlib.sha256(source_bytes).hexdigest()[:16]


def _find_reference_start(lines: Sequence[str]) -> int:
    """识别连续编号且带出版信息的参考文献尾部，拒绝仅凭单个 ``1.`` 截断。"""

    for index, line in enumerate(lines):
        first_match = _NUMBERED_REFERENCE_PATTERN.match(line)
        if not first_match or first_match.group(1) != "1":
            continue
        window = lines[index : index + 12]
        has_second_entry = any(
            (match := _NUMBERED_REFERENCE_PATTERN.match(candidate)) is not None
            and match.group(1) == "2"
            for candidate in window[1:]
        )
        if has_second_entry and _REFERENCE_SIGNAL_PATTERN.search("\n".join(window)):
            return index
    raise ValueError("无法可靠识别参考文献尾部，禁止生成可能混入引用噪声的校准副本")


def build_cleaned_retrieval_copy(
    page_content: str,
    *,
    source_hash: str,
) -> CleanedRetrievalCopy:
    """从历史 PDF 处理文本生成仅供隔离校准使用的正文副本。

    清洗只删除结构可证明的噪声，不改写事实、不翻译术语，也不跨行猜测缺失内容。PDF
    页脚 URI 末尾若紧跟正文，会保留页码标记后的正文后缀，避免把换页处的业务事实一并
    删除。
    """

    normalized = page_content.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines()
    reference_start = _find_reference_start(lines)
    body_lines = lines[:reference_start]
    first_title = next((line.strip() for line in body_lines if line.strip()), "")

    output_lines: list[str] = []
    removed_citations = 0
    removed_headers = 0
    recovered_suffixes = 0
    for raw_line in body_lines:
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue
        if _CITATION_ONLY_PATTERN.fullmatch(line):
            removed_citations += 1
            continue
        if _PDF_TIMESTAMP_PATTERN.fullmatch(line):
            removed_headers += 1
            continue
        uri_match = _PDF_FILE_URI_PATTERN.match(line)
        if uri_match:
            removed_headers += 1
            line = uri_match.group(1).strip()
            if not line:
                continue
            recovered_suffixes += 1
        # PDF 每页重复打印文章标题。保留文档首行，后续完全相同的标题均属于页眉。
        if line == first_title and output_lines:
            removed_headers += 1
            continue
        if output_lines and output_lines[-1] == line:
            continue
        output_lines.append(line)

    text = "\n".join(output_lines).strip()
    if len(text) < 500:
        raise ValueError("清洗后的检索正文少于 500 字符，禁止上传")
    return CleanedRetrievalCopy(
        text=text,
        source_hash=source_hash,
        output_hash=_digest(text),
        input_chars=len(normalized),
        input_lines=len(lines),
        output_chars=len(text),
        output_lines=len(output_lines),
        reference_start_line=reference_start + 1,
        removed_citation_lines=removed_citations,
        removed_header_lines=removed_headers,
        recovered_page_suffix_lines=recovered_suffixes,
    )


def snapshot_resources(
    workspace_client: Any,
    *,
    user_id: int,
    excluded_workspace_slugs: Sequence[str] = (),
) -> ResourceSnapshot:
    """读取全部 workspace 及其绑定文档；任一读取失败都必须使校准失败。"""

    excluded = {value.casefold() for value in excluded_workspace_slugs}
    workspaces: list[tuple[str, str, str]] = []
    documents: list[tuple[str, str, str]] = []
    for workspace in workspace_client.list_workspaces(user_id=user_id):
        if workspace.slug.casefold() in excluded:
            continue
        workspaces.append((workspace.slug, workspace.id, workspace.name))
        for document in workspace_client.list_documents(
            workspace.slug,
            user_id=user_id,
        ):
            documents.append((workspace.slug, document.location, document.id))
    return ResourceSnapshot(
        workspaces=tuple(sorted(workspaces)),
        documents=tuple(sorted(documents)),
    )


def _validate_owned_workspace(
    workspace: AnythingLLMWorkspace,
    *,
    owned: OwnedResources,
    baseline: ResourceSnapshot,
) -> str:
    baseline_slugs = {item[0].casefold() for item in baseline.workspaces}
    slug = str(workspace.slug or "").strip()
    if (
        not slug
        or slug.casefold() in baseline_slugs
        or owned.execution_token not in owned.workspace_name
    ):
        raise IsolatedCalibrationError("新 workspace 身份无法与既有资源安全区分")
    return slug


def _validate_owned_document(
    document: AnythingLLMDocument,
    *,
    owned: OwnedResources,
    baseline: ResourceSnapshot,
) -> str:
    location = normalize_document_path(document.location)
    file_name = location.rsplit("/", 1)[-1].casefold()
    if not location.startswith("custom-documents/"):
        raise IsolatedCalibrationError("上传文档位置不属于 custom-documents")
    baseline_locations = {item[1].casefold() for item in baseline.documents}
    if location.casefold() in baseline_locations:
        # 即使上传端点返回了该位置，也不得把执行前已绑定的文档登记为本次删除对象。
        raise IsolatedCalibrationError("上传响应复用了既有绑定文档位置，禁止取得删除权")
    # 上传响应本身已经能够证明 location 来自本次调用。先登记该确切位置，确保后续附加
    # 的命名约束失败时 finally 仍可补偿，不会因为防御性校验反而遗留临时文档。
    owned.document_location = location
    if owned.execution_token.casefold() not in file_name:
        raise IsolatedCalibrationError("上传文档位置不含本次随机标识，禁止取得删除权")
    return location


def _recover_workspace_slug(
    workspace_client: Any,
    *,
    owned: OwnedResources,
    baseline: ResourceSnapshot,
    user_id: int,
) -> None:
    """创建响应不确定时，只按本次随机名称恢复唯一临时 workspace 的删除权。"""

    if owned.workspace_slug:
        return
    baseline_slugs = {item[0].casefold() for item in baseline.workspaces}
    matches = [
        workspace
        for workspace in workspace_client.list_workspaces(user_id=user_id)
        if workspace.name == owned.workspace_name
        and workspace.slug.casefold() not in baseline_slugs
    ]
    if len(matches) > 1:
        raise IsolatedCalibrationError("发现多个同名临时 workspace，无法证明唯一所有权")
    if matches:
        owned.workspace_slug = matches[0].slug


def _document_storage_path(storage_root: Path, location: str) -> Path:
    documents_root = (storage_root / "documents").resolve()
    candidate = (documents_root / Path(*location.split("/"))).resolve()
    if not _is_relative_to(candidate, documents_root):
        raise IsolatedCalibrationError("临时文档位置越过 AnythingLLM documents 目录")
    return candidate


def _recover_document_location(
    *,
    owned: OwnedResources,
    storage_root: Path,
) -> None:
    """上传响应不确定时，从本地存储文件名中的随机标识恢复唯一文档位置。"""

    if owned.document_location:
        return
    documents_root = (storage_root / "documents").resolve()
    custom_root = documents_root / "custom-documents"
    matches = [
        path
        for path in custom_root.glob("*")
        if path.is_file()
        and owned.execution_token.casefold() in path.name.casefold()
    ]
    if len(matches) > 1:
        raise IsolatedCalibrationError("发现多个带本次随机标识的文档，无法证明唯一所有权")
    if matches:
        owned.document_location = matches[0].relative_to(documents_root).as_posix()


def _assert_existing_resources_unchanged(
    workspace_client: Any,
    *,
    baseline: ResourceSnapshot,
    owned_workspace_slug: str,
    user_id: int,
) -> None:
    current = snapshot_resources(
        workspace_client,
        user_id=user_id,
        excluded_workspace_slugs=(owned_workspace_slug,),
    )
    if current != baseline:
        raise IsolatedCalibrationError("隔离执行期间既有 workspace 或文档快照发生变化")


def _assert_temporary_binding(
    workspace_client: Any,
    *,
    workspace_slug: str,
    document_location: str,
    user_id: int,
) -> None:
    documents = workspace_client.list_documents(workspace_slug, user_id=user_id)
    locations = {normalize_document_path(document.location) for document in documents}
    if locations != {document_location}:
        raise IsolatedCalibrationError("临时 workspace 未保持单文档隔离")


def _wait_until_searchable(
    workspace_client: Any,
    *,
    workspace_slug: str,
    query: CalibrationQuery,
    user_id: int,
    timeout_seconds: float,
    poll_interval_seconds: float,
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
) -> None:
    deadline = monotonic() + timeout_seconds
    while True:
        results = workspace_client.vector_search(
            workspace_slug,
            query.text,
            user_id=user_id,
            top_n=1,
            score_threshold=0.0,
        )
        if results:
            return
        if monotonic() >= deadline:
            raise IsolatedCalibrationError("临时文档在限定时间内未产生可检索向量")
        sleep(min(poll_interval_seconds, max(0.0, deadline - monotonic())))


def _cleanup_owned_resources(
    workspace_client: Any,
    document_client: Any,
    *,
    owned: OwnedResources,
    storage_root: Path,
    user_id: int,
) -> tuple[bool, bool]:
    """尽力清理两个本次资源，并通过重新读取确认，而不是只相信 DELETE 返回。"""

    if owned.workspace_slug:
        try:
            workspace_client.delete_workspace(owned.workspace_slug, user_id=user_id)
        except Exception:
            logger.warning("临时 workspace 首次删除未确认，准备读取状态后补偿")

    if owned.document_location:
        try:
            document_client.delete_document(owned.document_location, user_id=user_id)
        except Exception:
            logger.warning("临时全局文档首次删除未确认，准备检查本地存储后补偿")

    current_workspaces = workspace_client.list_workspaces(user_id=user_id)
    workspace_exists = bool(
        owned.workspace_slug
        and any(
            item.slug.casefold() == owned.workspace_slug.casefold()
            for item in current_workspaces
        )
    )
    if workspace_exists:
        # 仅当只读确认本次随机 workspace 仍存在时执行一次有界补偿。
        workspace_client.delete_workspace(owned.workspace_slug, user_id=user_id)
        current_workspaces = workspace_client.list_workspaces(user_id=user_id)
        workspace_exists = any(
            item.slug.casefold() == owned.workspace_slug.casefold()
            for item in current_workspaces
        )

    document_exists = False
    if owned.document_location:
        document_path = _document_storage_path(storage_root, owned.document_location)
        document_exists = document_path.exists()
        if document_exists:
            # 同样只补偿删除带本次 execution token 的确切 location。
            document_client.delete_document(owned.document_location, user_id=user_id)
            document_exists = document_path.exists()

    return not workspace_exists, not document_exists


def run_isolated_calibration(
    workspace_client: Any,
    document_client: Any,
    *,
    source_json: Path,
    storage_root: Path,
    queries: Sequence[CalibrationQuery],
    top_n: int,
    user_id: int,
    readiness_timeout_seconds: float,
    poll_interval_seconds: float,
    execution_token: str | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """执行隔离上传、嵌入、检索和补偿，并只返回脱敏指标。"""

    if not queries:
        raise ValueError("queries 不能为空")
    token = execution_token or uuid.uuid4().hex
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise ValueError("execution_token 必须是 32 位小写十六进制字符串")

    storage_root = _required_directory(storage_root, name="storage-root")
    page_content, source_hash = _load_processed_page_content(
        source_json,
        storage_root=storage_root,
    )
    cleaned = build_cleaned_retrieval_copy(page_content, source_hash=source_hash)
    baseline = snapshot_resources(workspace_client, user_id=user_id)
    owned = OwnedResources(
        execution_token=token,
        workspace_name=f"{_WORKSPACE_NAME_PREFIX}{token}",
    )

    operation_error: BaseException | None = None
    query_results: list[dict[str, Any]] = []
    digest_frequency: Counter[str] = Counter()
    workspace_deleted = False
    document_deleted = False

    with tempfile.TemporaryDirectory(prefix=f"docsense-stage1d0r-{token}-") as temp_dir:
        copy_path = Path(temp_dir) / f"docsense-stage1d0r-{token}.retrieval.md"
        copy_path.write_text(cleaned.text + "\n", encoding="utf-8")
        try:
            workspace = workspace_client.create_workspace(
                owned.workspace_name,
                settings=document_rag_workspace_settings(),
                user_id=user_id,
            )
            owned.workspace_slug = _validate_owned_workspace(
                workspace,
                owned=owned,
                baseline=baseline,
            )
            document = document_client.upload_document(
                str(copy_path),
                user_id=user_id,
                metadata={
                    "calibrationExecution": token,
                    "sourceKind": "stage1d0r-isolated-cleaned-copy",
                },
            )
            owned.document_location = _validate_owned_document(
                document,
                owned=owned,
                baseline=baseline,
            )
            workspace_client.update_embeddings(
                owned.workspace_slug,
                adds=[owned.document_location],
                user_id=user_id,
            )
            _assert_existing_resources_unchanged(
                workspace_client,
                baseline=baseline,
                owned_workspace_slug=owned.workspace_slug,
                user_id=user_id,
            )
            _assert_temporary_binding(
                workspace_client,
                workspace_slug=owned.workspace_slug,
                document_location=owned.document_location,
                user_id=user_id,
            )
            _wait_until_searchable(
                workspace_client,
                workspace_slug=owned.workspace_slug,
                query=queries[0],
                user_id=user_id,
                timeout_seconds=readiness_timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
                monotonic=monotonic,
                sleep=sleep,
            )
            for query in queries:
                result, digests = _query_result(
                    workspace_client,
                    workspace_slug=owned.workspace_slug,
                    query=query,
                    top_n=top_n,
                    user_id=user_id,
                )
                query_results.append(result)
                digest_frequency.update(set(digests))
        except BaseException as exc:  # 清理完成后再按原类型重新抛出。
            operation_error = exc
        finally:
            try:
                _recover_workspace_slug(
                    workspace_client,
                    owned=owned,
                    baseline=baseline,
                    user_id=user_id,
                )
                _recover_document_location(owned=owned, storage_root=storage_root)
                workspace_deleted, document_deleted = _cleanup_owned_resources(
                    workspace_client,
                    document_client,
                    owned=owned,
                    storage_root=storage_root,
                    user_id=user_id,
                )
            except BaseException as cleanup_exc:
                raise IsolatedCalibrationError(
                    "隔离校准资源清理或清理验证失败，必须人工检查本次随机资源"
                ) from cleanup_exc

    final_snapshot = snapshot_resources(workspace_client, user_id=user_id)
    baseline_restored = final_snapshot == baseline
    if not workspace_deleted or not document_deleted or not baseline_restored:
        raise IsolatedCalibrationError("隔离校准结束后未恢复执行前资源快照")
    if operation_error is not None:
        raise operation_error

    output = {
        "schemaVersion": 1,
        "operation": "isolated-temporary-cleaned-copy-reindex-and-vector-search",
        "remoteMutationPerformed": True,
        "existingResourcesModified": False,
        "sourceStrategy": "processed-page-content-reference-tail-v1",
        "equivalentToMhtmlMainContentV1": False,
        "limitation": "original-mhtml-is-no-longer-available",
        "executionHash": _digest(token),
        "workspaceHash": _digest(owned.workspace_slug or ""),
        "documentLocationHash": _digest(owned.document_location or ""),
        "baseline": {
            "snapshotHash": baseline.digest,
            "workspaceCount": len(baseline.workspaces),
            "boundDocumentCount": len(baseline.documents),
        },
        "cleanedCopy": {
            "sourceFileHash": cleaned.source_hash,
            "contentHash": cleaned.output_hash,
            "inputChars": cleaned.input_chars,
            "inputLines": cleaned.input_lines,
            "outputChars": cleaned.output_chars,
            "outputLines": cleaned.output_lines,
            "removedCharacterRatio": round(
                1.0 - (cleaned.output_chars / cleaned.input_chars),
                6,
            ),
            "referenceStartLine": cleaned.reference_start_line,
            "removedCitationLines": cleaned.removed_citation_lines,
            "removedHeaderLines": cleaned.removed_header_lines,
            "recoveredPageSuffixLines": cleaned.recovered_page_suffix_lines,
        },
        "scoreThreshold": 0.0,
        "topN": top_n,
        "queryCount": len(query_results),
        "queryResults": query_results,
        "crossQueryRepeatedCandidateCount": sum(
            count >= 2 for count in digest_frequency.values()
        ),
        "cleanup": {
            "workspaceDeletionVerified": workspace_deleted,
            "globalDocumentDeletionVerified": document_deleted,
            "baselineSnapshotRestored": baseline_restored,
            "temporaryLocalCopyRemoved": True,
        },
    }
    logger.info(
        "隔离检索校准完成并已恢复基线: execution_hash=%s query_count=%d "
        "baseline_workspace_count=%d baseline_document_count=%d",
        output["executionHash"],
        len(query_results),
        len(baseline.workspaces),
        len(baseline.documents),
    )
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="创建并清理隔离 AnythingLLM 资源，校准去噪副本的真实检索质量",
    )
    parser.add_argument("--source-json", type=Path, required=True)
    parser.add_argument("--storage-root", type=Path, required=True)
    parser.add_argument("--query-file", type=Path, required=True)
    parser.add_argument("--result-file", type=Path)
    parser.add_argument("--top-n", type=int, default=100)
    parser.add_argument("--user-id", type=int, default=1)
    parser.add_argument("--readiness-timeout-seconds", type=float, default=90.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=1.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.top_n < 1 or args.top_n > 1000:
        raise ValueError("top-n 必须是 1~1000 的整数")
    if args.user_id < 1:
        raise ValueError("user-id 必须是正整数")
    if args.readiness_timeout_seconds <= 0 or args.readiness_timeout_seconds > 600:
        raise ValueError("readiness-timeout-seconds 必须大于 0 且不超过 600")
    if args.poll_interval_seconds <= 0 or args.poll_interval_seconds > 10:
        raise ValueError("poll-interval-seconds 必须大于 0 且不超过 10")

    queries = _load_queries(args.query_file.resolve())
    config = load_anythingllm_config()
    with AnythingLLMWeaponryClientFactory(config).create() as clients:
        output = run_isolated_calibration(
            clients.workspaces,
            clients.documents,
            source_json=args.source_json,
            storage_root=args.storage_root,
            queries=queries,
            top_n=args.top_n,
            user_id=args.user_id,
            readiness_timeout_seconds=float(args.readiness_timeout_seconds),
            poll_interval_seconds=float(args.poll_interval_seconds),
        )

    rendered = json.dumps(
        output,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    )
    if args.result_file is None:
        sys.stdout.write(rendered + "\n")
    else:
        result_path = args.result_file.expanduser().resolve()
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(rendered + "\n", encoding="utf-8")
        logger.info("隔离校准脱敏结果已写入: result_file_name=%s", result_path.name)
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        raise SystemExit(main())
    except Exception as exc:
        logger.error("隔离检索校准失败: error_type=%s", type(exc).__name__)
        raise SystemExit(1) from None
