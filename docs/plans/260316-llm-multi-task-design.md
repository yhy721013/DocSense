# LLM 多任务与进度扩展设计

**日期：** 2026-03-16

**目标：** 在继续以 [api-test.md](/e:/DocSense/api-test.md) 为最高优先级的前提下，为 `/llm/analysis`、`/llm/check-task`、`/llm/progress` 增加多文件任务、批量任务查询、单连接多订阅 WebSocket，并保持现有单任务调用方式兼容。

## 1. 背景与约束

- 甲方正式协议仍以 [api-test.md](/e:/DocSense/api-test.md) 为准。
- 当前实现实际上只处理 `params[0]`，导致多文件请求只会真正处理第一个对象。
- 当前 WebSocket 只把首条消息视为订阅请求，后续消息被读取但不会执行任何动作。
- 本次改动必须遵循“最小、低风险、可回滚”的原则，不引入新基础设施，不扩大到无关模块。
- 用户额外确认的约束如下：
  - 上传多个文件时按请求顺序串行处理，不做并行解析。
  - 翻译内部已有的细分进度点不对外推送，继续保留当前粗粒度阶段进度即可。

## 2. 方案选择

采用“兼容扩展方案”：

- 保持 `/llm/analysis`、`/llm/check-task`、`/llm/progress` 路径不变。
- 单任务调用继续兼容现有协议和现有测试。
- 当 `params` 中出现多个对象时，后端扩展为批量受理、多任务管理和串行执行。
- 不引入新的批次表，不重构成新的任务层级，继续沿用“一个业务对象对应一条任务记录”的模型。

选择该方案的原因：

- 与甲方现有文档最贴近。
- 现有单文件联调脚本可以继续工作。
- 对 SQLite 任务表、路由和服务层只做增量修改，回滚简单。

## 3. `/llm/analysis` 设计

### 3.1 请求兼容策略

- 保持 `businessType=file` 不变。
- `params` 支持 1..N 个文件对象。
- 每个对象仍使用现有字段：
  - `fileName`
  - `filePath`
  - `architectureList`
  - `channel`
  - `country`
  - `format`
  - `maturity`

### 3.2 校验规则

- `params` 不能为空，且每个元素都必须是对象。
- 每个对象都必须提供合法的 `fileName` 和 `filePath`。
- 同一批次内若出现重复 `fileName`，直接返回 `400`。
- 若任一 `fileName` 已存在进行中的任务，整批返回 `409`，避免任务状态被覆盖。

### 3.3 受理与返回

- 路由层先一次性校验全部 `params`。
- 全部合法后，为每个文件分别创建任务记录。
- 单文件请求时，返回保持当前兼容结构：
  - `message`
  - `businessType`
  - `task`
- 多文件请求时，返回：
  - `message`
  - `businessType`
  - `tasks`
- 每个任务摘要包含：
  - `business_key`
  - `status`
  - `progress`
  - `callback_status`

### 3.4 执行模型

- 一个批次只启动一个后台线程。
- 后台线程内部按 `params` 顺序逐个处理文件。
- 第一个任务在受理后立即进入 `status=1`。
- 后续任务初始设为 `status=0`、`progress=0.0`，表示已受理但未开始。
- 当前文件处理完成后，再将下一个文件切换到 `status=1` 并开始处理。

## 4. 文件任务模型与状态流转

继续使用现有 SQLite 单表 `llm_tasks`，主键保持：

- 文件任务：`(business_type='file', business_key=fileName)`
- 报告任务：`(business_type='report', business_key=reportId)`

### 4.1 文件任务状态

对外状态仍保持甲方定义：

- `0` 未解析
- `1` 解析中
- `2` 已解析
- `3` 解析失败

### 4.2 文件任务进度

继续使用当前粗粒度进度点：

- `0.0` 已受理未开始
- `0.15` 正在下载文件
- `0.35` 正在执行文档解析
- `0.65` 正在翻译文档
- `0.95` 翻译完成，准备回调
- `1.0` 已结束

说明：

- 不接入翻译内部 `0.0/0.5/0.8/1.0` 的细分进度。
- “细粒度进度”在本期的实现边界中，指的是多个独立任务都能被各自订阅、查询和推送，不再只支持一个任务。

### 4.3 重复提交策略

- 同一批次内重复业务键：直接拒绝。
- 存在进行中任务时再次提交同一业务键：直接拒绝。
- 已结束任务再次提交同一业务键：允许覆盖重跑，按新一轮任务重新写入状态。

## 5. `/llm/check-task` 设计

### 5.1 单项查询

- 当 `params` 只有 1 项时，保持当前返回结构兼容：

```json
{
  "businessType": "file",
  "data": {
    "fileName": "sample.txt",
    "status": "1",
    "progress": 0.35,
    "callbackStatus": "pending"
  },
  "callbackReplayed": false
}
```

### 5.2 批量查询

- 当 `params` 有多项时，`data` 改为数组，每项独立返回任务快照：

