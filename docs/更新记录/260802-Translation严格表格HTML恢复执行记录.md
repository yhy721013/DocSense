# Translation 严格表格 HTML 恢复执行记录

## 1. 背景与根因

Analysis 全文翻译读取 Markdown Artifact 后，由安全 HTML Renderer 生成
`documentTranslationOne`、`documentTranslationTwo`。MinerU 对 PPTX、XLSX、DOCX 以及正常
MHTML → PDF → MinerU 路径中的表格，会在 Markdown 中保留 `<table>`、`<tr>`、`<th>`、
`<td>` 等原始 HTML。旧 Renderer 为阻断 XSS，在 Markdown 解析前统一转义全部原始 HTML，
因此表格标签最终作为大量可见文本输出，浏览器无法形成表格 DOM。

Markdown 管道原生生成的 `| ... |` 表格不受此问题影响；MHTML 浏览器转换明确失败后使用的
纯文本降级路径会在文档处理阶段丢失表格结构，也不属于 Renderer 能够恢复的范围。

## 2. 已实施修复

1. 保留“所有原始 HTML 先转义”的既有默认安全边界，不开放通用 HTML。
2. 在 Markdown 生成 DOM 后，仅从不含其他真实 DOM 节点的文本段落中识别表格候选；代码块、
   Markdown 链接和已生成的其他结构不会被重新解释。
3. 新增独立的严格表格校验组件，候选必须同时满足：
   - 标签属于表格结构与窄单元格内容白名单；`script`、`style`、`iframe`、SVG、注释、声明等
     任一未知或危险节点都会使整个候选保持转义；
   - 父子关系、嵌套关系、开始/结束标签顺序精确匹配，不使用 BeautifulSoup 的宽松补写结果
     替代输入合法性；
   - 隔离 Parser 和 BeautifulSoup 的表格、单元格、节点计数完全一致；每一行直接拥有单元格，
     空壳或被自动规范化的结构不予恢复；
   - 单文档最多恢复 256 张表格、50,000 个表格节点、20,000 个单元格，表格最多嵌套 4 层，
     单个候选最多 8 MiB，`rowspan`/`colspan`/列跨度为 `1..1000`；超限时失败关闭并输出不含
     原文内容的任务级告警日志。
4. 校验通过后清空并按白名单重建属性：仅保留合法跨度、`th.scope`、安全链接、有限长度的
   标题/替代文本和受限图片 URL；事件属性、CSS、`id`、`class`、`border`、未知属性全部删除，
   不安全链接被去除，非位图 Data URL 图片节点被删除。
5. 表格在 TranslationUnit 提取之前恢复，翻译引擎只收到标题、表头和单元格的纯文本；翻译
   结果继续经过 `html.escape` 后填回占位符，Engine 不接触标签，也不能用译文注入 HTML。
6. Renderer 指纹从 `docsense-translation-html-v2` 升级为
   `docsense-translation-html-v3`，使任务接受时冻结的渲染语义能够识别新旧版本。

## 3. 分层与并发边界

- `html_renderer.py` 负责 Markdown/Text 路由、TranslationUnit 占位与最终单语/双语 HTML；
  `safe_table_html.py` 只负责表格候选校验、资源预算和安全 DOM 恢复，不依赖 Analysis、MinerU
  或具体翻译引擎。
- 校验 Parser、BeautifulSoup 和资源预算均为单次模板构建的局部对象，无全局可变缓存或大锁；
  同一 Renderer 实例可被多个任务并发复用。
- 资源上限是进程内输入防护，不是可靠队列背压、多实例全局配额或分布式隔离证明。

## 4. 接口、数据与发布边界

- `docs/接口文档/` 零修改；未增删请求参数、响应字段、路由、状态码、Header、Callback、SSE 或
  WebSocket 语义。现有 `documentTranslationOne`、`documentTranslationTwo` 字段仍为 HTML
  字符串，仅修复其中合法表格由“可见标签文本”变为真实表格 DOM。
- 无数据库 Schema 或历史数据迁移。已持久化的旧翻译结果不会自动重渲染。
- Renderer 指纹变化采用失败关闭语义：发布前应排空或协调处理按 v2 指纹接受、尚未执行的
  Analysis 翻译任务；未来多实例滚动发布必须保证接受者和执行者的 Renderer Profile 一致，
  不能把进程内测试当作跨版本兼容证明。
- 回滚时可还原 Renderer、严格表格组件及 v3 指纹；回滚会恢复旧的表格标签文本问题。

## 5. 离线验证

全部使用项目 `venv`、临时目录、临时 SQLite 和 Fake；未启动 `run.py`，未连接真实翻译、
浏览器、MinerU 或其他后台服务。

- `tests.test_translation_module`：19 项通过，覆盖合法表格、属性清洗、恶意/畸形/跨度超限拒绝、
  Text 不恢复、MinerU 当前 PPTX 表格形态及 32 次 8 线程确定性恢复。
- `tests.test_stage1h_consumer_cutover`：9 项通过，证明 Analysis 的两个翻译字段均得到真实表格，
  Engine 调用记录只有四个表格文本节点。
- Translation、Analysis 消费切换、文档格式和翻译隔离联合回归：45 项通过。
- `test_analysis*.py` 定向发现回归：269 项通过；输出中的异常栈均为既有故障注入用例。
- Architecture 与 DocumentProcessing Architecture 门禁：40 项通过。
- 相关 Python 文件 `py_compile` 通过，`git diff --check` 通过；
  `git diff --name-only -- docs/接口文档` 输出为空。

以上证据只证明 Windows、离线 Fake、临时 SQLite 与当前单实例组合下的行为。正常 MHTML
浏览器/MinerU 真实链路、浏览器失败后的纯文本 MHTML 降级、真实 Office 复杂文档、生产供应商、
可靠任务队列、多实例一致性与容量仍需各自的实机或部署门禁。
