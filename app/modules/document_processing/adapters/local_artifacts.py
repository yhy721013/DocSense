"""阶段 1 的单实例本地 Artifact Store。"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, ContextManager, Iterator
from uuid import uuid4

from app.modules.document_processing.domain import (
    ArtifactConflictError,
    ArtifactIntegrityError,
    ArtifactMetadata,
    ArtifactRef,
    derive_artifact_id,
)
from app.modules.document_processing.ports import (
    ArtifactContent,
    ArtifactPublication,
)
from app.modules.tasks.domain import TaskId


logger = logging.getLogger(__name__)
_COPY_CHUNK_BYTES = 1024 * 1024


@dataclass(slots=True)
class _LockEntry:
    lock: threading.Lock
    users: int = 0


class LocalArtifactStoreAdapter:
    """用任务哈希隔离命名空间，并以同目录临时文件原子发布。

    当前锁只提供单进程串行化，符合阶段 1 的单实例边界；阶段 3 使用 MinIO 条件写替换
    本 Adapter。即使确定性 ID 重复，只有元数据完全一致时才视为幂等命中，内容不同会
    fail closed，绝不覆盖既有 Artifact。
    """

    def __init__(self, root: str | Path) -> None:
        resolved_root = self._canonical_resolved(Path(root).expanduser())
        if resolved_root.exists() and not resolved_root.is_dir():
            raise ValueError("Artifact root 必须是目录")
        self._root = resolved_root
        self._locks_guard = threading.Lock()
        self._artifact_locks: dict[str, _LockEntry] = {}

    @property
    def root(self) -> Path:
        """仅供组合根和离线诊断读取，不属于 Application Port。"""

        return self._root

    @property
    def retained_lock_count(self) -> int:
        """仅供离线诊断确认锁表会在无使用者时回收。"""

        with self._locks_guard:
            return len(self._artifact_locks)

    def publish(
        self,
        publication: ArtifactPublication,
        content: ArtifactContent,
    ) -> ArtifactRef:
        if not isinstance(publication, ArtifactPublication):
            raise TypeError("publication 必须是 ArtifactPublication")
        if not isinstance(content, ArtifactContent):
            raise TypeError("content 必须实现 ArtifactContent")

        artifact_id = derive_artifact_id(
            step_key=publication.step_key,
            kind=publication.kind,
            representation=publication.representation,
            ordinal=publication.ordinal,
        )
        task_root = self._task_root(publication.task_id)
        destination = self._artifact_path(task_root, artifact_id)
        temporary = destination.with_name(
            f".{artifact_id}.{uuid4().hex}.part"
        )
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            # 目录可能在 mkdir 前后被替换为符号链接，因此创建后再次解析并校验。
            task_root = self._task_root(publication.task_id)
            destination = self._artifact_path(task_root, artifact_id)
            temporary = destination.with_name(
                f".{artifact_id}.{uuid4().hex}.part"
            )
            digest = hashlib.sha256()
            size_bytes = 0
            with content.open_reader() as reader, temporary.open("xb") as writer:
                while True:
                    chunk = reader.read(_COPY_CHUNK_BYTES)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise TypeError("ArtifactContent reader 必须返回 bytes")
                    writer.write(chunk)
                    digest.update(chunk)
                    size_bytes += len(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            metadata = ArtifactMetadata(
                media_type=publication.media_type,
                size_bytes=size_bytes,
                sha256=digest.hexdigest(),
            )

            # 同一进程内对同一确定性身份串行检查/替换，避免 os.replace 覆盖冲突内容。
            with self._artifact_lock(artifact_id):
                if destination.exists():
                    actual = self._metadata_for(
                        destination,
                        media_type=publication.media_type,
                    )
                    if actual != metadata:
                        raise ArtifactConflictError()
                    self._unlink_best_effort(temporary)
                else:
                    os.replace(temporary, destination)
                    self._fsync_directory_best_effort(destination.parent)
                    actual = self._metadata_for(
                        destination,
                        media_type=publication.media_type,
                    )
                    if actual != metadata:
                        raise ArtifactIntegrityError(
                            "Artifact 原子发布后复核失败"
                        )
        except (ArtifactConflictError, ArtifactIntegrityError):
            self._unlink_best_effort(temporary)
            raise
        except Exception as exc:
            self._unlink_best_effort(temporary)
            logger.exception(
                "本地 Artifact 发布失败: task_id=%s step_key=%s "
                "artifact_id=%s",
                publication.task_id,
                publication.step_key[:12],
                artifact_id[:12],
            )
            raise ArtifactIntegrityError("无法发布本地 Artifact") from exc

        artifact = ArtifactRef(
            task_id=publication.task_id,
            artifact_id=artifact_id,
            step_key=publication.step_key,
            kind=publication.kind,
            representation=publication.representation,
            metadata=metadata,
            ordinal=publication.ordinal,
        )
        logger.info(
            "本地 Artifact 已发布: task_id=%s artifact_id=%s kind=%s "
            "bytes=%d checksum=%s",
            publication.task_id,
            artifact_id[:12],
            publication.kind.value,
            metadata.size_bytes,
            metadata.sha256[:12],
        )
        return artifact

    def verify(self, artifact: ArtifactRef) -> bool:
        if not isinstance(artifact, ArtifactRef):
            raise TypeError("artifact 必须是 ArtifactRef")
        try:
            with self._artifact_lock(artifact.artifact_id):
                path = self.resolve_path(artifact)
                actual = self._metadata_for(
                    path,
                    media_type=artifact.metadata.media_type,
                )
        except (OSError, ArtifactIntegrityError):
            logger.warning(
                "本地 Artifact 完整性检查失败: task_id=%s artifact_id=%s",
                artifact.task_id,
                artifact.artifact_id[:12],
                exc_info=True,
            )
            return False
        return actual == artifact.metadata

    @contextmanager
    def open_reader(self, artifact: ArtifactRef) -> Iterator[BinaryIO]:
        """校验并在同一读租约内返回同一个文件句柄。

        进程内删除与重新发布会等待该租约释放；校验后不再二次打开文件，消除
        verify/open 之间的 TOCTOU 窗口。多实例边界仍由阶段 3 对象存储实现负责。
        """

        if not isinstance(artifact, ArtifactRef):
            raise TypeError("artifact 必须是 ArtifactRef")
        with self._artifact_lock(artifact.artifact_id):
            path = self.resolve_path(artifact)
            try:
                with path.open("rb") as reader:
                    actual = self._metadata_from_reader(
                        reader,
                        media_type=artifact.metadata.media_type,
                    )
                    if actual != artifact.metadata:
                        raise ArtifactIntegrityError()
                    reader.seek(0)
                    yield reader
            except OSError as exc:
                raise ArtifactIntegrityError() from exc

    def delete_if_owned(self, artifact: ArtifactRef) -> bool:
        """仅删除当前 Store 可定位且内容仍匹配引用的文件。"""

        with self._artifact_lock(artifact.artifact_id):
            try:
                path = self.resolve_path(artifact)
                actual = self._metadata_for(
                    path,
                    media_type=artifact.metadata.media_type,
                )
            except (OSError, ArtifactIntegrityError):
                return False
            if actual != artifact.metadata:
                return False
            try:
                path.unlink()
            except OSError:
                logger.exception(
                    "删除本地 Artifact 失败: task_id=%s artifact_id=%s",
                    artifact.task_id,
                    artifact.artifact_id[:12],
                )
                return False
        for directory in (path.parent, path.parent.parent):
            try:
                directory.rmdir()
            except OSError:
                pass
        return True

    def resolve_path(self, artifact: ArtifactRef) -> Path:
        """仅供同层 Processor Adapter 协作，不属于 Application Port。"""

        if not isinstance(artifact, ArtifactRef):
            raise TypeError("artifact 必须是 ArtifactRef")
        task_root = self._task_root(artifact.task_id)
        expected_id = derive_artifact_id(
            step_key=artifact.step_key,
            kind=artifact.kind,
            representation=artifact.representation,
            ordinal=artifact.ordinal,
        )
        if expected_id != artifact.artifact_id:
            raise ArtifactIntegrityError("Artifact ID 与引用事实不一致")
        candidate = self._artifact_path(task_root, artifact.artifact_id)
        if not candidate.is_file():
            raise ArtifactIntegrityError("Artifact 文件不存在")
        return candidate

    def _task_root(self, task_id: TaskId) -> Path:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        namespace = hashlib.sha256(task_id.value.encode("utf-8")).hexdigest()
        task_root = self._canonical_resolved(self._root / namespace)
        self._require_contained(task_root, self._root)
        return task_root

    def _artifact_path(self, task_root: Path, artifact_id: str) -> Path:
        candidate = self._canonical_resolved(
            task_root / "artifacts" / f"{artifact_id}.bin"
        )
        self._require_contained(candidate, task_root)
        return candidate

    @contextmanager
    def _artifact_lock(self, artifact_id: str) -> Iterator[None]:
        """领取可回收的按 Artifact 锁，避免长运行进程锁表无限增长。"""

        with self._locks_guard:
            entry = self._artifact_locks.get(artifact_id)
            if entry is None:
                entry = _LockEntry(lock=threading.Lock())
                self._artifact_locks[artifact_id] = entry
            entry.users += 1
        entry.lock.acquire()
        try:
            yield
        finally:
            entry.lock.release()
            with self._locks_guard:
                entry.users -= 1
                if entry.users == 0:
                    self._artifact_locks.pop(artifact_id, None)

    @staticmethod
    def _metadata_for(path: Path, *, media_type: str) -> ArtifactMetadata:
        with path.open("rb") as reader:
            return LocalArtifactStoreAdapter._metadata_from_reader(
                reader,
                media_type=media_type,
            )

    @staticmethod
    def _metadata_from_reader(
        reader: BinaryIO,
        *,
        media_type: str,
    ) -> ArtifactMetadata:
        digest = hashlib.sha256()
        size_bytes = 0
        for chunk in iter(lambda: reader.read(_COPY_CHUNK_BYTES), b""):
            digest.update(chunk)
            size_bytes += len(chunk)
        return ArtifactMetadata(
            media_type=media_type,
            size_bytes=size_bytes,
            sha256=digest.hexdigest(),
        )

    @staticmethod
    def _require_contained(candidate: Path, root: Path) -> None:
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ArtifactIntegrityError(
                "Artifact 路径越出任务命名空间"
            ) from exc

    @staticmethod
    def _canonical_resolved(path: Path) -> Path:
        """解析真实路径并统一 Windows 等价扩展路径前缀。"""

        resolved = path.resolve()
        if os.name != "nt":
            return resolved
        text = str(resolved)
        if text.startswith("\\\\?\\UNC\\"):
            return Path("\\\\" + text[8:])
        if text.startswith("\\\\?\\"):
            return Path(text[4:])
        return resolved

    @staticmethod
    def _unlink_best_effort(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "本地 Artifact 临时文件清理失败: file_name=%s",
                path.name,
                exc_info=True,
            )

    @staticmethod
    def _fsync_directory_best_effort(directory: Path) -> None:
        if os.name == "nt":
            return
        try:
            descriptor = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


__all__ = ["LocalArtifactStoreAdapter"]
