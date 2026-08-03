# 核心配置与旧路径式 OCR/MinerU 兼容链完整删除执行记录

## 1. 改造目标

完整删除 `LLM_DOWNLOAD_DIR`、`OCR_CACHE_DIR`、`MINERU_CACHE_DIR` 及对应 Config 字段、
环境变量解析、旧路径式 OCR/MinerU 自由函数和 Report/Analysis fallback。当前共享
DocumentProcessing 保持唯一文件处理链。

本次不修改公开 HTTP、SSE、WebSocket、Callback 或前后端参数，不迁移或自动删除旧目录数据。

## 2. 分阶段执行结果

### 2.1 阶段 0：基线、契约与删除清单冻结

- 基线分支：`feat/weaponry-chat`；
- 基线提交：`9a6a15015d698c6f6174121b9b2638be47c0ebcb`；
- 阶段开始时工作区只有本次新增实施计划，没有其他用户未提交修改；
- 7 份 `docs/接口文档/` 文件的 SHA-256 已记录，Git diff 为空；
- 正式 `app/container.py` 中 Report 和 Analysis 均注入同一个共享 `document_preparer`，Analysis
  同时注入 `analysis_rag_projector`；
- 精确引用清单确认三个路径常量只进入旧 Config 字段，旧 OCR/MinerU 自由函数和 Report/Analysis
  的 `document_preparer is None` fallback；
- Core、Container、DocumentProcessing、Report、Analysis、Stage 1H 和 Stage 1G 定向基线执行
  103 项，失败 0、错误 0、跳过 0。

阶段 0 门禁通过。未发现公开接口依赖、正式链遗漏、仓库外资源要求或待商讨事项。

进入阶段 1 前发现原计划的源码拆分顺序会制造不可运行中间态：删除 OCR Config 路径字段后，尚未
删除的旧自由函数和 Analysis fallback 会立即失去必需属性。代码修改因此暂停，实施计划已补充
“原子源码切换、分层依次验收”规则；不增加临时兼容字段或空路径默认值。该修正不扩大范围、
不改变公开接口，修正后没有遗留待商讨事项。

### 2.2 阶段 1：删除 Core 路径与 Config 字段

- 从 `app/services/core/settings.py` 删除三个废弃目录常量及环境变量路径解析；
- 从 `OCRConfig` 删除 `cache_dir`、`mineru_cache_dir`，从 `LLMIntegrationConfig` 删除
  `download_dir`，并同步删除配置加载赋值和所有直接构造参数；
- 回归测试显式设置三个旧环境变量，证明其不再形成配置字段、也不会创建对应目录；
- 组合根结构测试改为验证进程级 Office 转换器只注入共享 `document_preparer` 和
  `ApplicationServices`，Report/Analysis 不再直接接收旧转换参数；
- 定向执行 Core、Container、Chat 与 Weaponry 相关测试 71 项，失败 0、错误 0、跳过 0；
- 静态复核确认生产 Core 中已不存在三个常量、三个旧环境变量和三个旧 Config 字段。

阶段 1 门禁通过。未修改公开接口，未发现待商讨事项。

### 2.3 阶段 2：删除 DocumentProcessing 旧自由函数

- 从 `builtin_ocr.py` 删除 `OCRRuntimeConfig`、旧缓存键、路径式上传准备、路径式
  OCR/MinerU 处理、缓存目录创建和原子缓存写入辅助函数；
- 保留当前 `BuiltinOCRDocumentProcessorAdapter`、处理 Profile、扫描 PDF 判定与渲染能力，
  这些能力的输出位置继续由调用方的任务级工作区或 Artifact Store 决定；
- 同步移除阶段 1H 当前消费者清单中已物理删除的 Report 上传桥接文件；
- DocumentProcessing、架构清单和 Report/Analysis 消费切换定向执行 38 项，失败 0、错误 0、
  跳过 0；日志中的 MinerU/OCR 异常栈均为既有故障注入用例的预期输出；
- 静态复核确认旧自由函数、旧运行配置、缓存键与缓存写入辅助函数已无生产定义或引用。

阶段 2 门禁通过。未修改公开接口，未发现待商讨事项。

### 2.4 阶段 3：删除 Report/Analysis fallback

