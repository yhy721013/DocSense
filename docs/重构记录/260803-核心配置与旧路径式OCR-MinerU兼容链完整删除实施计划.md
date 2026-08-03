# 核心配置与旧路径式 OCR/MinerU 兼容链完整删除实施计划

## 1. 文档信息

| 项目 | 内容 |
| --- | --- |
| 制订日期 | 2026-08-03 |
| 当前状态 | 已完成（2026-08-03） |
| 目标分支基线 | `feat/weaponry-chat` / `9a6a15015d698c6f6174121b9b2638be47c0ebcb` |
| 核心目标 | 完整删除三个旧目录常量、配置字段、环境变量解析和 Report/Analysis 旧路径式 OCR/MinerU 兼容分支 |
| 数据策略 | 当前处于开发阶段，不迁移、不读取、不兼容三个旧目录中的数据 |
| 公开接口影响 | 无；不修改路由、请求参数、响应字段、状态码、Header、Callback、SSE 或 WebSocket 语义 |
| 外部服务 | 实施与离线验收不启动 `run.py`，不访问 AnythingLLM、MinerU、OCR、LibreOffice、Callback 或模型服务 |

## 2. 对“显式接收任务级输出目录”的澄清

此前提出的“仍需保留的 OCR/MinerU 内部能力改为显式接收任务级输出目录”，原本是一个保守备选：
如果还需要保留 `prepare_file_for_upload(...)`、`prepare_analysis_file_for_upload(...)` 等旧路径函数，
就不能再让它们从进程级 Settings 读取共享缓存目录，而应由调用方显式提供当前任务拥有的输出目录。

负责人现已明确要求删除 Report/Analysis 中仅供旧路径式 OCR/MinerU 使用的兼容分支，因此本计划
**不采用该备选方案**，也不会把旧函数改个参数后继续保留。实施后的唯一正式能力是当前共享
DocumentProcessing 链：

```text
Container
  -> LocalDocumentPreparationAdapter
       -> MinerUDocumentProcessorAdapter(materialization_root=.../mineru)
       -> BuiltinOCRDocumentProcessorAdapter(materialization_root=.../ocr)
       -> Artifact Store + Processing Record
```

这里的 `materialization_root` 由组合根显式注入，Processor 再按不可变 `step_key` 创建独占工作目录；
它不是旧的进程级共享缓存，也不通过 `OCRConfig` 携带宿主路径。输出先形成受管理 Artifact，临时物化
目录由资源所有者按既有规则清理。这个现行机制予以保留。

## 3. 当前代码事实与删除判断

### 3.1 三个路径变量

| 变量 | 当前写入位置 | 当前读取情况 | 结论 |
| --- | --- | --- | --- |
| `LLM_DOWNLOAD_DIR` | `settings.py` -> `LLMIntegrationConfig.download_dir` | 正式代码没有读取 `download_dir` | 常量、字段和赋值全部删除 |
| `OCR_CACHE_DIR` | `settings.py` -> `OCRConfig.cache_dir` | 仅旧路径式 OCR 自由函数及旧 Adapter 分支读取 | 随旧分支完整删除 |
| `MINERU_CACHE_DIR` | `settings.py` -> `OCRConfig.mineru_cache_dir` | 仅旧路径式 MinerU 自由函数及旧 Adapter 分支读取 | 随旧分支完整删除 |

对应的 `FILE_DOWNLOAD_DIR`、`DOCSENSE_OCR_CACHE_DIR`、
`DOCSENSE_MINERU_CACHE_DIR` 也不再由当前代码解析。`FILE_DOWNLOAD_TIMEOUT` 仍控制真实下载超时，
不是目录配置，必须保留。

### 3.2 正式业务链

当前 `app/container.py` 已经为 Report 和 Analysis 注入同一个 `document_preparer`：

- Report 的 `normalize_source(...)` 使用共享 DocumentProcessing，`prepare_upload_files(...)` 只把
  已准备 Artifact 映射为 Report RAG Input；
