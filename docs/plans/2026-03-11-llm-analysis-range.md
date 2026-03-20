# LLM Analysis Range Constraint Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make `/llm/analysis` honor caller-provided ranges for `channel`, `country`, `format`, `maturity`, and `architectureList`, while supplying default mock ranges only when those fields are absent or empty during testing.

**Architecture:** Add a small normalization layer ahead of the formal file-analysis prompt so request ranges are either accepted as-is or backfilled from default test ranges. Then tighten the prompt and server-side mapping so model output is validated against the final effective range set before entering the callback payload.

**Tech Stack:** Flask, Python `unittest`, existing AnythingLLM integration, existing file-analysis mapping services.

---

### Task 1: Add Effective Range Normalization For File Analysis

**Files:**
- Create: `tests/test_llm_range_defaults.py`
- Modify: `app/services/llm_analysis_service.py`

**Step 1: Write the failing test**

```python
import unittest

from app.services.llm_analysis_service import build_effective_analysis_ranges


class LLMRangeDefaultTests(unittest.TestCase):
    def test_missing_ranges_use_default_test_values(self):
        ranges = build_effective_analysis_ranges({"fileName": "demo.txt"})
        self.assertEqual([item["value"] for item in ranges["format"]], ["音频类", "文档类", "图片类"])
        self.assertTrue(ranges["architectureList"])

    def test_explicit_ranges_override_defaults(self):
        ranges = build_effective_analysis_ranges(
            {
                "fileName": "demo.txt",
                "country": [{"key": "99", "value": "德国"}],
            }
        )
        self.assertEqual([item["value"] for item in ranges["country"]], ["德国"])
```

**Step 2: Run test to verify it fails**

Run: `pwsh -NoLogo -Command "& 'E:/DocSense/.venv/Scripts/python.exe' -m unittest tests.test_llm_range_defaults -v"`

Expected: FAIL because `build_effective_analysis_ranges` does not exist.

**Step 3: Write minimal implementation**

```python
def build_effective_analysis_ranges(request_params: dict) -> dict:
    return {
        "country": request_params.get("country") or DEFAULT_COUNTRY_OPTIONS,
        "channel": request_params.get("channel") or DEFAULT_CHANNEL_OPTIONS,
        "format": request_params.get("format") or DEFAULT_FORMAT_OPTIONS,
        "maturity": request_params.get("maturity") or DEFAULT_MATURITY_OPTIONS,
        "architectureList": request_params.get("architectureList") or DEFAULT_ARCHITECTURE_OPTIONS,
    }
```

- Add explicit default test ranges for `country`, `channel`, `format`, `maturity`.
- Add generated default `architectureList` nodes that mirror the hierarchy in `rag_with_ocr.py`.
- Treat missing, empty list, and invalid-list values as “use default”.

**Step 4: Run test to verify it passes**

Run: `pwsh -NoLogo -Command "& 'E:/DocSense/.venv/Scripts/python.exe' -m unittest tests.test_llm_range_defaults -v"`

Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_llm_range_defaults.py app/services/llm_analysis_service.py
git commit -m "feat: add default llm analysis range normalization"
```

### Task 2: Tighten The Formal File Analysis Prompt

**Files:**
- Modify: `tests/test_llm_analysis_service.py`
- Modify: `app/services/llm_prompts.py`
- Modify: `app/services/llm_analysis_service.py`

**Step 1: Write the failing test**

Extend the existing prompt test with:

```python
def test_build_file_analysis_prompt_uses_effective_range_values(self):
    prompt = build_file_analysis_prompt(
        {
            "fileName": "demo.txt",
            "country": [{"key": "02", "value": "美国"}],
            "format": [{"key": "03", "value": "文档类"}],
            "architectureList": [{"id": 1, "name": "军事基地"}],
        }
    )
    self.assertIn('"country"', prompt)
    self.assertIn('"美国"', prompt)
    self.assertIn('"architectureId"', prompt)
    self.assertIn("不要直接原样返回候选对象", prompt)
