# Callback Debug Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 DocSense 增加一个本地只读调试页，读取 `.runtime/call_back.json`，并对 `file` 与 `report` 回调提供人工可读的结构化展示。

**Architecture:** 新增一个独立的 `debug` blueprint 承载本地调试路由，避免把调试逻辑混入甲方协议蓝图。新增一个专门读取 `.runtime/call_back.json` 的小型服务模块，负责统一处理文件不存在、JSON 非法和 payload 返回；页面模板使用原生 JavaScript 读取 JSON 接口，并用文本面板与 `iframe.srcdoc` 分别展示纯文本和 HTML 内容。

**Tech Stack:** Flask, Jinja2 template, 原生 JavaScript, Python `unittest`

---

## File Structure

- Create: `app/blueprints/debug.py`
  - 本地调试蓝图，提供 `/debug/callback` 和 `/debug/api/callback`
- Create: `app/services/utils/callback_preview.py`
  - 统一读取 `.runtime/call_back.json`，返回固定 JSON 结构
- Create: `app/templates/debug/callback.html`
  - 调试页模板，包含最小 CSS 和原生 JS 渲染逻辑
- Create: `tests/test_callback_debug_routes.py`
  - 覆盖调试接口和页面壳层的回归测试
- Modify: `app/__init__.py`
  - 注册 `debug_bp`

## Implementation Notes

1. 读取回调文件时使用 `app.services.core.settings.RUNTIME_DIR / "call_back.json"` 作为默认路径。
2. `load_callback_preview()` 不抛异常给路由层，而是统一返回：
   - `{"ok": True, "message": "读取成功", "payload": {...}}`
   - `{"ok": False, "message": "当前还没有回调结果文件", "payload": None}`
   - `{"ok": False, "message": "回调文件不是合法 JSON", "payload": None}`
   - `{"ok": False, "message": "回调文件根节点必须为对象", "payload": None}`
3. `/debug/api/callback` 对这些已知状态统一返回 HTTP 200，页面只根据 JSON 中的 `ok` 和 `message` 渲染。
4. HTML 内容预览统一使用 `iframe.srcdoc`，避免回调 HTML 样式污染调试页主页面。
5. `originalText` 只按纯文本显示，不进行 HTML 注入。
6. 页面底部永远保留完整原始 JSON。

### Task 1: Add Callback Preview Reader And JSON API

**Files:**
- Create: `app/services/utils/callback_preview.py`
- Create: `app/blueprints/debug.py`
- Modify: `app/__init__.py`
- Test: `tests/test_callback_debug_routes.py`

- [ ] **Step 1: Write the failing API tests**

```python
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from app import create_app
from tests import workspace_tempdir


class CallbackDebugRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        self._tempdir = workspace_tempdir()
        self.tmp = Path(self._tempdir.__enter__())
        self.callback_path = self.tmp / "call_back.json"
        self.path_patcher = patch(
            "app.services.utils.callback_preview.CALLBACK_PREVIEW_PATH",
            self.callback_path,
        )
        self.path_patcher.start()

    def tearDown(self):
        self.path_patcher.stop()
        self._tempdir.__exit__(None, None, None)

    def test_callback_api_returns_missing_state_when_file_does_not_exist(self):
        response = self.client.get("/debug/api/callback")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "ok": False,
                "message": "当前还没有回调结果文件",
                "payload": None,
            },
        )

    def test_callback_api_returns_payload_for_file_callback(self):
        payload = {
            "businessType": "file",
            "data": {
                "fileName": "demo.txt",
                "status": "2",
                "fileDataItem": {
                    "originalText": "原文第一行\n原文第二行",
                    "documentTranslationOne": "<p>单语翻译</p>",
                    "documentTranslationTwo": "<p>双语翻译</p>",
                },
            },
            "msg": "解析成功",
        }
        self.callback_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

        response = self.client.get("/debug/api/callback")
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["message"], "读取成功")
        self.assertEqual(data["payload"]["businessType"], "file")
        self.assertEqual(
            data["payload"]["data"]["fileDataItem"]["originalText"],
            "原文第一行\n原文第二行",
        )

    def test_callback_api_returns_payload_for_report_callback(self):
        payload = {
            "businessType": "report",
            "data": {
                "reportId": 132,
                "status": "1",
                "details": "<h1>报告正文</h1>",
            },
            "msg": "生成成功",
        }
        self.callback_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

        response = self.client.get("/debug/api/callback")
        data = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["payload"]["businessType"], "report")
        self.assertEqual(data["payload"]["data"]["reportId"], 132)

    def test_callback_api_returns_invalid_json_state(self):
        self.callback_path.write_text("{invalid", encoding="utf-8")

        response = self.client.get("/debug/api/callback")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json(),
            {
                "ok": False,
                "message": "回调文件不是合法 JSON",
                "payload": None,
            },
        )
```

