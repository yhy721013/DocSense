# Chat Debug Page UX Optimization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 `/debug/chat` 优化为更接近常见大模型页面的聊天调试页：右侧整合为单一聊天主区域，文件选择改成“已选标签 + 勾选面板”，SSE 在主消息区实时输出，同时保留折叠的调试详情。

**Architecture:** 保持 `/debug/api/chat/bootstrap` 与 `/llm/chat*` 协议不变，只重构 `app/templates/debug/chat.html` 的 DOM 结构、前端状态和交互脚本。测试以模板结构钩子和现有 debug/callback 回归为主，确保 UX 优化不影响甲方真实回调链路。

**Tech Stack:** Flask、Jinja2 模板、原生 JavaScript、unittest

---

## File Map

- Modify: `app/templates/debug/chat.html`
  责任：重构右侧聊天主区域、文件选择器、SSE 主消息显示与折叠调试区。
- Modify: `tests/test_chat_debug_routes.py`
  责任：更新页面结构和关键交互钩子的断言，覆盖文件勾选面板、已选标签区、折叠调试区和实时流式展示函数。
- Optional Modify: `README.md`
  责任：如果页面行为变化导致现有说明不准确，再补充 `/debug/chat` 的使用描述。

## Constraints To Preserve

- 不修改 `app/blueprints/debug.py`
- 不修改 `app/services/utils/chat_debug_preview.py`
- 不修改 `app/services/core/database.py`
- 不修改 `/llm/chat`、`/llm/chat/history`、`/llm/chat/delete`
- 不修改 `/debug/callback` 与回调写盘相关链路
- 不让 `/debug/chat` 写入 `.runtime/call_back.json`

### Task 1: Replace Native Multi-Select with Checkbox Picker Model

**Files:**
- Modify: `tests/test_chat_debug_routes.py`
- Modify: `app/templates/debug/chat.html`

- [ ] **Step 1: Write the failing template assertions for the new file picker**

Replace the old native-multiselect expectations in `tests/test_chat_debug_routes.py` with these assertions inside `test_chat_page_renders_shell`:

```python
        self.assertIn('id="selected-files"', html)
        self.assertIn('id="toggle-file-picker-button"', html)
        self.assertIn('id="file-picker-panel"', html)
        self.assertIn('id="available-file-options"', html)
        self.assertIn("function toggleFilePicker()", html)
        self.assertIn("function renderSelectedFiles()", html)
        self.assertIn("function renderFilePickerOptions(files)", html)
        self.assertIn("function toggleSelectedFile(fileName)", html)
        self.assertIn("function removeSelectedFile(fileName)", html)
        self.assertNotIn('id="chat-file-select"', html)
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
. .venv/bin/activate && python -m unittest tests.test_chat_debug_routes.ChatDebugRouteTests.test_chat_page_renders_shell -v
```

Expected:

```text
FAIL: 'id="selected-files"' not found in html
```

- [ ] **Step 3: Implement the new file picker structure and state hooks**

In `app/templates/debug/chat.html`, replace the current file selector area:

```html
<label class="form-row">
  <span>已解析文件</span>
  <select id="chat-file-select" multiple size="8"></select>
</label>
```

with:

```html
<section class="context-row">
  <div class="context-header">
    <span>已解析文件</span>
    <button id="toggle-file-picker-button" class="button secondary" type="button">添加文件</button>
  </div>
  <div id="selected-files" class="selected-files"></div>
  <div id="file-picker-panel" class="file-picker-panel" hidden>
    <div id="available-file-options" class="file-option-list"></div>
  </div>
</section>
```

Add/replace state and functions:

```javascript
    const toggleFilePickerButton = document.getElementById("toggle-file-picker-button");
    const selectedFilesNode = document.getElementById("selected-files");
    const filePickerPanel = document.getElementById("file-picker-panel");
    const availableFileOptionsNode = document.getElementById("available-file-options");
```

```javascript
    const state = {
      sessions: [],
      availableFiles: [],
      activeChatId: "",
      historyMessages: [],
      streamEvents: [],
      streamingReply: "",
      isStreaming: false,
      selectedFileNames: [],
      isFilePickerOpen: false,
    };
```

```javascript
    function toggleFilePicker() {
      state.isFilePickerOpen = !state.isFilePickerOpen;
      filePickerPanel.hidden = !state.isFilePickerOpen;
    }
```

