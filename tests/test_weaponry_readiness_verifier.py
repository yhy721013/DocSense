"""Weaponry 真实生产证明生成器的离线生命周期测试。"""

from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import patch

from app.integrations.anythingllm.models import (
    AnythingLLMAnswer,
    AnythingLLMDocument,
    AnythingLLMSource,
    AnythingLLMThread,
    AnythingLLMWorkspace,
)
from app.modules.weaponry.adapters import (
    WeaponryRuntimeConfig,
    evaluate_weaponry_production_gate,
)
from scripts.verify_weaponry_production_readiness import (
    load_ingested_identity_catalog,
    verify_and_build_attestation,
)
from tests import workspace_tempdir


def _config() -> WeaponryRuntimeConfig:
    return WeaponryRuntimeConfig(
        runtime_mode="single_instance",
        scan_interval_seconds=0.1,
        accepted_batch_size=50,
        dispatch_failure_retry_seconds=1.0,
        maintenance_interval_seconds=1.0,
        maintenance_limit=50,
        running_sample_limit=10,
        stop_timeout_seconds=1.0,
        cleanup_http_timeout_seconds=1.0,
        cleanup_lease_seconds=7.0,
        provider_fingerprint="provider-test",
        embedding_fingerprint="embedding-test",
        document_processing_fingerprint="processing-test",
        extraction_model_fingerprint="model-test",
    )


class _WorkspaceClient:
    def __init__(self) -> None:
        self._base_document = AnythingLLMDocument(
            id="provider-a",
            location="custom-documents/a-provider-a.json",
            title="测试装备文档",
            document_ref="document:provider-a",
            raw_document_id="provider-a",
        )
        self._workspaces = {
            "existing": AnythingLLMWorkspace("existing", "existing", "existing")
        }
        self._documents = {"existing": [self._base_document]}

    def list_workspaces(self, *, user_id=None):
        return list(self._workspaces.values())

    def list_documents(self, workspace_slug, *, user_id=None):
        return list(self._documents.get(workspace_slug, ()))

    def create_workspace(self, name, *, settings=None, user_id=None):
        workspace = AnythingLLMWorkspace(name, name, name)
        self._workspaces[name] = workspace
        self._documents[name] = []
        return workspace

    def update_embeddings(self, workspace_slug, *, adds=(), deletes=(), user_id=None):
        if adds:
            self._documents[workspace_slug] = [self._base_document]

    def vector_search(
        self,
        workspace_slug,
        query,
        *,
        top_n=None,
        score_threshold=None,
        user_id=None,
    ):
        return [
            AnythingLLMSource(
                document_ref="name:a.pdf",
                text="测试装备文档中的完整候选证据正文。",
                score=0.91,
                score_present=True,
                score_valid=True,
                metadata=MappingProxyType(
                    {"url": self._base_document.location}
                ),
            )
        ]

    def delete_workspace(self, workspace_slug, *, user_id=None):
        self._workspaces.pop(workspace_slug)
        self._documents.pop(workspace_slug)


class _ThreadClient:
    def create_thread(self, workspace_slug, name, *, user_id=None):
        return AnythingLLMThread("thread", "thread")

    def ask(
        self,
        workspace_slug,
        thread_slug,
        prompt,
        *,
        mode,
        user_id=None,
        document_ids=None,
    ):
        nonce = prompt.rsplit("\n", 1)[-1]
        return AnythingLLMAnswer(nonce, nonce, ())

    def delete_thread(self, workspace_slug, thread_slug, *, user_id=None):
        return None


class WeaponryReadinessVerifierTests(unittest.TestCase):
    def test_real_shape_evidence_is_bound_and_all_temporary_resources_are_removed(self) -> None:
        workspaces = _WorkspaceClient()
        client = SimpleNamespace(
            workspaces=workspaces,
            threads=_ThreadClient(),
        )
        with patch(
            "scripts.verify_weaponry_production_readiness."
            "load_weaponry_runtime_config",
            return_value=_config(),
        ):
            attestation = verify_and_build_attestation(
                client,
                environment="offline-verifier-test",
                user_id=1,
                top_n=8,
                readiness_timeout_seconds=1.0,
                valid_for_seconds=3600.0,
                ingested_ref_by_location={
                    "custom-documents/a-provider-a.json": "name:a-provider-a.json"
                },
            )

        self.assertEqual(["existing"], [item.slug for item in workspaces.list_workspaces()])
        self.assertTrue(
            attestation["evidence"]["sourceIdentity"]["passed"]
        )
        self.assertEqual(
            ["url"],
            attestation["evidence"]["sourceIdentity"]["identityFields"],
        )

        with workspace_tempdir() as directory:
            path = Path(directory) / "attestation.json"
            path.write_text(json.dumps(attestation), encoding="utf-8")
            result = evaluate_weaponry_production_gate(
                attestation_path=str(path),
                profile_id=attestation["profileId"],
                fingerprints=attestation["fingerprints"],
            )
        self.assertTrue(result.ready)

    def test_identity_catalog_reads_authoritative_lineage_without_writing(self) -> None:
        import sqlite3

        with workspace_tempdir() as directory:
            path = Path(directory) / "knowledge.sqlite3"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    """
                    CREATE TABLE documents (
                        doc_path TEXT NOT NULL,
                        ingested_file_name TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO documents VALUES (?, ?)",
                    ("custom-documents/a.json", "a.mhtml"),
                )
                connection.commit()

            before = path.stat().st_size
            catalog = load_ingested_identity_catalog(path)
            after = path.stat().st_size

        self.assertEqual(
            {"custom-documents/a.json": "name:a.mhtml"},
            catalog,
        )
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
