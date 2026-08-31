# Analysis 摘要翻译兼容路径删除执行记录

## 1. 背景与结论

文件分析曾把未公开的 `enableFullTranslation` 当作内部兼容开关：值为假时，不执行全文翻译，
而是把 `fileDataItem.summary` 交给纯文本翻译引擎，并将结果写入
`documentTranslationOne`、`documentTranslationTwo`。该分支不符合现行业务需求，本次已删除。

改造后，Analysis 翻译阶段只有一个业务含义：翻译当前任务的完整 prepared Artifact。
`fileDataItem.summary` 仍是分析模型的正式输出字段，但不再作为翻译输入。

## 2. 已实施改动

1. Analysis Application 不再读取原始请求快照决定翻译类型，也不再读取
   `fileDataItem.summary` 发起翻译；具备 prepared Artifact 时始终发起全文翻译。
2. Analysis Translation Port 删除 `AnalysisTranslationKind`，请求删除 `kind`、`text`，结果删除
   `kind`，使内部类型只能表达全文翻译，避免废弃分支被其他调用方重新接入。
3. 生产 Artifact Adapter 和离线兼容 Adapter 均删除摘要翻译方法、HTML 包装与摘要错误码；
   生产 Adapter 缺少 Artifact 时稳定返回可降级失败，不调用纯文本引擎。
4. 目录联调脚本删除废弃开关，相关 Fake、隔离测试、Port 测试和现行 README 同步收窄为
   全文翻译语义。

## 3. 当前路由语义

| 输入状态 | Analysis 翻译行为 | 展示字段结果 |
| --- | --- | --- |
| 存在当前任务的 prepared Artifact | 调用 `TranslatePreparedDocument` 完成全文翻译 | 成功时写入单语、双语 HTML |
| OCR 明确失败且没有 prepared Artifact | 生产 Adapter 返回 `document_translation_artifact_missing` | 按既有可降级语义保留空值 |
| `fileDataItem.summary` 存在或为空 | 不参与翻译决策，也不进入 Translation Port | 仅保留为 Analysis 摘要结果 |

## 4. 接口契约边界

- `docs/接口文档/` 零修改；未增删任何已公开请求参数、响应字段、状态码、Callback、SSE 或
  WebSocket 语义。
- 废弃开关从生产代码、脚本和测试调用中删除。由于它从未写入权威接口文档，本次没有新增
  “识别后返回 400”的公开校验规则；未声明扩展字段仍遵循 `/llm/analysis` 现有通用解析语义。
- `documentTranslationOne`、`documentTranslationTwo` 和 `fileDataItem.summary` 三个现有响应字段
  均保留，只有其内部数据来源被收窄为实际业务要求。

## 5. 离线验证

全部使用项目 `venv`、临时目录、临时 SQLite 和 Fake；未启动 `run.py`，未连接真实后台服务。

- `test_analysis*.py`：269 项通过。
- `test_stage1h*.py`：9 项通过。
- `test_translation*.py`：14 项通过。
- `test_routes`、`test_translation_module`、`test_dependency_container`、
  `test_architecture_boundaries`：106 项通过。
- 静态检索确认生产代码、测试和脚本中无废弃开关、`AnalysisTranslationKind`、Analysis 摘要
  翻译方法及其错误码残留；`docs/接口文档/` 差异为空，`git diff --check` 通过。

以上证据只证明 Windows、离线 Fake、临时 SQLite 与当前单实例组合下的行为，不代表可靠任务
队列、多实例 fencing、生产数据库一致性、容量或真实翻译供应商验收。