- Analysis 的 `prepare(...)` 使用共享 DocumentProcessing，并通过独立 RAG 投影形成最终上传 Artifact；
- OCR/MinerU 的 Profile、Artifact、Processing Record、容量许可和物化目录均由
  DocumentProcessing 拥有；
- 只有在手工构造 Adapter 且不注入 `document_preparer` 时，现有代码才回退到旧 normalizer、
  OCR/MinerU 路径函数和目录字段。

因此，本次删除不会改变当前正式组合根的成功、降级或失败语义；它会有意删除仓库内直接构造旧
Adapter、调用旧自由函数或读取旧 Config 字段的内部 Python 兼容能力。当前处于开发阶段，不为这些
内部调用方提供迁移层或弃用期。

## 4. 改造目标

### 4.1 功能目标

1. Settings、Config、生产代码和现行测试中不再定义或读取三个旧路径常量。
2. 三个旧环境变量不再被解析，也不再影响任何应用行为。
3. Report/Analysis 不存在“共享 DocumentProcessing 未注入时回退旧路径处理”的第二执行链。
4. OCR、MinerU、扫描 PDF 检测与失败降级继续由现行 DocumentProcessing Processor/Pipeline 实现。
5. Report/Analysis 的公开 HTTP、任务、Progress、Callback 和 RAG 结果合同保持不变。

### 4.2 架构目标

1. 文件转换只有一个权威执行链，避免同一种格式因构造方式不同而使用不同目录、缓存和降级规则。
2. Config 只承载算法参数和真实基础设施参数，不再传递宿主文件系统缓存路径。
3. Report/Analysis Adapter 对 DocumentProcessing 依赖改为必需依赖，缺失时在装配阶段失败关闭，
   禁止运行中静默切换旧链。
4. 临时目录所有权继续绑定 `step_key` 和 Artifact 生命周期，为未来多线程隔离、可靠任务队列和
   多实例共享存储替换保留清晰边界。
5. 不新增进程级共享缓存、全局可变对象、后台线程或第二套资源清理逻辑。

## 5. 明确不在本次范围内的事项

1. 不修改 `docs/接口文档/`，不增删任何前后端接口参数。
2. 不修改 OCR 语言、DPI、扫描检测阈值、MinerU 语言/API 等仍有效算法配置。
3. 不删除 `BuiltinOCRDocumentProcessorAdapter`、`MinerUDocumentProcessorAdapter`、
   `LocalDocumentPreparationAdapter` 或其 Profile/Artifact/Record 能力。
4. 不改变 Legacy Office 由共享 DocumentProcessing 处理的现行能力，也不删除
   `office_conversion/jobs`。
5. 不顺带删除独立 MHTML `path_compat.py` 测试资产；本次只确保 Report/Analysis 正式和兼容分支
   不再导入它。若需物理删除该独立资产，应另行核查其基线/迁移清单。
6. 不自动删除磁盘上已经存在的 `llm_downloads`、`ocr_markdown`、`mineru_markdown`；停服后的物理
   清理由维护操作按精确路径完成。
7. 不修改数据库 Schema，不编写旧数据迁移或双读逻辑。

## 6. 预计修改文件范围

### 6.1 Core Settings 与 Config

#### `app/services/core/settings.py`

- 删除 `LLM_DOWNLOAD_DIR`、`OCR_CACHE_DIR`、`MINERU_CACHE_DIR`；
- 删除对应 `_resolve_component_path(...)` 调用和已不再成立的兼容注释；
- 保留 Runtime 根、数据库路径、`SQLITE_EXPORT_DIR` 等仍有所有者的路径规则。

#### `app/services/core/config.py`

- 删除三个 Settings 常量导入；
- 从 `LLMIntegrationConfig` 删除 `download_dir`；
- 从 `OCRConfig` 删除 `cache_dir`、`mineru_cache_dir`；
- 从 `load_llm_integration_config()`、`load_ocr_config()` 删除对应赋值；
- 保留 `download_timeout`、OCR 算法参数、`analysis_scanned_pdf_engine`、MinerU 参数和
  `tessdata_prefix`。

这些都是内部 Python 配置类型调整，不映射到公开 HTTP 字段。

