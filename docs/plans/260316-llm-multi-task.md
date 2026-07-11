# LLM Multi-Task Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extend the formal `/llm/*` integration to support multi-file analysis submission, serial task execution, batch `check-task`, and single-WebSocket multi-subscription while preserving existing single-task compatibility.

**Architecture:** Keep the existing Flask `llm` blueprint and SQLite task table, but expand request parsing from single `params[0]` to batch-aware helpers. Analysis batches are accepted together and executed serially in one background worker, while `check-task` and `/llm/progress` gain batch-aware read paths without introducing new persistence layers.

**Tech Stack:** Flask, Flask-Sock, SQLite (`sqlite3`), Python `threading`, Python `unittest`, PowerShell 7.

---

### Task 1: 先补任务服务与路由层的批量测试

**Files:**
- Modify: `tests/test_llm_task_service.py`
- Modify: `tests/test_llm_routes.py`
- Modify: `tests/test_llm_progress_and_check_task.py`
- Test: `tests/test_llm_task_service.py`
- Test: `tests/test_llm_routes.py`
- Test: `tests/test_llm_progress_and_check_task.py`

**Step 1: Write the failing test**

在 `tests/test_llm_task_service.py` 增加“未开始任务”和“批量读取快照”测试：

```python
def test_create_file_task_can_start_as_pending(self):
    task = service.create_file_task(file_name="demo-2.pdf", request_payload={"businessType": "file"}, status="0")
    self.assertEqual(task["status"], "0")
    self.assertEqual(task["progress"], 0.0)

def test_get_tasks_returns_snapshots_in_request_order(self):
    service.create_file_task("a.pdf", {"businessType": "file"}, status="1")
    service.create_file_task("b.pdf", {"businessType": "file"}, status="0")
    tasks = service.get_tasks("file", ["a.pdf", "b.pdf"])
    self.assertEqual([item["business_key"] for item in tasks], ["a.pdf", "b.pdf"])
```

在 `tests/test_llm_routes.py` 增加多文件受理测试：

```python
@patch("app.blueprints.llm.threading.Thread")
def test_analysis_accepts_multiple_files_and_starts_one_batch_thread(self, mock_thread):
    response = self.client.post(
        "/llm/analysis",
        json={
            "businessType": "file",
            "params": [
                {"fileName": "a.txt", "filePath": "http://127.0.0.1:8000/a.txt"},
                {"fileName": "b.txt", "filePath": "http://127.0.0.1:8000/b.txt"},
            ],
        },
    )
    self.assertEqual(response.status_code, 202)
    self.assertEqual(len(response.get_json()["tasks"]), 2)
    mock_thread.assert_called_once()
```

在 `tests/test_llm_progress_and_check_task.py` 增加批量 `check-task` 的返回测试。

**Step 2: Run test to verify it fails**

Run: `& 'C:\Program Files\PowerShell\7\pwsh.exe' -NoLogo -Command "python -m unittest tests.test_llm_task_service tests.test_llm_routes tests.test_llm_progress_and_check_task -v"`

Expected: FAIL，因为当前实现还不支持待开始任务、批量快照、多文件受理和批量查询。

**Step 3: Write minimal implementation**

先只在服务和路由层补最小能力：

```python
def create_file_task(self, file_name: str, request_payload: dict, status: str = "1") -> dict:
    return self._upsert_task("file", file_name, request_payload, status=status)

def get_tasks(self, business_type: str, business_keys: list[str]) -> list[dict]:
    return [task for key in business_keys if (task := self.get_task(business_type, key))]
```

- 扩展任务服务支持创建 `status="0"` 的待开始任务。
- 增加按输入顺序批量读取任务快照的方法。
- 路由层从“只认 `params[0]`”改成“校验整个 `params` 列表并允许多项”。

**Step 4: Run test to verify it passes**

Run: `& 'C:\Program Files\PowerShell\7\pwsh.exe' -NoLogo -Command "python -m unittest tests.test_llm_task_service tests.test_llm_routes tests.test_llm_progress_and_check_task -v"`

Expected: PASS。

**Step 5: Commit**

```bash
git add tests/test_llm_task_service.py tests/test_llm_routes.py tests/test_llm_progress_and_check_task.py app/services/task_service.py app/blueprints/llm.py
git commit -m "feat: add batch llm task acceptance primitives"
```

### Task 2: 实现 `/llm/analysis` 多文件串行受理与执行

**Files:**
- Modify: `app/blueprints/llm.py`
- Modify: `app/services/llm_analysis_service.py`
- Modify: `tests/test_llm_routes.py`
- Modify: `tests/test_llm_analysis_service.py`
- Test: `tests/test_llm_analysis_service.py`

**Step 1: Write the failing test**

在 `tests/test_llm_analysis_service.py` 增加串行批次测试：

