# LLM Integration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a formal `/llm/*` integration layer that matches `api-test.md`, adds persistent task orchestration, active callbacks, WebSocket progress, and developer-facing interface test tooling without regressing the existing debug routes.

**Architecture:** Add a dedicated `llm` blueprint on top of the current Flask app. Keep OCR and AnythingLLM as the processing core, but route all formal `file` and `report` requests through a new SQLite-backed task service that owns state transitions, callback replay, and progress broadcasting.

**Tech Stack:** Flask, requests, SQLite (`sqlite3`), Flask-Sock or equivalent lightweight Flask WebSocket integration, PyMuPDF, Python `unittest`, PowerShell 7 scripts.

---

### Task 1: Scaffold Formal LLM Routes And Config

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/test_llm_routes.py`
- Create: `app/blueprints/llm.py`
- Modify: `app/__init__.py`
- Modify: `config.py`
- Modify: `requirements-offline.txt`

**Step 1: Write the failing test**

```python
import unittest

from app import create_app


class LLMRouteValidationTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()

    def test_analysis_rejects_invalid_business_type(self):
        response = self.client.post("/llm/analysis", json={"businessType": "wrong", "params": [{}]})
        self.assertEqual(response.status_code, 400)

    def test_generate_report_rejects_missing_params(self):
        response = self.client.post("/llm/generate-report", json={"businessType": "report"})
        self.assertEqual(response.status_code, 400)
```

**Step 2: Run test to verify it fails**

Run: `pwsh -NoLogo -Command "python -m unittest tests.test_llm_routes -v"`

Expected: FAIL because `/llm/analysis` and `/llm/generate-report` do not exist yet.

**Step 3: Write minimal implementation**

```python
llm_bp = Blueprint("llm", __name__)


@llm_bp.post("/llm/analysis")
def llm_analysis():
    payload = request.get_json(silent=True) or {}
    if payload.get("businessType") != "file":
        return jsonify({"error": "businessType必须为file"}), 400
    if not isinstance(payload.get("params"), list) or not payload["params"]:
        return jsonify({"error": "params不能为空"}), 400
    return jsonify({"message": "accepted"}), 202
```

- Register `llm_bp` in `app/__init__.py`.
- Add config placeholders for callback URL, SQLite path, download timeout.
- Add the minimal WebSocket dependency to `requirements-offline.txt`.

**Step 4: Run test to verify it passes**

Run: `pwsh -NoLogo -Command "python -m unittest tests.test_llm_routes -v"`

Expected: PASS.

**Step 5: Commit**

```bash
git add tests/__init__.py tests/test_llm_routes.py app/blueprints/llm.py app/__init__.py config.py requirements-offline.txt
git commit -m "feat: scaffold llm protocol routes"
```

### Task 2: Add SQLite Task Persistence And Status Mapping

**Files:**
- Create: `tests/test_llm_task_service.py`
- Create: `app/services/llm_task_service.py`
- Modify: `app/settings.py`
- Modify: `app/blueprints/llm.py`

**Step 1: Write the failing test**

```python
import tempfile
import unittest

from app.services.llm_task_service import LLMTaskService


