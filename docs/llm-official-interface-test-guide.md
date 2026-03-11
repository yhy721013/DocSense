# 甲方正式接口完整测试指导

本文档只覆盖甲方正式接口与出站回调能力：

- `POST /llm/analysis`
- `POST /llm/generate-report`
- `POST /llm/check-task`
- `WS /llm/progress`
- 出站回调 `POST /llm/callback`

不覆盖调试接口 `/api/classify/*`、`/api/chat/*`，也不覆盖现有 Web 页面手工操作。

## 1. 本地开发联调

### 1.1 目标

本部分的目标是让你在不依赖甲方前后端的情况下，本地独立完成以下验证：

- 文件解析任务可正常提交
- 报告生成任务可正常提交
- WebSocket 进度可正常订阅
- `check-task` 可查询任务状态
- 任务完成后可按甲方协议主动回调
- 回调失败后可通过 `check-task` 补发
- 参数错误和业务失败场景可被正确识别

### 1.2 前置条件

所有命令默认在仓库根目录 `E:\DocSense` 执行，终端使用 PowerShell 7。

需要提前准备：

- AnythingLLM 服务可用
- 已配置 `ANYTHINGLLM_BASE_URL`
- 已配置 `ANYTHINGLLM_API_KEY`
- 建议显式配置 `ANYTHINGLLM_TIMEOUT`
- Python 虚拟环境已可用
- 测试素材存在于 [tests/fixtures/files/sample.txt](/e:/DocSense/tests/fixtures/files/sample.txt)

本地联调会使用这些现成脚本：

- [scripts/start_test_file_server.ps1](/e:/DocSense/scripts/start_test_file_server.ps1)
- [scripts/mock_callback_server.py](/e:/DocSense/scripts/mock_callback_server.py)
- [scripts/test_llm_analysis.ps1](/e:/DocSense/scripts/test_llm_analysis.ps1)
- [scripts/test_llm_report.ps1](/e:/DocSense/scripts/test_llm_report.ps1)
- [scripts/test_llm_check_task.ps1](/e:/DocSense/scripts/test_llm_check_task.ps1)
- [scripts/test_llm_progress.ps1](/e:/DocSense/scripts/test_llm_progress.ps1)

本地联调会使用这些样例请求：

- [tests/fixtures/llm/analysis_request.json](/e:/DocSense/tests/fixtures/llm/analysis_request.json)
- [tests/fixtures/llm/report_request.json](/e:/DocSense/tests/fixtures/llm/report_request.json)
- [tests/fixtures/llm/check_task_file_request.json](/e:/DocSense/tests/fixtures/llm/check_task_file_request.json)
- [tests/fixtures/llm/check_task_report_request.json](/e:/DocSense/tests/fixtures/llm/check_task_report_request.json)

### 1.3 关键观察点

联调过程中，优先看这 4 个位置：

- 服务端控制台日志
- 假回调服务控制台输出
- [last_callback.json](/e:/DocSense/.runtime/mock_callback/last_callback.json)
- [llm_tasks.sqlite3](/e:/DocSense/.runtime/llm_tasks.sqlite3)

其中：

- `.runtime/mock_callback/last_callback.json` 只保留最后一次收到的回调
- `.runtime/llm_tasks.sqlite3` 保存正式 `/llm/*` 任务状态

### 1.4 启动顺序

建议开 4 个 PowerShell 7 窗口，顺序不要变。

#### 窗口 1：启动本地文件服务

```powershell
pwsh -NoLogo -Command "./scripts/start_test_file_server.ps1"
```

预期结果：

- 控制台出现 `Serving HTTP on`
- `http://127.0.0.1:8000/sample.txt` 可访问

#### 窗口 2：启动本地假回调服务

```powershell
pwsh -NoLogo -Command "python scripts/mock_callback_server.py"
```

预期结果：

- 控制台出现 `Mock callback server listening on http://127.0.0.1:9000`

#### 窗口 3：配置环境变量并启动应用

```powershell
$env:DOCSENSE_LLM_CALLBACK_URL='http://127.0.0.1:9000/llm/callback'
$env:ANYTHINGLLM_BASE_URL='http://localhost:3001/api/v1'
$env:ANYTHINGLLM_API_KEY='你的APIKey'
$env:ANYTHINGLLM_TIMEOUT='120'
python web_ui.py
```

预期结果：

