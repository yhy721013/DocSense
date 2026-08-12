# Shared Domain

本目录只保存多个业务模块必须共同遵守的稳定纯领域规则。代码不得读取环境、文件、数据库、
网络或系统时钟，也不得导入 Web 框架、供应商 SDK、Application、Port 或 Adapter。

当前 `knowledge_workspace.py` 是永久知识谱系 Workspace 名称的唯一所有者。Analysis 入库和
Reassign 目标准备必须复用该规则；AnythingLLM 通用集成层只接收最终名称，不拥有业务前缀。