class LLMTaskServiceTests(unittest.TestCase):
    def test_create_file_task_defaults_to_processing(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task = service.create_file_task(file_name="demo.pdf", request_payload={"businessType": "file"})
            self.assertEqual(task["business_key"], "demo.pdf")
            self.assertEqual(task["status"], "1")
            self.assertEqual(task["callback_status"], "pending")

    def test_completed_task_with_failed_callback_should_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            task = service.create_report_task(report_id=7, request_payload={"businessType": "report"})
            service.mark_business_completed("report", "7", {"details": "<div>ok</div>"}, status="1")
            service.mark_callback_failed("report", "7", "timeout")
            self.assertTrue(service.should_replay_callback("report", "7"))
```

**Step 2: Run test to verify it fails**

Run: `pwsh -NoLogo -Command "python -m unittest tests.test_llm_task_service -v"`

Expected: FAIL with import or attribute errors because `LLMTaskService` does not exist.

**Step 3: Write minimal implementation**

```python
class LLMTaskService:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def create_file_task(self, file_name: str, request_payload: dict) -> dict:
        return self._insert_task("file", file_name, request_payload, status="1")

    def should_replay_callback(self, business_type: str, business_key: str) -> bool:
        task = self.get_task(business_type, business_key)
        return bool(task and task["status"] in {"1", "2"} and task["callback_status"] != "success")
```

- Create a single SQLite table for file and report tasks.
- Persist `request_payload`, `result_payload`, `callback_attempts`, `last_callback_error`.
- Add helper methods for create, get, update progress, mark success/failure, and callback replay checks.

**Step 4: Run test to verify it passes**

Run: `pwsh -NoLogo -Command "python -m unittest tests.test_llm_task_service -v"`

Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_llm_task_service.py app/services/llm_task_service.py app/settings.py app/blueprints/llm.py
git commit -m "feat: add sqlite llm task persistence"
```

### Task 3: Add Download And Callback Services

**Files:**
- Create: `tests/test_llm_io_services.py`
- Create: `app/services/llm_download_service.py`
- Create: `app/services/llm_callback_service.py`
- Modify: `config.py`

**Step 1: Write the failing test**

```python
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.clients.callback_client import post_callback_payload
from app.utils.file_downloader import download_to_temp_file


class LLMIOServicesTests(unittest.TestCase):
    @patch("app.services.llm_download_service.requests.get")
    def test_download_to_temp_file_saves_content(self, mock_get):
        mock_get.return_value = Mock(ok=True, content=b"demo", headers={})
        with tempfile.TemporaryDirectory() as tmp:
            path = download_to_temp_file("http://example.test/file.pdf", "demo.pdf", tmp, timeout=10)
            self.assertTrue(path.endswith("demo.pdf"))

    @patch("app.services.llm_callback_service.requests.post")
    def test_post_callback_payload_returns_true_on_200(self, mock_post):
        mock_post.return_value = Mock(ok=True, status_code=200, text="ok")
        self.assertTrue(post_callback_payload("http://callback.test/llm/callback", {"msg": "解析成功"}, timeout=5))
```

**Step 2: Run test to verify it fails**

Run: `pwsh -NoLogo -Command "python -m unittest tests.test_llm_io_services -v"`

Expected: FAIL because the services do not exist.

**Step 3: Write minimal implementation**

```python
def download_to_temp_file(url: str, file_name: str, temp_root: str, timeout: float) -> str:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    path = Path(temp_root) / file_name
    path.write_bytes(response.content)
    return str(path)


def post_callback_payload(callback_url: str, payload: dict, timeout: float) -> bool:
    response = requests.post(callback_url, json=payload, timeout=timeout)
    return bool(response.ok)
```

- Normalize safe file names.
- Create temp directories when absent.
- Return explicit error text for logging and retry decisions.

**Step 4: Run test to verify it passes**

Run: `pwsh -NoLogo -Command "python -m unittest tests.test_llm_io_services -v"`

Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_llm_io_services.py app/services/file_downloader.py app/services/callback_client.py config.py
git commit -m "feat: add llm download and callback services"
```

### Task 4: Adapt The File Analysis Pipeline To甲方 Protocol

**Files:**
- Create: `tests/test_llm_analysis_service.py`
- Create: `app/services/llm_analysis_service.py`
- Create: `app/services/llm_prompts.py`
- Modify: `rag_with_ocr.py`
- Modify: `app/blueprints/llm.py`

**Step 1: Write the failing test**

```python
import unittest

from app.services.llm_analysis_service import build_file_callback_payload, map_analysis_result


class LLMAnalysisServiceTests(unittest.TestCase):
    def test_map_analysis_result_keeps_translation_fields_blank(self):
        result = map_analysis_result(
            parsed_result={"summary": "摘要", "language": "中文", "score": 3.6},
            request_params={
                "fileName": "demo.pdf",
                "country": [{"key": "02", "value": "美国"}],
                "channel": [{"key": "01", "value": "装发"}],
                "maturity": [{"key": "02", "value": "阶段成果"}],
                "format": [{"key": "03", "value": "文档类"}],
                "architectureList": [{"id": 10, "name": "测试"}],
            },
        )
        self.assertEqual(result["fileDataItem"]["documentTranslationOne"], "")
        self.assertEqual(result["fileDataItem"]["documentTranslationTwo"], "")

    def test_build_file_callback_payload_uses_fixed_success_message(self):
        payload = build_file_callback_payload("demo.pdf", {"summary": "摘要"}, status="2")
        self.assertEqual(payload["msg"], "解析成功")
```

**Step 2: Run test to verify it fails**

Run: `pwsh -NoLogo -Command "python -m unittest tests.test_llm_analysis_service -v"`

Expected: FAIL because the service functions do not exist.

**Step 3: Write minimal implementation**

```python
def build_file_callback_payload(file_name: str, mapped_result: dict, status: str) -> dict:
    return {
        "businessType": "file",
        "data": {"fileName": file_name, "status": status, **mapped_result},
        "msg": "解析成功" if status == "2" else "解析失败",
    }
```

- Add a dedicated甲方 prompt builder in `app/services/llm_prompts.py`.
- Use request candidate ranges from `architectureList`, `country`, `channel`, `maturity`, `format`.
- Keep `documentTranslationOne` and `documentTranslationTwo` fixed to empty strings.
- Prefer reusing `pipeline.prepare_upload_files` and `AnythingLLMClient`.

**Step 4: Run test to verify it passes**

Run: `pwsh -NoLogo -Command "python -m unittest tests.test_llm_analysis_service -v"`

Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_llm_analysis_service.py app/services/llm_analysis_service.py app/services/llm_prompts.py rag_with_ocr.py app/blueprints/llm.py
git commit -m "feat: add file analysis protocol mapping"
```

### Task 5: Add Report Generation With HTML Fallback

**Files:**
- Create: `tests/test_llm_report_service.py`
- Create: `app/services/llm_report_service.py`
- Modify: `app/services/llm_prompts.py`
- Modify: `app/blueprints/llm.py`

**Step 1: Write the failing test**

```python
import unittest

from app.services.llm_report_service import build_report_callback_payload, ensure_report_html


class LLMReportServiceTests(unittest.TestCase):
    def test_ensure_report_html_wraps_plain_text(self):
        html = ensure_report_html("报告正文")
        self.assertIn("<div", html)
        self.assertIn("报告正文", html)

    def test_build_report_callback_payload_uses_fixed_success_message(self):
        payload = build_report_callback_payload(132, "<div>ok</div>", status="1")
        self.assertEqual(payload["msg"], "生成成功")
```

**Step 2: Run test to verify it fails**

Run: `pwsh -NoLogo -Command "python -m unittest tests.test_llm_report_service -v"`

Expected: FAIL because the report service does not exist.

**Step 3: Write minimal implementation**

```python
def ensure_report_html(content: str) -> str:
    text = (content or "").strip()
    if text.startswith("<") and text.endswith(">"):
        return text
    return f'<div class="report-content"><pre>{html.escape(text)}</pre></div>'


def build_report_callback_payload(report_id: int, details: str, status: str) -> dict:
    return {
        "businessType": "report",
        "data": {"reportId": report_id, "status": status, "details": details},
        "msg": "生成成功" if status == "1" else "生成失败",
    }
```

- Build a report prompt that injects `templateDesc`, `templateOutline`, and `requirement`.
- Reuse one temporary workspace per report request.
- Keep HTML fallback entirely server-side.

**Step 4: Run test to verify it passes**

Run: `pwsh -NoLogo -Command "python -m unittest tests.test_llm_report_service -v"`

Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_llm_report_service.py app/services/llm_report_service.py app/services/llm_prompts.py app/blueprints/llm.py
git commit -m "feat: add report generation service"
```

### Task 6: Wire Async Execution, Check-Task Replay, And Progress Broadcasting

**Files:**
- Create: `tests/test_llm_progress_and_check_task.py`
- Create: `app/services/llm_progress_hub.py`
- Modify: `app/blueprints/llm.py`
- Modify: `app/services/llm_task_service.py`
- Modify: `app/services/llm_analysis_service.py`
- Modify: `app/services/llm_report_service.py`

**Step 1: Write the failing test**

```python
import tempfile
import unittest
from unittest.mock import patch

from app.core.llm_progress_hub import LLMProgressHub
from app.services.llm_task_service import LLMTaskService


class LLMProgressAndCheckTaskTests(unittest.TestCase):
    def test_progress_hub_broadcasts_latest_message(self):
        hub = LLMProgressHub()
        sink = []
        hub.subscribe("file", "demo.pdf", sink.append)
        hub.publish("file", "demo.pdf", {"businessType": "file", "data": {"fileName": "demo.pdf", "progress": 0.35}})
        self.assertEqual(sink[-1]["data"]["progress"], 0.35)

    @patch("app.services.llm_task_service.post_callback_payload", return_value=True)
    def test_check_task_replays_failed_callback(self, _mock_callback):
        with tempfile.TemporaryDirectory() as tmp:
            service = LLMTaskService(db_path=f"{tmp}/tasks.sqlite3")
            service.create_file_task("demo.pdf", {"businessType": "file"})
            service.mark_business_completed("file", "demo.pdf", {"fileName": "demo.pdf"}, status="2")
            service.mark_callback_failed("file", "demo.pdf", "timeout")
            replayed = service.replay_callback_if_needed("file", "demo.pdf")
            self.assertTrue(replayed)
```

**Step 2: Run test to verify it fails**

Run: `pwsh -NoLogo -Command "python -m unittest tests.test_llm_progress_and_check_task -v"`

Expected: FAIL because broadcast and replay logic do not exist yet.

**Step 3: Write minimal implementation**

```python
class LLMProgressHub:
    def __init__(self):
        self._subscribers = {}

    def publish(self, business_type: str, business_key: str, payload: dict) -> None:
        for callback in self._subscribers.get((business_type, business_key), []):
            callback(payload)
```

- Add `subscribe`, `unsubscribe`, and latest-event cache for late joiners.
- Wire `analysis` and `report` worker threads to publish at every stage boundary.
- Implement `/llm/check-task` to query SQLite and replay callback when needed.
- Expose `/llm/progress` through the WebSocket integration selected in Task 1.

**Step 4: Run test to verify it passes**

Run: `pwsh -NoLogo -Command "python -m unittest tests.test_llm_progress_and_check_task -v"`

Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_llm_progress_and_check_task.py app/services/llm_progress_hub.py app/blueprints/llm.py app/services/llm_task_service.py app/services/llm_analysis_service.py app/services/llm_report_service.py
git commit -m "feat: add llm progress and callback replay"
```

### Task 7: Add Developer Testing Fixtures And Manual Test Scripts

**Files:**
- Create: `tests/fixtures/llm/analysis_request.json`
- Create: `tests/fixtures/llm/report_request.json`
- Create: `tests/fixtures/llm/check_task_file_request.json`
- Create: `tests/fixtures/llm/check_task_report_request.json`
- Create: `tests/fixtures/files/README.md`
- Create: `scripts/test_llm_analysis.ps1`
- Create: `scripts/test_llm_report.ps1`
- Create: `scripts/test_llm_check_task.ps1`
- Create: `scripts/test_llm_progress.ps1`
- Create: `scripts/start_test_file_server.ps1`
- Create: `scripts/mock_callback_server.py`
- Create: `tests/test_llm_test_assets.py`

**Step 1: Write the failing test**

```python
import json
import pathlib
import unittest


class LLMTestAssetsTests(unittest.TestCase):
    def test_analysis_request_fixture_has_required_fields(self):
        payload = json.loads(pathlib.Path("tests/fixtures/llm/analysis_request.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["businessType"], "file")
        self.assertIn("filePath", payload["params"][0])

    def test_report_request_fixture_has_required_fields(self):
        payload = json.loads(pathlib.Path("tests/fixtures/llm/report_request.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["businessType"], "report")
        self.assertIn("filePathList", payload["params"][0])
```

**Step 2: Run test to verify it fails**

Run: `pwsh -NoLogo -Command "python -m unittest tests.test_llm_test_assets -v"`

Expected: FAIL because the fixtures do not exist.

**Step 3: Write minimal implementation**

```powershell
# scripts/test_llm_analysis.ps1
param(
  [string]$BaseUrl = "http://127.0.0.1:5001",
  [string]$PayloadPath = "tests/fixtures/llm/analysis_request.json"
)

$body = Get-Content -Path $PayloadPath -Raw -Encoding utf8
Invoke-RestMethod -Uri "$BaseUrl/llm/analysis" -Method Post -ContentType "application/json; charset=utf-8" -Body $body
```

- Populate JSON fixtures with realistic placeholders.
- Make `start_test_file_server.ps1` serve `tests/fixtures/files` with `python -m http.server`.
- Make `test_llm_progress.ps1` use `.NET` `ClientWebSocket` to send the subscribe message and print pushed progress.
- Make `mock_callback_server.py` log incoming payloads to stdout and optionally save them to disk.

**Step 4: Run test to verify it passes**

Run: `pwsh -NoLogo -Command "python -m unittest tests.test_llm_test_assets -v"`

Expected: PASS.

**Step 5: Commit**

```bash
git add tests/fixtures/llm/analysis_request.json tests/fixtures/llm/report_request.json tests/fixtures/llm/check_task_file_request.json tests/fixtures/llm/check_task_report_request.json tests/fixtures/files/README.md scripts/test_llm_analysis.ps1 scripts/test_llm_report.ps1 scripts/test_llm_check_task.ps1 scripts/test_llm_progress.ps1 scripts/start_test_file_server.ps1 scripts/mock_callback_server.py tests/test_llm_test_assets.py
git commit -m "test: add llm manual integration assets"
```

### Task 8: Document The Workflow And Run End-To-End Verification

**Files:**
- Modify: `README.md`
- Modify: `docs/plans/2026-03-09-llm-integration-design.md`

**Step 1: Update documentation**

Add a new `README.md` section that covers:

- Required env vars for callback URL, SQLite path, temp download root, download timeout.
- How to start the app.
- How to start the local test file server.
- How to start the mock callback server.
- How to run each PowerShell test script.
- How to subscribe to `/llm/progress`.

**Step 2: Run the focused unit tests**

Run: `pwsh -NoLogo -Command "python -m unittest tests.test_llm_routes tests.test_llm_task_service tests.test_llm_io_services tests.test_llm_analysis_service tests.test_llm_report_service tests.test_llm_progress_and_check_task tests.test_llm_test_assets -v"`

Expected: PASS.

**Step 3: Run manual smoke checks**

Run:

```powershell
pwsh -NoLogo -Command "python scripts/mock_callback_server.py"
pwsh -NoLogo -Command "./scripts/start_test_file_server.ps1"
pwsh -NoLogo -Command "python web_ui.py"
pwsh -NoLogo -Command "./scripts/test_llm_analysis.ps1"
pwsh -NoLogo -Command "./scripts/test_llm_report.ps1"
pwsh -NoLogo -Command "./scripts/test_llm_check_task.ps1"
pwsh -NoLogo -Command "./scripts/test_llm_progress.ps1"
```

Expected:

- `/llm/analysis` returns an accepted response.
- `/llm/generate-report` returns an accepted response.
- `/llm/check-task` returns task status and replays callback if needed.
- `/llm/progress` prints stage updates until completion.
- Mock callback server receives the final callback payload.

**Step 4: Commit**

```bash
git add README.md docs/plans/2026-03-09-llm-integration-design.md
git commit -m "docs: add llm integration testing workflow"
```