```javascript
    function renderSelectedFiles() {
      if (!state.selectedFileNames.length) {
        selectedFilesNode.innerHTML = '<div class="empty-chip">尚未选择文件</div>';
        return;
      }

      selectedFilesNode.innerHTML = state.selectedFileNames
        .map((fileName) => {
          const file = state.availableFiles.find((item) => item.fileName === fileName);
          const detail = file ? `architectureId=${file.architectureId}` : "未找到文件信息";
          return `<button type="button" class="file-chip" data-file-name="${escapeHtml(fileName)}">${escapeHtml(fileName)}<span>${escapeHtml(detail)}</span><strong>移除</strong></button>`;
        })
        .join("");

      selectedFilesNode.querySelectorAll(".file-chip").forEach((node) => {
        node.addEventListener("click", () => removeSelectedFile(node.dataset.fileName || ""));
      });
    }
```

```javascript
    function renderFilePickerOptions(files) {
      if (!files.length) {
        availableFileOptionsNode.innerHTML = '<div class="event-item">暂无可选文件</div>';
        return;
      }

      availableFileOptionsNode.innerHTML = files
        .map((item) => {
          const checked = state.selectedFileNames.includes(item.fileName) ? "checked" : "";
          return `<label class="file-option"><input type="checkbox" data-file-name="${escapeHtml(item.fileName)}" ${checked}><span>${escapeHtml(item.fileName)}</span><small>architectureId=${escapeHtml(item.architectureId)}</small></label>`;
        })
        .join("");

      availableFileOptionsNode.querySelectorAll('input[type="checkbox"]').forEach((node) => {
        node.addEventListener("change", () => toggleSelectedFile(node.dataset.fileName || ""));
      });
    }
```

```javascript
    function toggleSelectedFile(fileName) {
      if (!fileName) {
        return;
      }

      if (state.selectedFileNames.includes(fileName)) {
        state.selectedFileNames = state.selectedFileNames.filter((item) => item !== fileName);
      } else {
        state.selectedFileNames = [...state.selectedFileNames, fileName];
      }

      renderSelectedFiles();
      renderFilePickerOptions(state.availableFiles);
    }
```

```javascript
    function removeSelectedFile(fileName) {
      state.selectedFileNames = state.selectedFileNames.filter((item) => item !== fileName);
      renderSelectedFiles();
      renderFilePickerOptions(state.availableFiles);
    }
```

Update `loadBootstrap()` and session switching to call:

```javascript
      renderSelectedFiles();
      renderFilePickerOptions(state.availableFiles);
```

and replace old selected-file logic:

```javascript
      state.selectedFileNames = [...item.fileNames];
```

- [ ] **Step 4: Run the test to verify it passes**

Run:

```bash
. .venv/bin/activate && python -m unittest tests.test_chat_debug_routes.ChatDebugRouteTests.test_chat_page_renders_shell -v
```

Expected:

```text
ok
```

- [ ] **Step 5: Commit**

```bash
git add app/templates/debug/chat.html tests/test_chat_debug_routes.py
git commit -m "feat: add chat debug checkbox file picker"
```

### Task 2: Restructure the Right Side into a Single Chat Surface

**Files:**
- Modify: `tests/test_chat_debug_routes.py`
- Modify: `app/templates/debug/chat.html`

- [ ] **Step 1: Write the failing template assertions for the unified chat surface**

Append these assertions in `test_chat_page_renders_shell`:

```python
        self.assertIn('id="chat-shell"', html)
        self.assertIn('id="chat-toolbar"', html)
        self.assertIn('id="chat-context"', html)
        self.assertIn('id="chat-scroll-area"', html)
        self.assertIn('id="chat-composer"', html)
        self.assertNotIn("<h2>聊天记录</h2>", html)
        self.assertNotIn("<h2>SSE 事件流</h2>", html)
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
. .venv/bin/activate && python -m unittest tests.test_chat_debug_routes.ChatDebugRouteTests.test_chat_page_renders_shell -v
```

Expected:

```text
FAIL: 'id="chat-shell"' not found in html
```

- [ ] **Step 3: Rewrite the right-side DOM into a chat-product layout**

In `app/templates/debug/chat.html`, replace the current right-side stack:

```html
      <section class="stack">
        <section class="panel">
          ...
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
```

with:

