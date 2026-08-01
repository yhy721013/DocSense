from __future__ import annotations

from contextlib import closing
from copy import deepcopy
import csv
import json
import os
import shutil
import sqlite3
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import requests

from scripts import run_llm_weaponry_directory as weaponry_directory_runner


ROOT_DIR = Path(__file__).resolve().parent.parent
IS_WINDOWS = os.name == "nt"

# 自动检测可用 shell
if IS_WINDOWS:
    _pwsh = shutil.which("pwsh") or shutil.which("powershell")
    SHELL_BIN: str | None = _pwsh
    SHELL_AVAILABLE = SHELL_BIN is not None
else:
    _zsh = shutil.which("zsh")
    SHELL_BIN = _zsh
    SHELL_AVAILABLE = SHELL_BIN is not None


def _script_ext() -> str:
    return ".ps1" if IS_WINDOWS else ".sh"


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class RequestRecorderHandler(BaseHTTPRequestHandler):
    last_request: dict | None = None

    def do_POST(self) -> None:  # noqa: N802
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length).decode("utf-8")
        type(self).last_request = {
            "path": self.path,
            "body": body,
            "content_type": self.headers.get("Content-Type"),
        }
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


@unittest.skipUnless(SHELL_AVAILABLE, f"所需 shell 不可用（{'pwsh/powershell' if IS_WINDOWS else 'zsh'}）")
class LocalScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.processes: list[subprocess.Popen[str]] = []
        self.servers: list[ThreadingHTTPServer] = []
        self.server_threads: list[threading.Thread] = []

    def tearDown(self) -> None:
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for thread in self.server_threads:
            thread.join(timeout=5)
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    def _start_recording_server(self) -> tuple[ThreadingHTTPServer, int]:
        RequestRecorderHandler.last_request = None
        port = find_free_port()
        server = ThreadingHTTPServer(("127.0.0.1", port), RequestRecorderHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append(server)
        self.server_threads.append(thread)
        return server, port

    def _run_script(
        self,
        relative_path: str,
        *args: str,
        env: dict[str, str] | None = None,
        unset_env: set[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        script_path = ROOT_DIR / relative_path
        script_env = os.environ.copy()
        for name in unset_env or set():
            script_env.pop(name, None)
        if env:
            script_env.update(env)

        assert SHELL_BIN is not None
        if IS_WINDOWS:
            cmd = [SHELL_BIN, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script_path), *args]
        else:
            cmd = [SHELL_BIN, str(script_path), *args]

        return subprocess.run(
            cmd,
            cwd=ROOT_DIR,
            env=script_env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

    def _require_fixture(self, relative_path: str) -> Path:
        """本地联调请求受 .gitignore 排除；干净克隆缺失时应明确跳过。"""

        fixture = ROOT_DIR / relative_path
        if not fixture.is_file():
            self.skipTest(f"本地联调夹具未提供: {relative_path}")
        return fixture

    def _start_app_server(self, port: int) -> subprocess.Popen[str]:
        env = os.environ.copy()
        env.update(
            {
                "APP_HOST": "127.0.0.1",
                "APP_PORT": str(port),
                "APP_DEBUG": "false",
            }
        )
        process = subprocess.Popen(
            [sys.executable, "run.py"],
            cwd=ROOT_DIR,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.processes.append(process)

        deadline = time.time() + 15
        last_error = None
        while time.time() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=5)
                self.fail(f"run.py 提前退出，stdout={stdout!r}, stderr={stderr!r}")
            try:
                response = requests.get(f"http://127.0.0.1:{port}/llm/progress", timeout=1)
                if response.status_code != 404:
                    return process
            except requests.RequestException as exc:
                last_error = exc
            time.sleep(0.2)

        self.fail(f"run.py 未在预期时间内启动成功: {last_error!r}")

    def _create_runner_task_db(self, task_db: Path) -> None:
        # ``Connection.__exit__`` 只提交/回滚而不关闭句柄；Windows 会因此无法删除
        # TemporaryDirectory 中的 SQLite 文件，测试必须显式关闭连接。
        with closing(sqlite3.connect(task_db)) as conn, conn:
            conn.execute(
                """
                CREATE TABLE llm_tasks (
                    business_type TEXT NOT NULL,
                    business_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress REAL NOT NULL,
                    message TEXT NOT NULL,
                    result_payload TEXT,
                    callback_status TEXT NOT NULL,
                    callback_attempts INTEGER NOT NULL,
                    last_callback_error TEXT NOT NULL,
                    execution_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (business_type, business_key)
                )
                """
            )

    def _valid_forced_empty_contract_result(self) -> dict:
        def empty_field(name: str) -> dict:
            return {
                "fieldName": name,
                "fieldType": "INPUT",
                "analyseData": "",
                "analyseDataSource": [
                    deepcopy(weaponry_directory_runner.STANDARD_EMPTY_DATA_SOURCE)
                ],
            }

        real_source = {
            "content": "USS Nimitz (CVN-68)",
            "source": "nimitz.pdf",
            "time": "",
            "fileName": "nimitz.pdf",
            "rows": ["USS Nimitz (CVN-68)"],
            "translate": "",
        }
        fields = [
            empty_field(name)
            for name in weaponry_directory_runner.FORCED_EMPTY_CONTRACT_INPUT_FIELDS
        ]
        fields.append(
            {
                "fieldName": weaponry_directory_runner.FORCED_EMPTY_CONTRACT_CONTROL_FIELD,
                "fieldType": "INPUT",
                "analyseData": "CVN-68",
                "analyseDataSource": [deepcopy(real_source)],
            }
        )
        mixed_row = [
            empty_field(name)
            for name in weaponry_directory_runner.FORCED_EMPTY_CONTRACT_INPUT_FIELDS
        ]
        mixed_row.extend(
            [
                {
                    "fieldName": "单舰名称",
                    "fieldType": "INPUT",
                    "analyseData": "USS Nimitz",
                    "analyseDataSource": [deepcopy(real_source)],
                },
                {
                    "fieldName": "舷号",
                    "fieldType": "INPUT",
                    "analyseData": "CVN-68",
                    "analyseDataSource": [deepcopy(real_source)],
                },
            ]
        )
        fields.extend(
            [
                {
                    "fieldName": (
                        weaponry_directory_runner
                        .FORCED_EMPTY_CONTRACT_MIXED_TABLE_FIELD
                    ),
                    "fieldType": "TABLE",
                    "tableFieldList": [mixed_row],
                },
                {
                    "fieldName": (
                        weaponry_directory_runner
                        .FORCED_EMPTY_CONTRACT_RESERVED_TABLE_FIELD
                    ),
                    "fieldType": "TABLE",
                    "tableFieldList": [
                        [
                            empty_field(name)
                            for name in (
                                weaponry_directory_runner
                                .FORCED_EMPTY_CONTRACT_INPUT_FIELDS
                            )
                        ]
                    ],
                },
            ]
        )
        return {
            "code": 200,
            "data": {
                "weaponryTemplateFieldList": fields,
            },
        }

    def test_start_test_file_server_serves_fixture_file(self) -> None:
        port = find_free_port()
        expected_bytes = (ROOT_DIR / "tests/fixtures/files/sample.txt").read_bytes()

        assert SHELL_BIN is not None
        if IS_WINDOWS:
            cmd = [SHELL_BIN, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                   str(ROOT_DIR / f"scripts/start_test_file_server{_script_ext()}"), str(port), "tests/fixtures/files"]
        else:
            cmd = [SHELL_BIN, str(ROOT_DIR / "scripts/start_test_file_server.sh"), str(port), "tests/fixtures/files"]

        process = subprocess.Popen(
            cmd,
            cwd=ROOT_DIR,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.processes.append(process)

        deadline = time.time() + 10
        last_error = None
        while time.time() < deadline:
            if process.poll() is not None:
                stdout, stderr = process.communicate(timeout=5)
                self.fail(f"静态文件服务提前退出，stdout={stdout!r}, stderr={stderr!r}")
            try:
                response = requests.get(f"http://127.0.0.1:{port}/sample.txt", timeout=1)
                if response.ok:
                    self.assertEqual(response.content, expected_bytes)
                    return
            except requests.RequestException as exc:
                last_error = exc
            time.sleep(0.2)

        self.fail(f"静态文件服务未成功响应: {last_error!r}")

    def test_analysis_shell_script_posts_fixture_to_expected_path(self) -> None:
        payload = self._require_fixture("tests/fixtures/llm/analysis_request.json")
        _, port = self._start_recording_server()

        result = self._run_script(f"scripts/test_llm_analysis{_script_ext()}", f"http://127.0.0.1:{port}", str(payload))

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIsNotNone(RequestRecorderHandler.last_request)
        self.assertEqual(RequestRecorderHandler.last_request["path"], "/llm/analysis")
        # PowerShell 的 Invoke-WebRequest 发送的 body 可能与原文件内容存在末尾换行差异
        posted_body = RequestRecorderHandler.last_request["body"].strip()
        expected_body = payload.read_text(encoding="utf-8").strip()
        self.assertEqual(posted_body, expected_body)

    def test_report_shell_script_posts_fixture_to_expected_path(self) -> None:
        payload = self._require_fixture("tests/fixtures/llm/report_request.json")
        _, port = self._start_recording_server()

        result = self._run_script(f"scripts/test_llm_report{_script_ext()}", f"http://127.0.0.1:{port}", str(payload))

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIsNotNone(RequestRecorderHandler.last_request)
        self.assertEqual(RequestRecorderHandler.last_request["path"], "/llm/generate-report")
        posted_body = RequestRecorderHandler.last_request["body"].strip()
        expected_body = payload.read_text(encoding="utf-8").strip()
        self.assertEqual(posted_body, expected_body)

    def test_weaponry_shell_script_posts_fixture_to_expected_path(self) -> None:
        payload = self._require_fixture("tests/fixtures/llm/weaponry_request.json")
        _, port = self._start_recording_server()

        result = self._run_script(f"scripts/test_llm_weaponry{_script_ext()}", f"http://127.0.0.1:{port}", str(payload))

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIsNotNone(RequestRecorderHandler.last_request)
        self.assertEqual(RequestRecorderHandler.last_request["path"], "/llm/weaponry")
        posted_body = RequestRecorderHandler.last_request["body"].strip()
        expected_body = payload.read_text(encoding="utf-8").strip()
        self.assertEqual(posted_body, expected_body)

    def test_weaponry_directory_script_dry_run_writes_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "weaponry-directory-dry-run"
            result = self._run_script(
                f"scripts/test_llm_weaponry_directory{_script_ext()}",
                "tests/fixtures/files",
                "--pattern",
                "JFS_5701-JFS_-06-Mar-2024.pdf",
                "--dry-run",
                "--output-dir",
                str(output_dir),
                "--architecture-base",
                "998570100",
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            manifest_path = output_dir / "qwen3-4b-new_manifest.json"
            self.assertTrue(manifest_path.exists())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertTrue(manifest["dry_run"])
            self.assertEqual(manifest["files"][0]["fileName"], "JFS_5701-JFS_-06-Mar-2024.pdf")
            self.assertEqual(manifest["files"][0]["architectureId"], 998570100)
            # 未显式传入 DB 时，子进程会读取自己的 .env；父测试进程可能已在
            # 更早的模块导入阶段冻结不同设置，因此这里只验证清单记录的是明确
            # 绝对 SQLite 路径。显式路径保真由下一条独立测试负责。
            knowledge_db = Path(manifest["knowledge_base_db"])
            self.assertTrue(knowledge_db.is_absolute())
            self.assertEqual(".sqlite3", knowledge_db.suffix)
            self.assertFalse(manifest["verify_forced_empty_contract"])
            self.assertEqual(manifest["field_count"], 75)
            self.assertEqual(manifest["extractable_field_count"], 70)
            self.assertEqual(
                set(manifest["forced_empty_fields"]),
                set(weaponry_directory_runner.FORCED_EMPTY_FIELD_NAMES),
            )

    def test_weaponry_directory_wrapper_preserves_explicit_database_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_root = Path(tmpdir)
            runtime_dir = temp_root / "caller-runtime"
            task_db = temp_root / "caller-task.sqlite3"
            knowledge_db = temp_root / "caller-knowledge.sqlite3"
            legacy_db = temp_root / "caller-legacy-knowledge.sqlite3"
            output_dir = temp_root / "modern-output"
            explicit_env = {
                "DOCSENSE_RUNTIME_DIR": str(runtime_dir),
                "DOCSENSE_LLM_TASK_DB": str(task_db),
                "DOCSENSE_KNOWLEDGE_BASE_DB": str(knowledge_db),
                "KNOWLEDGE_BASE_DB_PATH": str(legacy_db),
            }

            result = self._run_script(
                f"scripts/test_llm_weaponry_directory{_script_ext()}",
                "tests/fixtures/files",
                "--pattern",
                "sample.txt",
                "--dry-run",
                "--output-dir",
                str(output_dir),
                "--architecture-base",
                "998570300",
                env=explicit_env,
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            manifest = json.loads(
                (output_dir / "qwen3-4b-new_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(manifest["runtime_dir"], str(runtime_dir.resolve()))
            self.assertEqual(manifest["task_db"], str(task_db.resolve()))
            self.assertEqual(
                manifest["knowledge_base_db"],
                str(knowledge_db.resolve()),
            )

            legacy_output_dir = temp_root / "legacy-output"
            legacy_result = self._run_script(
                f"scripts/test_llm_weaponry_directory{_script_ext()}",
                "tests/fixtures/files",
                "--pattern",
                "sample.txt",
                "--dry-run",
                "--output-dir",
                str(legacy_output_dir),
                "--architecture-base",
                "998570400",
                env={
                    "DOCSENSE_RUNTIME_DIR": str(runtime_dir),
                    "DOCSENSE_LLM_TASK_DB": str(task_db),
                    "KNOWLEDGE_BASE_DB_PATH": str(legacy_db),
                },
                unset_env={"DOCSENSE_KNOWLEDGE_BASE_DB"},
            )

            self.assertEqual(legacy_result.returncode, 0, msg=legacy_result.stderr)
            legacy_manifest = json.loads(
                (legacy_output_dir / "qwen3-4b-new_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                legacy_manifest["knowledge_base_db"],
                str(legacy_db.resolve()),
            )

    def test_weaponry_directory_default_and_contract_probe_payloads(self) -> None:
        default_payload = weaponry_directory_runner.build_weaponry_payload(
            998570100,
            "nimitz.pdf",
            1772442376645740,
        )
        default_fields = default_payload["params"]["weaponryTemplateFieldList"]
        self.assertEqual(weaponry_directory_runner.FIELD_NAMES, [
            field["fieldName"] for field in default_fields
        ])
        self.assertTrue(all(field["fieldType"] == "INPUT" for field in default_fields))

        probe_payload = weaponry_directory_runner.build_weaponry_payload(
            998570100,
            "nimitz.pdf",
            1772442376645740,
            verify_forced_empty_contract=True,
        )
        probe_fields = probe_payload["params"]["weaponryTemplateFieldList"]
        self.assertEqual(
            [
                *weaponry_directory_runner.FORCED_EMPTY_CONTRACT_INPUT_FIELDS,
                weaponry_directory_runner.FORCED_EMPTY_CONTRACT_CONTROL_FIELD,
                weaponry_directory_runner.FORCED_EMPTY_CONTRACT_MIXED_TABLE_FIELD,
                weaponry_directory_runner.FORCED_EMPTY_CONTRACT_RESERVED_TABLE_FIELD,
            ],
            [field["fieldName"] for field in probe_fields],
        )
        self.assertEqual(
            list(weaponry_directory_runner.FORCED_EMPTY_CONTRACT_MIXED_COLUMNS),
            [
                cell["fieldName"]
                for cell in probe_fields[6]["tableFieldList"][0]
            ],
        )
        self.assertEqual(
            list(weaponry_directory_runner.FORCED_EMPTY_CONTRACT_INPUT_FIELDS),
            [
                cell["fieldName"]
                for cell in probe_fields[7]["tableFieldList"][0]
            ],
        )

    def test_weaponry_directory_contract_probe_dry_run_uses_minimal_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "weaponry-contract-probe-dry-run"
            result = self._run_script(
                f"scripts/test_llm_weaponry_directory{_script_ext()}",
                "tests/fixtures/files",
                "--pattern",
                "JFS_5701-JFS_-06-Mar-2024.pdf",
                "--dry-run",
                "--verify-forced-empty-contract",
                "--output-dir",
                str(output_dir),
                "--architecture-base",
                "998570200",
            )

            self.assertEqual(result.returncode, 0, msg=result.stderr)
            manifest = json.loads(
                (output_dir / "qwen3-4b-new_manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(manifest["verify_forced_empty_contract"])
            self.assertEqual(manifest["field_count"], 8)
            self.assertEqual(manifest["input_field_count"], 6)
            self.assertEqual(manifest["extractable_field_count"], 1)
            self.assertEqual(
                manifest["input_field_names"],
                [
                    *weaponry_directory_runner.FORCED_EMPTY_CONTRACT_INPUT_FIELDS,
                    weaponry_directory_runner.FORCED_EMPTY_CONTRACT_CONTROL_FIELD,
                ],
            )
            with (output_dir / "qwen3-4b-new.csv").open(
                encoding="utf-8-sig",
                newline="",
            ) as csv_file:
                header = next(csv.reader(csv_file))
            self.assertEqual(
                header,
                ["文件名", *manifest["input_field_names"]],
            )

    def test_weaponry_directory_statistics_exclude_forced_empty_fields(self) -> None:
        placeholder = [deepcopy(weaponry_directory_runner.STANDARD_EMPTY_DATA_SOURCE)]
        fields = {
            name: {"analyseData": "", "analyseDataSource": deepcopy(placeholder)}
            for name in weaponry_directory_runner.FORCED_EMPTY_CONTRACT_INPUT_FIELDS
        }
        fields["舷号"] = {
            "analyseData": "CVN-68",
            "analyseDataSource": [
                {
                    "content": "USS Nimitz CVN-68",
                    "source": "nimitz.pdf",
                    "time": "",
                    "fileName": "nimitz.pdf",
                    "rows": ["USS Nimitz (CVN-68)"],
                    "translate": "",
                }
            ],
        }
        statistics = weaponry_directory_runner.summarize_input_field_statistics(
            fields,
            (
                *weaponry_directory_runner.FORCED_EMPTY_CONTRACT_INPUT_FIELDS,
                "舷号",
                "中文型号",
            ),
        )

        self.assertEqual(statistics["extractable_field_count"], 2)
        self.assertEqual(statistics["non_empty_count"], 1)
        self.assertEqual(statistics["missing_fields"], ["中文型号"])
        self.assertEqual(statistics["forced_empty_violations"], [])
        self.assertEqual(statistics["forced_empty_source_violations"], [])

        fields["装备编号"]["analyseData"] = "SHOULD-NOT-LEAK"
        violated = weaponry_directory_runner.summarize_input_field_statistics(
            fields,
            (*weaponry_directory_runner.FORCED_EMPTY_CONTRACT_INPUT_FIELDS, "舷号"),
        )
        self.assertEqual(violated["non_empty_count"], 1)
        self.assertEqual(violated["forced_empty_violations"], ["装备编号"])

    def test_weaponry_directory_wait_task_accepts_empty_check_task_body_and_reads_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            task_db = Path(tmpdir) / "llm_tasks.sqlite3"
            self._create_runner_task_db(task_db)
            with closing(sqlite3.connect(task_db)) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO llm_tasks (
                        business_type, business_key, status, progress, message,
                        result_payload, callback_status, callback_attempts,
                        last_callback_error, execution_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "weaponry",
                        "998570100",
                        "2",
                        100,
                        "done",
                        json.dumps({"code": 200, "data": {}}),
                        "success",
                        1,
                        "",
                        "weaponry-task-1",
                        "2026-07-26T00:00:00+00:00",
                        "2026-07-26T00:00:01+00:00",
                    ),
                )
            with (
                patch.object(weaponry_directory_runner, "TASK_DB", task_db),
                patch.object(
                    weaponry_directory_runner,
                    "request_json",
                    return_value={"raw": ""},
                ) as request_mock,
            ):
                snapshot, response = weaponry_directory_runner.wait_task(
                    "http://127.0.0.1:5001",
                    "weaponry",
                    "architectureId",
                    998570100,
                    timeout_seconds=1,
                    poll_interval=0,
                    label="weaponry probe",
                )

            self.assertEqual(snapshot["status"], "2")
            self.assertEqual(snapshot["callback_status"], "success")
            self.assertEqual(snapshot["result_payload"], {"code": 200, "data": {}})
            self.assertEqual(response, {"raw": ""})
            request_mock.assert_called_once()

    def test_weaponry_directory_wait_task_waits_for_callback_pending_to_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            task_db = Path(tmpdir) / "llm_tasks.sqlite3"
            self._create_runner_task_db(task_db)
            with closing(sqlite3.connect(task_db)) as conn, conn:
                conn.execute(
                    """
                    INSERT INTO llm_tasks (
                        business_type, business_key, status, progress, message,
                        result_payload, callback_status, callback_attempts,
                        last_callback_error, execution_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "weaponry",
                        "998570100",
                        "2",
                        100,
                        "done",
                        json.dumps({"code": 200, "data": {}}),
                        "pending",
                        0,
                        "",
                        "weaponry-task-1",
                        "2026-07-26T00:00:00+00:00",
                        "2026-07-26T00:00:01+00:00",
                    ),
                )

            request_count = 0

            def complete_callback_on_second_poll(*args, **kwargs):
                nonlocal request_count
                request_count += 1
                if request_count == 2:
                    with closing(sqlite3.connect(task_db)) as conn, conn:
                        conn.execute(
                            """
                            UPDATE llm_tasks
                            SET callback_status='success', callback_attempts=1
                            WHERE business_type='weaponry'
                              AND business_key='998570100'
                            """
                        )
                return {"raw": ""}

            with (
                patch.object(weaponry_directory_runner, "TASK_DB", task_db),
                patch.object(
                    weaponry_directory_runner,
                    "request_json",
                    side_effect=complete_callback_on_second_poll,
                ) as request_mock,
            ):
                snapshot, response = weaponry_directory_runner.wait_task(
                    "http://127.0.0.1:5001",
                    "weaponry",
                    "architectureId",
                    998570100,
                    timeout_seconds=1,
                    poll_interval=0,
                    label="weaponry probe",
                    wait_for_callback=True,
                )

            self.assertEqual(snapshot["status"], "2")
            self.assertEqual(snapshot["callback_status"], "success")
            self.assertEqual(snapshot["callback_attempts"], 1)
            self.assertEqual(response, {"raw": ""})
            self.assertEqual(request_mock.call_count, 2)

    def test_weaponry_directory_wait_task_returns_terminal_callback_failures(self) -> None:
        for callback_status in ("failed", "skipped", "outcome_unknown"):
            with self.subTest(callback_status=callback_status):
                snapshot = {
                    "status": "2",
                    "progress": 100,
                    "callback_status": callback_status,
                    "callback_attempts": 1,
                    "last_callback_error": "callback unavailable",
                }
                with (
                    patch.object(
                        weaponry_directory_runner,
                        "request_json",
                        return_value={"raw": ""},
                    ) as request_mock,
                    patch.object(
                        weaponry_directory_runner,
                        "get_task_snapshot",
                        return_value=snapshot,
                    ),
                ):
                    actual_snapshot, response = (
                        weaponry_directory_runner.wait_task(
                            "http://127.0.0.1:5001",
                            "weaponry",
                            "architectureId",
                            998570100,
                            timeout_seconds=1,
                            poll_interval=0,
                            label="weaponry probe",
                            wait_for_callback=True,
                        )
                    )

                self.assertIs(actual_snapshot, snapshot)
                self.assertEqual(response, {"raw": ""})
                request_mock.assert_called_once()

    def test_weaponry_directory_wait_task_reports_task_db_mismatch_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            task_db = Path(tmpdir) / "llm_tasks.sqlite3"
            self._create_runner_task_db(task_db)
            with (
                patch.object(weaponry_directory_runner, "TASK_DB", task_db),
                patch.object(
                    weaponry_directory_runner,
                    "request_json",
                    return_value={"raw": ""},
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    r"HTTP 200.*TASK_DB=.*不一致",
                ):
                    weaponry_directory_runner.wait_task(
                        "http://127.0.0.1:5001",
                        "file",
                        "fileName",
                        "missing.pdf",
                        timeout_seconds=30,
                        poll_interval=5,
                        label="analysis missing.pdf",
                    )

    def test_weaponry_directory_contract_verifier_checks_values_sources_callback_and_audit(self) -> None:
        result_payload = self._valid_forced_empty_contract_result()
        snapshot = {
            "status": "2",
            "callback_status": "success",
            "callback_attempts": 1,
            "last_callback_error": "",
            "execution_id": "weaponry-task-1",
        }
        analysis_snapshot = {
            "status": "2",
            "callback_status": "success",
            "callback_attempts": 1,
            "last_callback_error": "",
        }
        interaction_rows = [
            {"field_sequence": 6, "operation": "target_retrieval"},
            {"field_sequence": 6, "operation": "evidence_extraction"},
            {"field_sequence": 7, "operation": "target_retrieval"},
            {"field_sequence": 7, "operation": "evidence_extraction"},
        ]

        verification = weaponry_directory_runner.verify_forced_empty_contract(
            result_payload,
            snapshot,
            interaction_rows,
            analysis_task_snapshot=analysis_snapshot,
        )

        self.assertTrue(verification["passed"], verification["errors"])
        self.assertTrue(verification["callback"]["passed"])
        self.assertTrue(verification["top_level_forced_empty"]["passed"])
        self.assertTrue(verification["mixed_table"]["passed"])
        self.assertTrue(verification["reserved_only_table"]["passed"])
        self.assertTrue(verification["ordinary_control"]["passed"])
        self.assertTrue(verification["interaction_audit"]["passed"])

        interaction_rows.append(
            {"field_sequence": 1, "operation": "target_retrieval"}
        )
        violated = weaponry_directory_runner.verify_forced_empty_contract(
            result_payload,
            snapshot,
            interaction_rows,
            analysis_task_snapshot=analysis_snapshot,
        )
        self.assertFalse(violated["passed"])
        self.assertIn(
            "target_retrieval",
            " ".join(violated["interaction_audit"]["errors"]),
        )

        failed_analysis_callback = dict(analysis_snapshot)
        failed_analysis_callback["callback_status"] = "failed"
        callback_violation = (
            weaponry_directory_runner.verify_forced_empty_contract(
                result_payload,
                snapshot,
                interaction_rows[:-1],
                analysis_task_snapshot=failed_analysis_callback,
            )
        )
        self.assertFalse(callback_violation["passed"])
        self.assertIn(
            "analysis_callback",
            " ".join(callback_violation["callback"]["errors"]),
        )

        malformed_tables = deepcopy(result_payload)
        malformed_fields = malformed_tables["data"]["weaponryTemplateFieldList"]
        mixed_cells = malformed_fields[6]["tableFieldList"][0]
        mixed_cells[0], mixed_cells[1] = mixed_cells[1], mixed_cells[0]
        reserved_cells = malformed_fields[7]["tableFieldList"][0]
        reserved_cells.append(deepcopy(reserved_cells[-1]))
        shape_violation = weaponry_directory_runner.verify_forced_empty_contract(
            malformed_tables,
            snapshot,
            interaction_rows[:-1],
            analysis_task_snapshot=analysis_snapshot,
        )
        self.assertFalse(shape_violation["passed"])
        self.assertIn(
            "expected_columns",
            " ".join(
                [
                    *shape_violation["mixed_table"]["errors"],
                    *shape_violation["reserved_only_table"]["errors"],
                ]
            ),
        )

    def test_check_task_shell_script_posts_fixture_to_expected_path(self) -> None:
        payload = self._require_fixture(
            "tests/fixtures/llm/check_task_file_request.json"
        )
        _, port = self._start_recording_server()

        result = self._run_script(f"scripts/test_llm_check_task{_script_ext()}", f"http://127.0.0.1:{port}", str(payload))

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIsNotNone(RequestRecorderHandler.last_request)
        self.assertEqual(RequestRecorderHandler.last_request["path"], "/llm/check-task")
        posted_body = RequestRecorderHandler.last_request["body"].strip()
        expected_body = payload.read_text(encoding="utf-8").strip()
        self.assertEqual(posted_body, expected_body)

    @unittest.skipUnless(
        os.getenv("DOCSENSE_RUN_MAIN_PROCESS_TEST", "").strip() == "1",
        "需要显式设置 DOCSENSE_RUN_MAIN_PROCESS_TEST=1 才允许测试启动 run.py",
    )
    def test_progress_shell_script_reads_progress_snapshot_from_local_app(self) -> None:
        port = find_free_port()
        self._start_app_server(port)
        payload = ROOT_DIR / "tests/fixtures/llm/check_task_file_request.json"

        result = self._run_script(
            f"scripts/test_llm_progress{_script_ext()}",
            f"ws://127.0.0.1:{port}/llm/progress",
            str(payload),
            "1",
            "false",
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        message = json.loads(result.stdout.strip().splitlines()[0])
        self.assertEqual(message["businessType"], "file")
        self.assertIn("progress", message["data"])
