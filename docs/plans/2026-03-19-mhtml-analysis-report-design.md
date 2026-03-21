# LLM `mhtml` 文件支持设计

**日期：** 2026-03-19

> 归档说明（2026-03-21）：本文为历史设计记录。当前仓库已移除前端页面与调试路由，文中“明确不改”的前端模块项仅保留历史背景意义。

**目标：** 仅针对 [api-test.md](/e:/DocSense/api-test.md) 中的 `/llm/analysis` 与 `/llm/generate-report`，新增对 `mhtml`/`mht` 文件的解析支持，并保持现有其他入口与链路不变。

## 1. 背景与约束

- 甲方正式接口以 [api-test.md](/e:/DocSense/api-test.md) 为准。
- 本地项目最终部署于甲方前后端系统中，本次只扩展甲方调用到的两个正式接口：
  - `/llm/analysis`
  - `/llm/generate-report`
- 用户明确要求：
  - 默认做最小、低风险、可回滚的改动。
  - 不扩大到 Web UI、分类上传、对话上传等无关链路。
  - 允许新增轻量依赖。
- 当前实现的实际情况：
  - 路由层并未显式拒绝 `mhtml`。
  - `/llm/analysis` 下载文件后会直接交给 `pipeline_process_file_with_rag`，但原文回填 `_read_original_text` 不支持 `mhtml`。
  - `/llm/generate-report` 下载文件后直接调用 `prepare_upload_files`，未对 `mhtml` 做任何归一化。
  - 通用 `pipeline.py` 目前只对 PDF 做 OCR 特判，若直接改成全局支持，会影响本次范围外的入口。

## 2. 方案选择

本次采用“接口内归一化方案”：

- 仅在 `/llm/analysis` 和 `/llm/generate-report` 对应服务层内识别 `mhtml`/`mht`。
- 下载到本地后，先将 `mhtml` 归一化为 UTF-8 文本型 Markdown 文件，再复用现有上传与推理流程。
- 非 `mhtml` 文件完全不走新逻辑。

不选其他方案的原因：

- 不选“原文件直传 + 少量补丁”：
  - 风险依赖 AnythingLLM 是否稳定支持 `mhtml` 原文件。
  - 报告生成链路仍然不可控。
- 不选“全局改 `pipeline.py`”：
  - 会把影响扩散到分类、对话、Web UI 等非本次范围内的流程。
  - 不符合最小改动原则。

## 3. 整体思路

新增一个仅供甲方接口使用的 `mhtml` 处理辅助模块，负责：

- 识别 `.mhtml` / `.mht`
- 从 MIME 结构中提取正文：
  - 优先 `text/html`
  - 其次 `text/plain`
- 若取到 HTML，则转换为纯文本并按 Markdown 方式落盘
- 输出 UTF-8 编码的归一化文件，供现有 AnythingLLM 上传逻辑复用

数据流如下：

### 3.1 `/llm/analysis`

1. 下载甲方文件到本地临时目录。
2. 若扩展名是 `mhtml`/`mht`，执行归一化。
3. 将归一化后的 `.md` 文件传给 `pipeline_process_file_with_rag`。
4. 回调组装时，`originalText` 优先读取归一化产物，保证最小可用。
5. 若归一化失败，则降级回原始文件继续流程，不直接中断任务。

### 3.2 `/llm/generate-report`

1. 下载 `filePathList` 中的每个文件。
2. 若扩展名是 `mhtml`/`mht`，先归一化。
3. 将归一化后的文件加入现有 `files_to_upload`。
4. 继续复用 `prepare_upload_files` / `run_anythingllm_rag` 的现有流程。
5. 若归一化失败，则降级回原始文件继续流程。

## 4. 依赖与解析策略

依赖选择：

- MIME 结构解析：使用 Python 标准库 `email`
- HTML 转文本：使用 Python 标准库 `html.parser`

选择原因：

- `email` 适合拆解 `mhtml`/`mht` 这种 MIME 包结构，不需要额外依赖。
- `html.parser` 足以完成本次“提取正文文本并落盘”的最小目标。
- 不增加新依赖，部署和回滚风险更低。

解析规则：

- 优先寻找 `text/html` 正文 part。
- 若无 `text/html`，退回 `text/plain`。
- HTML 清洗时：
  - 移除 `script`、`style`、`noscript`
  - 保留标题、段落、表格、列表的文本换行
  - 输出 UTF-8 文本
- 归一化文件命名为：
  - `<原文件名>.normalized.md`

## 5. 改动边界

本次预计修改文件：

- 新增：`app/services/mhtml_normalizer.py`
- 修改：`app/services/llm_analysis_service.py`
- 修改：`app/services/llm_report_service.py`
- 修改：`README.md`
- 修改：`api-test.md`
- 修改：`tests/test_llm_analysis_service.py`
- 修改：`tests/test_llm_report_service.py`
- 新增或修改：`tests/test_mhtml_normalizer.py`

明确不改：

- `pipeline.py`
- `web_ui.py`
- （历史条目，当前已移除）`app/blueprints/classify.py`
- （历史条目，当前已移除）`app/blueprints/chat.py`
- （历史条目，当前已移除）Web UI 页面与静态资源
- 翻译子系统的全局格式支持范围

## 6. 失败降级与错误处理

- 非 `mhtml`/`mht` 文件：
  - 不做任何行为变化。
- `mhtml` 解析失败：
  - 记录日志。
  - 降级为原始文件继续上传，不直接抛错中断任务。
- `mhtml` 中没有 `text/html` 或 `text/plain`：
  - 视为归一化失败，走同样降级逻辑。
- `/llm/analysis`：
  - 即使归一化失败，任务仍可尝试继续执行并按现有错误处理回调。
- `/llm/generate-report`：
  - 单个 `mhtml` 归一化失败不单独中断整批报告生成流程，仍走原文件上传。

## 7. 测试策略

按 TDD 覆盖以下行为：

### 7.1 归一化模块

- 能识别 `.mhtml` / `.mht`
- 能从最小 `mhtml` 样本中提取 HTML 文本
- 能写出 UTF-8 的 `.normalized.md`
- 无可用正文 part 时抛出明确异常或返回失败

### 7.2 `/llm/analysis`

- 下载到 `mhtml` 时，会先归一化后再调用 `pipeline_process_file_with_rag`
- 原文回填优先读取归一化产物
- 归一化失败时会降级回原文件

### 7.3 `/llm/generate-report`

- `filePathList` 中出现 `mhtml` 时，会把归一化后的文件加入上传列表
- 归一化失败时降级为原文件

### 7.4 回归测试

- 现有 `.txt`/`.pdf` 路径保持不变
- 现有 `analysis` / `report` 成功路径测试继续通过

## 8. 本期实施边界

本期纳入：

- `mhtml`/`mht` 下载后归一化
- `/llm/analysis` 解析支持
- `/llm/generate-report` 解析支持
- 文档与依赖同步更新

本期不纳入：

- 其他接口的 `mhtml` 支持
- Web UI 上传链路支持
- AnythingLLM 原生 `mhtml` 能力探测
- 翻译子系统对 `mhtml` 的直接格式支持
