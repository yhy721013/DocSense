# Chat Debug Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增独立的 `/debug/chat` 本地联调页，覆盖文件对话模块的发消息、查历史、删会话三类接口，同时保证现有甲方真实回调链路和 `/debug/callback` 行为完全不受影响。

**Architecture:** 保持 `/llm/chat*` 作为唯一真实 chat 协议入口，只新增一个 `/debug/api/chat/bootstrap` 本地只读初始化接口。后端改动限定在调试页蓝图、数据库只读查询和 bootstrap 聚合层；前端采用单个 Flask 模板 + 原生 JavaScript 直接消费正式 chat 接口的 SSE 流与历史查询结果。

**Tech Stack:** Flask、sqlite3、Jinja2 模板、原生 JavaScript、pytest/unittest

---

## File Map

- Modify: `app/services/core/database.py`
  责任：补充 `ChatDatabaseService.list_chats()` 与 `DatabaseService.list_document_records()` 两个只读查询方法。
- Create: `app/services/utils/chat_debug_preview.py`
  责任：聚合本地会话列表与已解析文件列表，返回 `/debug/api/chat/bootstrap` 需要的固定 JSON 结构。
- Modify: `app/blueprints/debug.py`
  责任：新增 `GET /debug/chat` 和 `GET /debug/api/chat/bootstrap` 两条调试路由，并持有只读数据库服务实例。
- Create: `app/templates/debug/chat.html`
  责任：独立 chat 调试页，提供会话列表、文件选择、消息输入、聊天记录视图、SSE 事件流视图和删除操作。
- Create: `tests/test_chat_debug_preview.py`
  责任：覆盖数据库只读方法和 bootstrap 聚合层的成功、空状态、失败场景。
- Create: `tests/test_chat_debug_routes.py`
  责任：覆盖 `/debug/chat` 页面、`/debug/api/chat/bootstrap` 数据接口，以及模板内关键渲染/交互钩子。

## Constraints To Preserve

- 不修改 `app/services/utils/callback_client.py`。
- 不修改 `app/services/utils/callback_preview.py`。
- 不修改 `/debug/callback` 与 `/debug/api/callback` 的语义。
- 不让 `/debug/chat` 写入 `.runtime/call_back.json`。
- 不修改 `/llm/chat`、`/llm/chat/history`、`/llm/chat/delete` 的对外协议。

### Task 1: Add Read-Only Database Queries

**Files:**
- Modify: `app/services/core/database.py`
- Create: `tests/test_chat_debug_preview.py`

- [ ] **Step 1: Write the failing database query tests**

```python
import unittest

from app.services.core.database import ChatDatabaseService, DatabaseService
from tests import workspace_tempdir


class ChatDebugDatabaseQueryTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = workspace_tempdir()
        self.tmp = self._tempdir.__enter__()
        self.kb_service = DatabaseService(db_path=f"{self.tmp}/knowledge.sqlite3")
        self.chat_db = ChatDatabaseService(db_path=f"{self.tmp}/chat.sqlite3")

    def tearDown(self):
        self._tempdir.__exit__(None, None, None)

    def test_list_document_records_returns_rows_sorted_by_file_name(self):
        self.kb_service.save_document_record(
            "zulu.pdf",
            9,
            "doc-zulu",
            "custom-documents/doc-zulu.json",
        )
        self.kb_service.save_document_record(
            "alpha.pdf",
            3,
            "doc-alpha",
            "custom-documents/doc-alpha.json",
        )

        rows = self.kb_service.list_document_records()

        self.assertEqual(
            [row["file_name"] for row in rows],
            ["alpha.pdf", "zulu.pdf"],
        )
        self.assertEqual(rows[0]["architecture_id"], 3)
        self.assertEqual(rows[0]["anything_doc_id"], "doc-alpha")

    def test_list_chats_returns_latest_updated_first_with_decoded_file_names(self):
        self.chat_db.create_chat("chat-older", ["a.pdf"], "ws-a", "th-a")
        self.chat_db.create_chat("chat-newer", ["b.pdf"], "ws-b", "th-b")
        self.chat_db.update_file_names("chat-older", ["a.pdf", "c.pdf"])

        rows = self.chat_db.list_chats()

        self.assertEqual(rows[0]["chat_id"], "chat-older")
        self.assertEqual(rows[0]["file_names"], ["a.pdf", "c.pdf"])
        self.assertEqual(rows[1]["chat_id"], "chat-newer")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
. .venv/bin/activate && python -m pytest tests/test_chat_debug_preview.py -k "DatabaseQueryTests" -v
```

