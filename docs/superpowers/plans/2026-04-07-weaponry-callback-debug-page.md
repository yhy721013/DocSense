# Weaponry Callback Debug Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 DocSense 的本地回调调试页增加 `weaponry` 结果展示，并补齐舰艇字段样例、macOS 联调脚本、README 说明与回归测试。

**Architecture:** 保持现有 Flask `debug` 页面架构不变，继续由 `app/templates/debug/callback.html` 承载原生 JS 渲染逻辑，新增 `weaponry` 专用渲染分支和递归字段/溯源展示函数。联调入口沿用仓库现有脚本组织方式，在 `tests/fixtures/llm/` 中补全舰艇请求样例，在 `scripts/` 中补全 macOS `zsh` 脚本，并通过 `unittest` 覆盖模板壳层、脚本行为和测试样例契约。

**Tech Stack:** Flask, Jinja2 template, 原生 JavaScript, zsh, PowerShell, Python `unittest`

---

## Execution Notes

1. 仓库 AGENTS.md 明确禁止 `git-worktree`，因此本计划在当前分支直接执行，依赖小步提交控制风险。
2. 首轮验证环境固定为本机 macOS。
3. 回调调试页依赖 `.runtime/call_back.json`，手工联调时要同时启动 `scripts/mock_callback_server.py` 并配置 `CALLBACK_URL`。

## File Structure

- Modify: `app/templates/debug/callback.html`
  - 新增 `weaponry` 状态映射、字段统计、INPUT/TABLE 渲染和溯源折叠逻辑。
- Create: `scripts/test_llm_weaponry.sh`
  - 复用 `scripts/_script_common.sh`，提供 macOS 的 `POST /llm/weaponry` 联调脚本。
- Modify: `scripts/test_llm_weaponry.ps1`
  - 如需，仅做注释或参数默认值对齐；不改变其基本调用方式。
- Modify: `tests/fixtures/llm/weaponry_request.json`
  - 替换为舰艇 20 字段请求样例。
- Modify: `tests/fixtures/llm/check_task_weaponry_request.json`
  - 与 `weaponry_request.json` 的 `architectureId` 对齐。
- Modify: `tests/test_test_assets.py`
  - 校验舰艇字段样例的结构、字段顺序和 `architectureId` 一致性。
- Modify: `tests/test_local_scripts.py`
  - 新增 `weaponry` 脚本回归测试。
- Modify: `tests/test_callback_debug_routes.py`
  - 新增 `weaponry` payload API 读取测试和模板渲染钩子断言。
- Modify: `README.md`
  - 把 `weaponry` 纳入双平台联调说明和调试页联调建议。

### Task 1: Replace Weaponry Fixtures With Ship Fields

**Files:**
- Modify: `tests/fixtures/llm/weaponry_request.json`
- Modify: `tests/fixtures/llm/check_task_weaponry_request.json`
- Modify: `tests/test_test_assets.py`

- [ ] **Step 1: Write the failing asset contract test**

```python
import json
import pathlib
import unittest


class LLMTestAssetsTests(unittest.TestCase):
    def test_weaponry_request_fixture_uses_ship_fields(self):
        payload = json.loads(pathlib.Path("tests/fixtures/llm/weaponry_request.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["businessType"], "weaponry")

        params = payload["params"]
        self.assertEqual(params["architectureId"], 10502)

        field_names = [field["fieldName"] for field in params["weaponryTemplateFieldList"]]
        self.assertEqual(
            field_names,
            [
                "舰级名称",
                "单舰名称",
                "舷号",
                "建造厂",
                "开工时间",
                "下水时间",
                "服役时间",
                "状态",
                "标准排水量",
                "满载排水量",
                "舰长",
                "舰宽",
                "吃水",
                "甲板长度",
                "甲板宽度",
                "航速",
                "编制",
                "动力系统",
                "武器系统",
                "传感器系统",
            ],
        )

        for field in params["weaponryTemplateFieldList"]:
            self.assertEqual(field["fieldType"], "INPUT")
            self.assertIn("fieldDescription", field)
            self.assertNotIn("analyseData", field)
            self.assertNotIn("analyseDataSource", field)

    def test_check_task_weaponry_fixture_matches_request_architecture_id(self):
        request_payload = json.loads(pathlib.Path("tests/fixtures/llm/weaponry_request.json").read_text(encoding="utf-8"))
        check_payload = json.loads(pathlib.Path("tests/fixtures/llm/check_task_weaponry_request.json").read_text(encoding="utf-8"))

        self.assertEqual(check_payload["businessType"], "weaponry")
        self.assertEqual(check_payload["params"][0]["architectureId"], request_payload["params"]["architectureId"])
```

