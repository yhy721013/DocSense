# 回调结果本地调试页设计

## 1. 背景

DocSense 当前是纯后端接口服务，没有现成前端工程，也没有本地调试页面。文件解析和报告生成任务在回调发送前，会将回调报文写入仓库根目录的 `.runtime/call_back.json`，该文件已经可以作为本地结果展示的数据来源。

本次目标是在不引入新前端工程、不改动现有解析主流程的前提下，为本地联调增加一个最小可用的调试页面，用于以更适合人工阅读的方式展示最近一次回调结果。

## 2. 目标

本期只解决以下问题：

1. 在现有 Flask 服务中增加一个本地只读调试页。
2. 页面读取 `.runtime/call_back.json` 并展示最近一次回调结果。
3. 对 `businessType=file` 和 `businessType=report` 提供友好展示。
4. 对 `file` 回调中的 `originalText`、`documentTranslationOne`、`documentTranslationTwo` 做适合内容类型的展示。
5. 页面提供手动刷新能力，不引入自动轮询。

## 3. 非目标

以下内容明确不在本期范围内：

1. 不引入 React、Vue、Vite、Next.js 等前端工程。
2. 不修改文件解析、报告生成、回调发送的主业务流程。
3. 不增加历史回调列表，只展示当前 `.runtime/call_back.json` 的最新内容。
4. 不对 `businessType=weaponry` 做结构化友好展示。
5. 不增加登录、鉴权或对外暴露的调试能力；该页面仅用于本地调试。
6. 不加入自动轮询、实时推送、搜索、下载等扩展功能。

## 4. 约束与前提

1. 当前仓库仅有 Flask 后端，没有现成模板页面体系。
2. `.runtime/call_back.json` 由 `app/services/utils/callback_client.py` 在回调发送前写入。
3. 同一个文件会被 `file` 和 `report` 回调轮流覆盖，因此页面必须根据 `businessType` 分支渲染。
4. `file.data.fileDataItem.originalText` 是纯文本。
5. `file.data.fileDataItem.documentTranslationOne`、`file.data.fileDataItem.documentTranslationTwo` 是 HTML 内容或普通文本。
6. `report.data.details` 按 HTML 内容处理；实现上必须同时兼容完整 HTML 文档字符串和 HTML 片段字符串。

## 5. 方案选择

### 5.1 备选方案

#### 方案 A：Flask 调试页 + 只读 JSON 接口 + 页面内少量原生 JS

在现有 Flask 服务中新增：

1. 一个页面路由，例如 `/debug/callback`
2. 一个数据接口，例如 `/debug/api/callback`

页面通过原生 JavaScript 拉取接口返回的 JSON，再根据 `businessType` 进行渲染。

#### 方案 B：纯服务端模板渲染

每次访问页面时由 Flask 模板直接读取 `.runtime/call_back.json`，不单独增加数据接口。

#### 方案 C：通用 JSON 查看页 + 少量字段特殊处理

页面主体仍以原始 JSON 为主，仅对少数字段做增强显示。

### 5.2 推荐方案

本期采用方案 A。

原因如下：

1. 改动仍然很小，不需要前端工程。
2. 模板只负责页面骨架，文件读取和异常处理集中在接口层，更清晰。
3. `file` 和 `report` 两类展示差异较大，前端做条件渲染比把所有分支塞进模板更容易维护。
4. 后续若要扩展更多调试字段或展示样式，不需要重构页面入口。

## 6. 页面与接口设计

### 6.1 路由

新增两个本地调试入口：

1. `GET /debug/callback`
   - 返回调试页面 HTML。
   - 不接收业务参数。
   - 仅用于本地调试阅读。

2. `GET /debug/api/callback`
   - 返回 `.runtime/call_back.json` 的读取结果。
   - 用于页面点击“刷新”时重新获取最新数据。

### 6.2 接口返回结构

接口统一返回 JSON，至少包含以下字段：

1. `ok`
   - 布尔值。
   - 表示本次读取是否成功。

2. `message`
   - 字符串。
   - 用于描述当前状态，例如“读取成功”“未找到回调文件”“回调文件不是合法 JSON”。

