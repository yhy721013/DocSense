# 阶段 1H-7：关闭验收与 Artifact 保留执行记录

## 1. 目标与结论

本阶段完成共享文档处理与 Translation 解耦的永久架构门禁、50 任务隔离、遗留引用矩阵、
Artifact 生命周期语义和安全全仓关闭验收。

结论如下：

1. 当前生产组合根不再直接导入旧 `translator.mhtml2pdf` 或
   `translator.MinerUConverter`，Report/Analysis 只走共享 prepared Artifact 流水线。
2. Translation 只读取 prepared Markdown/Text Artifact 并调用语言引擎/Renderer，不再拥有
   MHTML、MinerU、OCR 或 LibreOffice 格式转换生命周期。
3. 旧 Handler/Service/Facade 仍有兼容代码和测试引用，尚未满足物理删除条件，本阶段保留。
4. 上游原始上传永不由本模块删除；有效 source/prepared Artifact 在 1H 保留，只清理明确归属的
   scratch、失败候选和 `.part`。自动 GC 留待具备引用、保留期、删除资格、fencing 和审计事实的
   后续阶段。
5. 未修改 `docs/接口文档/`，未增删任何前后端参数、响应字段、状态码或 Callback/Progress 合同。
6. MHTML 浏览器命令与 Profile 继续固定 `--no-sandbox` / `noSandbox=true`。

## 2. 代码与测试变更

### 2.1 永久架构门禁

`tests/test_document_processing_architecture.py` 新增：

- Translation 禁止导入 DocumentProcessing Adapter、旧服务和格式实现；
- 各业务 Application 禁止导入 DocumentProcessing Adapter/路径兼容解析器；
- 全部生产源码禁止直接导入旧 `mhtml2pdf`、`MinerUConverter` 路径；
- 冻结 `services/translator`、`services/utils` 到新格式 Adapter 的兼容文件集合，新增桥接必须先
  经过迁移评审。

全仓正向白名单同步增加了精确到模块/文件的 1H 依赖：

- MHTML Domain 只允许标准库 `email` 做不可变 MIME 字节解析；
- Translation Domain/Port 只允许共享 Artifact 值对象，不允许 Processor/Adapter；
- Legacy Office 路径/cleanup 例外只授予既有兼容 DTO 文件；
- Analysis/Translation Port 只共享 DocumentProcessing Domain，不获得路径或删除能力。

这些规则没有放开 Flask、SQLite、subprocess、网络客户端、供应商 SDK 或动态导入。

### 2.2 50 任务 Barrier 隔离

`tests/test_stage1h_closeout.py` 使用一个共享 `LocalDocumentPreparationAdapter`、一个本地
Artifact Store 和一个 SQLite Processing Record，让 50 个线程通过 `Barrier(50)` 同时开始：

- 50 个 TaskId、source Artifact ID、prepared Artifact ID 均唯一；
- 每个任务读回的 source/prepared 内容都只属于自己；
- 100 个有效 `.bin` Artifact 均保留且完整性复核成功；
- 没有 `.part` 残留；
- 测试不调用 MinerU/OCR/浏览器/LibreOffice 或网络服务。

## 3. 遗留引用与删除结论

| 引用类别 | 状态 | 结论 |
| --- | --- | --- |
| 当前生产组合根/公开路由 | 旧转换入口直接引用为 0 | 永久门禁锁定 |
| 遗留兼容源码 | `translation_service`、`DocumentTranslator`、旧 Handler 和旧 analysis/report service 仍存在 | 保留，禁止物理删除 |
| 测试 | 基线、Facade、翻译服务和隔离测试仍直接覆盖旧入口 | 保留到测试迁移和回滚观察期结束 |
| 配置 | 既有配置由 Container/Core Config 注入新 Adapter/Profile/Engine，不指向旧模块 | 配置名保持兼容 |
| 文档 | 历史记录继续出现旧名称 | 作为迁移证据保留 |

详细清单见
`docs/重构记录/阶段0资产/260729-阶段1H现状调用点与资源所有权矩阵.md` 第 9 节。

## 4. 验收结果

### 4.1 定向门禁

- 架构、1H-7 隔离及消费者切换：35 项通过；
- 50 个任务形成 100 个有效 Artifact，无路径/内容串扰和 `.part` 残留；
- `--no-sandbox` 既有命令/Profile 测试保持通过。

### 4.2 安全全仓

动态发现 `tests/test*.py` 共 2,128 项。按 `tests/README.md` 既有环境边界精确排除 13 项：
7 个可能调用本机 Shell/应用或测试文件服务器的脚本用例、1 个 Windows 无法可靠表达 POSIX
`0640` 的迁移断言、5 个依赖未提交本地请求夹具的测试。

| 发现 | 排除 | 执行 | 成功 | 失败 | 错误 | 跳过 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2,128 | 13 | 2,115 | 2,112 | 0 | 0 | 3 |

3 个跳过项分别是两个仅在 macOS 验证真实进程组回收的用例，以及当前 Windows 环境不允许创建
测试符号链接时的安全用例。输出中的 analysis worker `KeyboardInterrupt` 是既有测试主动注入
`BaseException` 验证 Dispatcher 隔离，不计为 unittest error。

另完成：

- `compileall`；
- `git diff --check`；
- 旧导入与敏感路径静态扫描；
- `docs/接口文档/` 工作区无改动。

## 5. 完成边界与后续输入

1H-0～1H-7 的代码与安全离线验收至此完成，但不等于 production ready。未完成项仍包括：

- 真实 MinerU/OCR/浏览器/LibreOffice Smoke；
- MySQL/MinIO 与 Artifact GC；
- Task Attempt、lease/fencing、可靠队列和跨实例全局容量；
- 真实 50+ 重任务吞吐、故障演练和生产 SLO；
- 旧 Handler/Service/Test 三类引用全部清零后的物理删除。

未来 Artifact GC 必须由数据库作为引用与删除资格真相源，禁止使用 MinIO TTL 或本地创建时间直接
删除仍排队、运行、恢复、待回调或结果未知的数据。
