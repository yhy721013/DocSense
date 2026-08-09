# 共享文档处理模块

`document_processing` 负责把任务下载后的源文件转换为可稳定引用、校验和追溯的
Markdown/Text Artifact。它拥有格式判断、MHTML、LibreOffice、MinerU、OCR、原子 Artifact
发布、Processing Record 和 lineage；不拥有 Report/Analysis/Weaponry 业务规则，也不执行翻译。

## 依赖方向

```text
业务 Adapter / Container
  -> document_processing Application
      -> Domain + Ports
      <- Processor / Store / Record / Capacity Adapters
```

- Domain/Application 不依赖 Flask、业务模块、Translation 或 `app.services`。
- 业务 Application 不直接导入本模块 Adapter；真实路径只在业务 Adapter 与本模块 Adapter
  边界内短暂存在。
- Translation 只能读取 prepared ArtifactRef，不能调用任何格式 Processor。

## Artifact 与资源所有权

- source Artifact 是任务下载文件在共享 Store 中的不可变副本，不等于上游系统保存的原始上传。
- normalized/prepared Artifact 是 LibreOffice、MHTML、MinerU、OCR 或文本直通形成的派生产物。
- Processing Record 与 lineage 保存稳定步骤、Profile、父子引用、摘要和处理终态。
- Artifact Catalog 登记 source 与派生产物，并允许同一步骤多个 ordinal；旧步骤结果表只保存
  当前单主产物兼容投影。
- Processor scratch、`.part`、浏览器目录和解压目录不是 Artifact；只有当前 Processor 能证明
  marker/路径所有权时才清理。
- 1H 阶段不即时删除有效 source/prepared Artifact。未来 GC 必须依赖数据库中的引用、保留期、
  删除资格、租约/fencing 和审计事实，不能按文件创建时间猜测删除。

## 当前实现边界

- 本地 `LocalArtifactStoreAdapter` 与 `SQLiteProcessingRecordAdapter` 只证明单实例离线正确性。
- 重型 Processor 通过进程内 FIFO 许可限流；这不是跨实例全局容量控制。
- FIFO 的当前实例等待者有硬上限；可靠业务积压仍必须保存在数据库/未来队列，而不是进程内。
- `running/outcome_unknown` 通过内部对账用例显式确认失败或恢复已验证 Artifact；禁止自动重提
  外部操作。陈旧 `running` 只能先隔离为 unknown。
- RAG-only Markdown 投影使用 v2 Profile：可渲染的 Base64 Markdown 图片会被完整移除，并用
  单个 ASCII 空格维持相邻 Token 边界；alt、媒体类型、摘要和 payload 长度不进入 RAG 正文，
  图片数量与移除字节数只写入脱敏统计日志。canonical prepared Artifact 保持不变。
- 本地 Store 的读取校验、文件句柄和删除使用同一进程内读租约，锁条目会回收；这仍不能替代
  多实例对象存储的条件写/租约。
- MHTML 浏览器 Profile 固定 `confirmed failure -> Markdown`、`unknown -> reconcile`，并按负责人
  要求持续使用 `--no-sandbox`。
- 旧 `app/services/translator` 包以及 MHTML/OCR/MinerU 文件处理 Facade 已在阶段 1G 删除；当前
  生产组合根只能从本模块取得转换能力。历史文档中的 `mhtml2pdf`、`MinerUConverter` 等路径仅作为
  迁移证据保留，不得重新创建或接回运行链。
- MySQL、MinIO、可靠任务队列、跨实例 lease/fencing、自动 Artifact GC 和真实生产容量仍属于
  后续阶段。
- Analysis/Report accepted 快照尚未冻结全部 OCR/MinerU/Translation 参数与依赖版本；阶段 2
  必须升级内部 Task Input Schema 并在 Worker 端校验，不能把步骤首次执行 Profile 当成完整
  跨重启快照。

公开 HTTP/Callback/Progress/SSE/WebSocket 合同不属于本模块；任何相关变化都必须先核对并确认
`docs/接口文档/`，且不得增删前后端接口参数。