- 应用启动成功
- 默认地址可访问 `http://127.0.0.1:5001`

说明：

- `DOCSENSE_LLM_CALLBACK_URL` 必须在启动 `web_ui.py` 的同一个终端里设置
- 如果没有这个变量，任务可能成功，但不会产生本地回调文件

#### 窗口 4：执行测试命令

后续所有测试命令都在这个窗口执行。

### 1.5 文件解析成功链路

#### 步骤 1：提交文件解析任务

```powershell
pwsh -NoLogo -Command "./scripts/test_llm_analysis.ps1"
```

预期结果：

- 返回 HTTP `202`
- 返回体中包含：

```json
{
  "message": "accepted",
  "businessType": "file",
  "task": {
    "business_type": "file",
    "business_key": "sample.txt"
  }
}
```

#### 步骤 2：订阅文件任务进度

```powershell
pwsh -NoLogo -Command "./scripts/test_llm_progress.ps1"
```

预期结果：

- 控制台打印若干条 JSON
- 至少会出现类似消息：

```json
{"businessType":"file","data":{"fileName":"sample.txt","progress":0.35}}
{"businessType":"file","data":{"fileName":"sample.txt","progress":1.0}}
```

说明：

- 进度消息只说明任务推进，不代表回调已经成功
- 脚本中的 `System.Threading.Tasks.VoidTaskResult` 可忽略

#### 步骤 3：查询文件任务状态

```powershell
pwsh -NoLogo -Command "./scripts/test_llm_check_task.ps1"
```

预期结果：

- 返回体中 `businessType=file`
- `data.fileName=sample.txt`
- 处理中时 `status=1`
- 成功时 `status=2`
- 失败时 `status=3`

成功示例：

```json
{
  "businessType": "file",
  "data": {
    "fileName": "sample.txt",
    "status": "2",
    "progress": 1.0,
    "callbackStatus": "success"
  },
  "callbackReplayed": false
}
```

#### 步骤 4：查看文件回调体

```powershell
Get-Content -Path ".runtime/mock_callback/last_callback.json" -Raw -Encoding utf8
```

如需格式化查看：

```powershell
Get-Content -Path ".runtime/mock_callback/last_callback.json" -Raw -Encoding utf8 | ConvertFrom-Json | ConvertTo-Json -Depth 20
```

文件解析成功时，应重点检查：

- `businessType = "file"`
- `data.fileName = "sample.txt"`
- `data.status = "2"`
- `msg = "解析成功"`
- `data.fileDataItem` 存在

至少应验证这些字段是否结构正确：

- `data.country`
- `data.channel`
- `data.maturity`
- `data.format`
- `data.architectureId`
- `data.fileDataItem.fileName`
- `data.fileDataItem.summary`
- `data.fileDataItem.originalText`
- `data.fileDataItem.documentTranslationOne`
- `data.fileDataItem.documentTranslationTwo`

补充说明：

- 当前翻译字段保留，但默认应为空字符串
- [analysis_request.json](/e:/DocSense/tests/fixtures/llm/analysis_request.json) 当前未传范围字段，后端会自动注入默认测试范围
- 正式联调时，如果甲方请求显式传入 `channel/country/format/maturity/architectureList`，最终以甲方请求范围为准

### 1.6 报告生成成功链路

#### 步骤 1：提交报告生成任务

```powershell
pwsh -NoLogo -Command "./scripts/test_llm_report.ps1"
```

预期结果：

- 返回 HTTP `202`
- 返回体中 `businessType=report`
- `task.business_key=132`

#### 步骤 2：订阅报告任务进度

```powershell
pwsh -NoLogo -Command "./scripts/test_llm_progress.ps1 -PayloadPath 'tests/fixtures/llm/check_task_report_request.json'"
```

预期结果：

- 能收到类似消息：

```json
{"businessType":"report","data":{"reportId":132,"progress":0.35}}
{"businessType":"report","data":{"reportId":132,"progress":1.0}}
```

#### 步骤 3：查询报告任务状态

```powershell
pwsh -NoLogo -Command "./scripts/test_llm_check_task.ps1 -PayloadPath 'tests/fixtures/llm/check_task_report_request.json'"
```

预期结果：

- `businessType=report`
- `data.reportId=132`
- 处理中时 `status=0`
- 成功时 `status=1`
- 失败时 `status=2`

成功示例：