- [ ] **Step 2: Run the asset test and verify it fails**

Run:

```bash
.venv/bin/python -m unittest tests.test_test_assets.LLMTestAssetsTests.test_weaponry_request_fixture_uses_ship_fields tests.test_test_assets.LLMTestAssetsTests.test_check_task_weaponry_fixture_matches_request_architecture_id -v
```

Expected:

```text
FAIL: test_weaponry_request_fixture_uses_ship_fields
AssertionError: Lists differ: ['任职人员姓名', ...] != ['舰级名称', ...]
```

- [ ] **Step 3: Replace the request fixtures with the ship-field sample**

`tests/fixtures/llm/weaponry_request.json`

```json
{
  "businessType": "weaponry",
  "params": {
    "architectureId": 10502,
    "weaponryTemplateFieldList": [
      { "fieldName": "舰级名称", "fieldType": "INPUT", "fieldDescription": "根据文档提取舰级名称", "templateClassifyId": 1001 },
      { "fieldName": "单舰名称", "fieldType": "INPUT", "fieldDescription": "根据文档提取单舰名称", "templateClassifyId": 1001 },
      { "fieldName": "舷号", "fieldType": "INPUT", "fieldDescription": "根据文档提取舷号", "templateClassifyId": 1001 },
      { "fieldName": "建造厂", "fieldType": "INPUT", "fieldDescription": "根据文档提取建造厂", "templateClassifyId": 1001 },
      { "fieldName": "开工时间", "fieldType": "INPUT", "fieldDescription": "根据文档提取开工时间", "templateClassifyId": 1001 },
      { "fieldName": "下水时间", "fieldType": "INPUT", "fieldDescription": "根据文档提取下水时间", "templateClassifyId": 1001 },
      { "fieldName": "服役时间", "fieldType": "INPUT", "fieldDescription": "根据文档提取服役时间", "templateClassifyId": 1001 },
      { "fieldName": "状态", "fieldType": "INPUT", "fieldDescription": "根据文档提取舰艇状态", "templateClassifyId": 1001 },
      { "fieldName": "标准排水量", "fieldType": "INPUT", "fieldDescription": "根据文档提取标准排水量", "templateClassifyId": 1001 },
      { "fieldName": "满载排水量", "fieldType": "INPUT", "fieldDescription": "根据文档提取满载排水量", "templateClassifyId": 1001 },
      { "fieldName": "舰长", "fieldType": "INPUT", "fieldDescription": "根据文档提取舰长", "templateClassifyId": 1001 },
      { "fieldName": "舰宽", "fieldType": "INPUT", "fieldDescription": "根据文档提取舰宽", "templateClassifyId": 1001 },
      { "fieldName": "吃水", "fieldType": "INPUT", "fieldDescription": "根据文档提取吃水", "templateClassifyId": 1001 },
      { "fieldName": "甲板长度", "fieldType": "INPUT", "fieldDescription": "根据文档提取甲板长度", "templateClassifyId": 1001 },
      { "fieldName": "甲板宽度", "fieldType": "INPUT", "fieldDescription": "根据文档提取甲板宽度", "templateClassifyId": 1001 },
      { "fieldName": "航速", "fieldType": "INPUT", "fieldDescription": "根据文档提取航速", "templateClassifyId": 1001 },
      { "fieldName": "编制", "fieldType": "INPUT", "fieldDescription": "根据文档提取编制", "templateClassifyId": 1001 },
      { "fieldName": "动力系统", "fieldType": "INPUT", "fieldDescription": "根据文档提取动力系统", "templateClassifyId": 1001 },
      { "fieldName": "武器系统", "fieldType": "INPUT", "fieldDescription": "根据文档提取武器系统", "templateClassifyId": 1001 },
      { "fieldName": "传感器系统", "fieldType": "INPUT", "fieldDescription": "根据文档提取传感器系统", "templateClassifyId": 1001 }
    ]
  }
}
```

