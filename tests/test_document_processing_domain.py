"""阶段 1H-1 文档处理领域对象门禁。"""

from __future__ import annotations

import dataclasses
import hashlib
import unittest

from app.modules.document_processing.domain import (
    ArtifactKind,
    ArtifactMetadata,
    ArtifactRef,
    DocumentProcessingRequest,
    DocumentProcessingResult,
    DocumentRepresentation,
    ProcessingOutcome,
    ProcessingProfile,
    derive_artifact_id,
)
from app.modules.tasks.domain import TaskId


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _source(task_id: TaskId) -> ArtifactRef:
    return ArtifactRef(
        task_id=task_id,
        artifact_id=_digest(f"{task_id}:source"),
        step_key=_digest(f"{task_id}:source-step"),
        kind=ArtifactKind.SOURCE,
        representation=DocumentRepresentation.ORIGINAL,
        metadata=ArtifactMetadata(
            media_type="text/plain",
            size_bytes=5,
            sha256=_digest("hello"),
        ),
    )


class DocumentProcessingDomainTests(unittest.TestCase):
    def test_profile_roundtrip_is_strict_and_canonical(self) -> None:
        profile = ProcessingProfile.create(
            processor_id="plain-text",
            processor_fingerprint="plain-text-v1",
            target_representation=DocumentRepresentation.TEXT,
            parameters={"encoding": "utf-8", "normalize": True},
        )
        rebuilt = ProcessingProfile.from_dict(profile.to_dict())

        self.assertEqual(profile, rebuilt)
        self.assertEqual(
            profile.profile_id,
            ProcessingProfile.create(
                processor_id="plain-text",
                processor_fingerprint="plain-text-v1",
                target_representation=DocumentRepresentation.TEXT,
                parameters={"normalize": True, "encoding": "utf-8"},
            ).profile_id,
        )
        invalid = profile.to_dict()
        invalid["unexpected"] = True
        with self.assertRaisesRegex(ValueError, "字段集合"):
            ProcessingProfile.from_dict(invalid)

    def test_step_and_artifact_id_are_deterministic(self) -> None:
        task_id = TaskId("stage1h-domain")
        profile = ProcessingProfile.create(
            processor_id="plain-text",
            processor_fingerprint="plain-text-v1",
            target_representation=DocumentRepresentation.TEXT,
        )
        first = DocumentProcessingRequest(
            task_id=task_id,
            step_id="prepare",
            source_artifact=_source(task_id),
            profile=profile,
            trace_id="trace-a",
        )
        second = DocumentProcessingRequest(
            task_id=task_id,
            step_id="prepare",
            source_artifact=_source(task_id),
            profile=profile,
            trace_id="trace-b",
        )

        self.assertEqual(first.step_key, second.step_key)
        self.assertEqual(
            derive_artifact_id(
                step_key=first.step_key,
                kind=ArtifactKind.PREPARED,
                representation=DocumentRepresentation.TEXT,
            ),
            derive_artifact_id(
                step_key=second.step_key,
                kind=ArtifactKind.PREPARED,
                representation=DocumentRepresentation.TEXT,
            ),
        )

    def test_values_are_immutable_and_validate_integrity_fields(self) -> None:
        metadata = ArtifactMetadata(
            media_type="TEXT/PLAIN",
            size_bytes=0,
            sha256=_digest(""),
        )
        self.assertEqual("text/plain", metadata.media_type)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            metadata.size_bytes = 1  # type: ignore[misc]
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            ArtifactMetadata(
                media_type="text/plain",
                size_bytes=1,
                sha256="not-a-digest",
            )
        with self.assertRaisesRegex(ValueError, "media_type"):
            ArtifactMetadata(
                media_type=" ",
                size_bytes=1,
                sha256=_digest("x"),
            )

    def test_non_success_result_cannot_expose_uncommitted_artifact(self) -> None:
        task_id = TaskId("stage1h-result")
        with self.assertRaisesRegex(ValueError, "非成功"):
            DocumentProcessingResult(
                outcome=ProcessingOutcome.OUTCOME_UNKNOWN,
                step_key=_digest("step"),
                artifact=_source(task_id),
                error_code="unknown",
            )


if __name__ == "__main__":
    unittest.main()