```json
{
  "businessType": "report",
  "data": {
    "reportId": 132,
    "status": "1",
    "progress": 1.0,
    "callbackStatus": "success"
  },
  "callbackReplayed": false
}
```

#### 步骤 4：查看报告回调体

报告回调同样会写入 [last_callback.json](/e:/DocSense/.runtime/mock_callback/last_callback.json)，因此执行报告链路前，建议先把文件回调结果另存。

可复制保存上一份结果：

```powershell
Copy-Item ".runtime/mock_callback/last_callback.json" ".runtime/mock_callback/file_callback_snapshot.json" -Force
```

然后查看当前最新回调：

```powershell
Get-Content -Path ".runtime/mock_callback/last_callback.json" -Raw -Encoding utf8 | ConvertFrom-Json | ConvertTo-Json -Depth 20
```

报告生成成功时，应重点检查：

- `businessType = "report"`
- `data.reportId = 132`
- `data.status = "1"`
- `data.details` 为 HTML 字符串
- `msg = "生成成功"`

### 1.7 回调补发场景

这个场景用于验证 `/llm/check-task` 是否能对“业务已完成但未成功回调”的任务执行补发。

#### 步骤 1：不要启动假回调服务

关闭窗口 2，保持应用继续运行。

#### 步骤 2：重新提交文件解析任务或报告任务

文件任务：

```powershell
pwsh -NoLogo -Command "./scripts/test_llm_analysis.ps1"
```

报告任务：

```powershell
pwsh -NoLogo -Command "./scripts/test_llm_report.ps1"
```

#### 步骤 3：等任务进入终态

用 `check-task` 查询，直到状态到达终态：

- 文件：`2` 或 `3`
- 报告：`1` 或 `2`

#### 步骤 4：重新启动假回调服务

```powershell
pwsh -NoLogo -Command "python scripts/mock_callback_server.py"
```

#### 步骤 5：再次调用 `check-task`

文件：

```powershell
pwsh -NoLogo -Command "./scripts/test_llm_check_task.ps1"
```

报告：

```powershell
pwsh -NoLogo -Command "./scripts/test_llm_check_task.ps1 -PayloadPath 'tests/fixtures/llm/check_task_report_request.json'"
```

预期结果：

- 返回体中 `callbackReplayed = true`
- 假回调服务收到回调
- `.runtime/mock_callback/last_callback.json` 被创建或更新

### 1.8 失败场景测试

#### 文件解析失败

做法：

- 把 [analysis_request.json](/e:/DocSense/tests/fixtures/llm/analysis_request.json) 里的 `filePath` 改成错误地址
- 或停止本地文件服务
- 或停止 AnythingLLM

预期结果：

- `/llm/analysis` 仍可能先返回 `202`
- 最终 `/llm/check-task` 返回 `status = "3"`
- 回调体中的 `msg = "解析失败"`

#### 报告生成失败

做法：

- 把 [report_request.json](/e:/DocSense/tests/fixtures/llm/report_request.json) 里的某个 `filePathList` 地址改错
- 或停止 AnythingLLM

预期结果：

- 最终 `/llm/check-task` 返回 `status = "2"`
- 回调体中的 `msg = "生成失败"`

### 1.9 参数校验失败场景

建议至少覆盖以下 6 个错误请求：

- `/llm/analysis` 中 `businessType != file`
- `/llm/analysis` 缺少 `params`
- `/llm/analysis` 缺少 `fileName`
- `/llm/analysis` 缺少 `filePath`
- `/llm/generate-report` 缺少 `reportId`
- `/llm/generate-report` 缺少 `filePathList`

可直接在 PowerShell 7 中手工构造：

```powershell
$body = @{
  businessType = 'file'
  params = @(
    @{
      fileName = 'sample.txt'
    }
  )
} | ConvertTo-Json -Depth 10

Invoke-RestMethod -Uri "http://127.0.0.1:5001/llm/analysis" -Method Post -ContentType "application/json; charset=utf-8" -Body $body
```

预期结果：

- 直接返回 HTTP `400`
- 不创建有效后台任务
- 不产生成功回调

### 1.10 本地联调验收清单

全部通过后，本地联调可视为完成：

- `/llm/analysis` 能返回 `202`
- `/llm/generate-report` 能返回 `202`
- `/llm/progress` 可推送文件进度
- `/llm/progress` 可推送报告进度
- `/llm/check-task` 可查询文件任务
- `/llm/check-task` 可查询报告任务
- 文件成功回调结构正确
- 报告成功回调结构正确
- 文件失败回调结构正确
- 报告失败回调结构正确
- `check-task` 补发回调生效
- 参数错误场景返回 `400`

