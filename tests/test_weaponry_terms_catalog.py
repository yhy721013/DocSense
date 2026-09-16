"""武器谱术语目录自动指纹、版本化同步与历史路由的离线验收。"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest

from app.integrations.anythingllm import (
    AnythingLLMDocument,
    AnythingLLMTimeoutError,
    AnythingLLMWorkspace,
)
from app.modules.weaponry.adapters import (
    AnythingLLMTermsCatalogCoordinator,
    SQLiteTermsCatalogStateStore,
    TermsCatalogSynchronizationError,
    TermsCatalogValidationError,
    TermsCatalogWorkspaceResolver,
    build_terms_catalog_manifest,
    workspace_name_for_fingerprint,
)


def _write_card(root: Path, number: int, body: str) -> Path:
    path = root / f"term_rule_{number:04d}_Card.md"
    path.write_text(
        f'---\ncard_id: "term_rule_{number:04d}"\n---\n\n# Card\n\n{body}\n',
        encoding="utf-8",
    )
    return path


class _FakeAnythingLLMRuntime:
    """只实现目录协调器用到的原子能力，并记录所有远端写入次数。"""

    def __init__(self) -> None:
        self.workspaces: list[AnythingLLMWorkspace] = []
        self.documents_by_slug: dict[str, list[AnythingLLMDocument]] = {}
        self.uploaded: dict[str, AnythingLLMDocument] = {}
        self.create_calls = 0
        self.upload_calls = 0
        self.embedding_calls = 0
        self.fail_create_after_commit = False
        self.fail_upload_after_commit = False
        self.fail_embedding_after_commit = False


class _FakeDocuments:
    def __init__(self, runtime: _FakeAnythingLLMRuntime) -> None:
        self._runtime = runtime

    def upload_document(self, file_path, *, user_id=None, metadata=None):
        self._runtime.upload_calls += 1
        path = Path(file_path)
        provider_file_name = re.sub(r"[-\s]+", "-", path.name)
        location = (
            f"custom-documents/{provider_file_name}-"
            f"00000000-0000-0000-0000-{self._runtime.upload_calls:012d}.json"
        )
        document = AnythingLLMDocument(
            id=f"upload-{self._runtime.upload_calls}",
            location=location,
            title=path.name,
            document_ref=f"document:upload-{self._runtime.upload_calls}",
        )
        self._runtime.uploaded[location] = document
        if self._runtime.fail_upload_after_commit:
            raise AnythingLLMTimeoutError("injected upload timeout")
        return document


class _FakeWorkspaces:
    def __init__(self, runtime: _FakeAnythingLLMRuntime) -> None:
        self._runtime = runtime

    def list_workspaces(self, *, user_id=None):
        return list(self._runtime.workspaces)

    def create_workspace(self, name, *, settings=None, user_id=None):
        self._runtime.create_calls += 1
        workspace = AnythingLLMWorkspace(
            id=f"workspace-{self._runtime.create_calls}",
            slug=f"slug-{self._runtime.create_calls}",
            name=name,
        )
        self._runtime.workspaces.append(workspace)
        self._runtime.documents_by_slug[workspace.slug] = []
        if self._runtime.fail_create_after_commit:
            raise AnythingLLMTimeoutError("injected create timeout")
        return workspace

    def list_documents(self, workspace_slug, *, user_id=None):
        return list(self._runtime.documents_by_slug[workspace_slug])

    def update_embeddings(
        self,
        workspace_slug,
        *,
        adds=None,
        deletes=None,
        user_id=None,
    ):
        self._runtime.embedding_calls += 1
        documents = self._runtime.documents_by_slug[workspace_slug]
        for location in adds or ():
            document = self._runtime.uploaded[location]
            if all(item.location != location for item in documents):
                # 真实 AnythingLLM 的 workspace 文档记录会把 title 改写成带上传 UUID
                # 的 location 文件名，而不是保留上传请求中的原始文件名。Fake 必须呈现
                # 这个生产差异，避免协调器再次错误依赖展示标题判断文档身份。
                documents.append(
                    replace(
                        document,
                        title=location.rsplit("/", 1)[-1],
                    )
                )
        if self._runtime.fail_embedding_after_commit:
            raise AnythingLLMTimeoutError("injected embedding timeout")
        return next(
            workspace
            for workspace in self._runtime.workspaces
            if workspace.slug == workspace_slug
        )

    def find_document(self, workspace_slug, location, *, user_id=None):
        return next(
            (
                document
                for document in self._runtime.documents_by_slug[workspace_slug]
                if document.location == location
            ),
            None,
        )


class _FakeClientFactory:
    def __init__(self, runtime: _FakeAnythingLLMRuntime) -> None:
        self._runtime = runtime

    @contextmanager
    def create(self):
        yield SimpleNamespace(
            documents=_FakeDocuments(self._runtime),
            workspaces=_FakeWorkspaces(self._runtime),
            threads=SimpleNamespace(),
        )


class TermsCatalogManifestTests(unittest.TestCase):
    def test_manifest_is_deterministic_and_changes_with_content(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = _write_card(root, 1, "alpha")
            _write_card(root, 2, "beta")
            manifest_a = build_terms_catalog_manifest(root)
            manifest_b = build_terms_catalog_manifest(root)
            self.assertEqual(manifest_a.fingerprint, manifest_b.fingerprint)
            self.assertTrue(
                manifest_a.fingerprint.startswith(
                    "terms-manifest-v1:sha256:"
                )
            )

            first.write_text(
                '---\ncard_id: "term_rule_0001"\n---\n\n# Card\n\ngamma\n',
                encoding="utf-8",
            )
            manifest_c = build_terms_catalog_manifest(root)
            self.assertNotEqual(manifest_a.fingerprint, manifest_c.fingerprint)
            self.assertNotEqual(
                manifest_a.workspace_name("weaponry-terms-rules"),
                manifest_c.workspace_name("weaponry-terms-rules"),
            )

    def test_duplicate_card_id_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_card(root, 1, "alpha")
            second = _write_card(root, 2, "beta")
            second.write_text(
                '---\ncard_id: "term_rule_0001"\n---\n\n# Duplicate\n',
                encoding="utf-8",
            )
            with self.assertRaises(TermsCatalogValidationError):
                build_terms_catalog_manifest(root)

    def test_non_utf8_card_is_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "term_rule_0001_Broken.md").write_bytes(b"\xff\xfe")
            with self.assertRaises(TermsCatalogValidationError):
                build_terms_catalog_manifest(root)


class TermsCatalogCoordinatorTests(unittest.TestCase):
    def _coordinator(self, root: Path, runtime: _FakeAnythingLLMRuntime):
        manifest = build_terms_catalog_manifest(root)
        factory = _FakeClientFactory(runtime)
        resolver = TermsCatalogWorkspaceResolver(
            factory,
            workspace_base_name="weaponry-terms-rules",
        )
        coordinator = AnythingLLMTermsCatalogCoordinator(
            factory,
            manifest=manifest,
            workspace_base_name="weaponry-terms-rules",
            state_store=SQLiteTermsCatalogStateStore(root / "state.sqlite3"),
            resolver=resolver,
        )
        return manifest, resolver, coordinator

    def test_empty_remote_is_created_uploaded_bound_and_second_call_has_no_writes(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_card(root, 1, "alpha")
            _write_card(root, 2, "beta")
            runtime = _FakeAnythingLLMRuntime()
            manifest, resolver, coordinator = self._coordinator(root, runtime)

            descriptor = coordinator.prepare()
            self.assertEqual(manifest.fingerprint, descriptor.fingerprint)
            self.assertEqual(1, runtime.create_calls)
            self.assertEqual(2, runtime.upload_calls)
            self.assertEqual(2, runtime.embedding_calls)
            self.assertEqual(descriptor.workspace_slug, resolver.resolve(manifest.fingerprint))

            coordinator.prepare()
            self.assertEqual(1, runtime.create_calls)
            self.assertEqual(2, runtime.upload_calls)
            self.assertEqual(2, runtime.embedding_calls)

    def test_complete_generation_is_reused_without_remote_writes(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_card(root, 1, "alpha")
            runtime = _FakeAnythingLLMRuntime()
            manifest, _, first = self._coordinator(root, runtime)
            first.prepare()
            writes = (
                runtime.create_calls,
                runtime.upload_calls,
                runtime.embedding_calls,
            )

            # 模拟进程重启：重新构造协调器和状态对象，但复用同一远端事实。
            self.assertNotEqual(
                manifest.cards[0].upload_file_name,
                runtime.documents_by_slug[runtime.workspaces[0].slug][0].title,
            )
            _, _, restarted = self._coordinator(root, runtime)
            restarted.prepare()
            self.assertEqual(
                writes,
                (
                    runtime.create_calls,
                    runtime.upload_calls,
                    runtime.embedding_calls,
                ),
            )
            self.assertEqual(
                workspace_name_for_fingerprint(
                    "weaponry-terms-rules",
                    manifest.fingerprint,
                ),
                runtime.workspaces[0].name,
            )

    def test_inspect_uses_persisted_location_instead_of_provider_title(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_card(root, 1, "alpha")
            runtime = _FakeAnythingLLMRuntime()
            _, _, coordinator = self._coordinator(root, runtime)
            coordinator.prepare()

            _, _, restarted = self._coordinator(root, runtime)
            plan = restarted.inspect()

            self.assertEqual((), plan.missing_card_ids)
            self.assertEqual((), plan.unexpected_document_titles)
            self.assertFalse(plan.blocked_outcome_unknown)
            self.assertFalse(plan.write_required)

    def test_partial_generation_only_uploads_the_missing_card(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_card(root, 1, "alpha")
            _write_card(root, 2, "beta")
            runtime = _FakeAnythingLLMRuntime()
            _, _, first = self._coordinator(root, runtime)
            descriptor = first.prepare()
            runtime.documents_by_slug[descriptor.workspace_slug].pop()

            _, _, restarted = self._coordinator(root, runtime)
            restarted.prepare()
            self.assertEqual(3, runtime.upload_calls)
            self.assertEqual(3, runtime.embedding_calls)

    def test_create_timeout_after_commit_recovers_by_exact_relist(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_card(root, 1, "alpha")
            runtime = _FakeAnythingLLMRuntime()
            runtime.fail_create_after_commit = True
            _, _, coordinator = self._coordinator(root, runtime)
            descriptor = coordinator.prepare()
            self.assertEqual("slug-1", descriptor.workspace_slug)
            self.assertEqual(1, runtime.create_calls)
            self.assertEqual(1, runtime.upload_calls)

    def test_concurrent_prepare_publishes_one_generation_once(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_card(root, 1, "alpha")
            runtime = _FakeAnythingLLMRuntime()
            _, _, coordinator = self._coordinator(root, runtime)
            barrier = threading.Barrier(12)
            results = []
            errors = []

            def prepare() -> None:
                try:
                    barrier.wait()
                    results.append(coordinator.prepare())
                except BaseException as exc:  # 测试线程必须把异常回传主线程。
                    errors.append(exc)

            threads = [threading.Thread(target=prepare) for _ in range(12)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5.0)

            self.assertFalse(errors)
            self.assertEqual(12, len(results))
            self.assertEqual(1, runtime.create_calls)
            self.assertEqual(1, runtime.upload_calls)
            self.assertEqual(1, runtime.embedding_calls)

    def test_upload_outcome_unknown_is_persisted_and_not_blindly_replayed(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_card(root, 1, "alpha")
            runtime = _FakeAnythingLLMRuntime()
            runtime.fail_upload_after_commit = True
            _, _, coordinator = self._coordinator(root, runtime)
            with self.assertRaises(TermsCatalogSynchronizationError):
                coordinator.prepare()
            self.assertEqual(1, runtime.upload_calls)

            runtime.fail_upload_after_commit = False
            _, _, restarted = self._coordinator(root, runtime)
            with self.assertRaises(TermsCatalogSynchronizationError):
                restarted.prepare()
            self.assertEqual(1, runtime.upload_calls)

    def test_binding_timeout_after_commit_recovers_by_exact_location(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_card(root, 1, "alpha")
            runtime = _FakeAnythingLLMRuntime()
            runtime.fail_embedding_after_commit = True
            _, _, coordinator = self._coordinator(root, runtime)

            with self.assertRaises(TermsCatalogSynchronizationError):
                coordinator.prepare()
            self.assertEqual(1, runtime.upload_calls)
            self.assertEqual(1, runtime.embedding_calls)

            runtime.fail_embedding_after_commit = False
            _, _, restarted = self._coordinator(root, runtime)
            restarted.prepare()
            self.assertEqual(1, runtime.upload_calls)
            self.assertEqual(1, runtime.embedding_calls)

    def test_non_empty_workspace_without_recovery_facts_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            _write_card(root, 1, "alpha")
            runtime = _FakeAnythingLLMRuntime()
            _, _, coordinator = self._coordinator(root, runtime)
            coordinator.prepare()
            writes = (runtime.upload_calls, runtime.embedding_calls)

            # 模拟本地恢复库丢失。远端 title 经过 Provider 改写后不能证明它一定属于
            # 当前目录，因此必须阻断启动，不能按标题猜测并重复上传。
            (root / "state.sqlite3").unlink()
            _, _, restarted = self._coordinator(root, runtime)
            with self.assertRaises(TermsCatalogSynchronizationError):
                restarted.prepare()
            self.assertEqual(writes, (runtime.upload_calls, runtime.embedding_calls))

    def test_legacy_manual_fingerprint_routes_to_original_read_only_workspace(
        self,
    ) -> None:
        runtime = _FakeAnythingLLMRuntime()
        runtime.workspaces.append(
            AnythingLLMWorkspace(
                id="legacy",
                slug="weaponry-terms-rules",
                name="Legacy Terms",
            )
        )
        resolver = TermsCatalogWorkspaceResolver(
            _FakeClientFactory(runtime),
            workspace_base_name="weaponry-terms-rules",
        )
        self.assertEqual(
            "weaponry-terms-rules",
            resolver.resolve("terms-20260719"),
        )


if __name__ == "__main__":
    unittest.main()
