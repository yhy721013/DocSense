"""武器谱术语目录的确定性清单、版本路由与启动期同步协调器。

本模块属于 Weaponry Adapter 层：它理解“术语卡、目录版本、只读共享工作区”等业务
语义，但复用 ``app.integrations.anythingllm`` 提供的原子 HTTP Client，不复制供应商
传输协议。所有远端写入只允许由启动协调器发起，字段执行路径只做按版本检索。
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import logging
from pathlib import Path
import re
import sqlite3
import threading
import tempfile
from typing import Final

from .anythingllm_clients import WeaponryAnythingLLMClientFactoryProtocol


logger = logging.getLogger(__name__)

TERMS_CATALOG_FINGERPRINT_SCHEMA: Final = "terms-manifest-v1"
_FINGERPRINT_PREFIX: Final = f"{TERMS_CATALOG_FINGERPRINT_SCHEMA}:sha256:"
_CARD_FILE_PATTERN = re.compile(r"^term_rule_.+\.md$", re.IGNORECASE)
_CARD_ID_PATTERN = re.compile(
    r"""(?m)^card_id:\s*(?P<quote>["']?)(?P<value>[^"'#\r\n]+)(?P=quote)\s*$"""
)
_SAFE_WORKSPACE_BASE = re.compile(r"[^a-z0-9-]+")


class TermsCatalogError(RuntimeError):
    """术语目录无法被安全准备时的稳定异常基类。"""


class TermsCatalogValidationError(TermsCatalogError):
    """本地术语文件不满足确定性清单约束。"""


class TermsCatalogSynchronizationError(TermsCatalogError):
    """远端目录同步或验证没有得到可证明的完成结果。"""


@dataclass(frozen=True)
class TermsCatalogCard:
    """一张经 UTF-8 严格读取并绑定内容摘要的本地术语卡。"""

    card_id: str
    relative_path: str
    source_path: Path
    content_sha256: str
    content: bytes

    @property
    def upload_file_name(self) -> str:
        """返回可由远端文档列表稳定识别的确定性上传文件名。"""

        return (
            f"{Path(self.relative_path).stem}--{self.content_sha256[:16]}.md"
        )


@dataclass(frozen=True)
class TermsCatalogManifest:
    """一次启动冻结的本地术语目录事实。"""

    root: Path
    cards: tuple[TermsCatalogCard, ...]
    content_sha256: str
    fingerprint: str

    def workspace_name(self, base_name: str) -> str:
        """按完整内容摘要派生版本化工作区名称，禁止时间戳和人工版本标签。"""

        normalized_base = normalize_terms_workspace_base(base_name)
        return f"{normalized_base}-{self.content_sha256[:16]}"


@dataclass(frozen=True)
class TermsCatalogDescriptor:
    """可安全发布给任务提交和检索路径的不可变目录描述符。"""

    fingerprint: str
    workspace_slug: str
    workspace_name: str
    card_count: int


@dataclass(frozen=True)
class TermsCatalogSyncPlan:
    """只读预检结果；供启动诊断与 ``--dry-run`` 复用。"""

    fingerprint: str
    workspace_name: str
    workspace_slug: str
    workspace_exists: bool
    expected_card_count: int
    missing_card_ids: tuple[str, ...]
    unexpected_document_titles: tuple[str, ...]
    blocked_outcome_unknown: bool

    @property
    def write_required(self) -> bool:
        return (not self.workspace_exists) or bool(self.missing_card_ids)


def normalize_terms_workspace_base(value: str) -> str:
    """把配置中的逻辑名称规范化为稳定、可派生的工作区前缀。"""

    if not isinstance(value, str) or not value.strip():
        raise TermsCatalogValidationError("术语 workspace 基础名称不能为空")
    normalized = _SAFE_WORKSPACE_BASE.sub(
        "-",
        value.strip().casefold().replace("_", "-"),
    ).strip("-")
    if not normalized:
        raise TermsCatalogValidationError("术语 workspace 基础名称无法规范化")
    return normalized


def fingerprint_digest(fingerprint: str) -> str:
    """校验自动指纹格式并返回其中的完整 SHA-256 摘要。"""

    if not isinstance(fingerprint, str) or not fingerprint.startswith(
        _FINGERPRINT_PREFIX
    ):
        raise TermsCatalogValidationError("术语目录指纹格式不受支持")
    digest = fingerprint[len(_FINGERPRINT_PREFIX) :]
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise TermsCatalogValidationError("术语目录指纹缺少有效 SHA-256 摘要")
    return digest


def workspace_name_for_fingerprint(base_name: str, fingerprint: str) -> str:
    """从任务快照中的自动指纹重新得到其版本化工作区名称。"""

    return (
        f"{normalize_terms_workspace_base(base_name)}-"
        f"{fingerprint_digest(fingerprint)[:16]}"
    )


def build_terms_catalog_manifest(terms_dir: str | Path) -> TermsCatalogManifest:
    """严格扫描 ``term_rule_*.md`` 并生成跨机器一致的目录指纹。

    摘要输入只包含清单协议版本、排序后的相对路径、卡片 ID 和原始 UTF-8 字节；绝不
    包含绝对路径、文件时间或启动时间。符号链接会被拒绝，防止目录外文件借链接混入。
    """

    root = Path(terms_dir).expanduser()
    try:
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise TermsCatalogValidationError(f"术语目录不存在或无法解析: {root}") from exc
    if not resolved_root.is_dir():
        raise TermsCatalogValidationError(f"术语目录不是文件夹: {resolved_root}")

    candidates = sorted(
        (
            path
            for path in resolved_root.iterdir()
            if _CARD_FILE_PATTERN.fullmatch(path.name)
        ),
        key=lambda item: item.name.casefold(),
    )
    if not candidates:
        raise TermsCatalogValidationError("术语目录中没有 term_rule_*.md 文件")

    cards: list[TermsCatalogCard] = []
    seen_card_ids: set[str] = set()
    for path in candidates:
        if path.is_symlink():
            raise TermsCatalogValidationError(f"术语卡不得是符号链接: {path.name}")
        try:
            resolved_path = path.resolve(strict=True)
            resolved_path.relative_to(resolved_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise TermsCatalogValidationError(
                f"术语卡超出配置目录边界: {path.name}"
            ) from exc
        if not resolved_path.is_file():
            raise TermsCatalogValidationError(f"术语卡不是普通文件: {path.name}")
        try:
            content = resolved_path.read_bytes()
            text = content.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise TermsCatalogValidationError(
                f"术语卡必须是可读取的 UTF-8 文件: {path.name}"
            ) from exc
        if not text.strip():
            raise TermsCatalogValidationError(f"术语卡内容不能为空: {path.name}")
        card_match = _CARD_ID_PATTERN.search(text)
        if card_match is None:
            raise TermsCatalogValidationError(f"术语卡缺少 card_id: {path.name}")
        card_id = card_match.group("value").strip()
        if not card_id:
            raise TermsCatalogValidationError(f"术语卡 card_id 不能为空: {path.name}")
        card_key = card_id.casefold()
        if card_key in seen_card_ids:
            raise TermsCatalogValidationError(f"术语卡 card_id 重复: {card_id}")
        seen_card_ids.add(card_key)
        cards.append(
            TermsCatalogCard(
                card_id=card_id,
                relative_path=resolved_path.relative_to(resolved_root).as_posix(),
                source_path=resolved_path,
                content_sha256=hashlib.sha256(content).hexdigest(),
                content=content,
            )
        )

    digest = hashlib.sha256()
    digest.update(f"{TERMS_CATALOG_FINGERPRINT_SCHEMA}\n".encode("ascii"))
    for card in cards:
        # 长度前缀消除字段拼接歧义；路径统一为 POSIX 形式，保证 Windows/Linux 一致。
        for value in (
            card.relative_path.encode("utf-8"),
            card.card_id.encode("utf-8"),
            card.content,
        ):
            digest.update(len(value).to_bytes(8, "big"))
            digest.update(value)
    content_sha256 = digest.hexdigest()
    fingerprint = f"{_FINGERPRINT_PREFIX}{content_sha256}"
    logger.info(
        "武器谱本地术语清单已冻结: card_count=%d fingerprint=%s",
        len(cards),
        fingerprint,
    )
    return TermsCatalogManifest(
        root=resolved_root,
        cards=tuple(cards),
        content_sha256=content_sha256,
        fingerprint=fingerprint,
    )


class SQLiteTermsCatalogStateStore:
    """保存非幂等远端写入的最小恢复事实，不跨网络调用持有事务。"""

    def __init__(
        self,
        database_path: str | Path,
        *,
        read_only: bool = False,
    ) -> None:
        self._database_path = str(database_path)
        self._read_only = bool(read_only)
        if not self._read_only:
            self._initialize()

    def _connect(self) -> sqlite3.Connection:
        database = self._database_path
        uri = False
        if self._read_only:
            database = f"file:{Path(database).resolve().as_posix()}?mode=ro"
            uri = True
        connection = sqlite3.connect(database, timeout=30.0, uri=uri)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS weaponry_terms_catalog_state (
                    fingerprint TEXT PRIMARY KEY,
                    workspace_name TEXT NOT NULL,
                    workspace_slug TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL,
                    error_code TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS weaponry_terms_card_state (
                    fingerprint TEXT NOT NULL,
                    card_id TEXT NOT NULL,
                    upload_file_name TEXT NOT NULL,
                    document_location TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL,
                    error_code TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (fingerprint, card_id)
                )
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def catalog_state(self, fingerprint: str) -> sqlite3.Row | None:
        try:
            with closing(self._connect()) as connection, connection:
                return connection.execute(
                    """
                    SELECT fingerprint, workspace_name, workspace_slug,
                           state, error_code
                    FROM weaponry_terms_catalog_state
                    WHERE fingerprint = ?
                    """,
                    (fingerprint,),
                ).fetchone()
        except sqlite3.OperationalError:
            if self._read_only:
                return None
            raise

    def put_catalog(
        self,
        *,
        fingerprint: str,
        workspace_name: str,
        workspace_slug: str,
        state: str,
        error_code: str = "",
    ) -> None:
        if self._read_only:
            raise RuntimeError("只读术语目录状态库不得写入")
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO weaponry_terms_catalog_state (
                    fingerprint, workspace_name, workspace_slug,
                    state, error_code, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    workspace_name = excluded.workspace_name,
                    workspace_slug = excluded.workspace_slug,
                    state = excluded.state,
                    error_code = excluded.error_code,
                    updated_at = excluded.updated_at
                """,
                (
                    fingerprint,
                    workspace_name,
                    workspace_slug,
                    state,
                    error_code,
                    self._now(),
                ),
            )

    def card_state(self, fingerprint: str, card_id: str) -> sqlite3.Row | None:
        try:
            with closing(self._connect()) as connection, connection:
                return connection.execute(
                    """
                    SELECT fingerprint, card_id, upload_file_name,
                           document_location, state, error_code
                    FROM weaponry_terms_card_state
                    WHERE fingerprint = ? AND card_id = ?
                    """,
                    (fingerprint, card_id),
                ).fetchone()
        except sqlite3.OperationalError:
            if self._read_only:
                return None
            raise

    def put_card(
        self,
        *,
        fingerprint: str,
        card: TermsCatalogCard,
        document_location: str,
        state: str,
        error_code: str = "",
    ) -> None:
        if self._read_only:
            raise RuntimeError("只读术语目录状态库不得写入")
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """
                INSERT INTO weaponry_terms_card_state (
                    fingerprint, card_id, upload_file_name, document_location,
                    state, error_code, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint, card_id) DO UPDATE SET
                    upload_file_name = excluded.upload_file_name,
                    document_location = excluded.document_location,
                    state = excluded.state,
                    error_code = excluded.error_code,
                    updated_at = excluded.updated_at
                """,
                (
                    fingerprint,
                    card.card_id,
                    card.upload_file_name,
                    document_location,
                    state,
                    error_code,
                    self._now(),
                ),
            )


class TermsCatalogWorkspaceResolver:
    """按任务快照指纹解析只读工作区，并为历史任务保留多代路由。"""

    def __init__(
        self,
        client_factory: WeaponryAnythingLLMClientFactoryProtocol,
        *,
        workspace_base_name: str,
        user_id: int | None = 1,
    ) -> None:
        if not isinstance(client_factory, WeaponryAnythingLLMClientFactoryProtocol):
            raise TypeError("client_factory 必须实现武器谱 AnythingLLM Client 工厂")
        normalized_base = normalize_terms_workspace_base(workspace_base_name)
        if user_id is not None and (
            isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1
        ):
            raise ValueError("user_id 必须是正整数或 None")
        self._client_factory = client_factory
        self._legacy_workspace_identity = workspace_base_name.strip()
        self._workspace_base_name = normalized_base
        self._user_id = user_id
        self._cache: dict[str, str] = {}
        self._lock = threading.RLock()

    def publish(self, descriptor: TermsCatalogDescriptor) -> None:
        """仅在协调器完整验证后发布指纹到 slug 的不可变映射。"""

        with self._lock:
            existing = self._cache.get(descriptor.fingerprint)
            if existing is not None and existing != descriptor.workspace_slug:
                raise TermsCatalogSynchronizationError(
                    "同一术语指纹解析到了不同 workspace slug"
                )
            self._cache[descriptor.fingerprint] = descriptor.workspace_slug

    def resolve(self, fingerprint: str) -> str:
        """解析当前或历史目录；缺失、重名均 fail-closed，绝不创建资源。"""

        with self._lock:
            cached = self._cache.get(fingerprint)
        if cached is not None:
            return cached
        try:
            target_name = workspace_name_for_fingerprint(
                self._workspace_base_name,
                fingerprint,
            )
            legacy_snapshot = False
        except TermsCatalogValidationError:
            # 1D 早期 execution 保存的是人工目录标签，且 Provider 直接把配置值作为
            # workspace slug。升级后仍允许这些只读历史任务访问原 workspace，但新任务
            # 只会生成自动内容指纹，绝不会继续写入该非版本化目录。
            target_name = self._legacy_workspace_identity
            legacy_snapshot = True
        with self._client_factory.create() as clients:
            matches = [
                workspace
                for workspace in clients.workspaces.list_workspaces(
                    user_id=self._user_id
                )
                if (
                    workspace.name.strip().casefold() == target_name.casefold()
                    or (
                        legacy_snapshot
                        and workspace.slug.strip().casefold()
                        == target_name.casefold()
                    )
                )
            ]
        if len(matches) != 1:
            raise TermsCatalogSynchronizationError(
                "术语目录对应的历史 workspace 缺失或名称不唯一"
            )
        descriptor = TermsCatalogDescriptor(
            fingerprint=fingerprint,
            workspace_slug=matches[0].slug,
            workspace_name=matches[0].name,
            card_count=0,
        )
        self.publish(descriptor)
        return descriptor.workspace_slug


class AnythingLLMTermsCatalogCoordinator:
    """在 Weaponry Worker 启动前幂等准备一个完整、只读、版本化术语目录。"""

    def __init__(
        self,
        client_factory: WeaponryAnythingLLMClientFactoryProtocol,
        *,
        manifest: TermsCatalogManifest,
        workspace_base_name: str,
        state_store: SQLiteTermsCatalogStateStore,
        resolver: TermsCatalogWorkspaceResolver,
        user_id: int | None = 1,
    ) -> None:
        self._client_factory = client_factory
        self._manifest = manifest
        self._workspace_name = manifest.workspace_name(workspace_base_name)
        self._state_store = state_store
        self._resolver = resolver
        self._user_id = user_id
        self._lock = threading.RLock()
        self._prepared: TermsCatalogDescriptor | None = None

    @property
    def manifest(self) -> TermsCatalogManifest:
        return self._manifest

    def prepare(self) -> TermsCatalogDescriptor:
        """执行可重复验证的同步；同一进程并发调用只允许一条实际准备链。"""

        with self._lock:
            if self._prepared is not None:
                return self._prepared
            descriptor = self._prepare_once()
            self._resolver.publish(descriptor)
            self._prepared = descriptor
            return descriptor

    def inspect(self) -> TermsCatalogSyncPlan:
        """只读取本地冻结清单、状态库和 AnythingLLM，绝不创建或上传。"""

        with self._client_factory.create() as clients:
            workspaces = clients.workspaces.list_workspaces(user_id=self._user_id)
            matches = [
                workspace
                for workspace in workspaces
                if workspace.name.strip().casefold() == self._workspace_name.casefold()
            ]
            if len(matches) > 1:
                raise TermsCatalogSynchronizationError(
                    "版本化术语 workspace 名称不唯一"
                )
            workspace = matches[0] if matches else None
            documents = (
                clients.workspaces.list_documents(
                    workspace.slug,
                    user_id=self._user_id,
                )
                if workspace is not None
                else ()
            )
        actual_titles = {document.title.casefold() for document in documents}
        expected_by_title = {
            card.upload_file_name.casefold(): card for card in self._manifest.cards
        }
        prior_catalog = self._state_store.catalog_state(self._manifest.fingerprint)
        blocked = bool(
            prior_catalog is not None
            and prior_catalog["state"] == "outcome_unknown"
            and workspace is None
        )
        for card in self._manifest.cards:
            prior_card = self._state_store.card_state(
                self._manifest.fingerprint,
                card.card_id,
            )
            blocked = blocked or bool(
                prior_card is not None
                and prior_card["state"] == "outcome_unknown"
                and card.upload_file_name.casefold() not in actual_titles
            )
        return TermsCatalogSyncPlan(
            fingerprint=self._manifest.fingerprint,
            workspace_name=self._workspace_name,
            workspace_slug=workspace.slug if workspace is not None else "",
            workspace_exists=workspace is not None,
            expected_card_count=len(self._manifest.cards),
            missing_card_ids=tuple(
                card.card_id
                for title, card in expected_by_title.items()
                if title not in actual_titles
            ),
            unexpected_document_titles=tuple(
                sorted(actual_titles - set(expected_by_title))
            ),
            blocked_outcome_unknown=blocked,
        )

    def _prepare_once(self) -> TermsCatalogDescriptor:
        logger.info(
            "开始准备武器谱术语目录: fingerprint=%s workspace_name=%s card_count=%d",
            self._manifest.fingerprint,
            self._workspace_name,
            len(self._manifest.cards),
        )
        with self._client_factory.create() as clients:
            workspaces = clients.workspaces.list_workspaces(user_id=self._user_id)
            matches = [
                workspace
                for workspace in workspaces
                if workspace.name.strip().casefold() == self._workspace_name.casefold()
            ]
            if len(matches) > 1:
                raise TermsCatalogSynchronizationError(
                    "版本化术语 workspace 名称不唯一"
                )
            if matches:
                workspace = matches[0]
            else:
                prior = self._state_store.catalog_state(
                    self._manifest.fingerprint
                )
                if prior is not None and prior["state"] == "outcome_unknown":
                    raise TermsCatalogSynchronizationError(
                        "上次创建术语 workspace 的结果未知，查回仍未发现目标"
                    )
                self._state_store.put_catalog(
                    fingerprint=self._manifest.fingerprint,
                    workspace_name=self._workspace_name,
                    workspace_slug="",
                    state="creating",
                )
                try:
                    workspace = clients.workspaces.create_workspace(
                        self._workspace_name,
                        user_id=self._user_id,
                    )
                except Exception as exc:
                    # 创建响应未知时只做一次精确查回；未查到即隔离，禁止盲目重放。
                    try:
                        recovered = self._find_workspace_after_create_error(
                            clients
                        )
                    except Exception:
                        recovered = None
                        logger.exception(
                            "创建术语 workspace 异常后的精确查回失败: "
                            "fingerprint=%s workspace_name=%s",
                            self._manifest.fingerprint,
                            self._workspace_name,
                        )
                    if recovered is None:
                        self._state_store.put_catalog(
                            fingerprint=self._manifest.fingerprint,
                            workspace_name=self._workspace_name,
                            workspace_slug="",
                            state="outcome_unknown",
                            error_code=getattr(exc, "code", type(exc).__name__),
                        )
                        raise TermsCatalogSynchronizationError(
                            "创建术语 workspace 的结果未知，已禁止自动重试"
                        ) from exc
                    workspace = recovered

            self._state_store.put_catalog(
                fingerprint=self._manifest.fingerprint,
                workspace_name=self._workspace_name,
                workspace_slug=workspace.slug,
                state="preparing",
            )
            self._synchronize_cards(clients, workspace.slug)
            self._verify_complete(clients, workspace.slug)

        descriptor = TermsCatalogDescriptor(
            fingerprint=self._manifest.fingerprint,
            workspace_slug=workspace.slug,
            workspace_name=self._workspace_name,
            card_count=len(self._manifest.cards),
        )
        self._state_store.put_catalog(
            fingerprint=descriptor.fingerprint,
            workspace_name=descriptor.workspace_name,
            workspace_slug=descriptor.workspace_slug,
            state="ready",
        )
        logger.info(
            "武器谱术语目录准备完成: fingerprint=%s workspace_slug=%s card_count=%d",
            descriptor.fingerprint,
            descriptor.workspace_slug,
            descriptor.card_count,
        )
        return descriptor

    def _find_workspace_after_create_error(self, clients):
        workspaces = clients.workspaces.list_workspaces(user_id=self._user_id)
        matches = [
            workspace
            for workspace in workspaces
            if workspace.name.strip().casefold() == self._workspace_name.casefold()
        ]
        if len(matches) > 1:
            raise TermsCatalogSynchronizationError(
                "创建异常后发现多个同名术语 workspace"
            )
        return matches[0] if matches else None

    def _synchronize_cards(self, clients, workspace_slug: str) -> None:
        documents = clients.workspaces.list_documents(
            workspace_slug,
            user_id=self._user_id,
        )
        remote_by_title: dict[str, list] = {}
        for document in documents:
            remote_by_title.setdefault(document.title.casefold(), []).append(document)
        expected_titles = {card.upload_file_name.casefold() for card in self._manifest.cards}
        unexpected = sorted(set(remote_by_title) - expected_titles)
        if unexpected:
            raise TermsCatalogSynchronizationError(
                "版本化术语 workspace 含有非本目录托管的文档"
            )

        for card in self._manifest.cards:
            matched = remote_by_title.get(card.upload_file_name.casefold(), [])
            if len(matched) > 1:
                raise TermsCatalogSynchronizationError(
                    f"术语 workspace 存在重复文档: {card.card_id}"
                )
            if matched:
                self._state_store.put_card(
                    fingerprint=self._manifest.fingerprint,
                    card=card,
                    document_location=matched[0].location,
                    state="bound",
                )
                continue
            self._upload_and_bind_card(clients, workspace_slug, card)

    def _upload_and_bind_card(self, clients, workspace_slug: str, card) -> None:
        previous = self._state_store.card_state(
            self._manifest.fingerprint,
            card.card_id,
        )
        if previous is not None and previous["state"] == "outcome_unknown":
            raise TermsCatalogSynchronizationError(
                f"术语卡上次上传或绑定结果未知: {card.card_id}"
            )

        # 上传标题需要携带内容摘要，故在系统临时目录创建内容完全相同的快照。源术语
        # 目录可能以只读卷挂载，绝不能在其中创建派生文件；TemporaryDirectory 会在
        # 正常和异常路径统一清理，不把部署绝对路径纳入任何目录身份。
        with tempfile.TemporaryDirectory(prefix="docsense-terms-") as temp_dir:
            snapshot_path = Path(temp_dir) / card.upload_file_name
            snapshot_path.write_bytes(card.content)
            self._state_store.put_card(
                fingerprint=self._manifest.fingerprint,
                card=card,
                document_location="",
                state="uploading",
            )
            try:
                document = clients.documents.upload_document(
                    str(snapshot_path),
                    user_id=self._user_id,
                    metadata={
                        "docsenseKind": "weaponry_terms_rule",
                        "cardId": card.card_id,
                        "contentSha256": card.content_sha256,
                        "catalogFingerprint": self._manifest.fingerprint,
                    },
                )
            except Exception as exc:
                self._state_store.put_card(
                    fingerprint=self._manifest.fingerprint,
                    card=card,
                    document_location="",
                    state="outcome_unknown",
                    error_code=getattr(exc, "code", type(exc).__name__),
                )
                raise TermsCatalogSynchronizationError(
                    f"术语卡上传结果未知，已禁止自动重试: {card.card_id}"
                ) from exc

        self._state_store.put_card(
            fingerprint=self._manifest.fingerprint,
            card=card,
            document_location=document.location,
            state="uploaded",
        )
        try:
            clients.workspaces.update_embeddings(
                workspace_slug,
                adds=[document.location],
                user_id=self._user_id,
            )
            verified = clients.workspaces.find_document(
                workspace_slug,
                document.location,
                user_id=self._user_id,
            )
        except Exception as exc:
            self._state_store.put_card(
                fingerprint=self._manifest.fingerprint,
                card=card,
                document_location=document.location,
                state="outcome_unknown",
                error_code=getattr(exc, "code", type(exc).__name__),
            )
            raise TermsCatalogSynchronizationError(
                f"术语卡绑定结果未知，已禁止自动重试: {card.card_id}"
            ) from exc
        if verified is None:
            self._state_store.put_card(
                fingerprint=self._manifest.fingerprint,
                card=card,
                document_location=document.location,
                state="outcome_unknown",
                error_code="binding_not_visible",
            )
            raise TermsCatalogSynchronizationError(
                f"术语卡绑定后无法精确查回: {card.card_id}"
            )
        self._state_store.put_card(
            fingerprint=self._manifest.fingerprint,
            card=card,
            document_location=document.location,
            state="bound",
        )

    def _verify_complete(self, clients, workspace_slug: str) -> None:
        documents = clients.workspaces.list_documents(
            workspace_slug,
            user_id=self._user_id,
        )
        actual = [document.title.casefold() for document in documents]
        expected = [card.upload_file_name.casefold() for card in self._manifest.cards]
        if len(actual) != len(set(actual)) or sorted(actual) != sorted(expected):
            raise TermsCatalogSynchronizationError(
                "术语 workspace 完整性校验失败"
            )


__all__ = [
    "AnythingLLMTermsCatalogCoordinator",
    "SQLiteTermsCatalogStateStore",
    "TERMS_CATALOG_FINGERPRINT_SCHEMA",
    "TermsCatalogCard",
    "TermsCatalogDescriptor",
    "TermsCatalogError",
    "TermsCatalogManifest",
    "TermsCatalogSynchronizationError",
    "TermsCatalogSyncPlan",
    "TermsCatalogValidationError",
    "TermsCatalogWorkspaceResolver",
    "build_terms_catalog_manifest",
    "fingerprint_digest",
    "workspace_name_for_fingerprint",
]
