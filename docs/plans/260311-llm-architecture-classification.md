# LLM Architecture Classification Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 强化 `/llm/analysis` 的领域分类能力，让正式提示词基于 `rag_with_ocr.py` 的分类判定规则稳定输出单个 `architectureId`，并补充正式接口测试文档中的 `architectureList` 结果展示流程。

**Architecture:** 保持单阶段文件解析链路不变，只增强正式文件解析 prompt 和 `architectureId` 服务端映射逻辑。领域分类规则从 `rag_with_ocr.py` 的 `PROMPT` 中提炼后写入正式 prompt，服务端继续只接受本次请求候选范围内的 `architectureId`，并新增 `name/pathName` 的低风险匹配。

**Tech Stack:** Python, Flask, unittest, PowerShell 7, AnythingLLM 集成服务

---

### Task 1: 为领域分类增强补充失败测试

**Files:**
- Modify: `tests/test_llm_analysis_service.py`
- Test: `tests/test_llm_analysis_service.py`

**Step 1: Write the failing tests**

新增测试覆盖以下行为：

```python
def test_build_file_analysis_prompt_includes_architecture_classification_rules():
    ...

def test_map_analysis_result_matches_architecture_by_path_name():
    ...

def test_map_analysis_result_matches_architecture_by_nested_name():
    ...
```

**Step 2: Run tests to verify they fail**

Run:

```powershell
& 'E:/DocSense/.venv/Scripts/python.exe' -m unittest tests.test_llm_analysis_service -v
```

Expected:

- 新增测试失败
- 失败原因应为当前 prompt 未包含分类判定规则，或当前映射未支持 `pathName` / 嵌套名称匹配

**Step 3: Commit**

暂不提交，等实现通过后与代码一起提交。

### Task 2: 增强正式文件解析 prompt 的领域分类规则

**Files:**
- Modify: `app/services/llm_prompts.py`
- Reference: `rag_with_ocr.py`
- Test: `tests/test_llm_analysis_service.py`

**Step 1: Write minimal implementation**

在 `build_file_analysis_prompt()` 中：

- 引入 `rag_with_ocr.py` 中与分类判定相关的规则摘要
- 删除旧分类协议相关输出要求，不引入 `category_candidates` 等字段
- 明确要求：
  - 必须从 `architectureList` 中选出一个最可能的节点
  - 只输出 `architectureId`
  - 不返回候选对象/候选数组/分类名称
  - 仅当与所有候选明显无关时才输出 `0`

**Step 2: Run targeted tests**

Run:

```powershell
& 'E:/DocSense/.venv/Scripts/python.exe' -m unittest tests.test_llm_analysis_service.LLMAnalysisServiceTests.test_build_file_analysis_prompt_includes_architecture_classification_rules -v
```

Expected:

- PASS

**Step 3: Commit**

暂不提交，等映射实现一起提交。

### Task 3: 增强 architectureId 服务端映射

**Files:**
- Modify: `app/services/llm_analysis_service.py`
- Test: `tests/test_llm_analysis_service.py`

**Step 1: Write minimal implementation**

增强 `_match_architecture_id()`：

- 优先接受 `architectureId` 或 `领域体系.id`
- 若模型返回 `architectureName`、`领域体系名称` 或 `领域体系.name`，支持按候选 `name` 匹配
- 增加按候选 `pathName` 匹配
- 若模型返回类似 `"作战指挥/组织机构"`，应能命中对应候选节点
- 仍然只允许返回本次候选列表中实际存在的 `id`

**Step 2: Run targeted tests**

Run:

```powershell
& 'E:/DocSense/.venv/Scripts/python.exe' -m unittest tests.test_llm_analysis_service -v
```

Expected:

- 所有文件解析相关测试通过

**Step 3: Commit**

```powershell
git add app/services/llm_prompts.py app/services/llm_analysis_service.py tests/test_llm_analysis_service.py
git commit -m "fix: improve llm architecture classification mapping"
```

### Task 4: 更新正式接口测试指导文档

**Files:**
- Modify: `docs/llm-official-interface-test-guide.md`

**Step 1: Write documentation update**

在“本地开发联调 -> 文件解析成功链路”中新增 `architectureList` 结果展示流程，包含：

- 查看请求或默认 `architectureList` 候选结构
- 查看回调中的 `architectureId`
- 反查命中的完整节点结构
- 说明 `architectureId=0` 时反查结果为空数组 `[]` 属于正常现象

**Step 2: Review doc content**

人工回读文档，确认命令均为 PowerShell 7，可直接复制执行。

**Step 3: Commit**

```powershell
git add docs/llm-official-interface-test-guide.md
git commit -m "docs: expand architecture test guidance"
```

### Task 5: 运行回归验证

**Files:**
- Test: `tests/test_llm_analysis_service.py`
- Test: `tests/test_llm_range_defaults.py`

**Step 1: Run focused regression suite**

Run:

```powershell
& 'E:/DocSense/.venv/Scripts/python.exe' -m unittest tests.test_llm_analysis_service tests.test_llm_range_defaults -v
```

Expected:

- 全部通过

**Step 2: Run broader llm regression suite**

Run:

```powershell
& 'E:/DocSense/.venv/Scripts/python.exe' -m unittest tests.test_llm_test_assets tests.test_llm_routes tests.test_llm_progress_and_check_task tests.test_llm_report_service tests.test_llm_analysis_service tests.test_llm_io_services tests.test_llm_task_service tests.test_llm_range_defaults -v
```

Expected:

- 全部通过

**Step 3: Commit if needed**

若验证过程中有小修正，修正后再次运行测试并单独提交：

```powershell
git add <changed-files>
git commit -m "test: align llm architecture classification verification"
```
