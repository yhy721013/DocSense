# LLM MHTML Analysis And Report Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add `mhtml`/`mht` file support only for `/llm/analysis` and `/llm/generate-report` by normalizing downloaded files into UTF-8 Markdown before reusing the existing AnythingLLM flow.

**Architecture:** Keep the existing Flask routes and core upload pipeline unchanged, and insert a small `mhtml` normalization layer only inside the two甲方接口服务流程. Normalize downloaded `mhtml` files into `.normalized.md`, use that artifact for RAG upload and fallback original-text extraction, and degrade to the original file if normalization fails.

**Tech Stack:** Python standard library `email`, Python standard library `html.parser`, Flask, Python `unittest`, PowerShell 7.

---

### Task 1: 先补 `mhtml` 归一化模块测试

**Files:**
- Create: `tests/test_mhtml_normalizer.py`
- Test: `tests/test_mhtml_normalizer.py`

**Step 1: Write the failing test**

在 `tests/test_mhtml_normalizer.py` 增加最小样本测试，覆盖：

```python
def test_is_mhtml_file_recognizes_mhtml_and_mht():
    self.assertTrue(is_mhtml_file("demo.mhtml"))
    self.assertTrue(is_mhtml_file("demo.mht"))
    self.assertFalse(is_mhtml_file("demo.txt"))

def test_normalize_mhtml_file_extracts_html_text_to_markdown():
    sample = Path(tmp) / "sample.mhtml"
    sample.write_text(MHTML_SAMPLE, encoding="utf-8")
    output = normalize_mhtml_file(str(sample))
    text = Path(output).read_text(encoding="utf-8")
    self.assertIn("Test Title", text)
    self.assertIn("Hello MHTML", text)
```

**Step 2: Run test to verify it fails**

Run: `& 'C:\Program Files\PowerShell\7\pwsh.exe' -NoLogo -Command "python -m unittest tests.test_mhtml_normalizer -v"`

Expected: FAIL，因为归一化模块尚不存在。

**Step 3: Write minimal implementation**

新建 `app/services/mhtml_normalizer.py`，先实现最小能力：

```python
def is_mhtml_file(file_path: str) -> bool:
    return Path(file_path).suffix.lower() in {".mhtml", ".mht"}

def normalize_mhtml_file(file_path: str) -> str:
    html_content = _extract_mhtml_body(file_path)
    text = _html_to_text(html_content)
    output_path = Path(file_path).with_name(f"{Path(file_path).name}.normalized.md")
    output_path.write_text(text, encoding="utf-8")
    return str(output_path)
```

- 用 `email` 提取 `text/html` 或 `text/plain`
- 用标准库 `html.parser` 清洗 HTML 并转文本
- 输出 UTF-8 `.normalized.md`

**Step 4: Run test to verify it passes**

Run: `& 'C:\Program Files\PowerShell\7\pwsh.exe' -NoLogo -Command "python -m unittest tests.test_mhtml_normalizer -v"`

Expected: PASS。

**Step 5: Commit**

```bash
git add tests/test_mhtml_normalizer.py app/services/mhtml_normalizer.py
git commit -m "feat: add mhtml normalization helper"
```

### Task 2: 先让 `/llm/analysis` 在 `mhtml` 上走失败测试

**Files:**
- Modify: `tests/test_llm_analysis_service.py`
- Test: `tests/test_llm_analysis_service.py`

**Step 1: Write the failing test**

在 `tests/test_llm_analysis_service.py` 增加：

```python
@patch("app.services.llm_analysis_service.post_callback_payload", return_value=True)
@patch("app.services.llm_analysis_service.pipeline_process_file_with_rag", return_value='{"summary":"摘要"}')
@patch("app.services.llm_analysis_service.normalize_file_for_llm", return_value="E:/tmp/sample.mhtml.normalized.md")
@patch("app.services.llm_analysis_service.download_to_temp_file")
def test_run_file_analysis_task_normalizes_mhtml_before_rag(...):
    ...
    mock_download.return_value = str(sample_mhtml)
    run_file_analysis_task(...)
    mock_normalize.assert_called_once_with(str(sample_mhtml))
    self.assertEqual(mock_pipeline.call_args.kwargs["file_path"], "E:/tmp/sample.mhtml.normalized.md")
```

再补一个降级测试：

```python
@patch("app.services.llm_analysis_service.normalize_file_for_llm", side_effect=RuntimeError("boom"))
def test_run_file_analysis_task_falls_back_to_original_file_when_mhtml_normalization_fails(...):
    ...
```

**Step 2: Run test to verify it fails**

Run: `& 'C:\Program Files\PowerShell\7\pwsh.exe' -NoLogo -Command "python -m unittest tests.test_llm_analysis_service -v"`

Expected: FAIL，因为当前分析服务不会调用归一化逻辑。

**Step 3: Write minimal implementation**

修改 `app/services/llm_analysis_service.py`：

- 新增一个小型适配函数：

```python
def normalize_file_for_llm(file_path: str) -> str:
    if not is_mhtml_file(file_path):
        return file_path
    return normalize_mhtml_file(file_path)
```

- 在下载后、调用 `pipeline_process_file_with_rag` 前接入：

```python
normalized_path = _normalize_file_for_analysis(downloaded_path)
raw_result = pipeline_process_file_with_rag(... file_path=normalized_path, ...)
mapped_result = map_analysis_result(..., original_text=_read_original_text(normalized_path))
```

- 若归一化抛错，记录日志并继续使用 `downloaded_path`

**Step 4: Run test to verify it passes**

Run: `& 'C:\Program Files\PowerShell\7\pwsh.exe' -NoLogo -Command "python -m unittest tests.test_llm_analysis_service -v"`