### 6.2 DocumentProcessing 旧路径函数

#### `app/modules/document_processing/adapters/builtin_ocr.py`

删除只服务旧路径缓存模型的内容：

- `OCRRuntimeConfig` 旧协议；
- `build_ocr_cache_key(...)`、`build_mineru_cache_key(...)`；
- `prepare_file_for_upload(...)`、`prepare_analysis_file_for_upload(...)`；
- `_prepare_scanned_pdf_with_builtin_ocr(...)`；
- `ocr_pdf_to_markdown(...)`、`mineru_pdf_to_markdown(...)`；
- `_resolve_cache_root(...)`、`_safe_cache_file(...)`、旧原子缓存写入辅助函数；
- 上述对象的 `__all__` 导出和仅为它们存在的 import。

保留并继续测试：

- `is_scanned_pdf(...)`；
- `build_builtin_ocr_profile(...)`；
- `_render_ocr_markdown(...)`；
- `BuiltinOCRDocumentProcessorAdapter` 及其受控物化、清理和错误映射。

MinerU 的现行能力继续由 `app/modules/document_processing/adapters/mineru.py` 提供，本次不恢复或复制
旧 `MinerUConverter(output_dir=cache_root)` 路径。

### 6.3 Report 单链收口

#### 删除 `app/modules/report/adapters/upload_files.py`

该文件只负责从全局 `load_ocr_config()` 取得旧缓存路径并调用旧 OCR 自由函数。删除文件及全部引用，
不保留转发 Facade。

#### `app/modules/report/adapters/legacy_files.py`

- 删除 `prepare_report_upload_files`、旧 normalizer、旧 OCR upload preparer 和 Legacy Office 直连导入；
- 删除 `normalizer`、`upload_preparer`、`legacy_office_preparer` 构造参数、类型别名、校验和实例字段；
- 将 `document_preparer` 改为必需依赖；
- `normalize_source(...)` 无条件进入共享 DocumentProcessing；
- `prepare_upload_files(...)` 只映射已经准备好的 Artifact，不再存在第二次 OCR 或路径序列分支；
- 删除只属于旧分支的 `_convert_legacy_source(...)` 等私有方法；
- 保留下载、Report Artifact 发布、模板下载与 Word 模板正文提取能力。

类名 `LegacyReportFileAdapter` 暂不在本次重命名，因为它仍承担 Report DTO/Artifact 适配职责；这里的
“Legacy” 不再表示存在旧文件处理链。重命名可在后续模块命名清理中独立执行。

### 6.4 Analysis 单链收口

#### `app/modules/analysis/adapters/legacy_files.py`

- 删除 `OCRConfig`/`load_ocr_config`、`dataclasses.replace`、旧 path compatibility 和旧 OCR 函数导入；
- 删除 `ocr_config_loader`、`normalizer`、`upload_preparer`、`text_reader`、
  `legacy_office_preparer` 构造参数及字段；
- 将 `document_preparer` 和 `rag_projector` 固化为必需依赖；
- `prepare(...)` 下载成功后无条件进入 `_prepare_shared_artifact(...)`；
- 删除 `_prepare_processing_path(...)`、`_convert_legacy_into_task(...)`、
  `_normalize_into_task(...)`、`_prepare_upload_path(...)`、旧正文读取等不可达方法；
- 保留任务私有下载目录、路径包含校验、Artifact 映射、RAG 投影和业务错误分类。

这意味着手工构造 Analysis Adapter 时若缺少共享 DocumentProcessing/RAG Projection，将明确拒绝，
而不是退回旧路径链。

### 6.5 组合根

#### `app/container.py`

- 继续唯一构造共享 `document_preparer`、Artifact Store、容量许可、Processing Record 和 RAG 投影；
- 调整 Report/Analysis Adapter 构造参数，删除重复传入的旧 `legacy_office_preparer` 等参数；
- 保持 `ocr_config` 中有效算法参数到 DocumentProcessing 的注入；
- 保持 `llm_config.task_db_path`、下载超时和 Callback 配置的既有使用。

### 6.6 测试和结构资产

