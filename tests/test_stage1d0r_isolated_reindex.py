"""阶段 1D-0R 隔离重建工具的资源所有权、补偿与清洗测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.integrations.anythingllm.models import (
    AnythingLLMDocument,
    AnythingLLMSource,
    AnythingLLMWorkspace,
)
from scripts.calibrate_weaponry_retrieval_quality import CalibrationQuery
from scripts.reindex_weaponry_retrieval_quality_isolated import (
    build_cleaned_retrieval_copy,
    run_isolated_calibration,
)


TOKEN = "a" * 32


class _FakeDocumentClient:
    def __init__(self, storage_root: Path, registry: dict[str, AnythingLLMDocument]) -> None:
        self.storage_root = storage_root
        self.registry = registry
        self.deleted_locations: list[str] = []

    def upload_document(self, file_path: str, **_: object) -> AnythingLLMDocument:
        source = Path(file_path)
        location = f"custom-documents/{source.name}-11111111-1111-1111-1111-111111111111.json"
        target = self.storage_root / "documents" / Path(*location.split("/"))
        target.write_text('{"temporary":true}\n', encoding="utf-8")
        document = AnythingLLMDocument(
            id="11111111-1111-1111-1111-111111111111",
            location=location,
            title=source.name,
            document_ref="document:11111111-1111-1111-1111-111111111111",
        )
        self.registry[location] = document
        return document

    def delete_document(self, location: str, **_: object) -> None:
        self.deleted_locations.append(location)
        target = self.storage_root / "documents" / Path(*location.split("/"))
        target.unlink(missing_ok=True)
        self.registry.pop(location, None)


class _CollidingDocumentClient(_FakeDocumentClient):
    def upload_document(self, *_: object, **__: object) -> AnythingLLMDocument:
        return self.registry["custom-documents/source.json"]


class _FakeWorkspaceClient:
    def __init__(self, registry: dict[str, AnythingLLMDocument]) -> None:
        self.registry = registry
        existing = AnythingLLMWorkspace(id="existing-id", slug="existing", name="既有工作区")
        self.workspaces: dict[str, AnythingLLMWorkspace] = {existing.slug: existing}
        self.bindings: dict[str, list[AnythingLLMDocument]] = {
            existing.slug: [registry["custom-documents/source.json"]]
        }
        self.deleted_slugs: list[str] = []

    def list_workspaces(self, **_: object) -> list[AnythingLLMWorkspace]:
        return list(self.workspaces.values())

    def list_documents(self, workspace_slug: str, **_: object) -> list[AnythingLLMDocument]:
        return list(self.bindings.get(workspace_slug, []))

    def create_workspace(self, name: str, **_: object) -> AnythingLLMWorkspace:
        workspace = AnythingLLMWorkspace(id=name, slug=name, name=name)
        self.workspaces[workspace.slug] = workspace
        self.bindings[workspace.slug] = []
        return workspace

    def update_embeddings(
        self,
        workspace_slug: str,
        *,
        adds: list[str],
        **_: object,
    ) -> AnythingLLMWorkspace:
        self.bindings[workspace_slug] = [self.registry[location] for location in adds]
        return self.workspaces[workspace_slug]

    def vector_search(self, *_: object, **__: object) -> list[AnythingLLMSource]:
        return [
            AnythingLLMSource(
                document_ref="document:temporary",
                text="尼米兹号属于尼米兹级航空母舰，装备 AN/SPS-48E 雷达。",
                score=0.91,
            )
        ]

    def delete_workspace(self, workspace_slug: str, **_: object) -> None:
        self.deleted_slugs.append(workspace_slug)
        self.workspaces.pop(workspace_slug, None)
        self.bindings.pop(workspace_slug, None)


class _FailingEmbeddingWorkspaceClient(_FakeWorkspaceClient):
    def update_embeddings(self, *_: object, **__: object) -> AnythingLLMWorkspace:
        raise RuntimeError("embedding failed")


def _source_page_content() -> str:
    body = "尼米兹号航空母舰的舰级、动力、雷达和导弹发射装置均由正文描述。" * 30
    return "\n".join(
        [
            "尼米兹号航空母舰",
            body,
            "2026/7/17 19:47",
            "尼米兹号航空母舰",
            "file:///C:/redacted/source...2/3换页后的正文事实。",
            "[12]",
            "AN/SPS-48E 与 RIM-116 均在正文中出现。",
            '1. "Reference title". Publisher. 2020.',
            "2. Another reference. ISBN 978-1-23456-789-0.",
        ]
    )


class Stage1D0RIsolatedReindexTests(unittest.TestCase):
    def _fixture(
        self,
        root: Path,
        *,
        workspace_type: type[_FakeWorkspaceClient] = _FakeWorkspaceClient,
    ) -> tuple[Path, _FakeWorkspaceClient, _FakeDocumentClient, bytes]:
        custom_root = root / "documents" / "custom-documents"
        custom_root.mkdir(parents=True)
        source_path = custom_root / "source.json"
        source_path.write_text(
            json.dumps({"pageContent": _source_page_content()}, ensure_ascii=False),
            encoding="utf-8",
        )
        original = source_path.read_bytes()
        source_document = AnythingLLMDocument(
            id="source-id",
            location="custom-documents/source.json",
            title="source.json",
            document_ref="document:source-id",
        )
        registry = {source_document.location: source_document}
        return (
            source_path,
            workspace_type(registry),
            _FakeDocumentClient(root, registry),
            original,
        )

    @staticmethod
    def _queries() -> tuple[CalibrationQuery, ...]:
        return (
            CalibrationQuery(
                query_id="ship-class",
                label="positive",
                text="字段：舰级名称",
                expected_terms=("尼米兹级",),
            ),
        )

    def test_cleaner_removes_reference_tail_and_recovers_page_suffix(self) -> None:
        cleaned = build_cleaned_retrieval_copy(
            _source_page_content(),
            source_hash="source-hash",
        )

        self.assertNotIn("Reference title", cleaned.text)
        self.assertNotIn("[12]", cleaned.text)
        self.assertNotIn("file:///", cleaned.text)
        self.assertIn("换页后的正文事实", cleaned.text)
        self.assertEqual(8, cleaned.reference_start_line)
        self.assertEqual(1, cleaned.recovered_page_suffix_lines)

    def test_success_cleans_only_owned_resources_and_restores_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source, workspaces, documents, original = self._fixture(root)

            output = run_isolated_calibration(
                workspaces,
                documents,
                source_json=source,
                storage_root=root,
                queries=self._queries(),
                top_n=10,
                user_id=1,
                readiness_timeout_seconds=1.0,
                poll_interval_seconds=0.01,
                execution_token=TOKEN,
            )

            self.assertEqual(original, source.read_bytes())
            self.assertEqual(["existing"], list(workspaces.workspaces))
            self.assertEqual([], list((root / "documents" / "custom-documents").glob(f"*{TOKEN}*")))
            self.assertTrue(output["cleanup"]["baselineSnapshotRestored"])
            self.assertFalse(output["existingResourcesModified"])
            self.assertFalse(output["equivalentToMhtmlMainContentV1"])
            self.assertEqual(1, output["queryCount"])

    def test_embedding_failure_still_cleans_workspace_and_document(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source, workspaces, documents, original = self._fixture(
                root,
                workspace_type=_FailingEmbeddingWorkspaceClient,
            )

            with self.assertRaisesRegex(RuntimeError, "embedding failed"):
                run_isolated_calibration(
                    workspaces,
                    documents,
                    source_json=source,
                    storage_root=root,
                    queries=self._queries(),
                    top_n=10,
                    user_id=1,
                    readiness_timeout_seconds=1.0,
                    poll_interval_seconds=0.01,
                    execution_token=TOKEN,
                )

            self.assertEqual(original, source.read_bytes())
            self.assertEqual(["existing"], list(workspaces.workspaces))
            self.assertEqual([], list((root / "documents" / "custom-documents").glob(f"*{TOKEN}*")))
            self.assertEqual(1, len(workspaces.deleted_slugs))
            self.assertEqual(1, len(documents.deleted_locations))

    def test_source_outside_storage_is_rejected_before_remote_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source, workspaces, documents, _ = self._fixture(root)
            outside = root / "outside.json"
            outside.write_bytes(source.read_bytes())

            with self.assertRaisesRegex(ValueError, "custom-documents"):
                run_isolated_calibration(
                    workspaces,
                    documents,
                    source_json=outside,
                    storage_root=root,
                    queries=self._queries(),
                    top_n=10,
                    user_id=1,
                    readiness_timeout_seconds=1.0,
                    poll_interval_seconds=0.01,
                    execution_token=TOKEN,
                )

            self.assertEqual([], workspaces.deleted_slugs)
            self.assertEqual([], documents.deleted_locations)

    def test_upload_response_cannot_turn_existing_document_into_cleanup_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source, workspaces, documents, original = self._fixture(root)
            colliding_documents = _CollidingDocumentClient(root, documents.registry)

            with self.assertRaisesRegex(RuntimeError, "既有绑定文档位置"):
                run_isolated_calibration(
                    workspaces,
                    colliding_documents,
                    source_json=source,
                    storage_root=root,
                    queries=self._queries(),
                    top_n=10,
                    user_id=1,
                    readiness_timeout_seconds=1.0,
                    poll_interval_seconds=0.01,
                    execution_token=TOKEN,
                )

            self.assertEqual(original, source.read_bytes())
            self.assertEqual([], colliding_documents.deleted_locations)
            self.assertEqual(["existing"], list(workspaces.workspaces))


if __name__ == "__main__":
    unittest.main()