Expected: PASS。

**Step 5: Commit**

```bash
git add app/services/analysis_service.py tests/test_llm_analysis_service.py
git commit -m "feat: normalize mhtml for llm analysis"
```

### Task 3: 让 `/llm/generate-report` 复用 `mhtml` 归一化

**Files:**
- Modify: `tests/test_llm_report_service.py`
- Modify: `app/services/llm_report_service.py`
- Test: `tests/test_llm_report_service.py`

**Step 1: Write the failing test**

在 `tests/test_llm_report_service.py` 增加：

```python
@patch("app.services.llm_report_service.normalize_file_for_llm")
def test_run_report_task_normalizes_mhtml_before_prepare_upload_files(...):
    mock_download.return_value = str(sample_mhtml)
    mock_normalize.return_value = str(normalized_md)
    ...
    mock_prepare.assert_called_once_with(str(normalized_md))
```

再补一个降级测试，断言归一化失败时 `prepare_upload_files` 仍用原文件。

**Step 2: Run test to verify it fails**

Run: `& 'C:\Program Files\PowerShell\7\pwsh.exe' -NoLogo -Command "python -m unittest tests.test_llm_report_service -v"`

Expected: FAIL，因为当前报告服务不会归一化 `mhtml`。

**Step 3: Write minimal implementation**

修改 `app/services/llm_report_service.py`：

```python
downloaded_path = download_to_temp_file(...)
prepared_source = _normalize_report_source(downloaded_path)
files_to_upload.extend(prepare_upload_files(prepared_source))
```

- 归一化失败时记录日志并回退到 `downloaded_path`

**Step 4: Run test to verify it passes**

Run: `& 'C:\Program Files\PowerShell\7\pwsh.exe' -NoLogo -Command "python -m unittest tests.test_llm_report_service -v"`

Expected: PASS。

**Step 5: Commit**

```bash
git add app/services/report_service.py tests/test_llm_report_service.py
git commit -m "feat: normalize mhtml for llm report generation"
```

### Task 4: 补回归测试并收敛实现

**Files:**
- Modify: `tests/test_llm_analysis_service.py`
- Modify: `tests/test_llm_report_service.py`
- Modify: `tests/test_mhtml_normalizer.py`
- Test: `tests/test_mhtml_normalizer.py`
- Test: `tests/test_llm_analysis_service.py`
- Test: `tests/test_llm_report_service.py`

**Step 1: Write the failing test**

补充一个原文读取回归测试：

```python
def test_read_original_text_supports_normalized_markdown_output():
    sample = Path(tmp) / "sample.mhtml.normalized.md"
    sample.write_text("# Title\n\nHello", encoding="utf-8")
    self.assertIn("Hello", _read_original_text(str(sample)))
```

如果当前还未显式支持 `.normalized.md` 这种结果文件路径，先让测试失败。

**Step 2: Run test to verify it fails**

Run: `& 'C:\Program Files\PowerShell\7\pwsh.exe' -NoLogo -Command "python -m unittest tests.test_mhtml_normalizer tests.test_llm_analysis_service tests.test_llm_report_service -v"`

Expected: FAIL，直到读取逻辑和依赖声明齐全。

**Step 3: Write minimal implementation**

- 如有必要，在 `app/services/llm_analysis_service.py` 中保持 `.md` 读取路径兼容
- 清理重复辅助函数命名，确保两条链路复用同一个归一化入口

**Step 4: Run test to verify it passes**

Run: `& 'C:\Program Files\PowerShell\7\pwsh.exe' -NoLogo -Command "python -m unittest tests.test_mhtml_normalizer tests.test_llm_analysis_service tests.test_llm_report_service -v"`

Expected: PASS。

**Step 5: Commit**

```bash
git add app/services/analysis_service.py app/services/report_service.py tests/test_mhtml_normalizer.py tests/test_llm_analysis_service.py tests/test_llm_report_service.py
git commit -m "feat: add mhtml support for llm analysis and report"
```

### Task 5: 更新协议文档与最终验证

**Files:**
- Modify: `README.md`
- Modify: `api-test.md`
- Test: `tests/test_mhtml_normalizer.py`
- Test: `tests/test_llm_analysis_service.py`
- Test: `tests/test_llm_report_service.py`

**Step 1: Write the failing test**

本任务不新增自动化断言，直接补文档说明并做最终回归验证。

**Step 2: Run test to verify current status**

Run: `& 'C:\Program Files\PowerShell\7\pwsh.exe' -NoLogo -Command "python -m unittest tests.test_mhtml_normalizer tests.test_llm_analysis_service tests.test_llm_report_service -v"`

Expected: 当前实现全部 PASS。

**Step 3: Write minimal documentation updates**

更新 [README.md](/e:/DocSense/README.md)：

- 在甲方协议接入和开发联调说明中补充 `/llm/analysis`、`/llm/generate-report` 现支持 `mhtml`
- 明确这是甲方正式接口链路内支持，不代表所有上传入口都已支持

更新 [api-test.md](/e:/DocSense/api-test.md)：

- 在文件解析接口说明中补充本项目兼容扩展：支持 `mhtml`/`mht`
- 在报告生成接口说明中补充 `filePathList` 可包含 `mhtml`/`mht`

**Step 4: Run final verification**

Run: `& 'C:\Program Files\PowerShell\7\pwsh.exe' -NoLogo -Command "python -m unittest tests.test_mhtml_normalizer tests.test_llm_analysis_service tests.test_llm_report_service -v"`

Expected: PASS。

**Step 5: Commit**

```bash
git add README.md api-test.md
git commit -m "docs: document mhtml support for llm interfaces"
```
