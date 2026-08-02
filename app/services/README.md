# 服务层目录说明

`services/` 只保留尚未模块化的兼容服务与共享运行能力。Chat 业务已经迁入
`app/modules/chat/`；本目录不得重新承接 `/llm/chat*` 或知识谱系对话编排。

## 子目录与文件说明

| 路径 | 作用 |
| --- | --- |
| `__init__.py` | 服务层包标记。 |
| `core/` | 配置、数据库、提示词、运行时设置、日志和进度等共享基础服务。 |
| `utils/` | 通用工具与迁移期兼容门面；不得继续向旧 AnythingLLM 门面堆叠新文件对话工作流。 |
| `llm_service/` | 分析、报告、翻译、任务等既有 LLM 服务；文件对话实现已迁出。 |
| `translator/` | 文档翻译与格式处理子系统，与文件对话状态机无直接耦合。 |

## 维护约束

- Chat 应用服务不可直接依赖本目录、`utils/anythingllm_client.py` 或 AnythingLLM 原始请求字段。
- 当前 Chat 实现使用 SQLite 单实例适配器；未来共享持久化、可靠队列和多实例协调应通过 `app/modules/chat/` 内的协议与适配器演进。
- 不得把 HTTP 参数、SSE 格式或 Flask 对象带入领域模型和持久化层。
