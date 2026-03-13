# LLM 集成设计

**日期：** 2026-03-09

**目标：** 在保留乙方现有调试页面和接口的前提下，新增一套与甲方 `api-test.md` 完整兼容的正式接入层，支持文件解析、报告生成、任务检查、进度推送和主动回调。

## 1. 背景与边界

- 本项目最终部署并整合在甲方系统内部。
- 联调完成后，以甲方现有前后端为唯一正式入口。
- 乙方仓库现有页面和 `/api/classify/*`、`/api/chat/*` 仅保留为调试能力。
- 对话功能不接入甲方正式链路。
- 翻译功能本期不实现，但文件回调中的翻译字段必须保留，当前固定返回空字符串。
- 所有协议行为以 [api-test.md](/e:/DocSense/api-test.md) 为最高优先级。

## 2. 总体方案

采用“兼容层方案”：

- 在现有 Flask 项目内新增正式 `llm` 蓝图，提供 `/llm/analysis`、`/llm/generate-report`、`/llm/check-task` 和 `/llm/progress`。
- 复用现有 OCR、AnythingLLM 上传、Embedding、问答、结果解析能力。
- 增加任务持久化、文件下载、主动回调、WebSocket 进度推送和报告生成服务。
- 现有调试页面和分类接口不删除、不重构，只与正式链路分层共存。

选择该方案的原因：

- 改动范围最小，低风险，可回滚。
- 能优先满足甲方接口联调，不引入额外基础设施依赖。
- 便于后续将正式链路和调试链路进一步抽象到统一服务层。

## 3. 正式接口设计

### 3.1 `POST /llm/analysis`

用途：

- 接收甲方文件解析请求。
- 仅使用 `params[0]`。
- 校验 `businessType=file`。
- 下载 `filePath` 指向的文件到临时目录。
- 创建异步任务并立即返回受理结果。

输入关键字段：

- `architectureList`
- `channel`
- `country`
- `fileName`
- `filePath`
- `format`
- `maturity`

内部处理：

- 建立文件任务记录。
- 异步执行下载、OCR/预处理、AnythingLLM 解析、结果映射、主动回调。

### 3.2 `POST /llm/generate-report`

用途：

- 接收甲方报告生成请求。
- 仅使用 `params[0]`。
- 校验 `businessType=report`。
- 下载 `filePathList` 对应的多个文件。
- 创建异步任务并立即返回受理结果。

输入关键字段：

- `filePathList`
- `reportId`
- `templateDesc`
- `templateOutline`
- `requirement`

内部处理：

- 建立报告任务记录。
- 异步执行多文件下载、OCR/预处理、AnythingLLM 汇总生成、HTML 兜底转换、主动回调。

### 3.3 `POST /llm/check-task`

用途：

- 按甲方业务主键查询任务状态。
- 若业务已完成但回调未成功，立即补发一次回调。

处理规则：

- 文件任务用 `fileName` 查询。
- 报告任务用 `reportId` 查询。
- 不重新执行已完成的业务处理，只补发回调。

### 3.4 `WS /llm/progress`

用途：

- 为甲方前端提供任务进度订阅能力。

订阅规则：

- 客户端建立 WebSocket 连接后发送首条订阅消息。
- 文件任务订阅参数：`businessType=file + fileName`
- 报告任务订阅参数：`businessType=report + reportId`
- 服务端按任务状态变化推送 `progress`。

进度语义：

- 采用阶段性进度，不追求伪精确百分比。
- 文件链路建议进度点：`0.15` 下载完成、`0.35` 预处理完成、`0.75` LLM 完成、`0.90` 映射完成、`1.0` 业务完成。
- 报告链路建议进度点：`0.15` 文件下载完成、`0.35` 预处理完成、`0.75` 汇总生成完成、`0.90` HTML 整理完成、`1.0` 业务完成。

## 4. 任务模型与状态机

内部任务需持久化，不能继续只使用当前进程内内存状态。

建议统一字段：

- `business_type`
- `business_key`
- `request_payload`
- `status`
- `progress`
- `message`
- `result_payload`
- `callback_status`
- `callback_attempts`
- `last_callback_error`
- `created_at`
- `updated_at`

