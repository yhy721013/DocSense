"""阶段 1H-1 本地 Artifact Store 门禁。"""

from __future__ import annotations

import hashlib
import os
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.modules.document_processing.adapters import (
    BytesArtifactContent,
    LocalArtifactStoreAdapter,
)
from app.modules.document_processing.domain import (
    ArtifactConflictError,
    ArtifactKind,
    DocumentRepresentation,
)
from app.modules.document_processing.ports import ArtifactPublication
from app.modules.tasks.domain import TaskId
from tests import workspace_tempdir


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _publication(index: int = 0) -> ArtifactPublication:
    return ArtifactPublication(
        task_id=TaskId(f"stage1h-artifact-{index:02d}"),
        step_key=_digest(f"step-{index}"),
        kind=ArtifactKind.PREPARED,
        representation=DocumentRepresentation.TEXT,
        media_type="text/plain",
    )


class LocalArtifactStoreTests(unittest.TestCase):
    def test_publish_is_atomic_verifiable_readable_and_owned_cleanup(self) -> None:
        with workspace_tempdir() as temporary:
            store = LocalArtifactStoreAdapter(Path(temporary) / "artifacts")
            artifact = store.publish(
                _publication(),
                BytesArtifactContent(b"hello"),
            )

            self.assertTrue(store.verify(artifact))
            with store.open_reader(artifact) as reader:
                self.assertEqual(b"hello", reader.read())
            self.assertFalse(
                any(store.root.rglob("*.part")),
                "成功发布后不得遗留半文件",
            )
            self.assertTrue(store.delete_if_owned(artifact))
            self.assertFalse(store.verify(artifact))

    def test_same_identity_is_idempotent_but_different_content_conflicts(self) -> None:
        with workspace_tempdir() as temporary:
            store = LocalArtifactStoreAdapter(Path(temporary) / "artifacts")
            publication = _publication()
            first = store.publish(
                publication,
                BytesArtifactContent(b"same"),
            )
            second = store.publish(
                publication,
                BytesArtifactContent(b"same"),
            )
            self.assertEqual(first, second)

            with self.assertRaises(ArtifactConflictError):
                store.publish(
                    publication,
                    BytesArtifactContent(b"different"),
                )
            self.assertTrue(store.verify(first))

    def test_fifty_distinct_tasks_publish_without_namespace_collision(self) -> None:
        with workspace_tempdir() as temporary:
            store = LocalArtifactStoreAdapter(Path(temporary) / "artifacts")

            def publish(index: int):
                return store.publish(
                    _publication(index),
                    BytesArtifactContent(f"payload-{index}".encode()),
                )

            with ThreadPoolExecutor(max_workers=50) as executor:
                artifacts = tuple(executor.map(publish, range(50)))

            self.assertEqual(50, len({item.artifact_id for item in artifacts}))
            self.assertTrue(all(store.verify(item) for item in artifacts))
            task_directories = [
                item for item in store.root.iterdir() if item.is_dir()
            ]
            self.assertEqual(50, len(task_directories))
            self.assertEqual(0, store.retained_lock_count)

    def test_open_reader_holds_delete_lease_and_lock_entry_is_recycled(
        self,
    ) -> None:
        with workspace_tempdir() as temporary:
            store = LocalArtifactStoreAdapter(Path(temporary) / "artifacts")
            artifact = store.publish(
                _publication(index=60),
                BytesArtifactContent(b"leased-content"),
            )
            reader_entered = threading.Event()
            release_reader = threading.Event()

            def read_slowly() -> bytes:
                with store.open_reader(artifact) as reader:
                    reader_entered.set()
                    release_reader.wait(timeout=10)
                    return reader.read()

            with ThreadPoolExecutor(max_workers=2) as executor:
                read_future = executor.submit(read_slowly)
                self.assertTrue(reader_entered.wait(timeout=10))
                delete_future = executor.submit(store.delete_if_owned, artifact)
                time.sleep(0.05)
                self.assertFalse(delete_future.done())
                self.assertEqual(1, store.retained_lock_count)
                release_reader.set()
                self.assertEqual(b"leased-content", read_future.result(timeout=10))
                self.assertTrue(delete_future.result(timeout=10))
            self.assertEqual(0, store.retained_lock_count)

    def test_symlink_escape_is_rejected_when_platform_allows_symlinks(self) -> None:
        with workspace_tempdir() as temporary:
            root = Path(temporary) / "artifacts"
            outside = Path(temporary) / "outside"
            outside.mkdir()
            publication = _publication()
            task_namespace = hashlib.sha256(
                publication.task_id.value.encode("utf-8")
            ).hexdigest()
            task_root = root / task_namespace
            task_root.mkdir(parents=True)
            try:
                os.symlink(outside, task_root / "artifacts", target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"当前平台不允许创建测试符号链接: {exc}")

            store = LocalArtifactStoreAdapter(root)
            with self.assertRaises(Exception):
                store.publish(publication, BytesArtifactContent(b"escape"))
            self.assertEqual([], list(outside.iterdir()))


if __name__ == "__main__":
    unittest.main()
