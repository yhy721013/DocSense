from __future__ import annotations

import unittest
from pathlib import Path

from app.services.core.config import (
    ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE,
    ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD,
    ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD,
    ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE,
    ANALYSIS_RUNTIME_MODE_SINGLE_INSTANCE,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MODE_DEFAULTS = {
    "DOCSENSE_ANALYSIS_CLASSIFICATION_MODE": (
        ANALYSIS_CLASSIFICATION_MODE_TOPK_TWO_STAGE
    ),
    "DOCSENSE_ANALYSIS_FILENAME_CONSTRAINT_MODE": (
        ANALYSIS_FILENAME_CONSTRAINT_MODE_SCOPE_GUARD
    ),
    "DOCSENSE_ANALYSIS_DATA_STANDARD_MODE": (
        ANALYSIS_DATA_STANDARD_MODE_SCOPE_GUARD
    ),
    "DOCSENSE_ANALYSIS_IDENTITY_RESELECT_MODE": (
        ANALYSIS_IDENTITY_RESELECT_MODE_ENFORCE
    ),
}

# 1F-5A 的后台运行参数必须在开发样例与 Docker 样例中同时显式固定。这样可以防止某一
# 部署入口遗漏严格的 single_instance 门禁，或让 Callback Guard 的租约预算随镜像环境漂移。
DISPATCHER_DEFAULTS = {
    "DOCSENSE_ANALYSIS_RUNTIME_MODE": ANALYSIS_RUNTIME_MODE_SINGLE_INSTANCE,
    "DOCSENSE_ANALYSIS_DISPATCH_SCAN_INTERVAL_SECONDS": "1",
    "DOCSENSE_ANALYSIS_DISPATCH_BATCH_SIZE": "50",
    "DOCSENSE_ANALYSIS_DISPATCH_RETRY_BASE_SECONDS": "5",
    "DOCSENSE_ANALYSIS_DISPATCH_RETRY_MAX_SECONDS": "300",
    "DOCSENSE_ANALYSIS_RESOURCE_SWEEP_INTERVAL_SECONDS": "30",
    "DOCSENSE_ANALYSIS_RESOURCE_SWEEP_BATCH_SIZE": "50",
    "DOCSENSE_ANALYSIS_RESOURCE_CLOSE_RUNNING_GRACE_SECONDS": "300",
    "DOCSENSE_ANALYSIS_RUNNING_ALERT_SECONDS": "30",
    "DOCSENSE_ANALYSIS_STOP_TIMEOUT_SECONDS": "5",
    "DOCSENSE_ANALYSIS_CALLBACK_HTTP_TIMEOUT_SECONDS": "10",
    "DOCSENSE_ANALYSIS_CALLBACK_LEASE_SECONDS": "30",
}


def _read_env_assignments(path: Path) -> dict[str, list[str]]:
    assignments: dict[str, list[str]] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        assignments.setdefault(name.strip(), []).append(value.strip())
    return assignments


class AnalysisDeploymentConfigTests(unittest.TestCase):
    def test_example_and_docker_env_pin_the_same_analysis_defaults(self):
        for relative_path in (".env.example", "docker/.env.docker"):
            with self.subTest(relative_path=relative_path):
                assignments = _read_env_assignments(
                    REPOSITORY_ROOT / relative_path
                )
                for name, expected_value in {
                    **MODE_DEFAULTS,
                    **DISPATCHER_DEFAULTS,
                }.items():
                    self.assertEqual(
                        assignments.get(name),
                        [expected_value],
                        f"{relative_path} 中的 {name} 必须唯一且显式",
                    )

    def test_docker_context_excludes_committed_test_documents(self):
        patterns = {
            line.strip()
            for line in (
                REPOSITORY_ROOT / ".dockerignore"
            ).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }

        self.assertIn("测试文件/", patterns)

    def test_offline_guide_documents_modes_and_container_recreation(self):
        guide = (
            REPOSITORY_ROOT / "docker/deploy/README-OFFLINE.md"
        ).read_text(encoding="utf-8")

        for name in {**MODE_DEFAULTS, **DISPATCHER_DEFAULTS}:
            self.assertIn(name, guide)
        self.assertIn("topk_single", guide)
        self.assertIn("single_instance", guide)
        self.assertIn("不得手工制造并行双跑", guide)
        self.assertIn("--force-recreate docsense", guide)


if __name__ == "__main__":
    unittest.main()
