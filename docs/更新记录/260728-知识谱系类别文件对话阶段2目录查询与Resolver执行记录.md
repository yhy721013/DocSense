# 知识谱系类别文件对话阶段 2 目录查询与 Resolver 执行记录

## 1. 阶段范围

本阶段实现知识目录的精确 architecture 查询和有界候选 Resolver。没有修改 Chat SQLite、
Coordinator、公开路由或远端 AnythingLLM 调用。

## 2. 实现内容

- `DatabaseService.list_document_records_by_architecture_id`：
  - 严格接收规范正整数；
  - SQL 使用 `WHERE architecture_id = ?`，不读取全量目录后过滤；
  - 使用既有 `(architecture_id, file_name)` 复合唯一索引；
  - `ORDER BY file_name ASC, id ASC`；
  - 严格解码 metadata；
  - 空结果统一表示类别不存在或没有直接文件。
- `DatabaseChatDocumentResolver.resolve_by_architecture_id`：
  - 一次精确查询形成单一读取时点；
  - 不读取树、不展开子类别；
  - 复用既有 `_resolve_record` 文档引用转换；
  - architecture 路径额外拒绝空原名；
  - 重复 file name、document ref、规范化 external location 或部分损坏时整体 `invalid`；
  - 空集合返回 `not_found`；
  - SQLite 执行错误继续抛出，不伪装成业务 404。
- 将 architecture 解析拆成独立 `ChatArchitectureDocumentResolver` capability Protocol。
  直接扩大原 `@runtime_checkable ChatDocumentResolver` 曾导致 19 个既有 fileNames 测试替身
  失配；能力拆分后旧协议保持不变，新路径在阶段 5 单独校验新能力。

## 3. 测试与门禁

- Database、Resolver、架构定向：50 项通过，0 失败、0 错误；
- Chat 动态发现：228 项通过，0 失败、0 错误；
- `compileall` 与 `git diff --check` 通过。

覆盖 exact ID、相邻/子类别排除、稳定排序、空集合、坏 metadata、空原名、部分损坏、重复
远端身份、一次目录读取、禁止全量扫描和数据库执行故障透传。

全部测试使用临时 SQLite 和 Mock/Fake，未执行 `run.py`，未连接真实服务。

## 4. 门禁结论

阶段 2 验收通过，可以进入阶段 3。没有待商讨事项；当前 Resolver 候选尚未写入 Chat DB，
生产路由也不会触发 architecture 查询。

回滚点为精确查询、独立 capability、Resolver 分支及对应测试；知识库 Schema 和数据未变化。