- 物理删除 Report 的旧上传准备桥接文件，Report 文件 Adapter 只调用一次共享
  `document_preparer`，再把结果映射到既有 Report Artifact 生命周期类别；
- Analysis 文件 Adapter 删除旧 OCR Config loader、Normalizer、UploadPreparer、正文读取器、
  Legacy Office 直连和 `None` fallback；构造时强制显式接收 `document_preparer` 与
  `rag_projector`；
- 生产组合根继续创建一个共享文档处理器，并注入 Report、Analysis；Office 转换器只作为
  DocumentProcessing 内部依赖，不再由两个业务 Adapter 直接持有；
- 更新模块与类注释，明确“任务级输出目录”指调用方提供任务根、处理器写入受控 Artifact
  Store、业务 Adapter 只映射本任务产物，并非重新引入三个全局缓存目录；
- Report、Analysis、组合与消费切换定向执行 99 项，失败 0、错误 0、跳过 0；故障栈均为
  既有失败关闭/恢复测试的预期输出；
- 静态复核确认两个构造器不再接受可选文档处理依赖，生产代码不存在 `None` fallback 或旧上传
  桥接 import。

阶段 3 门禁通过。未修改公开接口，未发现待商讨事项。

### 2.5 阶段 4：测试资产与当前文档收口

- 重写旧 OCR 测试为永久结构门禁，验证旧自由函数、旧 Config 字段不能回归，并验证当前内置
  OCR Adapter 显式拥有物化根；
- 更新阶段 1G 逐方法迁移清单：保留 7 条历史 sourceTest 审计事实，将目标测试改指共享
  DocumentProcessing 的当前降级、任务隔离和 API 删除门禁；
- 更新阶段 1H 当前消费者黄金清单，移除已物理删除的 Report 上传桥接文件；
- 增加共享文档处理测试夹具，所有 Report/Analysis 离线测试均显式装配任务级 Artifact Store、
  处理记录库与 RAG 投影器；
- 根 README 删除三个旧目录的当前说明，只保留任务私有目录、共享 Artifact/物化目录和仍有效的
  `office_conversion/jobs`；两份上一阶段历史文档只追加后续替代说明，不改写当时审计事实；
- Stage 1G、DocumentProcessing 架构、删除门禁与 Runtime 设置联合执行 19 项，失败 0、错误 0、
  跳过 0；
- 7 份接口文档 SHA-256 与阶段 0 基线完全一致。

阶段 4 门禁通过。未修改公开接口，未发现待商讨事项。

### 2.6 阶段 5：扩大离线回归与关闭验收

- 安全全仓动态发现 2192 项，精确排除仓库登记的 13 项环境/资产/平台测试，实际执行 2179 项，
  失败 0、错误 0、跳过 3；三个跳过项由 Windows 平台或可选真实能力条件触发，未计作通过；
- 排除预检曾两次在执行前失败关闭：第一次为 PowerShell 多行 `python -c` 引号传递错误，第二次为
  测试 ID 是否带 `tests.` 前缀不匹配；两次均未执行测试、不计入验收结果。最终运行先确认
  discovered=2192、excluded=13、missing_exclusions=0 后才开始；
- `python -B -m compileall -q app tests scripts` 通过；
- `git diff --check` 通过，仅有 Git 对 LF/CRLF 的工作区提示；
- 静态检查确认生产代码不存在三个旧常量、三个旧 Config 字段、三个旧环境变量解析、旧路径式
  OCR/MinerU 自由函数、旧上传桥接 import 或 Report/Analysis `None` fallback；
- 7 份接口文档 SHA-256 与阶段 0 基线完全一致，`docs/接口文档/` Git diff 为空；
- 未运行 `run.py`，未访问 AnythingLLM、MinerU、OCR、LibreOffice、Callback 或模型服务，未删除
  磁盘上既有旧目录。

阶段 5 门禁通过。未发现需要修改接口文档、扩大内部 API 范围或另行商讨的事项，本次改造关闭。

## 3. 证据边界

离线 Windows、SQLite、Fake 和静态检查只用于证明当前源码接线、状态语义和本地资源所有权，不能
证明真实 MinerU/OCR/LibreOffice/AnythingLLM、浏览器/UI、生产容量、可靠队列、多实例、
Exactly-once 或跨数据库一致性。本次不运行 `run.py`，不访问后台服务。