### 1.11 本地联调常见问题

#### 没有 `.runtime/mock_callback`

优先检查：

- 是否先启动了 [mock_callback_server.py](/e:/DocSense/scripts/mock_callback_server.py)
- 启动应用的终端里是否设置了 `DOCSENSE_LLM_CALLBACK_URL`
- 回调地址是否为 `http://127.0.0.1:9000/llm/callback`

#### 进度到了 `1.0` 但没有回调文件

优先检查：

- 应用启动时是否带了回调地址
- 假回调服务是否在任务完成时处于运行状态
- 再调用一次 `/llm/check-task`，看 `callbackReplayed` 是否变为 `true`

#### `check-task` 返回任务不存在

优先检查：

- `fileName` 是否与提交任务时完全一致
- `reportId` 是否一致
- 当前应用是否在使用同一个 [llm_tasks.sqlite3](/e:/DocSense/.runtime/llm_tasks.sqlite3)

#### 返回成功但很多抽取字段为空

优先检查：

- AnythingLLM 是否返回了有效结构化结果
- 当前测试文件是否真的包含这些信息
- 请求是否显式传入了范围字段，导致最终结果被范围裁剪为空

#### 任务长时间停在处理中

优先检查：

- AnythingLLM 是否可用
- `ANYTHINGLLM_BASE_URL` 和 `ANYTHINGLLM_API_KEY` 是否正确
- 是否显式配置了 `ANYTHINGLLM_TIMEOUT`

## 2. 部署到甲方系统后的联调方式

### 2.1 目标

本部分的目标是验证：本项目部署进甲方系统后，甲方真实前后端链路能够完整跑通正式协议。

验收的不是“单个接口能否访问”，而是整条业务链路：

- 甲方前端选择范围并提交任务
- 甲方系统调用本模块正式 `/llm/*` 接口
- 本模块异步处理任务
- 本模块向甲方真实回调地址发送结果
- 甲方业务系统接收回调并更新页面或业务状态

### 2.2 甲方环境前置条件

联调前至少确认这些条件：

- 本模块已部署到甲方系统内，且 `/llm/*` 路由可访问
- `DOCSENSE_LLM_CALLBACK_URL` 已改为甲方真实回调地址
- AnythingLLM 与本模块网络互通
- 甲方回调接收端已具备日志或落库能力
- 甲方系统可以提供真实或可替代的下载地址

建议甲方系统至少准备两类测试文件：

- 一份能稳定抽取到字段的文本类文件
- 一份能稳定触发失败的错误地址或无效文件

### 2.3 文件解析联调链路

#### 步骤 1：甲方前端或甲方后端发起文件解析

正式请求体应符合 [api-test.md](/e:/DocSense/api-test.md)。

关键点：

- `businessType` 固定为 `file`
- `params[0].fileName` 作为业务主键
- `params[0].filePath` 为可下载地址
- `channel/country/format/maturity/architectureList` 若甲方用户已选择范围，则请求中必须显式传入

范围规则：

- 模型只能在甲方请求给定范围内返回对应值
- 命中范围内值才保留
- 未命中或超出范围则置空
- `architectureId` 未命中时返回 `0`

#### 步骤 2：甲方前端订阅任务进度

订阅 `/llm/progress`，首条消息发送：

```json
{
  "businessType": "file",
  "params": [
    {
      "fileName": "业务文件名"
    }
  ]
}
```

验收点：

- 能收到 `businessType=file` 的进度推送
- `data.fileName` 与提交时一致
- `data.progress` 最终能推进到 `1.0`

#### 步骤 3：甲方后端或联调人员调用 `check-task`

当页面需要轮询补偿，或回调结果需要人工确认时，调用：

```json
{
  "businessType": "file",
  "params": [
    {
      "fileName": "业务文件名"
    }
  ]
}
```

验收点：

- 处理中时返回 `status = "1"`
- 成功时返回 `status = "2"`
- 失败时返回 `status = "3"`
- 如此前回调未成功，`callbackReplayed` 应可变为 `true`

#### 步骤 4：验证甲方真实回调

甲方回调接收端应检查：

