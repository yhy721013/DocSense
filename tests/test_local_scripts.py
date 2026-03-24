from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests


ROOT_DIR = Path(__file__).resolve().parent.parent
ZSH_BIN = "/bin/zsh"


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

    def _run_script(self, relative_path: str, *args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        script_path = ROOT_DIR / relative_path
        script_env = os.environ.copy()
        if env:
            script_env.update(env)
        return subprocess.run(
            [ZSH_BIN, str(script_path), *args],
            cwd=ROOT_DIR,
            env=script_env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )

    def _start_app_server(self, port: int) -> subprocess.Popen[str]:
        env = os.environ.copy()
        env.update(
            {
                "WEB_UI_HOST": "127.0.0.1",
                "WEB_UI_PORT": str(port),
                "WEB_UI_DEBUG": "false",
            }
        )
        process = subprocess.Popen(
            [str(ROOT_DIR / ".venv/bin/python"), "run.py"],
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

    def test_start_test_file_server_serves_fixture_file(self) -> None:
        port = find_free_port()
        expected_bytes = (ROOT_DIR / "tests/fixtures/files/sample.txt").read_bytes()
        process = subprocess.Popen(
            [ZSH_BIN, str(ROOT_DIR / "scripts/start_test_file_server.sh"), str(port), "tests/fixtures/files"],
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
        _, port = self._start_recording_server()
        payload = ROOT_DIR / "tests/fixtures/llm/analysis_request.json"

        result = self._run_script("scripts/test_llm_analysis.sh", f"http://127.0.0.1:{port}", str(payload))

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIsNotNone(RequestRecorderHandler.last_request)
        self.assertEqual(RequestRecorderHandler.last_request["path"], "/llm/analysis")
        self.assertEqual(RequestRecorderHandler.last_request["body"], payload.read_text(encoding="utf-8"))
        self.assertIn('"ok": true', result.stdout)

    def test_report_shell_script_posts_fixture_to_expected_path(self) -> None:
        _, port = self._start_recording_server()
        payload = ROOT_DIR / "tests/fixtures/llm/report_request.json"

        result = self._run_script("scripts/test_llm_report.sh", f"http://127.0.0.1:{port}", str(payload))

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIsNotNone(RequestRecorderHandler.last_request)
        self.assertEqual(RequestRecorderHandler.last_request["path"], "/llm/generate-report")
        self.assertEqual(RequestRecorderHandler.last_request["body"], payload.read_text(encoding="utf-8"))
        self.assertIn('"ok": true', result.stdout)

    def test_check_task_shell_script_posts_fixture_to_expected_path(self) -> None:
        _, port = self._start_recording_server()
        payload = ROOT_DIR / "tests/fixtures/llm/check_task_file_request.json"

        result = self._run_script("scripts/test_llm_check_task.sh", f"http://127.0.0.1:{port}", str(payload))

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIsNotNone(RequestRecorderHandler.last_request)
        self.assertEqual(RequestRecorderHandler.last_request["path"], "/llm/check-task")
        self.assertEqual(RequestRecorderHandler.last_request["body"], payload.read_text(encoding="utf-8"))
        self.assertIn('"ok": true', result.stdout)

    def test_progress_shell_script_reads_progress_snapshot_from_local_app(self) -> None:
        port = find_free_port()
        self._start_app_server(port)
        payload = ROOT_DIR / "tests/fixtures/llm/check_task_file_request.json"

        result = self._run_script(
            "scripts/test_llm_progress.sh",
            f"ws://127.0.0.1:{port}/llm/progress",
            str(payload),
            "1",
            "false",
        )

        self.assertEqual(result.returncode, 0, msg=result.stderr)
        message = json.loads(result.stdout.strip().splitlines()[0])
        self.assertEqual(message["businessType"], "file")
        self.assertIn("progress", message["data"])