### 4.1 文件任务状态

对外状态：

- `0` 未解析
- `1` 解析中
- `2` 已解析
- `3` 解析失败

内部阶段：

- `accepted`
- `downloading`
- `preprocessing`
- `llm_processing`
- `result_mapping`
- `completed`
- `failed`
- `callback_pending`
- `callback_success`
- `callback_failed`

### 4.2 报告任务状态

对外状态：

- `0` 生成中
- `1` 已生效
- `2` 生成失败

内部阶段：

- `accepted`
- `downloading_many`
- `preprocessing_many`
- `report_generating`
- `html_rendering`
- `completed`
- `failed`
- `callback_pending`
- `callback_success`
- `callback_failed`

### 4.3 回调规则

- 业务处理成功后，不因回调失败而回退业务状态。
- `callback_status` 独立记录为 `pending`、`success`、`failed`。
- `/llm/check-task` 查询到“业务已完成但回调未成功”时，立即补发一次回调。

## 5. 文件解析映射设计

现有 [rag_with_ocr.py](/e:/DocSense/rag_with_ocr.py) 的提示词主要面向乙方当前“五类军事分类 + extract”场景，不能直接作为甲方正式协议输出。

正式链路需新增“甲方协议专用提示词”和“结果映射器”。

### 5.1 输入约束

- `architectureList` 作为领域体系候选集传给模型。
- `country`、`channel`、`format`、`maturity` 作为候选枚举传给模型。
- 正式输出只允许从请求候选中选择结果。

### 5.2 输出映射

文件回调字段：

- `businessType` 固定为 `file`
- `data.fileName` 取请求中的 `fileName`
- `data.country`、`data.channel`、`data.maturity`、`data.format` 返回选中的 `value`
- `data.architectureId` 返回选中的 `architectureList.id`
- `data.status` 成功为 `2`，失败为 `3`
- `msg` 仅允许为 `解析成功` 或 `解析失败`

### 5.3 `fileDataItem` 字段策略

- `fileName`：请求中的 `fileName`
- `dataTime`、`keyword`、`summary`、`fileNo`、`source`、`originalLink`、`language`、`dataFormat`、`associatedEquipment`、`relatedTechnology`、`equipmentModel`、`documentOverview`：由模型抽取
- `originalText`：文本型文件优先取提取文本，扫描件优先取 OCR Markdown；拿不到则为空字符串
- `documentTranslationOne`：固定空字符串
- `documentTranslationTwo`：固定空字符串
- `score`：按 `0.0~5.0` 一位小数输出；无足够依据时返回 `0.0`

### 5.4 兜底策略

- 若证据不足以唯一命中某字段，则字符串字段返回空字符串。
- `architectureId` 未命中时返回 `0`。
- 不因部分字段缺失而直接判定整体业务失败，除非主流程异常无法完成。

## 6. 报告生成设计

当前仓库没有现成正式报告生成功能，需新增报告链路。

### 6.1 处理流程

- 下载 `filePathList` 所有文件。
- 逐个执行 OCR/预处理。
- 将全部文件上传到同一临时 workspace。
- 使用“报告生成专用提示词”结合 `templateDesc`、`templateOutline`、`requirement` 进行汇总生成。

### 6.2 输出规则

报告回调字段：

- `businessType` 固定为 `report`
- `data.reportId` 取请求中的 `reportId`
- `data.status` 成功为 `1`，失败为 `2`
- `data.details` 为 HTML 片段
- `msg` 仅允许为 `生成成功` 或 `生成失败`

### 6.3 HTML 两层兜底

主路径：

- 模型直接输出可嵌入甲方页面的 HTML 片段。

兜底路径：

- 若模型输出为空、非 HTML、结构不稳定，则服务端将纯文本安全包裹为基础 HTML 容器后再回调。

## 7. 模块拆分建议

正式接入新增模块：

- `app/blueprints/llm.py`
- `app/services/llm_task_service.py`
- `app/services/llm_download_service.py`
- `app/services/llm_analysis_service.py`
- `app/services/llm_report_service.py`
- `app/services/llm_progress_hub.py`
- `app/services/llm_callback_service.py`