3. `payload`
   - 对象或 `null`。
   - 当读取成功时为 `call_back.json` 的完整内容。

不对原始 payload 做裁剪或改写，页面直接基于完整 payload 渲染。

### 6.3 页面行为

页面只保留最小交互：

1. 首次打开时自动读取一次回调数据。
2. 页面顶部提供一个“刷新”按钮。
3. 点击“刷新”后重新请求 `/debug/api/callback` 并重绘页面。
4. 不做自动轮询。

## 7. 页面信息架构

页面固定包含以下四个区域：

### 7.1 顶部状态区

显示当前回调的核心识别信息：

1. `businessType`
2. `msg`
3. 主键字段
   - `file` 显示 `fileName`
   - `report` 显示 `reportId`
4. `status`
5. 刷新按钮
6. 读取错误提示或空状态提示

### 7.2 结构化结果区

根据 `businessType` 显示可读信息，而不是直接照搬 JSON 层级。

### 7.3 内容预览区

专门用于展示长文本和 HTML 内容，是页面的主要阅读区域。

### 7.4 原始 JSON 区

页面底部始终保留格式化后的完整 `call_back.json`，用于协议核对和调试兜底。

## 8. `file` 回调展示规则

`businessType=file` 时，页面按以下分组展示。

### 8.1 任务信息

展示：

1. `businessType`
2. `msg`
3. `data.fileName`
4. `data.status`

状态显示规则：

1. `2` 显示为“解析成功”
2. `3` 显示为“解析失败”
3. 其他状态显示为“未知状态（原始值）”

### 8.2 分类信息

展示以下字段：

1. `data.country`
2. `data.channel`
3. `data.maturity`
4. `data.format`
5. `data.architectureId`

该区适合使用紧凑卡片或双列表格布局，便于快速扫读。

### 8.3 文档摘要信息

展示以下字段：

1. `fileDataItem.summary`
2. `fileDataItem.keyword`
3. `fileDataItem.documentOverview`
4. `fileDataItem.score`
5. `fileDataItem.dataTime`
6. `fileDataItem.source`
7. `fileDataItem.originalLink`
8. `fileDataItem.language`
9. `fileDataItem.dataFormat`
10. `fileDataItem.associatedEquipment`
11. `fileDataItem.relatedTechnology`
12. `fileDataItem.equipmentModel`

展示规则：

1. 空值显示为“暂无内容”。
2. `originalLink` 若是合法 URL，则显示为可点击链接；否则按普通文本显示。

### 8.4 原文区

`fileDataItem.originalText` 单独显示为纯文本阅读面板。

展示要求：

1. 保留换行。
2. 使用 `white-space: pre-wrap` 保持原始段落结构。
3. 支持较长内容滚动。
4. 不进行 HTML 渲染。

原因是该字段属于纯文本，错误地当成 HTML 渲染会带来可读性和安全风险。

### 8.5 翻译区

`fileDataItem.documentTranslationOne` 和 `fileDataItem.documentTranslationTwo` 分为两个独立面板：

1. 单语翻译预览
2. 双语翻译预览

展示规则：

1. 默认显示渲染后的内容，而不是源码。
2. 若字段为空，显示“暂无内容”。
3. 若字段不是有效 HTML，则按普通文本包裹显示，保证页面仍可读。
4. 每个面板附带可折叠的“查看原始 HTML”区域，用于调试。

## 9. `report` 回调展示规则

`businessType=report` 时，页面按以下结构展示。

### 9.1 任务信息

展示：

1. `businessType`
2. `msg`
3. `data.reportId`
4. `data.status`

状态显示规则：

1. `1` 显示为“生成成功”
2. `2` 显示为“生成失败”
3. 其他状态显示为“未知状态（原始值）”

### 9.2 报告预览区

`data.details` 作为主阅读区域展示。

展示规则：

1. 优先按 HTML 内容渲染。
2. 若字段为空，显示“暂无内容”。
3. 若字段不是有效 HTML，则按普通文本包裹显示。
4. 同样保留可折叠的“查看原始 HTML”区域，便于调试。

