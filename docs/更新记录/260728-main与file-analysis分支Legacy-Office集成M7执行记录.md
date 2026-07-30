# `main` 与 `refactor/file-analysis` Legacy Office 集成 M7 执行记录

## 1. 阶段结论

M7“共享业务回归与存储治理”已完成，可以进入 M8。Weaponry 术语目录与普通文档上传没有受到
XLSX Folder 协议影响；新增 `scripts/inspect_xlsx_folder_inventory.py` 作为严格只读库存工具，
历史永久知识 Folder 只报告、不自动删除，未引用 Folder 也只进入人工所有权复核。

## 2. XLSX Folder 只读库存

1. `AnythingLLMDocumentClient.list_xlsx_folder_inventory()` 只调用根 `documents` 与各
   `documents/folder/<name>` GET 接口；普通文件和非 XLSX 目录不进入 Folder 分支；
2. 符合 XLSX 命名结构的根成员必须明确是 `folder`，大小写重复、畸形成员、成员位置冲突和空
   Folder 都按协议错误 fail-closed；
3. 库存 DTO 记录远端当前完整成员快照，但明确不代表删除所有权，也不签发 Cleanup Token；
4. CLI 使用 SQLite URI `mode=ro` 和 `PRAGMA query_only=ON` 读取 `documents.doc_path`，不初始化、
   迁移或修改数据库；
5. 输出不包含 Folder 名、Sheet 名、文件名、路径、Token、API Key 或 Base URL，只包含稳定 Folder
   Hash、成员计数、状态、建议动作和汇总指标；
6. 已提交且成员一致的 Folder 标为 `committed_protected`；已提交但发生成员漂移的 Folder 标为
   `committed_drifted_protected`，两者建议动作均固定为
   `report_only_no_automatic_delete`；
7. 无本地永久知识引用的 Folder 标为 `unreferenced_requires_ownership_review`，建议动作仅为
   `manual_ownership_review`，不会因为“未引用”自动推断排他所有权；
8. CLI 没有 `--apply`、`--cleanup`、`--delete` 或 `--token` 参数，远端变更标志固定为 false。

## 3. 自动清理边界

现有上传失败链仍是唯一自动清理入口：只有同一次可信上传响应的全部非重复成员属于同一个受控
Folder 时才能签发 opaque Cleanup Token；删除前必须重新读取 Folder 并证明成员集合与 Token
完全相同。成员漂移、端点缺失、网络结果未知或非确认成功都保留恢复事实，禁止删除。

永久知识替换继续只解除旧 Workspace 绑定，不删除旧 XLSX 全局 Folder Artifact。只读库存不会把
永久知识、历史 Folder 或“本地暂未引用”提升为删除授权，符合已确认的历史存储累积治理方案。

## 4. 门禁证据

首先执行 Documents 与库存脚本 41 项聚焦测试通过；随后执行 XLSX Inventory、AnythingLLM
Documents/RAG/Knowledge/Workspace、Weaponry Terms Catalog、Document Scope 和 Production
Adapter 共 191 项，全部通过，无 Failure、Error 或 Skip。新增普通格式门禁逐一证明 `.md`、`.pdf`
和 `.docx` 保持单文档上传，且不会调用 XLSX Folder 查询或删除端点。

CLI `--help` 离线运行通过，只出现 `--user-id` 和 `--knowledge-db-path`。改动模块语法编译和
`git diff --check` 通过，`docs/接口文档/` 没有修改。没有执行 `run.py`，没有连接真实
AnythingLLM、LibreOffice、模型、Callback 或生产数据库。

## 5. 阶段后商讨项检查

M7 没有增加公开 HTTP 接口，也没有改变单 Sheet、永久知识保留或清理授权规则。真实远端库存
只能在 M10 获得部署环境和凭据后执行，不影响本阶段只读实现与离线验收；可以进入 M8。