预计更新以下测试或资产，最终以阶段 0 的精确引用清单为准：

- `tests/test_runtime_settings.py`：不再导入三个常量；显式设置三个旧环境变量后证明其不产生目录或
  配置字段；
- `tests/test_dependency_container.py`、`tests/test_chat.py`、`tests/offline_application.py`、
  `tests/test_weaponry_stage1d6.py`：更新 `LLMIntegrationConfig` 构造；
- `tests/test_analysis_production_adapters.py`、`tests/test_analysis_rag_upload_pipeline.py`：删除旧注入点
  测试，补充共享 DocumentProcessing 必需依赖、Artifact/投影和任务隔离断言；
- `tests/test_report_io_adapters.py`、`tests/test_report_runtime_adapters.py`：删除 normalizer/upload
  preparer/直连 Office 分支测试，改为共享 DocumentProcessing 与 Report Artifact 映射测试；
- `tests/test_stage1h_consumer_cutover.py`：把“注入会失败的旧函数以证明未调用”升级为“构造签名和源码
  已不存在旧注入点”的永久门禁；
- `tests/test_ocr_preprocessor.py`：不再执行已删除自由函数；将仍有价值的扫描检测、内置 OCR Profile、
  Processor 输出/清理语义迁移到现行 DocumentProcessing 测试；
- `tests/test_document_processing_formats.py`、`tests/test_document_processing_architecture.py`：补齐唯一
  Processor 链、物化目录所有权和禁止旧入口回流的测试；
- `tests/contracts/stage1g_legacy_test_migration.json`：只更新活动目标测试映射，使历史来源断言指向
  新的现行 Processor 测试；不改写历史来源快照或伪造旧测试事实；
- `tests/README.md`：若迁移资产说明或推荐命令变化，同步当前测试说明。

测试中不保留旧字段或旧函数的假实现；否则静态引用虽然退出生产代码，测试仍会冻结已经删除的
内部合同。

### 6.7 文档

- 新增对应 `docs/更新记录/` 执行记录，逐阶段写入真实修改、统计和证据边界；
- 在已完成的
  `docs/重构记录/260803-废弃运行时空目录停止预创建实施计划.md` 与对应执行记录中仅追加“后续已由
  本计划完整删除兼容路径”的指向，不回写或篡改当时“保留纯路径解析”的历史决策；
- 当前 README 和 `.env.example` 已无三个旧目录说明，实施时再次复核，无事实变化则不制造无意义 diff；
- `docs/接口文档/` 保持零差异。

`docs/更新记录/260701-日志升级与Runtime绝对路径迁移.md` 等历史记录继续保留当时存在过的环境变量，
不以全仓字符串清零为理由修改历史证据。

## 7. 分阶段实施与验收门禁

### 7.0 原子源码切换说明

阶段 0 实际冻结引用后确认，`OCRConfig.cache_dir`/`mineru_cache_dir`、旧 OCR/MinerU 自由函数与
Analysis fallback 构成同一个编译和运行依赖环：先单独删除任一层，都会让其余层在阶段间短暂处于
不可调用状态。为满足“每个阶段结束时仓库必须可验证”的门禁，阶段 1 的源码补丁必须原子包含：

1. Core 路径常量和 Config 字段删除；
2. DocumentProcessing 旧自由函数删除；
3. Report/Analysis fallback 删除及 Container 构造调整；
4. 直接受构造签名影响的测试夹具同步更新。

这不合并验收责任：阶段 1 只关闭 Core 门禁，阶段 2 再关闭 DocumentProcessing 门禁，阶段 3 再关闭
Report/Analysis 业务门禁。任一门禁失败都停止后续阶段。禁止为了维持阶段间兼容而新增临时目录字段、
空路径默认值、过渡 Facade 或短期 fallback，因为这些对象本身正是本次要删除的风险来源。

### 阶段 0：基线、契约与删除清单冻结

#### 实施内容