```html
      <section id="chat-shell" class="panel chat-shell">
        <header id="chat-toolbar" class="chat-toolbar">
          <div class="toolbar-title">
            <h2>聊天</h2>
            <p>面向文件内容的本地联调视图</p>
          </div>
          <div class="toolbar-actions">
            <button id="load-history-button" class="button secondary" type="button">加载历史</button>
            <button id="delete-button" class="button danger" type="button">删除当前会话</button>
          </div>
        </header>

        <section id="chat-context" class="chat-context">
          <label class="form-row chat-id-row">
            <span>chatId</span>
            <input id="chat-id-input" type="text" placeholder="请输入 chatId">
          </label>
          <!-- file picker context block stays here -->
        </section>

        <section id="chat-scroll-area" class="chat-scroll-area">
          <div id="chat-thread" class="message-list"></div>
        </section>

        <section id="chat-composer" class="chat-composer">
          <label class="form-row">
            <span>message</span>
            <textarea id="chat-message-input" placeholder="请输入本轮问题"></textarea>
          </label>
          <div class="button-row">
            <button id="send-button" class="button" type="button">发送消息</button>
          </div>
        </section>
      </section>
```

Add layout styles:

```css
    .chat-shell {
      display: grid;
      gap: 16px;
      min-height: 720px;
    }

    .chat-toolbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
    }

    .chat-context {
      display: grid;
      gap: 16px;
      padding: 16px;
      border: 1px solid #dbe3f0;
      border-radius: 16px;
      background: #f8fafc;
    }

    .chat-scroll-area {
      min-height: 320px;
      max-height: 52vh;
      overflow: auto;
      padding-right: 6px;
    }

    .chat-composer {
      display: grid;
      gap: 12px;
      padding-top: 8px;
      border-top: 1px solid #e2e8f0;
    }
```

Remove duplicate old `load-history-button`, `delete-button`, `chat-id-input`, and `chat-message-input` nodes from their old positions.

- [ ] **Step 4: Run the test to verify it passes**

Run:

```bash
. .venv/bin/activate && python -m unittest tests.test_chat_debug_routes.ChatDebugRouteTests.test_chat_page_renders_shell -v
```

Expected:

```text
ok
```

- [ ] **Step 5: Commit**

```bash
git add app/templates/debug/chat.html tests/test_chat_debug_routes.py
git commit -m "feat: redesign chat debug main surface"
```

### Task 3: Move SSE to the Main Message Flow and Add Collapsible Debug Details

**Files:**
- Modify: `tests/test_chat_debug_routes.py`
- Modify: `app/templates/debug/chat.html`

- [ ] **Step 1: Write the failing template assertions for the collapsible debug view**

Append these assertions in `test_chat_page_renders_shell`:

```python
        self.assertIn('id="debug-details"', html)
        self.assertIn('id="debug-summary"', html)
        self.assertIn('id="chat-events"', html)
        self.assertIn("function renderDebugEventList()", html)
        self.assertNotIn("function renderEventList()", html)
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
. .venv/bin/activate && python -m unittest tests.test_chat_debug_routes.ChatDebugRouteTests.test_chat_page_renders_shell -v
```

Expected:

```text
FAIL: 'id="debug-details"' not found in html
```

- [ ] **Step 3: Add collapsible debug details and route SSE output into the main assistant bubble**

In `app/templates/debug/chat.html`, add this block inside `#chat-composer` after the send button row:

```html
          <details id="debug-details" class="debug-details">
            <summary id="debug-summary">查看调试详情</summary>
            <div id="chat-events" class="event-list"></div>
          </details>
```

Rename the event renderer:

```javascript
    function renderDebugEventList() {
      if (!state.streamEvents.length) {
        eventsNode.innerHTML = '<div class="event-item">暂无调试事件</div>';
        return;
      }

      eventsNode.innerHTML = state.streamEvents
        .map((item) => `<div class="event-item"><strong>${escapeHtml(item.eventName)}</strong><pre>${escapeHtml(JSON.stringify(item.data, null, 2))}</pre></div>`)
        .join("");
    }
```

Update all previous calls from `renderEventList()` to `renderDebugEventList()`.

Refine the streaming state so the main thread is the primary live surface:

```javascript
    function renderThread() {
      const messages = [...state.historyMessages];
      if (state.streamingReply) {
        messages.push({ role: "assistant", content: state.streamingReply, streaming: true });
      }

      if (!messages.length) {
        threadNode.innerHTML = '<div class="message-item empty-thread">暂无消息</div>';
        return;
      }

      threadNode.innerHTML = messages
        .map((item) => {
          const roleClass = item.role === "user" ? "message-item user-message" : "message-item assistant-message";
          const streamingClass = item.streaming ? " streaming-message" : "";
          return `<div class="${roleClass}${streamingClass}"><strong>${escapeHtml(item.role)}</strong><pre>${escapeHtml(item.content)}</pre></div>`;
        })
        .join("");
    }
```

