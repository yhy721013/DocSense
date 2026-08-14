"""报告任务的本地文件 Artifact 适配器。

阶段 1C 仍运行在单机开发环境，因此本实现把每次 execution 的临时文件和最终报告放入
彼此隔离的本地目录。业务层只持有 :class:`ReportArtifactRef`，不会看到或拼接真实路径；
阶段 3 替换为 MinIO 时，Application 与报告领域对象无需改变。

本模块额外提供少量“适配器协作方法”给同包的文件/RAG 适配器使用。这些方法不是业务
Port 的一部分，也不得被路由或 Application 直接调用。
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from pathlib import Path
from uuid import uuid4

from app.modules.document_processing.adapters import LocalArtifactStoreAdapter
from app.modules.document_processing.domain import ArtifactRef
from app.modules.report.domain.errors import ReportArtifactError
from app.modules.report.ports import (
    ReportArtifactCategory,
    ReportArtifactCleanupResult,
    ReportArtifactRef,
    ReportArtifactScope,
)
from app.modules.tasks.domain import TaskId


logger = logging.getLogger(__name__)

_SCRATCH_CATEGORIES = frozenset(
    {
        ReportArtifactCategory.SOURCE,
        ReportArtifactCategory.NORMALIZED_SOURCE,
        ReportArtifactCategory.RAG_INPUT,
        ReportArtifactCategory.TEMPLATE,
        ReportArtifactCategory.QUARANTINE,
    }
)


class LocalReportArtifactAdapter:
    """以任务哈希作为目录前缀的本地 Artifact 存储。

    ``TaskId`` 是内部不透明文本，不保证能安全充当文件名。因此目录名使用完整 SHA-256，
    既避免路径穿越，也不会在磁盘路径中泄露 execution ID。所有有效文件写入都先落到同
    目录临时文件，再通过 ``os.replace`` 原子发布，读取者不会观察到半文件。
    """

    def __init__(self, root: str | Path) -> None:
        resolved_root = self._canonical_resolved(Path(root).expanduser())
        if resolved_root.exists() and not resolved_root.is_dir():
            raise ValueError("Artifact root 必须是目录")
        self._root = resolved_root

    @property
    def root(self) -> Path:
        """返回已解析根目录，仅供组合根、诊断和离线测试使用。"""

        return self._root

    def begin(self, task_id: TaskId) -> ReportArtifactScope:
        """无副作用地分配 execution 独占命名空间。

        资源恢复记录必须先于任何文件/对象创建。具体目录只在首次写入时按类别惰性创建，
        从而避免 Store 登记失败后留下没有持久化所有者的空目录；未来 MinIO Adapter 也
        应保持 ``begin`` 只计算对象前缀、不创建占位对象。
        """

        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        namespace = self._namespace(task_id)
        logger.debug(
            "报告任务 Artifact 命名空间已分配: task_id=%s namespace=%s",
            task_id,
            namespace,
        )
        return ReportArtifactScope(task_id=task_id, namespace=namespace)

    def persist_report_html(
        self,
        scope: ReportArtifactScope,
        html_details: str,
    ) -> ReportArtifactRef:
        """原子保存最终 HTML，并返回带大小和 SHA-256 的不可变引用。"""

        self._require_scope(scope)
        if not isinstance(html_details, str):
            raise TypeError("html_details 必须是 str")
        destination = self._task_root(scope.task_id) / "output" / "report.html"
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
        payload = html_details.encode("utf-8")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("xb") as file_object:
                file_object.write(payload)
                file_object.flush()
                os.fsync(file_object.fileno())
            os.replace(temporary, destination)
        except OSError as exc:
            self._unlink_best_effort(temporary)
            logger.exception(
                "持久化报告 HTML Artifact 失败: task_id=%s bytes=%d",
                scope.task_id,
                len(payload),
            )
            raise ReportArtifactError("无法持久化最终报告") from exc

        reference = self._reference_for_path(
            scope.task_id,
            destination,
            ReportArtifactCategory.REPORT_HTML,
            sequence_no=None,
        )
        logger.info(
            "报告 HTML Artifact 已持久化: task_id=%s bytes=%d checksum=%s",
            scope.task_id,
            reference.size_bytes or 0,
            reference.checksum[:12],
        )
        return reference

    def load_report_html(self, artifact: ReportArtifactRef) -> str:
        """读取终态持有的 HTML，供同步 Callback 恢复重建原公开载荷。

        恢复路径不能把内部 result_ref 当作公开正文，也不能在文件被篡改后继续发送。
        因此这里在解码前同时复核类别、任务目录、大小与 SHA-256。
        """

        if not isinstance(artifact, ReportArtifactRef):
            raise TypeError("artifact 必须是 ReportArtifactRef")
        if artifact.category is not ReportArtifactCategory.REPORT_HTML:
            raise ReportArtifactError("只允许读取最终报告 HTML Artifact")
        path = self.resolve_path(artifact)
        actual = self._output_reference(artifact.task_id, path)
        if actual != artifact:
            logger.error(
                "读取报告 Artifact 时完整性复核失败: task_id=%s artifact_id=%s",
                artifact.task_id,
                artifact.artifact_id,
            )
            raise ReportArtifactError("最终报告 Artifact 完整性校验失败")
        try:
            return path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError) as exc:
            logger.exception(
                "读取报告 HTML Artifact 失败: task_id=%s artifact_id=%s",
                artifact.task_id,
                artifact.artifact_id,
            )
            raise ReportArtifactError("无法读取最终报告") from exc

    def cleanup_unretained(
        self,
        scope: ReportArtifactScope,
        *,
        retain: tuple[ReportArtifactRef, ...],
    ) -> ReportArtifactCleanupResult:
        """逐项清理 scratch 和未获终态所有权的 final Artifact。

        ``retain`` 只能包含当前任务的最终报告。旧 execution 在终态条件写返回 stale 后
        不会取得该引用，因此其 output/report.html 也必须删除，不能因为位于 output 目录
        就变成永久孤儿。逐文件处理会把 Windows 文件占用等失败精确返回为 ``pending``，
        由持久化资源记录继续恢复。
        """

        self._require_scope(scope)
        retained_ids: set[str] = set()
        for item in tuple(retain):
            if not isinstance(item, ReportArtifactRef) or item.task_id != scope.task_id:
                raise ReportArtifactError("retain Artifact 不属于当前任务")
            if item.category is not ReportArtifactCategory.REPORT_HTML:
                raise ReportArtifactError("只允许 retain 最终报告 Artifact")
            retained_ids.add(item.artifact_id)

        task_root = self._task_root(scope.task_id)
        scratch_root = task_root / "scratch"
        output_root = task_root / "output"
        cleaned: list[ReportArtifactRef] = []
        # ``retain`` 并不意味着可以跳过检查。最终报告引用已经写入任务终态，若文件被
        # 人工删除、磁盘损坏或内容被篡改，仍然把本次清理标记为成功，会让持久化资源
        # 记录永久进入 CLEANED。这里复核大小与 SHA-256，并把异常引用留在 pending，
        # 使恢复 Worker 和监控能够持续暴露问题，而不是静默丢失最终产物。
        pending: list[ReportArtifactRef] = [
            item for item in tuple(retain) if not self._retained_artifact_is_intact(item)
        ]
        roots = (
            (scratch_root, self._scratch_reference),
            (output_root, self._output_reference),
        )
        for current_root, reference_factory in roots:
            if not current_root.exists():
                continue
            files = sorted(
                (path for path in current_root.rglob("*") if path.is_file()),
                key=lambda item: item.as_posix(),
            )
            for path in files:
                # 保留产物已在上方完成完整性复核。先按任务内相对路径识别它，再计算其
                # 元数据，避免对已经确认损坏的文件重复读取并产生第二个异常路径。
                artifact_id = path.relative_to(task_root).as_posix()
                if artifact_id in retained_ids:
                    continue
                reference = reference_factory(scope.task_id, path)
                try:
                    path.unlink()
                except OSError:
                    pending.append(reference)
                    logger.exception(
                        "报告未保留 Artifact 清理失败: task_id=%s artifact_id=%s",
                        scope.task_id,
                        reference.artifact_id,
                    )
                else:
                    cleaned.append(reference)

            # 目录删除只清理已经为空的路径；pending 或 retained 文件会自然保留。
            for directory in sorted(
                (path for path in current_root.rglob("*") if path.is_dir()),
                key=lambda item: len(item.parts),
                reverse=True,
            ):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            try:
                current_root.rmdir()
            except OSError:
                pass

        # 没有保留产物且所有文件均已删除时，清理任务级空目录；存在 retained/pending 时
        # rmdir 会安全失败，不使用递归删除。
        for directory in sorted(
            (path for path in task_root.rglob("*") if path.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        try:
            task_root.rmdir()
        except OSError:
            pass

        logger.log(
            logging.WARNING if pending else logging.INFO,
            "报告未保留 Artifact 清理完成: task_id=%s retained_count=%d "
            "cleaned_count=%d pending_count=%d",
            scope.task_id,
            len(retained_ids),
            len(cleaned),
            len(pending),
        )
        return ReportArtifactCleanupResult(tuple(cleaned), tuple(pending))

    def _retained_artifact_is_intact(self, expected: ReportArtifactRef) -> bool:
        """复核终态持有的最终报告仍存在，且内容与持久化元数据完全一致。"""

        task_root = self._task_root(expected.task_id)
        try:
            candidate = self._canonical_resolved(task_root / expected.artifact_id)
            self._require_contained(candidate, task_root)
            if not candidate.is_file():
                raise ReportArtifactError("保留的最终报告文件不存在")
            actual = self._output_reference(expected.task_id, candidate)
        except (OSError, ReportArtifactError):
            logger.exception(
                "保留的报告 Artifact 完整性检查失败: task_id=%s artifact_id=%s",
                expected.task_id,
                expected.artifact_id,
            )
            return False

        if actual != expected:
            logger.error(
                "保留的报告 Artifact 元数据不一致: task_id=%s artifact_id=%s "
                "expected_size=%s actual_size=%s expected_checksum=%s actual_checksum=%s",
                expected.task_id,
                expected.artifact_id,
                expected.size_bytes,
                actual.size_bytes,
                expected.checksum[:12],
                actual.checksum[:12],
            )
            return False
        return True

    # ------------------------------------------------------------------
    # 以下方法仅供同一适配层中的文件/RAG 实现协作，Application 不应依赖。
    # ------------------------------------------------------------------

    def resolve_path(self, artifact: ReportArtifactRef) -> Path:
        """把本适配器签发的引用解析为存在的普通文件，并复核类别目录。"""

        if not isinstance(artifact, ReportArtifactRef):
            raise TypeError("artifact 必须是 ReportArtifactRef")
        task_root = self._task_root(artifact.task_id)
        candidate = self._canonical_resolved(task_root / artifact.artifact_id)
        self._require_contained(candidate, task_root)
        expected_prefix = (
            Path("output")
            if artifact.category is ReportArtifactCategory.REPORT_HTML
            else Path("scratch") / artifact.category.value
        )
        try:
            relative = candidate.relative_to(task_root)
        except ValueError as exc:  # pragma: no cover - 已由 _require_contained 防御
            raise ReportArtifactError("Artifact 路径越出任务命名空间") from exc
        if relative.parts[: len(expected_prefix.parts)] != expected_prefix.parts:
            raise ReportArtifactError("Artifact 类别与存储路径不一致")
        if not candidate.is_file():
            raise ReportArtifactError("Artifact 文件不存在")
        return candidate

    def publish_file(
        self,
        scope: ReportArtifactScope,
        *,
        category: ReportArtifactCategory,
        source_path: str | Path,
        file_name: str,
        sequence_no: int | None,
    ) -> ReportArtifactRef:
        """把适配器生成文件复制到指定 scratch 类别并原子发布。"""

        self._require_scope(scope)
        if category not in _SCRATCH_CATEGORIES:
            raise ReportArtifactError("publish_file 只能写入 scratch 类别")
        safe_name = Path(file_name).name
        if not safe_name or safe_name in {".", ".."}:
            raise ReportArtifactError("Artifact 文件名无效")
        source = self._canonical_resolved(Path(source_path))
        if not source.is_file():
            raise ReportArtifactError("待发布 Artifact 源文件不存在")
        destination = self._task_root(scope.task_id) / "scratch" / category.value / safe_name
        temporary = destination.with_name(f".{safe_name}.{uuid4().hex}.part")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as reader, temporary.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(temporary, destination)
        except OSError as exc:
            self._unlink_best_effort(temporary)
            logger.exception(
                "发布报告 scratch Artifact 失败: task_id=%s category=%s sequence_no=%s",
                scope.task_id,
                category.value,
                sequence_no,
            )
            raise ReportArtifactError("无法发布报告中间文件") from exc
        return self._reference_for_path(
            scope.task_id,
            destination,
            category,
            sequence_no=sequence_no,
        )

    def publish_document_artifact(
        self,
        scope: ReportArtifactScope,
        *,
        category: ReportArtifactCategory,
        artifact: ArtifactRef,
        document_store: LocalArtifactStoreAdapter,
        file_name: str,
        sequence_no: int | None,
    ) -> ReportArtifactRef:
        """把共享 DocumentProcessing Artifact 映射到既有报告合同。

        ``ReportArtifactRef`` 是报告模块已经冻结的业务引用，不能把共享 Artifact 字段
        塞入公开 DTO。本方法在适配层复核所有权与完整性后执行一次原子发布；共享 Store
        中的处理记录和 lineage 仍是转换事实的权威来源，报告 Store 只持有业务生命周期
        所需的映射副本。
        """

        self._require_scope(scope)
        if not isinstance(artifact, ArtifactRef):
            raise TypeError("artifact 必须是 ArtifactRef")
        if artifact.task_id != scope.task_id:
            raise ReportArtifactError("共享 Artifact 不属于当前报告任务")
        if not isinstance(document_store, LocalArtifactStoreAdapter):
            raise TypeError("document_store 必须是 LocalArtifactStoreAdapter")
        if not document_store.verify(artifact):
            raise ReportArtifactError("共享 Artifact 完整性检查失败")
        return self.publish_file(
            scope,
            category=category,
            source_path=document_store.resolve_path(artifact),
            file_name=file_name,
            sequence_no=sequence_no,
        )

    def staging_path(
        self,
        scope: ReportArtifactScope,
        *,
        category: ReportArtifactCategory,
        suffix: str,
    ) -> Path:
        """分配同一任务目录内的唯一临时路径，供 legacy 下载器先写后发布。"""

        self._require_scope(scope)
        if category not in _SCRATCH_CATEGORIES:
            raise ReportArtifactError("staging_path 只能用于 scratch 类别")
        normalized_suffix = suffix if suffix.startswith(".") else f".{suffix}" if suffix else ""
        staging = (
            self._task_root(scope.task_id)
            / "scratch"
            / category.value
            / f".stage-{uuid4().hex}{normalized_suffix}"
        )
        try:
            staging.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.exception(
                "创建报告 Artifact 暂存目录失败: task_id=%s category=%s",
                scope.task_id,
                category.value,
            )
            raise ReportArtifactError("无法创建报告任务文件目录") from exc
        return staging

    def remove_private_file(self, scope: ReportArtifactScope, path: str | Path) -> None:
        """删除仍位于当前任务目录内的适配器临时文件；不存在时幂等成功。"""

        self._require_scope(scope)
        candidate = self._canonical_resolved(Path(path))
        self._require_contained(candidate, self._task_root(scope.task_id))
        self._unlink_best_effort(candidate)

    def _scratch_reference(self, task_id: TaskId, path: Path) -> ReportArtifactRef:
        relative = path.relative_to(self._task_root(task_id))
        if len(relative.parts) < 3 or relative.parts[0] != "scratch":
            raise ReportArtifactError("scratch 文件路径结构无效")
        try:
            category = ReportArtifactCategory(relative.parts[1])
        except ValueError as exc:
            raise ReportArtifactError("scratch 文件类别无效") from exc
        sequence_no = self._sequence_from_name(path.name)
        return self._reference_for_path(
            task_id,
            path,
            category,
            sequence_no=sequence_no,
        )

    def _output_reference(self, task_id: TaskId, path: Path) -> ReportArtifactRef:
        """把 output 下的文件转换成可持久化清理引用。"""

        relative = path.relative_to(self._task_root(task_id))
        if len(relative.parts) < 2 or relative.parts[0] != "output":
            raise ReportArtifactError("output 文件路径结构无效")
        return self._reference_for_path(
            task_id,
            path,
            ReportArtifactCategory.REPORT_HTML,
            sequence_no=None,
        )

    def _reference_for_path(
        self,
        task_id: TaskId,
        path: Path,
        category: ReportArtifactCategory,
        *,
        sequence_no: int | None,
    ) -> ReportArtifactRef:
        task_root = self._task_root(task_id)
        resolved = self._canonical_resolved(path)
        self._require_contained(resolved, task_root)
        try:
            size_bytes = resolved.stat().st_size
            checksum = self._sha256_file(resolved)
        except OSError as exc:
            raise ReportArtifactError("无法读取 Artifact 元数据") from exc
        return ReportArtifactRef(
            task_id=task_id,
            artifact_id=resolved.relative_to(task_root).as_posix(),
            category=category,
            sequence_no=sequence_no,
            size_bytes=size_bytes,
            checksum=checksum,
        )

    def _require_scope(self, scope: ReportArtifactScope) -> None:
        if not isinstance(scope, ReportArtifactScope):
            raise TypeError("scope 必须是 ReportArtifactScope")
        if scope.namespace != self._namespace(scope.task_id):
            raise ReportArtifactError("Artifact scope 不属于当前存储或任务")

    def _task_root(self, task_id: TaskId) -> Path:
        if not isinstance(task_id, TaskId):
            raise TypeError("task_id 必须是 TaskId")
        task_root = self._canonical_resolved(self._root / self._namespace(task_id))
        self._require_contained(task_root, self._root)
        return task_root

    @staticmethod
    def _namespace(task_id: TaskId) -> str:
        digest = hashlib.sha256(task_id.value.encode("utf-8")).hexdigest()
        return f"report/{digest}"

    @staticmethod
    def _sequence_from_name(file_name: str) -> int | None:
        prefix = file_name.split("-", 1)[0].split(".", 1)[0]
        return int(prefix) if prefix.isdigit() and int(prefix) > 0 else None

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file_object:
            for chunk in iter(lambda: file_object.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _require_contained(candidate: Path, root: Path) -> None:
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ReportArtifactError("Artifact 路径越出允许的任务目录") from exc

    @staticmethod
    def _canonical_resolved(path: Path) -> Path:
        """解析真实路径并统一 Windows 扩展路径前缀。

        Windows 在目录由“不存在”变为“已创建”的并发窗口中，``Path.resolve`` 可能对
        同一绝对路径交替返回 ``C:\\...`` 与 ``\\\\?\\C:\\...``。两者访问的是同一个
        位置，但 ``relative_to`` 会把它们误判为不同根。这里先完成符号链接解析，再仅
        规范等价的 Win32 前缀；路径包含关系仍由解析后的真实路径校验。
        """

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
                "删除报告临时文件失败，将由后续 cleanup 重试: file_name=%s",
                path.name,
                exc_info=True,
            )


__all__ = ["LocalReportArtifactAdapter"]