`tests/fixtures/llm/check_task_weaponry_request.json`

```json
{
  "businessType": "weaponry",
  "params": [
    {
      "architectureId": 10502
    }
  ]
}
```

- [ ] **Step 4: Run the asset test and verify it passes**

Run:

```bash
.venv/bin/python -m unittest tests.test_test_assets.LLMTestAssetsTests.test_weaponry_request_fixture_uses_ship_fields tests.test_test_assets.LLMTestAssetsTests.test_check_task_weaponry_fixture_matches_request_architecture_id -v
```

Expected:

```text
OK
```

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/llm/weaponry_request.json tests/fixtures/llm/check_task_weaponry_request.json tests/test_test_assets.py
git commit -m "test: update weaponry fixtures for ship fields"
```

### Task 2: Add macOS Weaponry Script And Regression Test

**Files:**
- Create: `scripts/test_llm_weaponry.sh`
- Modify: `tests/test_local_scripts.py`

- [ ] **Step 1: Write the failing script regression test**

```python
def test_weaponry_shell_script_posts_fixture_to_expected_path(self) -> None:
    _, port = self._start_recording_server()
    payload = ROOT_DIR / "tests/fixtures/llm/weaponry_request.json"

    result = self._run_script(f"scripts/test_llm_weaponry{_script_ext()}", f"http://127.0.0.1:{port}", str(payload))

    self.assertEqual(result.returncode, 0, msg=result.stderr)
    self.assertIsNotNone(RequestRecorderHandler.last_request)
    self.assertEqual(RequestRecorderHandler.last_request["path"], "/llm/weaponry")
    posted_body = RequestRecorderHandler.last_request["body"].strip()
    expected_body = payload.read_text(encoding="utf-8").strip()
    self.assertEqual(posted_body, expected_body)
```

- [ ] **Step 2: Run the script regression test and verify it fails on macOS**

Run:

```bash
.venv/bin/python -m unittest tests.test_local_scripts.LocalScriptTests.test_weaponry_shell_script_posts_fixture_to_expected_path -v
```

Expected on macOS:

```text
FAIL: test_weaponry_shell_script_posts_fixture_to_expected_path
AssertionError: ... No such file or directory: 'scripts/test_llm_weaponry.sh'
```

- [ ] **Step 3: Add the zsh script**

`scripts/test_llm_weaponry.sh`

```zsh
#!/bin/zsh

set -euo pipefail

SCRIPT_DIR="${0:A:h}"
source "${SCRIPT_DIR}/_script_common.sh"

BASE_URL="${1:-}"
PAYLOAD_PATH="${2:-${ROOT_DIR}/tests/fixtures/llm/weaponry_request.json}"

load_env_file
if [[ -z "${BASE_URL}" ]]; then
  BASE_URL="$(default_base_url)"
fi

post_json "${BASE_URL}/llm/weaponry" "${PAYLOAD_PATH}"
```

- [ ] **Step 4: Run the script regression test and verify it passes**

Run:

```bash
.venv/bin/python -m unittest tests.test_local_scripts.LocalScriptTests.test_weaponry_shell_script_posts_fixture_to_expected_path -v
```

Expected:

```text
OK
```

- [ ] **Step 5: Commit**

```bash
git add scripts/test_llm_weaponry.sh tests/test_local_scripts.py
git commit -m "test: add macOS weaponry script coverage"
```

### Task 3: Add Weaponry Callback Page Coverage

**Files:**
- Modify: `tests/test_callback_debug_routes.py`
- Modify: `app/templates/debug/callback.html`

- [ ] **Step 1: Write the failing debug page tests**

Add this payload test:

```python
def test_callback_api_returns_payload_for_weaponry_callback(self):
    payload = {
        "businessType": "weaponry",
        "data": {
            "status": "2",
            "architectureId": 10502,
            "weaponryTemplateFieldList": [
                {
                    "fieldName": "舰级名称",
                    "fieldType": "INPUT",
                    "fieldDescription": "根据文档提取舰级名称",
                    "analyseData": "尼米兹级",
                    "analyseDataSource": [
                        {
                            "content": "舰级名称为尼米兹级",
                            "source": "CVN 文档片段",
                            "time": "2026-04-07 12:00:00",
                            "translate": "舰级名称为尼米兹级"
                        }
                    ]
                }
            ]
        },
        "msg": "解析成功",
    }
    self.callback_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    response = self.client.get("/debug/api/callback")
    data = response.get_json()

    self.assertEqual(response.status_code, 200)
    self.assertTrue(data["ok"])
    self.assertEqual(data["payload"]["businessType"], "weaponry")
    self.assertEqual(data["payload"]["data"]["architectureId"], 10502)