### 9.3 原始 JSON 区

与 `file` 一样，底部保留完整 JSON。

## 10. HTML 内容渲染策略

为避免 HTML 预览样式污染页面主体，HTML 内容不直接作为页面的一部分拼入主模板，而是放入独立预览容器中渲染。

渲染策略要求如下：

1. 主页面样式与预览内容隔离。
2. `documentTranslationOne`、`documentTranslationTwo`、`details` 三类 HTML 字段采用同一套渲染逻辑。
3. 当内容看起来是完整 HTML 文档时，仍能正常展示正文。
4. 当内容只是 HTML 片段时，也能被正常预览。
5. 当内容实际上只是纯文本时，自动降级为文本显示，不让页面出现空白区。

## 11. 异常与边界处理

### 11.1 文件不存在

当 `.runtime/call_back.json` 不存在时：

1. 接口返回 `ok=false`
2. `payload=null`
3. `message` 明确说明“当前还没有回调结果文件”
4. 页面显示空状态，不抛前端错误

### 11.2 JSON 非法

当文件内容不是合法 JSON 时：

1. 接口返回 `ok=false`
2. `payload=null`
3. `message` 明确说明“回调文件不是合法 JSON”
4. 页面显示错误提示，不继续结构化渲染

### 11.3 不支持的 `businessType`

若文件被写成 `weaponry` 或其他未支持类型：

1. 页面顶部仍显示 `businessType` 和 `msg`
2. 结构化区显示“当前类型暂未提供友好展示”
3. 原始 JSON 区仍完整展示

这样可以保证页面对未来类型变化具备兜底能力，而不扩大本期实现范围。

## 12. 最小实现拆分

本期实现应控制在以下最小改动集合：

1. 新增一个 Flask 蓝图或在现有蓝图中增加调试页路由。
2. 新增一个读取 `.runtime/call_back.json` 的只读接口。
3. 新增一个本地模板文件作为调试页壳层。
4. 在模板内加入少量原生 JavaScript 完成数据拉取与分支渲染。
5. 为新接口补充最小测试。

不对现有 `callback_client.py`、`analysis_service.py`、`report_service.py` 的回调生成逻辑做行为变更。

## 13. 测试策略

本期只做最小必要验证。

### 13.1 自动化测试

新增后端接口测试，覆盖以下场景：

1. `.runtime/call_back.json` 不存在时，接口返回空状态。
2. 文件内容是合法 JSON 且 `businessType=file` 时，接口返回成功。
3. 文件内容是合法 JSON 且 `businessType=report` 时，接口返回成功。
4. 文件内容非法时，接口返回错误状态。

### 13.2 手动验证

使用本地样例分别验证 `file` 和 `report`：

1. `file`
   - 基础字段正确显示
   - `originalText` 以纯文本可读方式展示
   - `documentTranslationOne` 与 `documentTranslationTwo` 以 HTML 预览方式展示
   - 原始 JSON 可核对

2. `report`
   - `reportId`、`status`、`msg` 正确显示
   - `details` 可作为报告正文直接阅读
   - 原始 JSON 可核对

不引入浏览器自动化测试。

## 14. 实现完成后的验收标准

满足以下条件即可认为本期目标完成：

1. 本地访问 `/debug/callback` 能看到调试页面。
2. 页面可以读取 `.runtime/call_back.json` 的最新内容。
3. `file` 回调能按结构化方式展示关键字段。
4. `originalText` 以纯文本方式展示。
5. `documentTranslationOne`、`documentTranslationTwo` 能作为 HTML 预览显示。
6. `report.details` 能作为 HTML 报告正文显示。
7. 页面提供手动刷新按钮。
8. 文件缺失、JSON 非法、不支持类型时页面仍可稳定显示提示和原始信息。

## 15. 后续扩展方向

以下内容可在后续迭代中考虑，但不进入本期计划：

1. 支持多份回调快照或历史记录列表。
2. 增加 `weaponry` 的结构化展示。
3. 增加按字段折叠、复制、下载等调试能力。
4. 增加自动轮询或 WebSocket 刷新。