复用模块：

- [pipeline.py](/e:/DocSense/pipeline.py)
- [anythingllm_client.py](/e:/DocSense/anythingllm_client.py)
- [ocr_preprocessor.py](/e:/DocSense/ocr_preprocessor.py)
- [config.py](/e:/DocSense/config.py)

保留调试用途模块：

- [app/blueprints/classify.py](/e:/DocSense/app/blueprints/classify.py)
- [app/blueprints/chat.py](/e:/DocSense/app/blueprints/chat.py)
- `templates/`
- `static/`

## 8. 存储与重复提交策略

首版建议使用 SQLite 持久化任务，而不是继续使用纯内存存储，也不引入 Redis/消息队列。

原因：

- 满足 `/llm/check-task` 和回调补发要求。
- 部署简单，低风险，可回滚。
- 不额外扩大本期范围。

重复提交策略：

- 同一 `fileName` 或 `reportId` 若已有处理中任务，则直接返回已存在任务。
- 若已有已完成但回调失败任务，则不重新处理，只补发回调。
- 若已有业务失败任务，则允许新请求覆盖为新一轮任务，并保留上一轮错误日志。

## 9. 错误处理原则

- 参数不合法、必填字段缺失、下载地址不可用时，接口立即返回请求错误。
- 异步任务受理后出现的下载失败、OCR 失败、AnythingLLM 异常、HTML 生成失败，写入任务状态并触发失败回调。
- 业务失败与回调失败分开记录。

## 10. 开发过程中的接口测试方案

开发过程中需提供一套可直接用于你手工联调和回归测试的方案。

建议分三层：

### 10.1 本地可调用的正式接口

- 开发阶段直接启动当前 Flask 服务。
- 在本地提供正式 `/llm/*` 接口，不要求先接入甲方前端。
- 通过独立测试请求直接调用：
  - `POST /llm/analysis`
  - `POST /llm/generate-report`
  - `POST /llm/check-task`
  - `WS /llm/progress`

### 10.2 可复用的测试材料

建议在仓库中补充一组仅供开发使用的测试资产：

- 一个或多个本地样例文件
- 一份文件解析请求样例 JSON
- 一份报告生成请求样例 JSON
- 一份 `check-task` 请求样例 JSON
- 一份 WebSocket 订阅样例消息

建议额外提供：

- PowerShell 7 调用脚本，便于直接发起 HTTP 请求
- WebSocket 简易测试脚本，便于观察进度推送
- 可选的本地假回调接收端，用于观察回调报文

### 10.3 本地联调模式

建议增加开发配置项，例如：

- 回调地址指向本地假服务或甲方测试环境
- 文件下载支持本地静态文件服务
- 日志中输出任务主键、任务状态、回调结果

推荐开发阶段保留以下最小工具链：

- `scripts/test_llm_analysis.ps1`
- `scripts/test_llm_report.ps1`
- `scripts/test_llm_check_task.ps1`
- `scripts/test_llm_progress.ps1` 或等价 Python 脚本
- `scripts/mock_callback_server.py`

这样可以在甲方前端未完全接通前，先独立验证协议、任务流、回调和进度推送。

## 11. 验证策略

验证分三层：

### 11.1 协议验证

- 文件解析请求结构正确
- 报告生成请求结构正确
- 任务查询请求结构正确
- WebSocket 进度推送结构正确

### 11.2 流程验证

- 成功回调
- 失败回调
- 回调失败后 `check-task` 补发
- WebSocket 进度完整推送

### 11.3 兼容验证

- 现有调试页面仍可打开
- 现有 `/api/classify/*` 调试能力不回归

## 12. 本期实施边界

本期纳入：

- `/llm/analysis`
- `/llm/generate-report`
- `/llm/check-task`
- `/llm/progress`
- 主动回调
- 任务持久化
- 文件下载
- 正式提示词与结果映射
- 开发期接口测试脚本与假回调方案

本期不纳入：

- 对话功能正式接入
- 翻译能力实现
- 无关 UI 重构
- 与甲方协议无关的顺手重构
