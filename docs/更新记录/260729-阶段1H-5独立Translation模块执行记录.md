# 阶段 1H-5：独立 Translation 模块执行记录

## 0. 执行结论

阶段 1H-5 已完成并通过离线门禁：

- 建立 `app/modules/translation/{domain,application,ports,adapters}`；
- `TranslationRequest` 只接受 prepared `ArtifactRef`、目标语言、翻译范围、冻结 Profile 和
  trace，不包含路径、`use_minerU`、OCR、MHTML 或格式后缀选择；
- 建立 `TranslatePreparedDocument`，只负责读取 prepared 文本、分段、语言转换和 Renderer；
- 将 HYMT/Argos/Ollama 的唯一运行时实现、Prompt、输出清洗、进度和 Chunk 规则迁入独立
  Translation 模块；
- 旧 `translator/core.py`、`utils.py`、`chunk_processor.py` 只保留兼容重新导出；
- 新增实例级 `HYMTTranslationEngineAdapter`，锁只包围单次线程不安全引擎调用；
- 新增安全 HTML Renderer，同时生成不可变双语/单语结果并统一 HTML 转义；
- 完成 199 项扩展离线回归，失败 0、错误 0、跳过 3；
- 未运行 `run.py`、未连接真实模型或后台服务，也未修改接口文档。

---

## 1. Translation 领域边界

新模块只拥有以下语义：

- `TranslationMode`：明确区分 machine 与 LLM；
- `TranslationFailurePolicy`：整文失败或单元占位；
- `TranslationProfile`：冻结引擎/Renderer 身份、指纹、模式、失败策略与纯规则；
- `TranslationRequest`：prepared Artifact、目标语言和非负 `item_limit`；
- `TranslationUnit`：有序源文本、译文、是否翻译和是否失败；
- `RenderedTranslation`：不可变双语/单语 HTML；
- `TranslationResult`：稳定 translation key、单元和结果计数。

Profile 支持严格 Schema 往返；未知字段、非法枚举、内容与 profile id 漂移均拒绝。当前
Application 只允许 Markdown/Text prepared Artifact，无法启动任何 Converter。

---

## 2. Application 与失败策略

`TranslatePreparedDocument` 的固定执行顺序为：

1. 校验冻结 Profile 与当前 Engine/Renderer 指纹；
2. 通过只读 Port 读取 prepared Artifact，并核对声明长度；
3. 严格 UTF-8 解码，按空行规则生成翻译单元；
4. 应用 `item_limit`，0 表示全部；
5. 保持旧 80% 中文字符跳过规则；
6. 调用语言引擎；
7. 按冻结策略整文失败，或写入不含原异常消息的失败类型占位；
8. Renderer 一次生成双语和单语不可变结果。

Artifact 读取、分段和 Renderer 均在引擎实例锁外。慢翻译只阻塞同一个不安全引擎实例的调用，
不再阻塞 MHTML、MinerU、OCR、LibreOffice 或其他文档准备任务。

---

## 3. HYMT 与 Renderer Adapter

HYMT/Argos/Ollama 的模型加载、Prompt、重试、源语言判断、ProgressTracker 和分块规则都保留在
Translation 边界。`HYMTTranslationEngineAdapter` 把领域 `machine/llm` 映射为旧运行时
`fast_translate` 参数，并使用实例级 `RLock` 保护单次引擎调用。

安全 HTML Renderer：

- 双语结果包含原文和译文；
- 单语结果只包含译文；
- 原文与译文全部经过 HTML 转义；
- 不读取路径、不处理图片、不解析 MIME、不运行翻译引擎；
- Renderer 失败使用独立稳定错误码，不与引擎失败混淆。

阶段 1H-5 选择“不可变渲染结果”方案；在 1H-6 的业务调用方兼容 Facade 中由既有 Presenter
继续投影为原返回字段。本阶段不新增公开字段。

---

## 4. 测试与验收

新增门禁覆盖：

- 请求字段精确集合，明确不存在五类格式转换开关；
- Profile 严格往返、未知字段和身份漂移；
- 英文翻译、中文跳过、范围限制与未翻译尾部保留；
- 空文本、非 UTF-8、引擎空结果/异常、整文失败和占位策略；
- Renderer 失败与 HTML 注入转义；
- 双语/单语结构；
- 第二任务可在第一任务持有引擎锁时完成 Artifact 读取和分段；
- 并发任务的不可变渲染结果不互相覆盖；
- Translation 模块零 Flask、services、MinerU、MHTML、OCR、LibreOffice 和
  DocumentProcessing Adapter 导入；
- 旧 core/utils/chunk 文件无类或函数实现，只保留兼容导出；
- 旧 Translation、Analysis、DocumentProcessing、Legacy Office、Report 与 Container 回归。

```text
Ran 199 tests
OK (skipped=3)
```

`compileall`、`git diff --check` 和接口文档只读 Hash 门禁通过。故障注入日志属于预期证据。

---

## 5. 阶段边界与下一步

旧 `DocumentTranslator`、Markdown/TXT/MHTML Handler 和 `LLMTranslationService` 的生产兼容
签名仍在使用旧调用链；为避免一次请求出现新旧双执行，本阶段没有提前切换它们。1H-6 将按
Translation → Analysis → Report/RAG 的调用方顺序，把每个入口接到
DocumentProcessing prepared Artifact，再调用新 Translation Application；每个调用方通过门禁后
才移除对应旧执行路径。

阶段复核未发现新的公开契约或业务语义待确认项，可以进入 1H-6。