1. 记录分支、HEAD、工作区状态和用户已有修改；
2. 记录 `docs/接口文档/` Hash/Git diff 基线；
3. 分类列出三个常量、三个环境变量、三个 Config 字段、旧自由函数、Adapter 构造注入点和测试引用；
4. 再次确认 Container 的 Report/Analysis 均注入共享 DocumentProcessing；
5. 执行改造前 Core Config、Container、Report、Analysis、DocumentProcessing 定向测试，记录基线。

#### 门禁

- 没有公开接口或 Callback 字段依赖这些内部路径；
- 正式组合根不存在未注入 `document_preparer` 的 Report/Analysis 构造；
- 当前 OCR/MinerU Processor 已覆盖正式格式、降级和资源清理语义；
- 如发现正式路径仍执行旧自由函数，立即停止并与负责人商讨。

### 阶段 1：删除 Core 路径与 Config 字段

#### 实施内容

1. 修改 Settings 和 Config；
2. 按 7.0 执行完整原子源码切换，保证阶段结束时没有悬空消费者；
3. 更新所有 `LLMIntegrationConfig`、`OCRConfig` 内部构造；
4. 加入旧环境变量无效、旧字段不存在的回归断言。

#### 门禁

- `app/` 与现行测试中三个常量定义/导入为 0；
- 三个旧环境变量的生产解析为 0；
- `LLMIntegrationConfig.download_dir`、`OCRConfig.cache_dir`、
  `OCRConfig.mineru_cache_dir` 为 0；
- Core Config、Runtime Settings、Container 测试全部通过；
- 旧环境变量即使设置为不存在路径，也不会创建目录或改变配置对象。

### 阶段 2：删除 DocumentProcessing 旧自由函数

#### 实施内容

1. 复核阶段 1 原子补丁已从 `builtin_ocr.py` 删除旧缓存协议、自由函数和辅助函数；
2. 复核 imports、exports 与测试 patch 引用清零；
3. 将仍有业务价值的断言迁移到当前 Processor/Profile 测试并关闭专项门禁。

#### 门禁

- 旧函数名和旧 cache-key 函数在 `app/` 与现行测试中引用为 0；
- `BuiltinOCRDocumentProcessorAdapter`、MinerU Processor 和 Local Pipeline 测试通过；
- OCR/MinerU 的输出只进入受控 Artifact/物化根；
- 失败降级、空结果、路径逃逸、清理所有权和容量限制语义不退化。

### 阶段 3：删除 Report/Analysis 兼容分支

#### 实施内容

1. 复核阶段 1 原子补丁已删除 Report `upload_files.py`；
2. 复核 Report Adapter 只保留共享 DocumentProcessing 单链；
3. 复核 Analysis Adapter 只保留共享 DocumentProcessing + RAG Projection 单链；
4. 完成 Container、Report、Analysis 专项业务回归并关闭门禁。

#### 门禁

- Report/Analysis 源码不再出现旧 OCR/MinerU、旧 normalizer、旧路径缓存 Config 注入；
- Adapter 构造缺失必需 DocumentProcessing 能力时明确失败，不能静默降级；
- Report 每个源文件只执行一次共享准备，再映射 RAG Input；
- Analysis 的 canonical Artifact、正文 Artifact 和 RAG 投影关系保持既有语义；
- Legacy Office、MHTML、扫描 PDF 和普通文件均通过共享流水线回归；
- 任务目录、Artifact 所有权和错误分类没有跨任务共享或路径逃逸。

### 阶段 4：测试资产与当前文档收口

#### 实施内容

1. 删除或迁移只验证旧分支的测试；
2. 更新 Stage 1G 活动迁移目标映射和测试说明；
3. 新增执行记录，并给上一份“停止预创建”记录追加后续替代指向；
4. 对当前说明与历史记录分区执行静态审计。

#### 门禁

- 测试不再 import、patch、构造或断言已删除内部 API；
- Stage 1G 迁移清单的每个目标测试真实存在，历史来源快照和总数保持可审计；
- 当前文档不再把旧兼容解析描述为现行能力；
- 历史文档中的旧事实未被删除；
- `docs/接口文档/` diff 为空、Hash 与阶段 0 一致。

### 阶段 5：扩大回归与关闭验收

