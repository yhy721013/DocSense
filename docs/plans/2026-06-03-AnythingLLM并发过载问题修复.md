# 修复 AnythingLLM Document Processor 并发过载问题

本次改造聚焦文件解析与报告生成链路中出现的 AnythingLLM Document Processor 上传失败问题。

## 1. 背景与问题描述

在处理多个文件解析任务（或连续请求）时，`/llm/analysis` 接口会在调用 AnythingLLM 上传文档时遭遇 `500` 报错。典型的错误日志包含两种变体：

1. **连接失败 (前驱状态)**:
   `500 {"success":false,"error":"fetch failed","documents":[]}`
2. **处理服务离线**:
   `500 {"success":false,"error":"Document processing API is not online. Document xxx will not be processed automatically."}`

### 1.1 问题根因

AnythingLLM 内部的 Document Processor 是一个相对脆弱的子进程。当短时间内发起大量并发上传请求，或者连续上传文件时，Processor 容易遭遇性能瓶颈并短暂崩溃（或拒绝连接）。

原有代码在调用 `AnythingLLMClient.upload_document` 时：
1. **缺乏并发控制**：多个解析请求会立即派发多个后台线程并发向 AnythingLLM 传输文件。
2. **缺乏容错机制**：遇到 500 错误直接返回 `None` 导致后续环节（如 RAG 解析、知识库存入）级联失败。

## 2. 修复方案说明

为了彻底解决这一问题，我们在客户端层和路由层分别实施了拦截与自愈策略。

### 2.1 客户端重试与指数退避（容错自愈）

**代码位置**：`app/services/utils/anythingllm_client.py`

在 `upload_document` 中针对特定的 Processor 异常状态引入了重试机制：
- **精准拦截**：通过 `_is_processor_unavailable()` 方法，仅针对响应中包含 `"fetch failed"` 或 `"Document processing API is not online"` 的 500 错误进行重试，不影响正常的业务 400/403 错误。
- **指数退避**：设定最大重试次数为 3 次，等待时间指数递增（3秒 → 6秒 → 12秒）。这足以覆盖 Processor 崩溃后的重启或自我恢复窗口。

### 2.2 全局信号量并发控制（源头限流）

**代码位置**：`app/blueprints/llm.py`

为了防止多用户请求直接把 Processor 压垮，我们在应用层引入了严格的串行机制：
- **全局 Semaphore**：创建了模块级的 `_upload_semaphore = threading.Semaphore(1)`。
- **关键路径加锁**：利用包装函数 `_with_upload_semaphore`，在执行 `run_file_analysis_task`、`run_file_analysis_batch_task` 和 `run_report_task` 等重度依赖文件上传的后台任务时，获取信号量。
- **安全区隔**：不涉及文件上传的任务（如 `run_weaponry_task`）不受此信号量限制，确保其他类型请求的吞吐量。

## 3. 数据流与执行流总览

当两个 `/llm/analysis` 请求并发到达时，系统的表现变化如下：

**修复前：**
```
请求 A → 启动线程 A → 上传大文件
请求 B → 启动线程 B → 并发上传大文件 
(Processor 崩溃) → 线程 A、B 同时收到 500 报错 → 任务级联失败
```

**修复后：**
```
请求 A → 启动线程 A → 获取 Semaphore → 开始上传大文件
请求 B → 启动线程 B → 尝试获取 Semaphore (阻塞等待)
(若 Processor 发生偶发抖动) 
   → 线程 A 收到 500 fetch failed 
   → 等待 3 秒后重试 → 上传成功 
   → 线程 A 释放 Semaphore
线程 B 获取 Semaphore → 开始上传 → 上传成功
```

## 4. 影响与测试验证

- 修复了因为上游崩溃而产生的二次连锁报错（如 `architectureId匹配失败: reason=no_candidate_name fallback=1`）。
- 现有的所有 49 个单元测试均一次性通过（涉及分析服务、聊天接口和 AnythingLLM 客户端工具等）。
- 不破坏原有的任务调度系统，无需修改现有数据库架构，属于非侵入性增强。