```

Add this template hook test:

```python
def test_callback_page_contains_renderer_hooks_for_weaponry(self):
    response = self.client.get("/debug/callback")
    html = response.get_data(as_text=True)

    self.assertEqual(response.status_code, 200)
    self.assertIn("function countWeaponryStats(fields)", html)
    self.assertIn("function renderWeaponrySources(sources)", html)
    self.assertIn("function renderWeaponryField(field)", html)
    self.assertIn("function renderWeaponryPayload(payload)", html)
    self.assertIn('if (result.payload.businessType === "weaponry")', html)
    self.assertIn("renderWeaponryPayload(result.payload)", html)
```

- [ ] **Step 2: Run the callback debug tests and verify the template hook test fails**

Run:

```bash
.venv/bin/python -m unittest tests.test_callback_debug_routes.CallbackDebugRouteTests.test_callback_api_returns_payload_for_weaponry_callback tests.test_callback_debug_routes.CallbackDebugRouteTests.test_callback_page_contains_renderer_hooks_for_weaponry -v
```

Expected:

```text
FAIL: test_callback_page_contains_renderer_hooks_for_weaponry
AssertionError: 'function renderWeaponryPayload(payload)' not found in ...
```

- [ ] **Step 3: Add the minimal weaponry renderer**

In `app/templates/debug/callback.html`, make these focused changes.

Extend `statusText`:

```javascript
function statusText(businessType, status) {
  if (businessType === "file" && status === "2") return "解析成功";
  if (businessType === "file" && status === "3") return "解析失败";
  if (businessType === "report" && status === "1") return "生成成功";
  if (businessType === "report" && status === "2") return "生成失败";
  if (businessType === "weaponry" && status === "2") return "解析成功";
  if (businessType === "weaponry" && status === "3") return "解析失败";
  return `未知状态（${status || "空"}）`;
}
```

Add stats and source helpers:

```javascript
function countWeaponryStats(fields) {
  const stats = { totalFields: 0, populatedFields: 0, tableFields: 0 };

  function visit(field) {
    if (!field || typeof field !== "object") return;
    if (field.fieldType === "TABLE") {
      stats.tableFields += 1;
      const rows = Array.isArray(field.tableFieldList) ? field.tableFieldList : [];
      rows.forEach((row) => Array.isArray(row) && row.forEach(visit));
      return;
    }

    stats.totalFields += 1;
    if (String(field.analyseData || "").trim()) {
      stats.populatedFields += 1;
    }
  }

  (Array.isArray(fields) ? fields : []).forEach(visit);
  return stats;
}

function renderWeaponrySources(sources) {
  const wrapper = document.createElement("details");
  const summaryNode = document.createElement("summary");
  const list = Array.isArray(sources) ? sources : [];
  summaryNode.textContent = `查看溯源（${list.length}）`;
  wrapper.appendChild(summaryNode);

  if (!list.length) {
    const empty = document.createElement("pre");
    empty.textContent = "暂无内容";
    wrapper.appendChild(empty);
    return wrapper;
  }

  list.forEach((item) => {
    const block = document.createElement("pre");
    block.textContent = [
      `content: ${displayValue(item.content)}`,
      `source: ${displayValue(item.source)}`,
      `time: ${displayValue(item.time)}`,
      `translate: ${displayValue(item.translate)}`,
    ].join("\n");
    wrapper.appendChild(block);
  });

  return wrapper;
}
```

Add field renderers:

```javascript
function renderWeaponryField(field) {
  const section = document.createElement("section");
  section.className = "panel";

  const heading = document.createElement("h2");
  heading.textContent = displayValue(field.fieldName);
  section.appendChild(heading);

  const fields = [
    { label: "字段说明", value: field.fieldDescription },
    { label: "提取结果", value: field.analyseData },
  ];
  const grid = document.createElement("div");
  grid.className = "field-grid";

  fields.forEach((item) => {
    const row = document.createElement("div");
    row.className = "field-row";
    const label = document.createElement("span");
    label.className = "field-label";
    label.textContent = item.label;
    row.appendChild(label);
    row.appendChild(renderFieldValue(item));
    grid.appendChild(row);
  });

  section.appendChild(grid);
  section.appendChild(renderWeaponrySources(field.analyseDataSource));
  structured.appendChild(section);
}

