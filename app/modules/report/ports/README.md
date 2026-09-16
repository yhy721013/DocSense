# 报告端口层

本目录描述报告 Application 所需的外部能力，不提供 SQLite、HTTP、文件系统或
AnythingLLM 实现。

| 文件 | 职责 |
| --- | --- |
| `files.py` | 下载、规范化、RAG 上传准备和 Word 模板提取。 |
| `artifacts.py` | 任务级命名空间、最终 HTML Artifact 和 scratch 清理。 |
| `rag.py` | 有序多文档报告生成、完整 trace 和外部资源清理引用。 |
| `audit.py` | 主交互、attempts、初始生命周期事件的原子审计及清理事件追加。 |
| `callbacks.py` | 提交阶段的有界 Guard 释放等待，以及 latest 之外的发送权、精确投递 outcome、Guard 完成和内部人工解除审计。 |
| `resources.py` | 任务级 Artifact/RAG/Audit 引用、终态所有权、CAS 清理恢复、待审计事件和逻辑隔离。 |
| `dispatcher.py` | 持久化任务提交后的常量空间唤醒信号，以及组合根拥有的显式 start/stop/close 生命周期。 |

端口 DTO 只携带领域对象、通用 `TaskId` 和供应商无关的不透明引用；禁止出现真实路径、
SQLAlchemy Session、SQLite connection、requests Response、AnythingLLM workspace DTO、
Celery/RabbitMQ 消息对象或 Flask/FastAPI 类型。

阶段 1C-2 已通过严格 Fake 验证这些协议；阶段 1C-3 又实现 tasks 侧兼容 SQLite Task
Command Adapter 和 report Codec，全面审查补强实现了 Callback Port 的单实例 SQLite
Guard/HTTP Adapter 核心。阶段 1C-4 已实现 File/Artifact/RAG/Audit Adapter 和离线组合；
阶段 1C-5 已完成 Callback 人工解除审计、Artifact 所有权及 cleanup/quarantine 持久恢复
闭环；阶段 1C-6 已实现并装配本地 Dispatcher、组合根生命周期和报告薄路由。端口保持
供应商无关，阶段 6 可替换为可靠队列 Dispatcher 而不改 Report Application。