```python
def test_run_file_analysis_batch_processes_files_in_order(self):
    transitions = []

    def fake_single(*, task_service, params, **kwargs):
        task = task_service.get_task("file", params["fileName"])
        transitions.append((params["fileName"], task["status"], task["progress"]))
        task_service.mark_business_result("file", params["fileName"], {"ok": True}, status="2", message="完成")

    request_payload = {
        "businessType": "file",
        "params": [
            {"fileName": "a.txt", "filePath": "http://127.0.0.1:8000/a.txt"},
            {"fileName": "b.txt", "filePath": "http://127.0.0.1:8000/b.txt"},
        ],
    }
```

并断言：

- `a.txt` 先进入处理中
- `b.txt` 在 `a.txt` 完成前保持 `status=0`
- `a.txt` 结束后 `b.txt` 才切到 `status=1`

在 `tests/test_llm_routes.py` 增加重复 `fileName` 返回 `400`、进行中任务冲突返回 `409` 的测试。

**Step 2: Run test to verify it fails**

Run: `& 'C:\Program Files\PowerShell\7\pwsh.exe' -NoLogo -Command "python -m unittest tests.test_llm_routes tests.test_llm_analysis_service -v"`

Expected: FAIL，因为当前后台处理函数只会读取 `params[0]`，也没有串行批次执行器。

**Step 3: Write minimal implementation**

把文件分析服务拆成两个入口：

```python
def run_single_file_analysis_task(*, task_service, progress_hub, params, download_root, callback_url, callback_timeout) -> None:
    ...

def run_file_analysis_batch_task(*, task_service, progress_hub, request_payload, download_root, callback_url, callback_timeout) -> None:
    for index, params in enumerate(request_payload["params"]):
        file_name = _as_text(params.get("fileName"))
        if index > 0:
            task_service.update_task_progress("file", file_name, progress=0.0, message="准备开始解析", status="1")
            _publish_progress(progress_hub, file_name, 0.0)
        run_single_file_analysis_task(...)
```

- 路由层多文件请求只启动一个批次线程。
- 首个任务创建为 `status=1`，后续任务创建为 `status=0`。
- 批次线程内部按顺序调用单文件处理函数。
- 单文件请求继续复用同一套处理函数，保证兼容。

**Step 4: Run test to verify it passes**

Run: `& 'C:\Program Files\PowerShell\7\pwsh.exe' -NoLogo -Command "python -m unittest tests.test_llm_routes tests.test_llm_analysis_service -v"`

Expected: PASS。

**Step 5: Commit**

```bash
git add app/blueprints/llm.py app/services/analysis_service.py tests/test_llm_routes.py tests/test_llm_analysis_service.py
git commit -m "feat: support serial multi-file llm analysis"
```

### Task 3: 实现批量 `check-task` 返回与逐项回调补发

**Files:**
- Modify: `app/blueprints/llm.py`
- Modify: `app/services/llm_task_service.py`
- Modify: `tests/test_llm_progress_and_check_task.py`
- Test: `tests/test_llm_progress_and_check_task.py`

**Step 1: Write the failing test**

在 `tests/test_llm_progress_and_check_task.py` 增加：

```python
def test_batch_check_task_returns_per_item_status_and_replay_flag(self):
    service.create_file_task("a.pdf", {"businessType": "file"}, status="1")
    service.create_file_task("b.pdf", {"businessType": "file"}, status="0")
```

然后通过 Flask client 调用：

```python
response = client.post(
    "/llm/check-task",
    json={
        "businessType": "file",
        "params": [{"fileName": "a.pdf"}, {"fileName": "b.pdf"}],
    },
)
```

断言：

- `response.status_code == 200`
- `data` 为数组
- 每项都包含 `fileName`、`status`、`progress`、`callbackStatus`

再加一个不存在任务的批量查询测试，断言该项返回 `exists=False` 而不是整体 404。

**Step 2: Run test to verify it fails**

Run: `& 'C:\Program Files\PowerShell\7\pwsh.exe' -NoLogo -Command "python -m unittest tests.test_llm_progress_and_check_task -v"`

Expected: FAIL，因为当前 `/llm/check-task` 只读取首个 `params[0]`。

**Step 3: Write minimal implementation**

路由层改成区分单项和多项：

```python
if len(params_list) == 1:
    return jsonify(single_item_payload)

return jsonify({
    "businessType": business_type,
    "data": item_payloads,
})
```

- 抽出“把任务记录格式化成甲方返回结构”的辅助函数。
- 批量模式下逐项执行回调补发判断。
- 单项模式保持现有返回结构兼容。

**Step 4: Run test to verify it passes**

Run: `& 'C:\Program Files\PowerShell\7\pwsh.exe' -NoLogo -Command "python -m unittest tests.test_llm_progress_and_check_task -v"`

Expected: PASS。

**Step 5: Commit**

```bash
git add app/blueprints/llm.py app/services/task_service.py tests/test_llm_progress_and_check_task.py
git commit -m "feat: add batch llm check-task responses"
```

### Task 4: 实现单连接多订阅 WebSocket 与 `query/unsubscribe`

**Files:**
- Modify: `app/blueprints/llm.py`
- Modify: `app/services/llm_progress_hub.py`
- Modify: `tests/test_llm_progress_and_check_task.py`
- Test: `tests/test_llm_progress_and_check_task.py`

**Step 1: Write the failing test**