```

**Step 2: Run test to verify it fails**

Run: `pwsh -NoLogo -Command "& 'E:/DocSense/.venv/Scripts/python.exe' -m unittest tests.test_llm_analysis_service -v"`

Expected: FAIL because prompt generation still ignores normalized range semantics.

**Step 3: Write minimal implementation**

```python
def build_file_analysis_prompt(request_params: dict) -> str:
    ranges = build_effective_analysis_ranges(request_params)
    schema = {...}
    return (
        "请仅基于文档内容进行字段抽取，并输出严格合法 JSON。\n"
        "不要直接原样返回候选对象...\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n"
        + _format_options("国家候选", ranges["country"])
        ...
    )
```

- Use normalized ranges, not raw request fields.
- Explicitly require scalar output for `country/channel/format/maturity`.
- Explicitly require numeric `architectureId`.

**Step 4: Run test to verify it passes**

Run: `pwsh -NoLogo -Command "& 'E:/DocSense/.venv/Scripts/python.exe' -m unittest tests.test_llm_analysis_service -v"`

Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_llm_analysis_service.py app/services/llm_prompts.py app/services/llm_analysis_service.py
git commit -m "fix: constrain llm analysis prompt to effective ranges"
```

### Task 3: Enforce Range Validation In Mapping

**Files:**
- Modify: `tests/test_llm_analysis_service.py`
- Modify: `app/services/llm_analysis_service.py`

**Step 1: Write the failing test**

Add tests like:

```python
def test_map_analysis_result_rejects_out_of_range_country(self):
    request_params = {
        "fileName": "demo.txt",
        "country": [{"key": "02", "value": "美国"}],
    }
    result = map_analysis_result({"country": "俄罗斯"}, request_params)
    assert result["country"] == ""

def test_map_analysis_result_uses_default_ranges_when_request_missing(self):
    result = map_analysis_result({"国家": {"value": "美国", "key": "02"}}, {"fileName": "demo.txt"})
    assert result["country"] == "美国"
```

**Step 2: Run test to verify it fails**

Run: `pwsh -NoLogo -Command "& 'E:/DocSense/.venv/Scripts/python.exe' -m unittest tests.test_llm_analysis_service -v"`

Expected: FAIL because mapping still reads raw request fields directly.

**Step 3: Write minimal implementation**

```python
def map_analysis_result(parsed_result: dict, request_params: dict, original_text: str = "") -> dict:
    ranges = build_effective_analysis_ranges(request_params)
    resolved_country = _match_option_value(..., ranges["country"])
    ...
```

- Route all range-constrained fields through normalized effective ranges.
- Keep out-of-range results empty.
- Keep `architectureId` at `0` when candidate ID is outside the effective range set.

**Step 4: Run test to verify it passes**

Run: `pwsh -NoLogo -Command "& 'E:/DocSense/.venv/Scripts/python.exe' -m unittest tests.test_llm_analysis_service -v"`

Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_llm_analysis_service.py app/services/llm_analysis_service.py
git commit -m "fix: enforce effective range validation in llm analysis mapping"
```

### Task 4: Update Test Fixtures And End-to-End File Analysis Checks

**Files:**
- Modify: `tests/fixtures/llm/analysis_request.json`
- Modify: `README.md`

**Step 1: Update fixture**

Make `tests/fixtures/llm/analysis_request.json` intentionally omit one or more range fields so the default-range path can be exercised during local testing.

**Step 2: Run the focused suite**

Run: `pwsh -NoLogo -Command "& 'E:/DocSense/.venv/Scripts/python.exe' -m unittest tests.test_llm_range_defaults tests.test_llm_analysis_service tests.test_llm_routes -v"`

Expected: PASS.

**Step 3: Run the full suite**

Run: `pwsh -NoLogo -Command "& 'E:/DocSense/.venv/Scripts/python.exe' -m unittest tests.test_llm_test_assets tests.test_llm_routes tests.test_llm_progress_and_check_task tests.test_llm_report_service tests.test_llm_analysis_service tests.test_llm_io_services tests.test_llm_task_service tests.test_llm_range_defaults -v"`

Expected: PASS.

**Step 4: Document the behavior**

Add a short README note describing:
- request range priority
- default mock ranges for testing
- architecture defaults coming from the `rag_with_ocr.py` taxonomy

**Step 5: Commit**

```bash
git add tests/fixtures/llm/analysis_request.json README.md tests/test_llm_range_defaults.py
git commit -m "docs: document llm analysis default range behavior"
```