```json
{
  "businessType": "file",
  "data": [
    {
      "fileName": "a.pdf",
      "status": "2",
      "progress": 1.0,
      "callbackStatus": "success",
      "callbackReplayed": false
    },
    {
      "fileName": "b.pdf",
      "status": "0",
      "progress": 0.0,
      "callbackStatus": "pending",
      "callbackReplayed": false
    }
  ]
}
```

### 5.3 不存在任务的处理

- 单项查询保持当前严格行为，任务不存在时直接 `404`。
- 批量查询不整体失败，按项返回：
  - `exists: false`
  - `message: "任务不存在"`

### 5.4 回调补发

- 单项和批量查询都沿用现有“业务已完成但回调未成功时补发一次回调”的逻辑。
- 批量模式下，`callbackReplayed` 为逐项字段，不再只返回顶层布尔值。

## 6. `/llm/progress` WebSocket 设计

### 6.1 兼容旧订阅方式

- 客户端连接后发送旧格式首条消息时：

```json
{
  "businessType": "file",
  "params": [
    {
      "fileName": "sample.txt"
    }
  ]
}
```

- 服务端按 `subscribe` 动作处理，保持老用法可用。

### 6.2 新增显式动作

一个连接内支持三类消息：

- `subscribe`
- `unsubscribe`
- `query`

示例：

```json
{
  "action": "subscribe",
  "businessType": "file",
  "params": [
    {
      "fileName": "a.pdf"
    },
    {
      "fileName": "b.pdf"
    }
  ]
}
```

```json
{
  "action": "query",
  "businessType": "file",
  "params": [
    {
      "fileName": "a.pdf"
    }
  ]
}
```

```json
{
  "action": "unsubscribe",
  "businessType": "file",
  "params": [
    {
      "fileName": "a.pdf"
    }
  ]
}
```

### 6.3 连接内状态

- 每个 WebSocket 连接维护自己的订阅集合。
- 一个连接可同时订阅多个 `fileName` 或多个 `reportId`。
- `subscribe` 成功后，立即回发当前最新进度快照。
- `query` 只返回快照，不改变订阅关系。
- `unsubscribe` 只影响当前连接，不影响其他连接。

### 6.4 推送消息结构

任务进度消息保持一条消息对应一个任务：

```json
{
  "businessType": "file",
  "data": {
    "fileName": "sample.txt",
    "progress": 0.35
  }
}
```

控制消息新增：

- `ack`
- `error`

示例：

```json
{
  "type": "ack",
  "action": "subscribe",
  "count": 2
}
```

```json
{
  "type": "error",
  "message": "订阅参数无效"
}
```

## 7. 服务拆分与最小改动范围

本期仅修改以下核心文件：

- [app/blueprints/llm.py](/e:/DocSense/app/blueprints/llm.py)
- [app/services/llm_analysis_service.py](/e:/DocSense/app/services/llm_analysis_service.py)
- [app/services/llm_task_service.py](/e:/DocSense/app/services/llm_task_service.py)
- [app/services/llm_progress_hub.py](/e:/DocSense/app/services/llm_progress_hub.py)
- `tests/test_llm_routes.py`
- `tests/test_llm_task_service.py`
- `tests/test_llm_progress_and_check_task.py`
- `tests/test_llm_analysis_service.py`

不做的事情：

- 不新增批次表。
- 不引入队列、Redis 或消息中间件。
- 不改报告生成接口的业务语义。
- 不重构与甲方协议无关的分类、对话和 UI 模块。

## 8. 错误处理原则

- `/llm/analysis` 在批量校验阶段发现任何非法项时，整批直接失败，不做部分受理。
- 批次一旦受理，单个文件失败不会中断后续文件的串行处理。
- 文件业务失败与回调失败继续分开记录。
- WebSocket 收到非法 JSON、非法 action、非法 params 时，返回 `error` 控制消息但不断开连接。

## 9. 测试策略

按 TDD 扩展现有测试，重点覆盖以下行为：

### 9.1 路由测试

- `analysis` 多文件请求返回 `tasks`
- 多文件请求仅启动一个批次线程
- 重复 `fileName` 返回 `400`
- 进行中任务重复提交返回 `409`
- `check-task` 多项查询返回数组 `data`

### 9.2 任务服务测试

- 可创建“未开始”任务
- 可批量读取任务快照
- 已完成但回调失败的任务仍可补发回调

### 9.3 分析服务测试

- 单文件成功路径保持兼容
- 串行批次处理中，第一个文件结束前第二个文件不进入进行中

### 9.4 WebSocket / 进度测试

- 旧订阅格式仍可订阅
- 单连接多订阅生效
- `query` 返回当前快照
- `unsubscribe` 后停止收到对应任务的进度

## 10. 本期实施边界

本期纳入：

- 文件解析多任务受理
- 文件任务串行执行
- 批量 `check-task`
- 单连接多订阅 WebSocket
- 现有粗粒度进度的多任务推送

本期不纳入：

- 文件任务并行执行
- 翻译内部细粒度进度推送
- 新的任务批次表或批次查询接口
- 与该需求无关的顺手重构