- [ ] **Step 2: Run the targeted tests and verify they fail**

Run:

```bash
.venv/bin/python -m unittest tests.test_callback_debug_routes -v
```

Expected:

```text
FAIL: test_callback_api_returns_missing_state_when_file_does_not_exist
AssertionError: 404 != 200
```

- [ ] **Step 3: Write the minimal reader service and JSON API**

`app/services/utils/callback_preview.py`

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.services.core.settings import RUNTIME_DIR


CALLBACK_PREVIEW_PATH = RUNTIME_DIR / "call_back.json"


def load_callback_preview(path: Path | None = None) -> dict[str, Any]:
    target = path or CALLBACK_PREVIEW_PATH
    if not target.exists():
        return {
            "ok": False,
            "message": "当前还没有回调结果文件",
            "payload": None,
        }

    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "ok": False,
            "message": "回调文件不是合法 JSON",
            "payload": None,
        }

    if not isinstance(payload, dict):
        return {
            "ok": False,
            "message": "回调文件根节点必须为对象",
            "payload": None,
        }

    return {
        "ok": True,
        "message": "读取成功",
        "payload": payload,
    }
```

`app/blueprints/debug.py`

```python
from __future__ import annotations

from flask import Blueprint, jsonify

from app.services.utils.callback_preview import load_callback_preview


debug_bp = Blueprint("debug", __name__)


@debug_bp.get("/debug/api/callback")
def callback_debug_api():
    return jsonify(load_callback_preview())
```

`app/__init__.py`

```python
from app.blueprints.debug import debug_bp
from app.blueprints.llm import llm_bp, sock


def create_app() -> Flask:
    setup_logging()
    app = Flask(__name__)
    app.config.update(
        MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH,
    )
    sock.init_app(app)

    app.register_blueprint(llm_bp)
    app.register_blueprint(debug_bp)

    return app
```

- [ ] **Step 4: Run the targeted tests again and verify they pass**

Run:

```bash
.venv/bin/python -m unittest tests.test_callback_debug_routes -v
```

Expected:

```text
Ran 4 tests in ...

OK
```

- [ ] **Step 5: Commit the API slice**

```bash
git add app/__init__.py app/blueprints/debug.py app/services/utils/callback_preview.py tests/test_callback_debug_routes.py
git commit -m "feat: add callback debug api"
```

### Task 2: Add The Debug Page Route And HTML Shell

**Files:**
- Modify: `app/blueprints/debug.py`
- Create: `app/templates/debug/callback.html`
- Modify: `tests/test_callback_debug_routes.py`

- [ ] **Step 1: Extend the tests with a failing page-shell assertion**

Add this test to `tests/test_callback_debug_routes.py`:

```python
    def test_callback_page_renders_debug_shell(self):
        response = self.client.get("/debug/callback")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("回调结果调试页", html)
        self.assertIn('id="refresh-button"', html)
        self.assertIn('id="callback-summary"', html)
        self.assertIn("/debug/api/callback", html)
```

- [ ] **Step 2: Run the page-shell test and verify it fails**

Run:

```bash
.venv/bin/python -m unittest tests.test_callback_debug_routes.CallbackDebugRouteTests.test_callback_page_renders_debug_shell -v
```

Expected:

```text
FAIL: test_callback_page_renders_debug_shell
AssertionError: 404 != 200
```

- [ ] **Step 3: Implement the page route and minimal template shell**

Update `app/blueprints/debug.py`:

```python
from flask import Blueprint, jsonify, render_template


@debug_bp.get("/debug/callback")
def callback_debug_page():
    return render_template("debug/callback.html")