- `businessType = "file"`
- `data.fileName` 与业务主键一致
- `data.status` 为 `2` 或 `3`
- 成功时 `msg = "解析成功"`
- 失败时 `msg = "解析失败"`
- 成功时 `data.fileDataItem` 结构完整

如果甲方系统有落库或页面展示逻辑，还应继续检查：

- 甲方数据库中的任务状态是否已更新
- 页面是否已展示解析完成结果
- 结构化字段是否按甲方选择范围被正确裁剪

### 2.4 报告生成联调链路

#### 步骤 1：甲方前端或甲方后端发起报告生成

正式请求体应包含：

- `businessType = "report"`
- `params[0].reportId`
- `params[0].filePathList`
- `params[0].templateDesc`
- `params[0].templateOutline`
- `params[0].requirement`

#### 步骤 2：订阅报告任务进度

订阅 `/llm/progress`，首条消息发送：

```json
{
  "businessType": "report",
  "params": [
    {
      "reportId": 132
    }
  ]
}
```

验收点：

- 返回 `businessType=report`
- `data.reportId` 正确
- `data.progress` 最终推进到 `1.0`

#### 步骤 3：查询报告任务状态

调用 `/llm/check-task`：

```json
{
  "businessType": "report",
  "params": [
    {
      "reportId": 132
    }
  ]
}
```

验收点：

- 处理中时 `status = "0"`
- 成功时 `status = "1"`
- 失败时 `status = "2"`

#### 步骤 4：验证甲方真实回调

甲方回调接收端应检查：

- `businessType = "report"`
- `data.reportId` 与业务主键一致
- 成功时 `data.status = "1"`
- 失败时 `data.status = "2"`
- 成功时 `msg = "生成成功"`
- 失败时 `msg = "生成失败"`
- 成功时 `data.details` 为 HTML 字符串

如果甲方前端需要直接展示报告，还应继续检查：

- `details` 是否可被甲方页面安全嵌入
- HTML 片段是否满足甲方展示容器要求

### 2.5 部署后补发回调验证

这个场景必须保留，因为甲方系统真实网络和权限环境更容易出现回调失败。

推荐做法：

- 先临时让甲方回调接收端不可用
- 提交一个文件任务或报告任务
- 等任务进入终态
- 恢复甲方回调接收端
- 再调用 `/llm/check-task`

验收点：

- `callbackReplayed = true`
- 甲方回调接收端补收到完整报文
- 甲方侧业务状态被补齐

### 2.6 部署后联调验收清单

全部通过后，可视为部署联调完成：

- 甲方前端可触发文件解析
- 甲方前端可触发报告生成
- 甲方页面可收到或展示进度
- 甲方后端可调用 `check-task`
- 文件成功回调到达甲方系统
- 文件失败回调到达甲方系统
- 报告成功回调到达甲方系统
- 报告失败回调到达甲方系统
- 回调失败后可通过 `check-task` 补发
- 范围字段最终以甲方请求为准

### 2.7 部署后常见问题

#### 甲方已提交任务，但没有任何回调

优先检查：

- `DOCSENSE_LLM_CALLBACK_URL` 是否已指向甲方真实地址
- 甲方回调地址是否可从部署环境访问
- 甲方回调端是否有鉴权、白名单或网关限制

#### 甲方页面显示处理中，但后台其实已经完成

优先检查：

- 甲方页面是否订阅了正确的 `fileName` 或 `reportId`
- 甲方轮询是否调用了 `/llm/check-task`
- 回调失败后是否触发了补发

#### 甲方收到回调，但字段被置空

优先检查：

- 甲方请求是否显式传入了很窄的范围
- 文档内容是否真的命中该范围
- 范围项的 `key/value` 结构是否与正式接口约定一致

#### 报告回调成功，但甲方页面无法展示

优先检查：

- `details` 是否被甲方页面当作 HTML 富文本处理
- 甲方前端是否有额外的 HTML 清洗规则

## 3. 建议的执行顺序

推荐按下面顺序完成整套测试：

1. 本地文件解析成功链路
2. 本地报告生成成功链路
3. 本地回调补发链路
4. 本地失败链路
5. 本地参数校验链路
6. 甲方环境文件解析联调
7. 甲方环境报告生成联调
8. 甲方环境回调补发联调

按这个顺序执行，最容易定位问题归属：

- 如果本地都不通，优先修本模块
- 如果本地通、甲方环境不通，优先查网络、权限、回调和甲方系统集成