function renderWeaponryTable(field) {
  const section = document.createElement("section");
  section.className = "panel";
  section.innerHTML = `<h2>${escapeHtml(displayValue(field.fieldName))}</h2>`;

  const rows = Array.isArray(field.tableFieldList) ? field.tableFieldList : [];
  rows.forEach((row, rowIndex) => {
    const rowBlock = document.createElement("div");
    rowBlock.className = "field-grid";

    (Array.isArray(row) ? row : []).forEach((cell) => {
      const cellBlock = document.createElement("div");
      cellBlock.className = "field-row";
      cellBlock.innerHTML = `
        <span class="field-label">${escapeHtml(displayValue(cell.fieldName))}</span>
        <span class="field-value">${escapeHtml(displayValue(cell.analyseData))}</span>
      `;
      cellBlock.appendChild(renderWeaponrySources(cell.analyseDataSource));
      rowBlock.appendChild(cellBlock);
    });

    const rowTitle = document.createElement("p");
    rowTitle.textContent = `第 ${rowIndex + 1} 行`;
    section.appendChild(rowTitle);
    section.appendChild(rowBlock);
  });

  structured.appendChild(section);
}
```

Add the payload renderer and branch:

```javascript
function renderWeaponryPayload(payload) {
  const data = payload.data || {};
  const fields = Array.isArray(data.weaponryTemplateFieldList) ? data.weaponryTemplateFieldList : [];
  const stats = countWeaponryStats(fields);

  renderSummaryItems([
    { label: "businessType", value: payload.businessType },
    { label: "msg", value: payload.msg },
    { label: "architectureId", value: data.architectureId },
    { label: "status", value: statusText("weaponry", data.status) },
    { label: "字段总数", value: stats.totalFields },
    { label: "已提取字段数", value: stats.populatedFields },
    { label: "表格字段数", value: stats.tableFields },
  ]);

  if (!fields.length) {
    renderFieldGrid("解析结果", [{ label: "提示", value: payload.msg || "暂无内容" }]);
    return;
  }

  fields.forEach((field) => {
    if (field.fieldType === "TABLE") {
      renderWeaponryTable(field);
      return;
    }
    renderWeaponryField(field);
  });
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

  if (result.payload.businessType === "weaponry") {
    renderWeaponryPayload(result.payload);
    return;
  }

  renderUnsupportedPayload(result.payload);
}
```

- [ ] **Step 4: Run the callback debug tests and verify they pass**

Run:

```bash
.venv/bin/python -m unittest tests.test_callback_debug_routes.CallbackDebugRouteTests.test_callback_api_returns_payload_for_weaponry_callback tests.test_callback_debug_routes.CallbackDebugRouteTests.test_callback_page_contains_renderer_hooks_for_weaponry -v
```

Expected:

```text
OK
```

- [ ] **Step 5: Commit**

```bash
git add app/templates/debug/callback.html tests/test_callback_debug_routes.py
git commit -m "feat: render weaponry callback payloads"
```

### Task 4: Update README For Weaponry Local Debugging

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Update the local debugging section**

Replace the relevant README snippets so they include `weaponry` in both platform lists.

PowerShell block:

```powershell
pwsh -NoLogo -Command "./scripts/start_test_file_server.ps1"
pwsh -NoLogo -Command "python scripts/mock_callback_server.py"
pwsh -NoLogo -Command "./scripts/test_llm_analysis.ps1"
pwsh -NoLogo -Command "./scripts/test_llm_report.ps1"
pwsh -NoLogo -Command "./scripts/test_llm_weaponry.ps1"
pwsh -NoLogo -Command "./scripts/test_llm_check_task.ps1"
pwsh -NoLogo -Command "./scripts/test_llm_progress.ps1"
```

macOS block:

```bash
zsh scripts/start_test_file_server.sh
python scripts/mock_callback_server.py
zsh scripts/test_llm_analysis.sh
zsh scripts/test_llm_report.sh
zsh scripts/test_llm_weaponry.sh
zsh scripts/test_llm_check_task.sh
zsh scripts/test_llm_progress.sh
```

Default behavior bullet:

```markdown
- `test_llm_weaponry.sh` 默认请求 `POST /llm/weaponry`
```

Example command:

```bash
zsh scripts/test_llm_weaponry.sh http://127.0.0.1:5001 tests/fixtures/llm/weaponry_request.json
```

Debug page suggestion:

```markdown
2. 触发一次 `/llm/analysis`、`/llm/generate-report` 或 `/llm/weaponry`
```

- [ ] **Step 2: Verify the README mentions all weaponry entry points**

Run:

```bash
rg -n "test_llm_weaponry|/llm/weaponry|mock_callback_server" README.md
```

Expected:

```text
README.md:... test_llm_weaponry.ps1
README.md:... test_llm_weaponry.sh
README.md:... /llm/weaponry
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: add weaponry local debug instructions"
```

### Task 5: Run macOS-First Verification

**Files:**
- Verify only: `tests/test_test_assets.py`
- Verify only: `tests/test_local_scripts.py`
- Verify only: `tests/test_callback_debug_routes.py`
- Verify only: `README.md`

- [ ] **Step 1: Run the targeted automated tests on macOS**

Run:

```bash
.venv/bin/python -m unittest \
  tests.test_test_assets \
  tests.test_local_scripts.LocalScriptTests.test_weaponry_shell_script_posts_fixture_to_expected_path \
  tests.test_callback_debug_routes.CallbackDebugRouteTests.test_callback_api_returns_payload_for_weaponry_callback \
  tests.test_callback_debug_routes.CallbackDebugRouteTests.test_callback_page_contains_renderer_hooks_for_weaponry \
  -v