```

Create `app/templates/debug/callback.html`:

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>回调结果调试页</title>
  <style>
    body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif; background: #f5f7fb; color: #1f2937; }
    .page { max-width: 1280px; margin: 0 auto; padding: 32px 24px 64px; }
    .page-header { display: flex; justify-content: space-between; align-items: center; gap: 16px; margin-bottom: 24px; }
    .panel { background: #fff; border: 1px solid #dbe3f0; border-radius: 16px; padding: 20px; margin-bottom: 16px; box-shadow: 0 10px 30px rgba(15, 23, 42, 0.04); }
    .banner { min-height: 24px; margin-bottom: 16px; color: #475569; }
    .button { border: 0; border-radius: 999px; padding: 10px 18px; background: #0f172a; color: #fff; cursor: pointer; }
    pre { white-space: pre-wrap; word-break: break-word; }
  </style>
</head>
<body>
  <main class="page">
    <header class="page-header">
      <div>
        <p>DocSense Local Debug</p>
        <h1>回调结果调试页</h1>
      </div>
      <button id="refresh-button" class="button" type="button">刷新</button>
    </header>

    <section id="page-message" class="banner">等待加载...</section>
    <section id="callback-summary" class="panel"></section>
    <section id="structured-content" class="panel"></section>
    <section id="preview-sections"></section>
    <section class="panel">
      <h2>原始 JSON</h2>
      <pre id="raw-json">等待加载...</pre>
    </section>
  </main>

  <script>
    const API_URL = "/debug/api/callback";
    const refreshButton = document.getElementById("refresh-button");

    refreshButton.addEventListener("click", loadPayload);
    window.addEventListener("DOMContentLoaded", loadPayload);

    async function loadPayload() {
      const response = await fetch(API_URL, { cache: "no-store" });
      const result = await response.json();
      document.getElementById("page-message").textContent = result.message;
      document.getElementById("raw-json").textContent = JSON.stringify(result.payload, null, 2) || "暂无内容";
    }
  </script>
</body>
</html>
```

- [ ] **Step 4: Run the route tests and verify the shell passes**

Run:

```bash
.venv/bin/python -m unittest tests.test_callback_debug_routes -v
```

Expected:

```text
Ran 5 tests in ...

OK
```

- [ ] **Step 5: Commit the page shell**

```bash
git add app/blueprints/debug.py app/templates/debug/callback.html tests/test_callback_debug_routes.py
git commit -m "feat: add callback debug page shell"
```

### Task 3: Render File And Report Payloads In A Human-Readable Layout

**Files:**
- Modify: `app/templates/debug/callback.html`
- Modify: `tests/test_callback_debug_routes.py`

- [ ] **Step 1: Add a failing template-contract test for the renderer hooks**

Append this test to `tests/test_callback_debug_routes.py`:

```python
    def test_callback_page_contains_renderer_hooks_for_file_and_report(self):
        response = self.client.get("/debug/callback")
        html = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("function renderFilePayload(payload)", html)
        self.assertIn("function renderReportPayload(payload)", html)
        self.assertIn("function renderHtmlPreview(title, content)", html)
        self.assertIn('id="preview-sections"', html)
        self.assertIn('id="structured-content"', html)
```

- [ ] **Step 2: Run the contract test and verify it fails**

Run:

```bash
.venv/bin/python -m unittest tests.test_callback_debug_routes.CallbackDebugRouteTests.test_callback_page_contains_renderer_hooks_for_file_and_report -v
```

Expected:

```text
FAIL: test_callback_page_contains_renderer_hooks_for_file_and_report
AssertionError: 'function renderFilePayload(payload)' not found in html
```

- [ ] **Step 3: Replace the shell script with concrete renderer helpers**

Update the `<script>` section in `app/templates/debug/callback.html` to this structure:

