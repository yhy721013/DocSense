# 服务层目录说明

`services/` 只保留共享运行能力、少量无状态工具和阶段 2/3 尚待迁移的 SQLite 事实服务。
Analysis、Report、Weaponry、Reassign、Chat、DocumentProcessing 与 Translation 的业务执行均已
迁入 `app/modules/`；本目录不得重新成为跨业务编排层。

## 子目录与文件说明

| 路径 | 作用 |
| --- | --- |
| `__init__.py` | 服务层包标记。 |
| `core/` | 配置、数据库、提示词、运行时设置、日志和进度等共享基础服务。 |
| `utils/` | Callback、下载和 DOCX 文本提取等无业务状态工具；旧供应商与文件处理 Facade 已删除。 |
| `llm_service/` | 共享任务/执行/回调审计、知识索引操作和 RAG 资源租约的过渡 SQLite 事实服务。 |

## 维护约束

- 业务 Application 不得直接依赖本目录的具体实现；必须通过所属模块 Port/Adapter 接入共享事实或工具。
- 已删除的 `services/chat`、`services/translator` 和 `utils/anythingllm_client.py` 不得重新创建；Chat 与
  Translation 后续演进分别留在 `app/modules/chat` 和 `app/modules/translation`。
- 当前共享事实与 Chat 持久化仍是 SQLite 单实例边界；可靠队列、共享数据库和多实例协调必须通过
  模块端口演进，不能把业务 DTO 和外部 I/O 塞回 `services/`。
- 不得把 HTTP 参数、SSE 格式或 Flask 对象带入领域模型和持久化层。