In `handleSseEvent`, keep:

```javascript
      state.streamEvents.push({ eventName, data });
      renderDebugEventList();
```

and preserve:

```javascript
      if (eventName === "textChunk") {
        state.streamingReply += data.content || "";
        renderThread();
        return;
      }
```

Add styles:

```css
    .debug-details {
      border-top: 1px solid #e2e8f0;
      padding-top: 12px;
    }

    .debug-details summary {
      cursor: pointer;
      color: #475569;
      font-weight: 600;
    }

    .user-message {
      margin-left: auto;
      max-width: 78%;
      background: #dbeafe;
    }

    .assistant-message {
      max-width: 88%;
      background: #f8fafc;
    }

    .streaming-message {
      border: 1px dashed #93c5fd;
    }
```

- [ ] **Step 4: Run the test to verify it passes**

Run:

```bash
. .venv/bin/activate && python -m unittest tests.test_chat_debug_routes.ChatDebugRouteTests.test_chat_page_renders_shell -v
```

Expected:

```text
ok
```

- [ ] **Step 5: Commit**

```bash
git add app/templates/debug/chat.html tests/test_chat_debug_routes.py
git commit -m "feat: streamline chat debug sse display"
```

### Task 4: Finalize State Sync for File Removal, Session Switching, and Regression Coverage

**Files:**
- Modify: `tests/test_chat_debug_routes.py`
- Modify: `app/templates/debug/chat.html`
- Optional Modify: `README.md`

- [ ] **Step 1: Write the failing template assertions for session/file sync behavior**

Append these assertions in `test_chat_page_renders_shell`:

```python
        self.assertIn("state.selectedFileNames = [...item.fileNames];", html)
        self.assertIn("renderSelectedFiles();", html)
        self.assertIn("renderFilePickerOptions(state.availableFiles);", html)
        self.assertIn("state.selectedFileNames = state.selectedFileNames.filter((item) => item !== fileName);", html)
        self.assertIn("const fileNames = [...state.selectedFileNames];", html)
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
. .venv/bin/activate && python -m unittest tests.test_chat_debug_routes.ChatDebugRouteTests.test_chat_page_renders_shell -v
```

Expected:

```text
FAIL because one or more selected-file sync strings are missing
```

- [ ] **Step 3: Finalize state synchronization and update README only if needed**

In `app/templates/debug/chat.html`:

1. Update session switching:

```javascript
          state.selectedFileNames = [...item.fileNames];
          renderSelectedFiles();
          renderFilePickerOptions(state.availableFiles);
```

2. Update sending logic:

```javascript
      const fileNames = [...state.selectedFileNames];
```

3. Update bootstrap refresh:

```javascript
      renderSelectedFiles();
      renderFilePickerOptions(state.availableFiles);
      renderDebugEventList();
```

4. Ensure delete/reset clears file selection state only when intended:

```javascript
      state.selectedFileNames = [];
```

If current `README.md` still describes `/debug/chat` as native multi-select or separate SSE panel, update it with a compact summary:

```markdown
- 文件选择器以“已选标签 + 添加文件面板”展示，支持勾选与取消勾选
- SSE 主输出在聊天区实时显示，调试事件收纳于折叠详情中
```

- [ ] **Step 4: Run the regression suite**

Run:

```bash
. .venv/bin/activate && python -m unittest tests.test_chat_debug_routes tests.test_callback_debug_routes tests.test_chat -v
```

Expected:

```text
OK
```

- [ ] **Step 5: Commit**

```bash
git add app/templates/debug/chat.html tests/test_chat_debug_routes.py README.md
git commit -m "feat: polish chat debug experience"
```

## Self-Review Checklist

- Spec coverage: 文件勾选面板、已选标签区、右侧单一聊天主区域、主消息区 SSE 实时展示、折叠调试详情、chatId 明显可见、左侧列表基本保持不变，这些要求都落在任务 1-4 中。
- Placeholder scan: 计划中没有占位描述；每一步都给了具体 DOM、函数名、断言、命令和提交动作。
- Type consistency: 计划始终以 `state.selectedFileNames` 作为文件选择唯一真相源，以 `state.streamingReply` 和 `state.streamEvents` 分别承担主展示与调试展示；命名在各任务中保持一致。