```html
  <script>
    const API_URL = "/debug/api/callback";
    const refreshButton = document.getElementById("refresh-button");
    const pageMessage = document.getElementById("page-message");
    const summary = document.getElementById("callback-summary");
    const structured = document.getElementById("structured-content");
    const previews = document.getElementById("preview-sections");
    const rawJson = document.getElementById("raw-json");

    refreshButton.addEventListener("click", loadPayload);
    window.addEventListener("DOMContentLoaded", loadPayload);

    function statusText(businessType, status) {
      if (businessType === "file" && status === "2") return "解析成功";
      if (businessType === "file" && status === "3") return "解析失败";
      if (businessType === "report" && status === "1") return "生成成功";
      if (businessType === "report" && status === "2") return "生成失败";
      return `未知状态（${status || "空"}）`;
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
    }

    function displayValue(value) {
      if (value === 0) {
        return "0";
      }
      return value ? String(value) : "暂无内容";
    }

    function previewDocument(content) {
      const text = String(content ?? "").trim();
      if (!text) {
        return "<!DOCTYPE html><html lang=\"zh-CN\"><body><p>暂无内容</p></body></html>";
      }

      if (/<[a-z][\\s\\S]*>/i.test(text)) {
        return text;
      }

      return `<!DOCTYPE html><html lang="zh-CN"><body><pre>${escapeHtml(text)}</pre></body></html>`;
    }

    function resetPanels() {
      summary.innerHTML = "";
      structured.innerHTML = "";
      previews.innerHTML = "";
    }

    function renderSummaryItems(items) {
      summary.innerHTML = items
        .map((item) => `<div class="summary-item"><span>${item.label}</span><strong>${escapeHtml(displayValue(item.value))}</strong></div>`)
        .join("");
    }

    function renderFieldValue(field) {
      const rawValue = field.value;
      const normalized = displayValue(rawValue);

      if (field.isLink && /^https?:\\/\\/\\S+$/i.test(String(rawValue || ""))) {
        return `<a class="field-link" href="${escapeHtml(rawValue)}" target="_blank" rel="noreferrer">${escapeHtml(rawValue)}</a>`;
      }

      return `<span class="field-value">${escapeHtml(normalized)}</span>`;
    }

    function renderFieldGrid(title, fields) {
      const section = document.createElement("section");
      section.className = "panel";
      const rows = fields.map((field) => `
        <div class="field-row">
          <span class="field-label">${field.label}</span>
          ${renderFieldValue(field)}
        </div>
      `).join("");
      section.innerHTML = `<h2>${title}</h2><div class="field-grid">${rows}</div>`;
      structured.appendChild(section);
    }

    function renderPlainTextPreview(title, content) {
      const section = document.createElement("section");
      section.className = "panel";
      section.innerHTML = `<h2>${title}</h2><pre>${escapeHtml(displayValue(content))}</pre>`;
      previews.appendChild(section);
    }

    function renderHtmlPreview(title, content) {
      const section = document.createElement("section");
      section.className = "panel";

      const heading = document.createElement("h2");
      heading.textContent = title;

      const iframe = document.createElement("iframe");
      iframe.className = "html-preview";
      iframe.setAttribute("title", title);
      iframe.srcdoc = previewDocument(content);

      const details = document.createElement("details");
      const summaryNode = document.createElement("summary");
      summaryNode.textContent = "查看原始 HTML";
      const source = document.createElement("pre");
      source.textContent = content || "暂无内容";

      details.appendChild(summaryNode);
      details.appendChild(source);
      section.appendChild(heading);
      section.appendChild(iframe);
      section.appendChild(details);
      previews.appendChild(section);
    }

    function renderFilePayload(payload) {
      const data = payload.data || {};
      const fileDataItem = data.fileDataItem || {};

      renderSummaryItems([
        { label: "businessType", value: payload.businessType },
        { label: "msg", value: payload.msg },
        { label: "fileName", value: data.fileName },
        { label: "status", value: statusText("file", data.status) },
      ]);

      renderFieldGrid("分类信息", [
        { label: "国家", value: data.country },
        { label: "渠道", value: data.channel },
        { label: "成熟度", value: data.maturity },
        { label: "格式", value: data.format },
        { label: "领域体系 ID", value: data.architectureId },
      ]);

      renderFieldGrid("文档摘要信息", [
        { label: "摘要", value: fileDataItem.summary },
        { label: "关键词", value: fileDataItem.keyword },
        { label: "文件概述", value: fileDataItem.documentOverview },
        { label: "评分", value: fileDataItem.score },
        { label: "资料年代", value: fileDataItem.dataTime },
        { label: "资料来源", value: fileDataItem.source },
        { label: "原文链接", value: fileDataItem.originalLink, isLink: true },
        { label: "语种", value: fileDataItem.language },
        { label: "资料格式", value: fileDataItem.dataFormat },
        { label: "所属装备", value: fileDataItem.associatedEquipment },
        { label: "所属技术", value: fileDataItem.relatedTechnology },
        { label: "装备型号", value: fileDataItem.equipmentModel },
      ]);

      renderPlainTextPreview("原文", fileDataItem.originalText);
      renderHtmlPreview("单语翻译预览", fileDataItem.documentTranslationOne);
      renderHtmlPreview("双语翻译预览", fileDataItem.documentTranslationTwo);
    }

    function renderReportPayload(payload) {
      const data = payload.data || {};

      renderSummaryItems([
        { label: "businessType", value: payload.businessType },
        { label: "msg", value: payload.msg },
        { label: "reportId", value: data.reportId },
        { label: "status", value: statusText("report", data.status) },
      ]);

      renderFieldGrid("报告信息", [
        { label: "报告 ID", value: data.reportId },
        { label: "状态", value: data.status },
      ]);

      renderHtmlPreview("报告预览", data.details);
    }

    function renderUnsupportedPayload(payload) {
      renderSummaryItems([
        { label: "businessType", value: payload.businessType },
        { label: "msg", value: payload.msg },
      ]);
      renderFieldGrid("未支持类型", [
        { label: "提示", value: "当前类型暂未提供友好展示" },
      ]);
    }

    function renderResponse(result) {
      pageMessage.textContent = result.message;
      rawJson.textContent = result.payload ? JSON.stringify(result.payload, null, 2) : "暂无内容";
      resetPanels();

      if (!result.ok || !result.payload) {
        return;
      }

      if (result.payload.businessType === "file") {
        renderFilePayload(result.payload);
        return;
      }

      if (result.payload.businessType === "report") {
        renderReportPayload(result.payload);
        return;
      }

      renderUnsupportedPayload(result.payload);
    }

    async function loadPayload() {
      try {
        const response = await fetch(API_URL, { cache: "no-store" });
        const result = await response.json();
        renderResponse(result);
      } catch (error) {
        resetPanels();
        pageMessage.textContent = `加载失败：${error.message}`;
        rawJson.textContent = "暂无内容";
      }
    }
  </script>
```

