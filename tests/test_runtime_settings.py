import json
import os
import pathlib
import subprocess
import sys
import unittest

from tests import workspace_tempdir


ROOT = pathlib.Path(__file__).resolve().parents[1]


class RuntimeSettingsTests(unittest.TestCase):
    def _environment(self, runtime_dir: str) -> dict[str, str]:
        environment = os.environ.copy()
        environment["DOCSENSE_RUNTIME_DIR"] = runtime_dir
        for name in (
            "DOCSENSE_LLM_TASK_DB",
            "DOCSENSE_KNOWLEDGE_BASE_DB",
            "KNOWLEDGE_BASE_DB_PATH",
            "DOCSENSE_CHAT_DB",
            "FILE_DOWNLOAD_DIR",
            "DOCSENSE_OCR_CACHE_DIR",
            "DOCSENSE_MINERU_CACHE_DIR",
        ):
            environment.pop(name, None)
        return environment

    def test_absolute_runtime_root_derives_all_component_paths(self):
        with workspace_tempdir() as temp_dir:
            runtime_dir = pathlib.Path(temp_dir) / "runtime"
            script = (
                "import json; "
                "from app.services.core.settings import ("
                "RUNTIME_DIR, LLM_TASK_DB_PATH, KNOWLEDGE_BASE_DB_PATH, CHAT_DB_PATH, "
                "LLM_DOWNLOAD_DIR, OCR_CACHE_DIR, MINERU_CACHE_DIR, SQLITE_EXPORT_DIR"
                "); "
                "print(json.dumps([str(p) for p in ("
                "RUNTIME_DIR, LLM_TASK_DB_PATH, KNOWLEDGE_BASE_DB_PATH, CHAT_DB_PATH, "
                "LLM_DOWNLOAD_DIR, OCR_CACHE_DIR, MINERU_CACHE_DIR, SQLITE_EXPORT_DIR"
                ")]))"
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=self._environment(str(runtime_dir.resolve())),
                capture_output=True,
                text=True,
                check=True,
            )

            paths = [pathlib.Path(value) for value in json.loads(result.stdout)]
            expected = [
                runtime_dir,
                runtime_dir / "llm_tasks.sqlite3",
                runtime_dir / "knowledge_base.sqlite3",
                runtime_dir / "chat_sessions.sqlite3",
                runtime_dir / "llm_downloads",
                runtime_dir / "ocr_markdown",
                runtime_dir / "mineru_markdown",
                runtime_dir / "sqlite",
            ]
            self.assertEqual([path.resolve() for path in paths], [path.resolve() for path in expected])

    def test_explicit_runtime_root_must_be_absolute(self):
        result = subprocess.run(
            [sys.executable, "-c", "import app.services.core.settings"],
            cwd=ROOT,
            env=self._environment("relative/runtime"),
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DOCSENSE_RUNTIME_DIR必须配置为绝对路径", result.stderr)


if __name__ == "__main__":
    unittest.main()
