# 业务模块目录说明

`app/modules/` 是 DocSense 模块化单体的业务聚合根。每个一级子目录代表一个拥有明确业务语言和数据职责的模块，而不是按 Flask、Celery 或数据库技术分类的工具集合。

## 目标依赖方向

```text
Web/Worker Adapter
  -> Application
      -> Domain
      -> Ports
  -> Infrastructure Adapter（实现 Ports）
```

- `domain/` 只表达稳定业务概念和规则，不依赖 Web 框架、队列、数据库或网络客户端。
- `application/` 编排用例，只依赖本模块的领域类型/抽象端口；业务模块可依赖 tasks 的
  公共 `TaskId` 与控制面 Port，但不得依赖 tasks Adapter。
- `ports/` 描述应用层所需能力，不包含具体 SQL、HTTP 路径或供应商对象。
- `adapters/` 实现端口并隔离遗留服务、数据库、队列和外部系统。
- 模块之间通过明确的应用入口、事件或共享端口协作，不直接导入对方 Adapter，也不跨模块写表。

## 当前模块

| 模块 | 职责 | 当前状态 |
| --- | --- | --- |
| `tasks/` | 通用任务身份、状态读取、写入命令、回调恢复与进度边界 | 1A/1B 已接管 Progress 并建立可靠恢复命令；1C 增加 SQLite Task Command/Queue Inspection、原子 latest Progress Guard和共享审计 Schema v3；1D-5 抽取业务无关持久扫描 Dispatcher 与 OS 单实例锁，Report/Weaponry 通过薄适配复用 |
| `report/` | 报告输入、HTML 结果、回调载荷及报告执行用例 | 阶段 1C 已关闭：契约、Domain/Application/Ports、SQLite 任务事实、任务级 I/O/RAG/Audit、Callback/资源恢复、一条执行 Worker与两条隔离维护线程、毒任务冷却、跨进程单实例门禁、组合根、当前 Flask 薄路由及最终扩大回归均已完成；尚未部署生产或接入可靠队列 |
| `weaponry/` | 武器谱字段检索、证据选择、抽取与回调 | 1D-1～1D-7 的开发分支代码与离线验收已关闭：Domain/受理/Ports、唯一 Schema v2、生产 I/O Adapter、Submit/Run/字段 Application、严格配置、通用本地 Dispatcher、真实 Callback Guard、同步恢复、资源恢复、生产组合根、公开 202 空体薄路由、Creation Intent、HTTP 租约及永久 AST/配置门禁均已完成。50 个 accepted 只形成持久行、一条 Worker 和零内存积压项；有效 production attestation 与生产容量仍待真实环境验收，代码尚未部署生产 |

## 维护规则

- 禁止新增按技术命名的通用业务模块，例如 `utils`、`common_service` 或 `generic_llm`。
- 只有语义稳定且至少被两个业务模块一致使用的能力，才能经过评审进入共享层。
- 新模块必须先写明数据所有权、公开应用入口、允许依赖和禁止依赖，再加入实现。
- 本目录不定义 HTTP、SSE、WebSocket 或 Callback 的外部字段；对外契约始终以 `docs/接口文档/` 为准。