Expected:

```text
E   AttributeError: 'DatabaseService' object has no attribute 'list_document_records'
E   AttributeError: 'ChatDatabaseService' object has no attribute 'list_chats'
```

- [ ] **Step 3: Write the minimal database query implementation**

Add these methods to `app/services/core/database.py`:

```python
    def list_document_records(self) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT file_name, architecture_id, anything_doc_id, doc_path
                FROM documents
                ORDER BY file_name ASC
                """
            )
            return [dict(row) for row in cursor.fetchall()]
```

```python
    def list_chats(self) -> list[dict]:
        import json

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT chat_id, file_names, workspace_slug, thread_slug, created_at, updated_at
                FROM chats
                ORDER BY updated_at DESC
                """
            )
            rows = []
            for row in cursor.fetchall():
                record = dict(row)
                record["file_names"] = json.loads(record["file_names"])
                rows.append(record)
            return rows
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```bash
. .venv/bin/activate && python -m pytest tests/test_chat_debug_preview.py -k "DatabaseQueryTests" -v
```

Expected:

```text
tests/test_chat_debug_preview.py::ChatDebugDatabaseQueryTests::test_list_document_records_returns_rows_sorted_by_file_name PASSED
tests/test_chat_debug_preview.py::ChatDebugDatabaseQueryTests::test_list_chats_returns_latest_updated_first_with_decoded_file_names PASSED
```

- [ ] **Step 5: Commit**

```bash
git add app/services/core/database.py tests/test_chat_debug_preview.py
git commit -m "feat: add chat debug database queries"
```

### Task 2: Add Bootstrap Aggregation Service

**Files:**
- Create: `app/services/utils/chat_debug_preview.py`
- Modify: `tests/test_chat_debug_preview.py`

- [ ] **Step 1: Write the failing bootstrap aggregation tests**

Append these tests to `tests/test_chat_debug_preview.py`:

```python
import sqlite3
from unittest.mock import patch

from app.services.utils.chat_debug_preview import load_chat_debug_bootstrap


class ChatDebugPreviewTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = workspace_tempdir()
        self.tmp = self._tempdir.__enter__()
        self.kb_service = DatabaseService(db_path=f"{self.tmp}/knowledge.sqlite3")
        self.chat_db = ChatDatabaseService(db_path=f"{self.tmp}/chat.sqlite3")

    def tearDown(self):
        self._tempdir.__exit__(None, None, None)

    def test_load_chat_debug_bootstrap_returns_sessions_and_available_files(self):
        self.chat_db.create_chat("conv-001", ["alpha.pdf"], "ws-1", "th-1")
        self.kb_service.save_document_record(
            "alpha.pdf",
            12,
            "doc-alpha",
            "custom-documents/doc-alpha.json",
        )

        result = load_chat_debug_bootstrap(chat_db=self.chat_db, kb_service=self.kb_service)

        self.assertTrue(result["ok"])
        self.assertEqual(result["message"], "读取成功")
        self.assertEqual(result["data"]["sessions"][0]["chatId"], "conv-001")
        self.assertEqual(result["data"]["sessions"][0]["fileNames"], ["alpha.pdf"])
        self.assertEqual(result["data"]["availableFiles"][0]["fileName"], "alpha.pdf")
        self.assertEqual(result["data"]["availableFiles"][0]["architectureId"], 12)

    def test_load_chat_debug_bootstrap_returns_empty_lists_for_empty_databases(self):
        result = load_chat_debug_bootstrap(chat_db=self.chat_db, kb_service=self.kb_service)

        self.assertEqual(
            result,
            {
                "ok": True,
                "message": "读取成功",
                "data": {"sessions": [], "availableFiles": []},
            },
        )

    def test_load_chat_debug_bootstrap_returns_error_state_when_query_fails(self):
        with patch.object(self.chat_db, "list_chats", side_effect=sqlite3.Error("boom")):
            result = load_chat_debug_bootstrap(chat_db=self.chat_db, kb_service=self.kb_service)

        self.assertFalse(result["ok"])
        self.assertEqual(result["data"], {"sessions": [], "availableFiles": []})
        self.assertIn("读取失败", result["message"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
. .venv/bin/activate && python -m pytest tests/test_chat_debug_preview.py -k "ChatDebugPreviewTests" -v
```

Expected:

```text
E   ModuleNotFoundError: No module named 'app.services.utils.chat_debug_preview'
```

- [ ] **Step 3: Write the minimal bootstrap aggregation implementation**

Create `app/services/utils/chat_debug_preview.py` with this code:

```python
from __future__ import annotations

from typing import Any

from app.services.core.database import ChatDatabaseService, DatabaseService


def load_chat_debug_bootstrap(
    *,
    chat_db: ChatDatabaseService,
    kb_service: DatabaseService,
) -> dict[str, Any]:
    try:
        sessions = [
            {
                "chatId": item["chat_id"],
                "fileNames": item["file_names"],
                "createdAt": item["created_at"],
                "updatedAt": item["updated_at"],
            }
            for item in chat_db.list_chats()
        ]
        available_files = [
            {
                "fileName": item["file_name"],
                "architectureId": item["architecture_id"],
            }
            for item in kb_service.list_document_records()
        ]
    except Exception as exc:
        return {
            "ok": False,
            "message": f"读取失败: {exc}",
            "data": {"sessions": [], "availableFiles": []},
        }

    return {
        "ok": True,
        "message": "读取成功",
        "data": {
            "sessions": sessions,
            "availableFiles": available_files,
        },
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```bash
. .venv/bin/activate && python -m pytest tests/test_chat_debug_preview.py -k "ChatDebugPreviewTests" -v
```

Expected:

```text
tests/test_chat_debug_preview.py::ChatDebugPreviewTests::test_load_chat_debug_bootstrap_returns_sessions_and_available_files PASSED
tests/test_chat_debug_preview.py::ChatDebugPreviewTests::test_load_chat_debug_bootstrap_returns_empty_lists_for_empty_databases PASSED
tests/test_chat_debug_preview.py::ChatDebugPreviewTests::test_load_chat_debug_bootstrap_returns_error_state_when_query_fails PASSED
```

- [ ] **Step 5: Commit**

```bash
git add app/services/utils/chat_debug_preview.py tests/test_chat_debug_preview.py
git commit -m "feat: add chat debug bootstrap preview"
```

### Task 3: Add Debug Routes and Minimal Chat Page Shell

**Files:**
- Modify: `app/blueprints/debug.py`
- Create: `app/templates/debug/chat.html`
- Create: `tests/test_chat_debug_routes.py`

- [ ] **Step 1: Write the failing route and shell tests**

Create `tests/test_chat_debug_routes.py` with:

```python
import unittest
from unittest.mock import patch

from app import create_app
from app.services.core.database import ChatDatabaseService, DatabaseService
from tests import workspace_tempdir


class ChatDebugRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        self._tempdir = workspace_tempdir()
        self.tmp = self._tempdir.__enter__()
        self.chat_db = ChatDatabaseService(db_path=f"{self.tmp}/chat.sqlite3")
        self.kb_service = DatabaseService(db_path=f"{self.tmp}/knowledge.sqlite3")
        self.chat_patch = patch("app.blueprints.debug.chat_db", self.chat_db)
        self.kb_patch = patch("app.blueprints.debug.kb_service", self.kb_service)
        self.chat_patch.start()
        self.kb_patch.start()

    def tearDown(self):
        self.chat_patch.stop()
        self.kb_patch.stop()
        self._tempdir.__exit__(None, None, None)

    def test_chat_bootstrap_api_returns_local_sessions_and_files(self):
        self.chat_db.create_chat("conv-001", ["alpha.pdf"], "ws-1", "th-1")
        self.kb_service.save_document_record(
            "alpha.pdf",
            12,
            "doc-alpha",
            "custom-documents/doc-alpha.json",
        )

        response = self.client.get("/debug/api/chat/bootstrap")
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["data"]["sessions"][0]["chatId"], "conv-001")
        self.assertEqual(data["data"]["availableFiles"][0]["fileName"], "alpha.pdf")

    def test_chat_page_renders_shell(self):
        response = self.client.get("/debug/chat")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("文件对话调试页", html)
        self.assertIn('id="chat-session-list"', html)
        self.assertIn('id="chat-thread"', html)
        self.assertIn('id="chat-events"', html)
        self.assertIn("/debug/api/chat/bootstrap", html)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
. .venv/bin/activate && python -m pytest tests/test_chat_debug_routes.py -v
```

Expected:

```text
E   AttributeError: <module 'app.blueprints.debug' ...> does not have the attribute 'chat_db'
E   jinja2.exceptions.TemplateNotFound: debug/chat.html
```

- [ ] **Step 3: Implement the routes and minimal page shell**

Update `app/blueprints/debug.py`:

```python
from flask import Blueprint, jsonify, render_template

from app.services.core.database import ChatDatabaseService, DatabaseService
from app.services.core.settings import CHAT_DB_PATH, KNOWLEDGE_BASE_DB_PATH
from app.services.utils.callback_preview import load_callback_preview
from app.services.utils.chat_debug_preview import load_chat_debug_bootstrap


debug_bp = Blueprint("debug", __name__)
chat_db = ChatDatabaseService(str(CHAT_DB_PATH))
kb_service = DatabaseService(str(KNOWLEDGE_BASE_DB_PATH))


@debug_bp.get("/debug/api/callback")
def callback_debug_api():
    return jsonify(load_callback_preview())


@debug_bp.get("/debug/api/chat/bootstrap")
def chat_debug_bootstrap_api():
    return jsonify(load_chat_debug_bootstrap(chat_db=chat_db, kb_service=kb_service))


@debug_bp.get("/debug/callback")
def callback_debug_page():
    return render_template("debug/callback.html")


@debug_bp.get("/debug/chat")
def chat_debug_page():
    return render_template("debug/chat.html")
```

Create a minimal `app/templates/debug/chat.html` shell:

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>文件对话调试页</title>
</head>
<body>
  <main>
    <h1>文件对话调试页</h1>
    <section id="chat-session-list"></section>
    <section id="chat-thread"></section>
    <section id="chat-events"></section>
    <script>
      const BOOTSTRAP_URL = "/debug/api/chat/bootstrap";
    </script>
  </main>
</body>
</html>
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```bash
. .venv/bin/activate && python -m pytest tests/test_chat_debug_routes.py -v
```

Expected:

```text
tests/test_chat_debug_routes.py::ChatDebugRouteTests::test_chat_bootstrap_api_returns_local_sessions_and_files PASSED
tests/test_chat_debug_routes.py::ChatDebugRouteTests::test_chat_page_renders_shell PASSED
```

- [ ] **Step 5: Commit**

```bash
git add app/blueprints/debug.py app/templates/debug/chat.html tests/test_chat_debug_routes.py
git commit -m "feat: add chat debug routes"
```

### Task 4: Build Bootstrap, Session List, and History Loading UI

**Files:**
- Modify: `app/templates/debug/chat.html`
- Modify: `tests/test_chat_debug_routes.py`

- [ ] **Step 1: Write the failing page hook tests for bootstrap and history**

Append these assertions to `test_chat_page_renders_shell` in `tests/test_chat_debug_routes.py`:

```python
        self.assertIn('id="page-message"', html)
        self.assertIn('id="refresh-button"', html)
        self.assertIn('id="chat-id-input"', html)
        self.assertIn('id="chat-file-select"', html)
        self.assertIn('id="chat-message-input"', html)
        self.assertIn('id="load-history-button"', html)
        self.assertIn("function loadBootstrap()", html)
        self.assertIn("function renderSessionList(sessions)", html)
        self.assertIn("function renderAvailableFiles(files)", html)
        self.assertIn("function loadHistory()", html)
        self.assertIn('const CHAT_HISTORY_URL = "/llm/chat/history";', html)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
. .venv/bin/activate && python -m pytest tests/test_chat_debug_routes.py::ChatDebugRouteTests::test_chat_page_renders_shell -v
```

Expected:

```text
E   AssertionError: 'id="page-message"' not found in html
```

- [ ] **Step 3: Implement the shell layout, bootstrap fetch, session rendering, and history loading**

Replace `app/templates/debug/chat.html` with this structure and script foundation:

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>文件对话调试页</title>
  <style>
    body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif; background: #f5f7fb; color: #1f2937; }
    .page { max-width: 1440px; margin: 0 auto; padding: 32px 24px 64px; }
    .page-header { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 24px; }
    .layout { display: grid; grid-template-columns: 320px minmax(0, 1fr); gap: 16px; }
    .panel { background: #fff; border: 1px solid #dbe3f0; border-radius: 16px; padding: 20px; box-shadow: 0 10px 30px rgba(15, 23, 42, 0.04); }
    .stack { display: grid; gap: 16px; }
    .session-list { display: grid; gap: 10px; }
    .session-item { border: 1px solid #dbe3f0; border-radius: 12px; padding: 12px; cursor: pointer; background: #f8fafc; }
    .session-item.active { border-color: #0f172a; background: #e2e8f0; }
    .form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .form-row { display: grid; gap: 6px; }
    .button-row { display: flex; gap: 12px; flex-wrap: wrap; }
    .button { border: 0; border-radius: 999px; padding: 10px 18px; background: #0f172a; color: #fff; cursor: pointer; }
    .button.secondary { background: #475569; }
    .button.danger { background: #b91c1c; }
    .message-list, .event-list { display: grid; gap: 12px; }
    .message-item, .event-item { border-radius: 12px; padding: 12px; background: #f8fafc; }
    textarea, input, select { width: 100%; box-sizing: border-box; border: 1px solid #cbd5e1; border-radius: 12px; padding: 10px 12px; font: inherit; background: #fff; }
    textarea { min-height: 120px; resize: vertical; }
    pre { margin: 0; white-space: pre-wrap; word-break: break-word; }
  </style>
</head>
<body>
  <main class="page">
    <header class="page-header">
      <div>
        <p>DocSense Local Debug</p>
        <h1>文件对话调试页</h1>
      </div>
      <button id="refresh-button" class="button secondary" type="button">刷新本地数据</button>
    </header>

    <section id="page-message" class="panel">等待加载...</section>

    <div class="layout">
      <aside class="panel">
        <h2>本地会话列表</h2>
        <div id="chat-session-list" class="session-list"></div>
      </aside>

      <section class="stack">
        <section class="panel">
          <h2>新建 / 续聊</h2>
          <div class="form-grid">
            <label class="form-row">
              <span>chatId</span>
              <input id="chat-id-input" type="text" placeholder="请输入 chatId">
            </label>
            <label class="form-row">
              <span>已解析文件</span>
              <select id="chat-file-select" multiple size="8"></select>
            </label>
          </div>
          <label class="form-row">
            <span>message</span>
            <textarea id="chat-message-input" placeholder="请输入本轮问题"></textarea>
          </label>
          <div class="button-row">
            <button id="send-button" class="button" type="button">发送消息</button>
            <button id="load-history-button" class="button secondary" type="button">加载历史</button>
            <button id="delete-button" class="button danger" type="button">删除当前会话</button>
          </div>
        </section>

        <section class="panel">
          <h2>聊天记录</h2>
          <div id="chat-thread" class="message-list"></div>
        </section>

        <section class="panel">
          <h2>SSE 事件流</h2>
          <div id="chat-events" class="event-list"></div>
        </section>
      </section>
    </div>
  </main>

  <script>
    const BOOTSTRAP_URL = "/debug/api/chat/bootstrap";
    const CHAT_HISTORY_URL = "/llm/chat/history";
    const pageMessage = document.getElementById("page-message");
    const refreshButton = document.getElementById("refresh-button");
    const sessionList = document.getElementById("chat-session-list");
    const chatIdInput = document.getElementById("chat-id-input");
    const fileSelect = document.getElementById("chat-file-select");
    const messageInput = document.getElementById("chat-message-input");
    const loadHistoryButton = document.getElementById("load-history-button");
    const threadNode = document.getElementById("chat-thread");
    const eventsNode = document.getElementById("chat-events");

    const state = {
      sessions: [],
      availableFiles: [],
      activeChatId: "",
      historyMessages: [],
      streamEvents: [],
      isStreaming: false,
    };

    function setMessage(text) {
      pageMessage.textContent = text;
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
    }

    function selectedFileNames() {
      return Array.from(fileSelect.selectedOptions).map((option) => option.value);
    }

    function renderThread() {
      if (!state.historyMessages.length) {
        threadNode.innerHTML = "<div class=\"message-item\">暂无消息</div>";
        return;
      }

      threadNode.innerHTML = state.historyMessages
        .map((item) => `<div class="message-item"><strong>${escapeHtml(item.role)}</strong><pre>${escapeHtml(item.content)}</pre></div>`)
        .join("");
    }

    function renderEventList() {
      if (!state.streamEvents.length) {
        eventsNode.innerHTML = "<div class=\"event-item\">暂无事件</div>";
        return;
      }

      eventsNode.innerHTML = state.streamEvents
        .map((item) => `<div class="event-item"><strong>${escapeHtml(item.eventName)}</strong><pre>${escapeHtml(JSON.stringify(item.data, null, 2))}</pre></div>`)
        .join("");
    }

    function renderAvailableFiles(files) {
      fileSelect.innerHTML = "";
      if (!files.length) {
        fileSelect.disabled = true;
        return;
      }

      fileSelect.disabled = false;
      files.forEach((item) => {
        const option = document.createElement("option");
        option.value = item.fileName;
        option.textContent = `${item.fileName} (architectureId=${item.architectureId})`;
        fileSelect.appendChild(option);
      });
    }

    function applySelectedFiles(fileNames) {
      const selected = new Set(fileNames);
      Array.from(fileSelect.options).forEach((option) => {
        option.selected = selected.has(option.value);
      });
    }

    async function loadHistory() {
      const chatId = chatIdInput.value.trim();
      if (!chatId) {
        setMessage("请先输入或选择 chatId");
        return;
      }

      const response = await fetch(`${CHAT_HISTORY_URL}?chatId=${encodeURIComponent(chatId)}`);
      const payload = await response.json();
      if (!response.ok) {
        setMessage(payload.error || "加载历史失败");
        return;
      }

      state.historyMessages = payload.messages || [];
      state.activeChatId = payload.chatId || chatId;
      renderThread();
      setMessage(`已加载会话 ${state.activeChatId} 的历史记录`);
    }

    function renderSessionList(sessions) {
      if (!sessions.length) {
        sessionList.innerHTML = "<div class=\"session-item\">暂无本地会话</div>";
        return;
      }

      sessionList.innerHTML = "";
      sessions.forEach((item) => {
        const node = document.createElement("button");
        node.type = "button";
        node.className = `session-item${item.chatId === state.activeChatId ? " active" : ""}`;
        node.innerHTML = `<strong>${escapeHtml(item.chatId)}</strong><div>文件数：${item.fileNames.length}</div><div>更新时间：${escapeHtml(item.updatedAt)}</div>`;
        node.addEventListener("click", async () => {
          if (state.isStreaming) {
            setMessage("当前流式响应尚未结束");
            return;
          }
          state.activeChatId = item.chatId;
          chatIdInput.value = item.chatId;
          applySelectedFiles(item.fileNames);
          state.streamEvents = [];
          renderEventList();
          renderSessionList(state.sessions);
          await loadHistory();
        });
        sessionList.appendChild(node);
      });
    }

    async function loadBootstrap() {
      setMessage("正在加载本地数据...");
      const response = await fetch(BOOTSTRAP_URL);
      const result = await response.json();
      state.sessions = result.data?.sessions || [];
      state.availableFiles = result.data?.availableFiles || [];
      renderAvailableFiles(state.availableFiles);
      renderSessionList(state.sessions);
      renderThread();
      renderEventList();
      setMessage(result.message || "读取完成");
    }

    refreshButton.addEventListener("click", loadBootstrap);
    loadHistoryButton.addEventListener("click", loadHistory);
    window.addEventListener("DOMContentLoaded", loadBootstrap);
  </script>
</body>
</html>
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```bash
. .venv/bin/activate && python -m pytest tests/test_chat_debug_routes.py::ChatDebugRouteTests::test_chat_page_renders_shell -v
```

Expected:

```text
tests/test_chat_debug_routes.py::ChatDebugRouteTests::test_chat_page_renders_shell PASSED
```

- [ ] **Step 5: Commit**

```bash
git add app/templates/debug/chat.html tests/test_chat_debug_routes.py
git commit -m "feat: add chat debug bootstrap page"
```

### Task 5: Add Streaming Send/Delete Logic and Run Regression Suite

**Files:**
- Modify: `app/templates/debug/chat.html`
- Modify: `tests/test_chat_debug_routes.py`

- [ ] **Step 1: Write the failing page hook tests for send, stream, and delete**

Append these assertions to `test_chat_page_renders_shell` in `tests/test_chat_debug_routes.py`:

```python
        self.assertIn('const CHAT_SEND_URL = "/llm/chat";', html)
        self.assertIn('const CHAT_DELETE_URL = "/llm/chat/delete";', html)
        self.assertIn("function sendCurrentMessage()", html)
        self.assertIn("function consumeSseStream(response)", html)
        self.assertIn("function handleSseBlock(block)", html)
        self.assertIn("function handleSseEvent(eventName, data)", html)
        self.assertIn("function deleteCurrentChat()", html)
        self.assertIn('if (state.isStreaming)', html)
        self.assertIn('setMessage("当前流式响应尚未结束")', html)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
. .venv/bin/activate && python -m pytest tests/test_chat_debug_routes.py::ChatDebugRouteTests::test_chat_page_renders_shell -v
```

Expected:

```text
E   AssertionError: 'const CHAT_SEND_URL = "/llm/chat";' not found in html
```

- [ ] **Step 3: Implement send, stream parsing, active-stream guarding, and delete flow**

Add these constants and functions to `app/templates/debug/chat.html`:

```html
<script>
  const BOOTSTRAP_URL = "/debug/api/chat/bootstrap";
  const CHAT_SEND_URL = "/llm/chat";
  const CHAT_HISTORY_URL = "/llm/chat/history";
  const CHAT_DELETE_URL = "/llm/chat/delete";
  const pageMessage = document.getElementById("page-message");
  const refreshButton = document.getElementById("refresh-button");
  const sessionList = document.getElementById("chat-session-list");
  const chatIdInput = document.getElementById("chat-id-input");
  const fileSelect = document.getElementById("chat-file-select");
  const messageInput = document.getElementById("chat-message-input");
  const sendButton = document.getElementById("send-button");
  const loadHistoryButton = document.getElementById("load-history-button");
  const deleteButton = document.getElementById("delete-button");
  const threadNode = document.getElementById("chat-thread");
  const eventsNode = document.getElementById("chat-events");

  const state = {
    sessions: [],
    availableFiles: [],
    activeChatId: "",
    historyMessages: [],
    streamEvents: [],
    streamingReply: "",
    isStreaming: false,
  };

  function setStreamingState(nextValue) {
    state.isStreaming = nextValue;
    sendButton.disabled = nextValue;
    deleteButton.disabled = nextValue;
    loadHistoryButton.disabled = nextValue;
    refreshButton.disabled = nextValue;
  }

  function renderThread() {
    const messages = [...state.historyMessages];
    if (state.streamingReply) {
      messages.push({ role: "assistant", content: state.streamingReply });
    }

    if (!messages.length) {
      threadNode.innerHTML = "<div class=\"message-item\">暂无消息</div>";
      return;
    }

    threadNode.innerHTML = messages
      .map((item) => `<div class="message-item"><strong>${escapeHtml(item.role)}</strong><pre>${escapeHtml(item.content)}</pre></div>`)
      .join("");
  }

  async function sendCurrentMessage() {
    if (state.isStreaming) {
      setMessage("当前流式响应尚未结束");
      return;
    }

    const chatId = chatIdInput.value.trim();
    const fileNames = selectedFileNames();
    const message = messageInput.value.trim();
    if (!chatId) {
      setMessage("chatId 不能为空");
      return;
    }
    if (!fileNames.length) {
      setMessage("请至少选择一个已解析文件");
      return;
    }
    if (!message) {
      setMessage("message 不能为空");
      return;
    }

    state.activeChatId = chatId;
    state.streamEvents = [];
    state.streamingReply = "";
    state.historyMessages = [...state.historyMessages, { role: "user", content: message }];
    renderThread();
    renderEventList();
    setStreamingState(true);
    setMessage(`正在发送会话 ${chatId}...`);

    const response = await fetch(CHAT_SEND_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        businessType: "chat",
        params: { chatId, fileNames, message },
      }),
    });

    if (!response.ok) {
      const errorPayload = await response.json().catch(() => ({}));
      setStreamingState(false);
      setMessage(errorPayload.error || "发送失败");
      return;
    }

    await consumeSseStream(response);
    messageInput.value = "";
  }

  async function consumeSseStream(response) {
    const reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const blocks = buffer.split(/\r?\n\r?\n/);
      buffer = blocks.pop() || "";
      blocks.forEach(handleSseBlock);
      if (done) {
        break;
      }
    }

    if (buffer.trim()) {
      handleSseBlock(buffer);
    }
  }

  function handleSseBlock(block) {
    let eventName = "message";
    const dataLines = [];

    block.split(/\r?\n/).forEach((line) => {
      if (line.startsWith("event:")) {
        eventName = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trim());
      }
    });

    let data = {};
    try {
      data = JSON.parse(dataLines.join("\n") || "{}");
    } catch (_error) {
      data = { raw: dataLines.join("\n") };
    }

    handleSseEvent(eventName, data);
  }

  async function handleSseEvent(eventName, data) {
    state.streamEvents.push({ eventName, data });
    renderEventList();

    if (eventName === "chatInfo" && data.chatId) {
      state.activeChatId = data.chatId;
      chatIdInput.value = data.chatId;
      setMessage(`已连接会话 ${data.chatId}`);
      return;
    }

    if (eventName === "textChunk") {
      state.streamingReply += data.content || "";
      renderThread();
      return;
    }

    if (eventName === "done") {
      if (state.streamingReply) {
        state.historyMessages = [...state.historyMessages, { role: "assistant", content: state.streamingReply }];
        state.streamingReply = "";
      }
      renderThread();
      setStreamingState(false);
      setMessage(`会话 ${state.activeChatId} 已完成`);
      await loadBootstrap();
      return;
    }

    if (eventName === "error") {
      state.streamingReply = "";
      renderThread();
      setStreamingState(false);
      setMessage(data.error || "流式响应失败");
    }
  }

  async function deleteCurrentChat() {
    if (state.isStreaming) {
      setMessage("当前流式响应尚未结束");
      return;
    }

    const chatId = chatIdInput.value.trim();
    if (!chatId) {
      setMessage("请先输入或选择 chatId");
      return;
    }

    const response = await fetch(CHAT_DELETE_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        businessType: "chat",
        params: { chatId },
      }),
    });
    const payload = await response.json();
    if (!response.ok) {
      setMessage(payload.error || "删除失败");
      return;
    }

    state.activeChatId = "";
    state.historyMessages = [];
    state.streamEvents = [];
    state.streamingReply = "";
    chatIdInput.value = "";
    messageInput.value = "";
    renderThread();
    renderEventList();
    setMessage(`会话 ${payload.chatId} 已删除`);
    await loadBootstrap();
  }

  sendButton.addEventListener("click", sendCurrentMessage);
  deleteButton.addEventListener("click", deleteCurrentChat);
</script>
```

- [ ] **Step 4: Run the full regression suite**

Run:

```bash
. .venv/bin/activate && python -m pytest \
  tests/test_chat_debug_preview.py \
  tests/test_chat_debug_routes.py \
  tests/test_chat.py \
  tests/test_callback_debug_routes.py -v
```

Expected:

```text
tests/test_chat_debug_preview.py ... PASSED
tests/test_chat_debug_routes.py ... PASSED
tests/test_chat.py ... PASSED
tests/test_callback_debug_routes.py ... PASSED
```

- [ ] **Step 5: Commit**

```bash
git add app/templates/debug/chat.html tests/test_chat_debug_routes.py
git commit -m "feat: add chat debug streaming page"
```

## Self-Review Checklist

- Spec coverage: 设计里要求的独立 `/debug/chat`、bootstrap 只读接口、混合入口、文件选择限制、双视图展示、删除/历史/发送三类联调动作、隔离 callback 链路，都在任务 1-5 中有落点。
- Placeholder scan: 计划中没有占位描述；所有代码步骤都给了具体函数、路由、模板 ID、测试命令和提交命令。
- Type consistency: 数据层统一使用 `chat_id/file_name` 原始数据库字段，聚合层统一映射为 `chatId/fileName` 页面字段；所有路由与模板里使用的 URL、方法名、状态字段彼此一致。
