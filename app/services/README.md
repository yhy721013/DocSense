# 服务层目录说明

`services/` 保存业务服务和共享运行能力。文件对话重构后，新的文件对话业务只能放在 `chat/` 子目录；旧的 `llm_service/` 保留其他 LLM 业务，不再承担 `/llm/chat*` 编排。

## 子目录与文件说明

| 路径 | 作用 |
| --- | --- |
| `__init__.py` | 服务层包标记。 |
| `chat/` | 文件对话的领域模型、应用用例、持久化、运行归属和清理逻辑。 |
| `core/` | 配置、数据库、提示词、运行时设置、日志和进度等共享基础服务。 |
| `utils/` | 通用工具与迁移期兼容门面；不得继续向旧 AnythingLLM 门面堆叠新文件对话工作流。 |
| `llm_service/` | 分析、报告、翻译、任务等既有 LLM 服务；文件对话实现已迁出。 |
| `translator/` | 文档翻译与格式处理子系统，与文件对话状态机无直接耦合。 |

## 文件对话分层流程

```text
路由层
  -> chat/application：受理、执行、历史、标题、中断、删除
  -> chat/domain：稳定实体、状态和事件
  -> chat/persistence：本地权威数据、租约、清理任务、事件账本
  -> chat/locking：运行归属与单会话互斥
  -> ports：供应商无关能力
  -> integrations：AnythingLLM 适配
```

## 维护约束

- 文件对话应用服务不可直接依赖 `utils/anythingllm_client.py` 或 AnythingLLM 原始请求字段。
- 当前实现使用 SQLite 单实例适配器；未来共享持久化、可靠队列和多实例协调应通过 `chat/` 内的协议与适配器演进。
- 不得把 HTTP 参数、SSE 格式或 Flask 对象带入领域模型和持久化层。