```

Expected:

```text
OK
```

- [ ] **Step 2: Run the manual macOS local-debug flow**

In terminal A:

```bash
python scripts/mock_callback_server.py
```

In terminal B:

```bash
export CALLBACK_URL="http://127.0.0.1:9000/llm/callback"
python run.py
```

In terminal C:

```bash
zsh scripts/test_llm_weaponry.sh http://127.0.0.1:5001 tests/fixtures/llm/weaponry_request.json
```

Expected:

```text
{"message":"accepted","businessType":"weaponry",...}
```

- [ ] **Step 3: Verify the debug page result**

Open:

```text
http://127.0.0.1:5001/debug/callback
```

Confirm:

```text
1. 页面顶部显示 architectureId、status、字段统计
2. 结构化区域不再显示“当前类型暂未提供友好展示”
3. 舰级名称、单舰名称、航速等字段可见
4. 每个字段都有“查看溯源（N）”折叠入口
5. 页面底部仍保留完整原始 JSON
```

- [ ] **Step 4: Confirm the branch is verification-ready**

```bash
git status --short
```

Expected:

```text
工作区为空，或只剩下本轮手工验证产生的临时文件。
如果仍有未提交的功能改动，回到对应任务补齐提交，而不是在这里做一次兜底大提交。
```

## Self-Review

### Spec Coverage Check

1. `weaponry` 通用模板展示：Task 3
2. `analyseDataSource` 折叠展示：Task 3
3. 舰艇字段样例：Task 1
4. macOS 脚本补齐：Task 2
5. README 双平台说明：Task 4
6. macOS 首轮验证：Task 5

### Placeholder Scan

1. 没有使用 `TODO`、`TBD`、`implement later` 等占位词。
2. 每个代码步骤都给出了具体代码片段或完整命令。

### Type Consistency Check

1. 模板中的新函数名统一为 `countWeaponryStats`、`renderWeaponrySources`、`renderWeaponryField`、`renderWeaponryPayload`。
2. 样例中的业务类型统一为 `weaponry`。
3. `architectureId` 在请求样例和 `check-task` 样例中统一为 `10502`。