先给路由模块中的消息解析辅助函数补测试，避免直接写复杂 WebSocket 集成：

```python
def test_parse_progress_message_supports_legacy_subscribe(self):
    payload = {"businessType": "file", "params": [{"fileName": "a.pdf"}]}
    command = _parse_progress_command(payload)
    self.assertEqual(command["action"], "subscribe")
    self.assertEqual(command["keys"], [("file", "a.pdf")])

def test_parse_progress_message_supports_query_and_unsubscribe(self):
    payload = {"action": "query", "businessType": "file", "params": [{"fileName": "a.pdf"}, {"fileName": "b.pdf"}]}
    command = _parse_progress_command(payload)
    self.assertEqual(len(command["keys"]), 2)
```

再补 `LLMProgressHub` 的多订阅测试：

```python
def test_progress_hub_keeps_latest_message_per_task(self):
    hub.publish("file", "a.pdf", {...})
    hub.publish("file", "b.pdf", {...})
    self.assertEqual(hub.get_latest("file", "a.pdf")["data"]["fileName"], "a.pdf")
```

**Step 2: Run test to verify it fails**

Run: `& 'C:\Program Files\PowerShell\7\pwsh.exe' -NoLogo -Command "python -m unittest tests.test_llm_progress_and_check_task -v"`

Expected: FAIL，因为当前没有命令解析辅助函数，也不支持一个连接上管理多个订阅。

**Step 3: Write minimal implementation**

在 [app/blueprints/llm.py](/e:/DocSense/app/blueprints/llm.py) 中先抽出纯函数：

```python
def _parse_progress_command(payload: dict) -> dict:
    action = payload.get("action") or "subscribe"
    ...
    return {"action": action, "business_type": business_type, "keys": keys}
```

在 [app/services/llm_progress_hub.py](/e:/DocSense/app/services/llm_progress_hub.py) 中补只读接口：

```python
def get_latest(self, business_type: str, business_key: str) -> dict | None:
    return self._latest.get((business_type, business_key))
```

再把 WebSocket 路由改成：

- 连接建立后循环读取消息
- `subscribe` 为当前连接注册多个键
- `query` 逐项返回当前快照
- `unsubscribe` 移除指定键
- 非法消息返回 `{"type": "error", ...}`，不主动断连

**Step 4: Run test to verify it passes**

Run: `& 'C:\Program Files\PowerShell\7\pwsh.exe' -NoLogo -Command "python -m unittest tests.test_llm_progress_and_check_task -v"`

Expected: PASS。

**Step 5: Commit**

```bash
git add app/blueprints/llm.py app/services/progress_hub.py tests/test_llm_progress_and_check_task.py
git commit -m "feat: support multi-subscription llm progress websocket"
```

### Task 5: 更新文档、样例与回归验证

**Files:**
- Modify: `README.md`
- Modify: `api-test.md`
- Modify: `scripts/test_llm_progress.ps1`
- Modify: `tests/fixtures/llm/analysis_request.json`
- Modify: `tests/fixtures/llm/check_task_file_request.json`
- Test: `tests/test_llm_routes.py`
- Test: `tests/test_llm_task_service.py`
- Test: `tests/test_llm_analysis_service.py`
- Test: `tests/test_llm_progress_and_check_task.py`

**Step 1: Write the failing test**

先补一个样例文件测试，确保批量请求样例存在：

```python
def test_analysis_request_fixture_can_represent_multiple_files(self):
    payload = json.loads(Path("tests/fixtures/llm/analysis_request.json").read_text(encoding="utf-8"))
    self.assertGreaterEqual(len(payload["params"]), 2)
```

并在 `README.md` 对应说明中加入：

- 多文件请求是串行处理
- 批量 `check-task` 返回数组 `data`
- `progress` 支持 `subscribe/query/unsubscribe`

**Step 2: Run test to verify it fails**

Run: `& 'C:\Program Files\PowerShell\7\pwsh.exe' -NoLogo -Command "python -m unittest tests.test_llm_test_assets -v"`

Expected: FAIL，因为现有样例还是单文件模式。

**Step 3: Write minimal implementation**

- 更新请求样例为批量文件。
- 更新 `scripts/test_llm_progress.ps1`，增加发送 `query` 和多订阅示例。
- 在 [README.md](/e:/DocSense/README.md) 中补充新的接口行为说明。
- 在 [api-test.md](/e:/DocSense/api-test.md) 中补充“本项目兼容扩展说明”，避免联调时误解“只支持 `params[0]`”。

**Step 4: Run test to verify it passes**

Run: `& 'C:\Program Files\PowerShell\7\pwsh.exe' -NoLogo -Command "python -m unittest tests.test_llm_routes tests.test_llm_task_service tests.test_llm_analysis_service tests.test_llm_progress_and_check_task tests.test_llm_test_assets -v"`

Expected: PASS。

**Step 5: Commit**

```bash
git add README.md api-test.md scripts/test_llm_progress.ps1 tests/fixtures/llm/analysis_request.json tests/fixtures/llm/check_task_file_request.json tests/test_llm_test_assets.py
git commit -m "docs: document batch llm task behavior"
```