Add the CSS needed by those helpers:

```css
.summary-item { display: inline-flex; flex-direction: column; gap: 6px; min-width: 180px; margin-right: 16px; margin-bottom: 12px; }
.field-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 12px 20px; }
.field-row { display: flex; flex-direction: column; gap: 4px; padding: 12px; background: #f8fafc; border-radius: 12px; }
.field-label { font-size: 13px; color: #64748b; }
.field-value { font-size: 14px; line-height: 1.6; }
.field-link { color: #2563eb; text-decoration: none; word-break: break-all; }
.html-preview { width: 100%; min-height: 420px; border: 1px solid #dbe3f0; border-radius: 12px; background: #fff; }
details { margin-top: 12px; }
```

- [ ] **Step 4: Run the route tests again and verify they pass**

Run:

```bash
.venv/bin/python -m unittest tests.test_callback_debug_routes -v
```

Expected:

```text
Ran 6 tests in ...

OK
```

- [ ] **Step 5: Run a manual smoke check for both payload types**

Write a file callback sample:

```bash
python - <<'PY'
import json
from pathlib import Path

payload = {
    "businessType": "file",
    "data": {
        "fileName": "demo.txt",
        "country": "美国",
        "channel": "军情",
        "maturity": "阶段成果",
        "format": "文档类",
        "status": "2",
        "architectureId": 101,
        "fileDataItem": {
            "summary": "示例摘要",
            "keyword": "示例关键词",
            "documentOverview": "示例概述",
            "score": 4.5,
            "dataTime": "2026-04-01",
            "source": "示例来源",
            "originalLink": "https://example.com/article",
            "language": "中文",
            "dataFormat": "简报",
            "associatedEquipment": "示例装备",
            "relatedTechnology": "示例技术",
            "equipmentModel": "示例型号",
            "originalText": "原文第一行\n原文第二行",
            "documentTranslationOne": "<h1>单语翻译</h1><p>这是 HTML 预览。</p>",
            "documentTranslationTwo": "<h1>双语翻译</h1><p>原文 / 译文</p>",
        },
    },
    "msg": "解析成功",
}

Path(".runtime/call_back.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
PY
```

Then run:

```bash
python run.py
open http://127.0.0.1:5001/debug/callback
```

Expected:

```text
页面顶部显示 file / 解析成功 / demo.txt
原文区保留换行
两个翻译区以 HTML 方式展示
底部原始 JSON 可见
```

Replace with a report sample:

```bash
python - <<'PY'
import json
from pathlib import Path

payload = {
    "businessType": "report",
    "data": {
        "reportId": 132,
        "status": "1",
        "details": "<!DOCTYPE html><html><body><h1>报告标题</h1><p>报告正文</p></body></html>",
    },
    "msg": "生成成功",
}

Path(".runtime/call_back.json").write_text(
    json.dumps(payload, ensure_ascii=False, indent=2),
    encoding="utf-8",
)
PY
```

Expected:

```text
页面顶部显示 report / 生成成功 / 132
报告预览区渲染 HTML 正文
底部原始 JSON 可见
```

- [ ] **Step 6: Commit the renderer slice**

```bash
git add app/templates/debug/callback.html tests/test_callback_debug_routes.py
git commit -m "feat: render callback debug payloads"
```

### Task 4: Run Regression And Finalize

**Files:**
- Modify: none expected
- Test: `tests/test_callback_debug_routes.py`
- Test: `tests/test_routes.py`
- Test: `tests/test_progress_and_check_task.py`

- [ ] **Step 1: Run the focused regression suite**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_callback_debug_routes \
  tests.test_routes \
  tests.test_progress_and_check_task \
  -v
```

Expected:

```text
Ran ... tests in ...

OK
```

- [ ] **Step 2: Verify the git working tree is clean except for intentional implementation files**

Run:

```bash
git status --short
```

Expected:

```text
工作树应为空，因为前三个任务已经完成分段提交
```