#### 实施内容

1. 执行全部定向测试；
2. 按仓库既有精确规则排除 13 个依赖真实本地服务、真实 LLM 资产或 POSIX 平台的用例，动态发现并
   执行其余全仓安全测试；
3. 执行 `compileall`、`git diff --check`、AST/字符串门禁、接口文档零差异和变更范围审计；
4. 写入真实发现数、排除数、执行数、失败数、错误数和跳过数；
5. 将本计划状态更新为“已完成”。

#### 最终门禁

- 生产代码和现行测试中旧常量、旧字段、旧环境变量解析、旧自由函数、旧 Adapter 注入点全部为 0；
- 定向测试和安全全仓测试失败 0、错误 0；
- 所有跳过项逐项说明，故障注入日志不误报为测试失败；
- 不存在 `upload_files.py` 或等价转发 Facade；
- Report/Analysis 只能通过共享 DocumentProcessing 形成 OCR/MinerU 结果；
- 接口契约没有变化，现有数据没有被迁移或自动删除；
- 工作区只有本计划授权范围内的修改。

每个阶段结束后都必须检查是否出现接口影响、正式链遗漏、测试语义缺口或新的资源所有权疑问。
存在任一未决事项时立即停止，不得跨阶段带疑问实施。

## 8. 测试计划

### 8.1 Core 与静态门禁

至少执行：

```text
python -m unittest tests.test_runtime_settings
python -m unittest tests.test_dependency_container
python -m unittest tests.test_stage1g_legacy_test_migration
python -m unittest tests.test_document_processing_architecture
```

静态检查分为两个口径：

1. `app/` 与现行测试要求旧符号、字段和环境变量解析全部为 0；
2. 历史文档允许保留旧名称，但当前计划/执行记录必须明确其已退出实现。

### 8.2 DocumentProcessing

至少覆盖：

- 文本型 PDF 不触发 OCR；
- 扫描 PDF 按冻结策略使用 MinerU 或内置 OCR；
- MinerU 明确失败后按当前 Pipeline 规则进入内置 OCR；
- 两级处理均明确失败时按当前合同保留原 PDF，而不是制造空 Markdown；
- Processor 输出进入 Artifact Store，临时物化目录按所有权安全清理；
- 同一/不同 `step_key` 的冲突、恢复和容量许可语义；
- OCR Profile、MinerU Profile 与 Processing Record 不依赖旧 Config 路径字段。

建议执行：

```text
python -m unittest tests.test_document_processing_formats
python -m unittest tests.test_stage1h_consumer_cutover
python -m unittest tests.test_document_processing_baseline
python -m unittest tests.test_document_processing_architecture
```

### 8.3 Report

至少覆盖：

- 下载仍进入任务 Artifact staging；
- `normalize_source(...)` 恰好调用一次共享 DocumentProcessing；
- `prepare_upload_files(...)` 只映射已有 prepared Artifact；
- 普通文件、扫描 PDF、MHTML、Legacy Office 成功与失败语义；
- 模板下载和 Word 文本提取不受旧 OCR 分支删除影响；
- 构造时缺少共享 DocumentProcessing 明确拒绝。

建议执行：

```text
python -m unittest tests.test_report_io_adapters
python -m unittest tests.test_report_runtime_adapters
python -m unittest tests.test_report_application
python -m unittest tests.test_stage1h_consumer_cutover
```

### 8.4 Analysis

至少覆盖：

- 下载目录保持 execution 私有；
- 所有输入无条件进入共享 DocumentProcessing；
- Markdown/Text canonical Artifact 经过 RAG-only 投影，正文读取仍来自 canonical Artifact；
- PDF 明确降级、Legacy Office、MHTML、跨任务隔离和路径包含校验；
- 缺少 DocumentProcessing 或 RAG Projector 时失败关闭；
- `analysis_scanned_pdf_engine` 仍正确映射到现行 `ScannedPDFEngine`。

建议执行：

```text
python -m unittest tests.test_analysis_production_adapters
python -m unittest tests.test_analysis_rag_upload_pipeline
python -m unittest tests.test_analysis_application
python -m unittest tests.test_stage1h_consumer_cutover
```

