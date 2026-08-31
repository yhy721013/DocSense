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

    def test_absolute_runtime_root_ignores_removed_legacy_directory_settings(self):
        with workspace_tempdir() as temp_dir:
            runtime_dir = pathlib.Path(temp_dir) / "runtime"
            environment = self._environment(str(runtime_dir.resolve()))
            removed_directories = (
                pathlib.Path(temp_dir) / "removed-download-dir",
                pathlib.Path(temp_dir) / "removed-ocr-dir",
                pathlib.Path(temp_dir) / "removed-mineru-dir",
            )
            for name, path in zip(
                (
                    "FILE_DOWNLOAD_DIR",
                    "DOCSENSE_OCR_CACHE_DIR",
                    "DOCSENSE_MINERU_CACHE_DIR",
                ),
                removed_directories,
            ):
                environment[name] = str(path.resolve())
            script = (
                "import json; import sys; "
                "from app.services.core.config import load_llm_integration_config, load_ocr_config; "
                "from app.services.core.settings import ("
                "RUNTIME_DIR, LLM_TASK_DB_PATH, KNOWLEDGE_BASE_DB_PATH, CHAT_DB_PATH, "
                "SQLITE_EXPORT_DIR"
                "); "
                "llm = load_llm_integration_config(); ocr = load_ocr_config(); "
                "sys.stdout.write(json.dumps([str(p) for p in ("
                "RUNTIME_DIR, LLM_TASK_DB_PATH, KNOWLEDGE_BASE_DB_PATH, CHAT_DB_PATH, "
                "SQLITE_EXPORT_DIR"
                ")] + [hasattr(llm, 'download_dir'), hasattr(ocr, 'cache_dir'), "
                "hasattr(ocr, 'mineru_cache_dir')]))"
            )
            result = subprocess.run(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=True,
            )

            values = json.loads(result.stdout)
            paths = [pathlib.Path(value) for value in values[:5]]
            expected = [
                runtime_dir,
                runtime_dir / "llm_tasks.sqlite3",
                runtime_dir / "knowledge_base.sqlite3",
                runtime_dir / "chat_sessions.sqlite3",
                runtime_dir / "sqlite",
            ]
            self.assertEqual(
                [path.resolve() for path in paths],
                [path.resolve() for path in expected],
            )
            self.assertEqual([False, False, False], values[5:])
            for deprecated_directory in removed_directories:
                self.assertFalse(
                    deprecated_directory.exists(),
                    f"已删除的目录环境变量不应产生文件系统副作用: {deprecated_directory}",
                )
            self.assertTrue(runtime_dir.is_dir())
            self.assertTrue((runtime_dir / "sqlite").is_dir())

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
