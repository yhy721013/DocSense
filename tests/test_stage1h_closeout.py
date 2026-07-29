"""阶段 1H-7 关闭验收：共享流水线的 50 任务隔离与 Artifact 保留语义。"""

from __future__ import annotations

import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from app.modules.document_processing import (
    LegacyOfficeConfig,
    LibreOfficeLegacyOfficePreparer,
)
from app.modules.document_processing.adapters import (
    FIFOCapacityAdapter,
    LocalArtifactStoreAdapter,
    LocalDocumentPreparationAdapter,
    LocalDocumentPreparationRequest,
    SQLiteProcessingRecordAdapter,
)
from app.modules.tasks.domain import TaskId


class Stage1HCloseoutTests(unittest.TestCase):
    """验证同一组本地 Adapter 在并发任务间不共享路径、内容或处理事实。"""

    def test_fifty_tasks_keep_artifacts_and_processing_facts_isolated(
        self,
    ) -> None:
        """50 个线程同时起跑；每个任务只能读回自己的 source/prepared Artifact。"""

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source_root = root / "sources"
            source_root.mkdir()
            artifact_store = LocalArtifactStoreAdapter(root / "artifacts")
            preparer = LocalDocumentPreparationAdapter(
                artifact_store=artifact_store,
                records=SQLiteProcessingRecordAdapter(
                    root / "processing.sqlite3"
                ),
                resource=FIFOCapacityAdapter(1),
                legacy_office_preparer=LibreOfficeLegacyOfficePreparer(
                    LegacyOfficeConfig.disabled(
                        jobs_root=root / "office-jobs"
                    )
                ),
                materialization_root=root / "materializations",
                legacy_policy_fingerprint="stage1h-closeout-policy-v1",
                ocr_languages="chi_sim+eng",
                ocr_dpi=300,
            )
            expected_payloads: dict[str, bytes] = {}
            for index in range(50):
                task_value = f"stage1h-closeout-{index:02d}"
                payload = f"task={task_value};payload={index:02d}".encode()
                (source_root / f"{index:02d}.txt").write_bytes(payload)
                expected_payloads[task_value] = payload

            barrier = threading.Barrier(50)

            def prepare(index: int):
                task_value = f"stage1h-closeout-{index:02d}"
                barrier.wait(timeout=20)
                result = preparer.prepare(
                    LocalDocumentPreparationRequest(
                        task_id=TaskId(task_value),
                        source_path=source_root / f"{index:02d}.txt",
                        logical_step="input",
                        trace_id=f"trace-{index:02d}",
                    )
                )
                with artifact_store.open_reader(
                    result.source_artifact
                ) as source_reader:
                    source_payload = source_reader.read()
                with artifact_store.open_reader(
                    result.prepared_artifact
                ) as prepared_reader:
                    prepared_payload = prepared_reader.read()
                return (
                    task_value,
                    result.source_artifact,
                    result.prepared_artifact,
                    source_payload,
                    prepared_payload,
                )

            with ThreadPoolExecutor(max_workers=50) as executor:
                results = tuple(executor.map(prepare, range(50)))

            # 有效 Artifact 在 1H 不做即时删除：Processing Record 与 lineage
            # 仍需依赖这些不可变内容完成幂等校验和后续恢复。
            self.assertEqual(50, len(results))
            self.assertEqual(
                50,
                len({item[1].artifact_id for item in results}),
            )
            self.assertEqual(
                50,
                len({item[2].artifact_id for item in results}),
            )
            self.assertEqual(
                50,
                len({item[1].task_id for item in results}),
            )
            for (
                task_value,
                source_artifact,
                prepared_artifact,
                source_payload,
                prepared_payload,
            ) in results:
                with self.subTest(task_id=task_value):
                    self.assertEqual(
                        TaskId(task_value),
                        source_artifact.task_id,
                    )
                    self.assertEqual(
                        TaskId(task_value),
                        prepared_artifact.task_id,
                    )
                    self.assertEqual(
                        expected_payloads[task_value],
                        source_payload,
                    )
                    self.assertEqual(
                        expected_payloads[task_value],
                        prepared_payload,
                    )
                    self.assertTrue(
                        artifact_store.verify(source_artifact)
                    )
                    self.assertTrue(
                        artifact_store.verify(prepared_artifact)
                    )

            self.assertEqual(
                100,
                len(tuple(artifact_store.root.rglob("*.bin"))),
            )
            self.assertEqual(
                (),
                tuple(artifact_store.root.rglob("*.part")),
            )


if __name__ == "__main__":
    unittest.main()