### 8.5 扩大离线回归

- 使用 `venv/Scripts/python.exe -B`；
- 先动态发现测试，再用仓库当前登记的精确测试 ID 排除 13 项；
- 输出 discovered/excluded/executed/failures/errors/skipped；
- 执行 `python -B -m compileall -q app tests scripts`；
- 不启动 `run.py`，不把 SQLite/Fake/Windows 离线结果表述为真实供应商、容量、多实例、可靠队列或
  Exactly-once 验证。

## 9. 验收标准与达成效果

完成后应达到：

1. `LLM_DOWNLOAD_DIR`、`OCR_CACHE_DIR`、`MINERU_CACHE_DIR` 不存在；
2. `LLMIntegrationConfig.download_dir`、`OCRConfig.cache_dir`、
   `OCRConfig.mineru_cache_dir` 不存在；
3. 三个旧环境变量不再被解析，即使部署环境残留也不会产生作用；
4. `llm_downloads`、`ocr_markdown`、`mineru_markdown` 不会被当前代码计算、创建、读取或写入；
5. Report/Analysis 不再具有旧路径 OCR/MinerU、旧 normalizer 或直连 Legacy Office 的备用执行链；
6. OCR/MinerU 只通过共享 DocumentProcessing Profile、Processor、Artifact、Record 和容量边界运行；
7. 未正确装配共享处理能力时应用失败关闭，不会在运行中静默切换行为；
8. 公开接口、任务结果、Progress 和 Callback 合同保持不变；
9. 目录所有权更清晰，有利于未来任务隔离、可靠队列 Worker、多实例共享 Artifact Store 和独立资源恢复；
10. 既有旧目录留给停服维护人工删除，本次代码不执行破坏性清理。

## 10. 风险、控制与回滚

### 10.1 主要风险

1. 某个未由正式 Container 构造的仓库内测试/脚本仍直接实例化旧 Config 或 Adapter；
2. 删除整条 fallback 后，某项 MHTML/Legacy Office/扫描 PDF 语义尚未被当前 Pipeline 测试承接；
3. Stage 1G 迁移清单仍指向被删除的测试方法；
4. 为让旧测试通过而重新增加可选依赖或隐式 fallback，造成“表面删除、实际保留”；
5. 把历史文档字符串计入零引用门禁，误删真实审计记录。

### 10.2 控制措施

- 阶段 0 对生产、测试、脚本、当前文档和历史文档分类检索；
- 先补齐当前 Processor/Artifact 语义测试，再删除旧测试和源码；
- Adapter 的共享处理依赖由可选改为必需，禁止 `None` 分支；
- 用 AST/签名测试禁止旧构造参数、旧 import 和转发 Facade 回流；
- 当前代码零引用与历史记录保留使用不同检查口径；
- 每阶段独立验收，发现语义缺口立即停止。

### 10.3 回滚

本次不涉及数据迁移或 Schema 变更。若尚未发布时发现正式语义缺口，应回滚本次代码改动并恢复到
已知提交，而不是临时恢复部分旧目录字段形成混合链。若发布后发现问题，停止新任务受理并回退完整
版本；不得盲目重放外部副作用或自动删除未知所有权的 Artifact。

## 11. 实施结论

本计划把上一阶段的“停止预创建但保留纯路径兼容”推进为物理删除：不保留旧常量、旧 Config 字段、
旧环境变量解析、旧 OCR/MinerU 自由函数、Report 桥接文件或 Analysis/Report fallback。当前共享
DocumentProcessing 是唯一文件处理链。

该范围不要求修改接口文档，也不涉及前后端接口参数。实施已严格按阶段 0 至阶段 5 执行，每阶段均在
门禁通过并确认无待商讨事项后进入下一阶段。最终安全全仓动态发现 2192 项、精确排除 13 项、执行
2179 项，失败 0、错误 0、跳过 3；`compileall`、差异格式、静态引用和接口文档哈希门禁均通过。
完整证据与能力边界见对应执行记录。
