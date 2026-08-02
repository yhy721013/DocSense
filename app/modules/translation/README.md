# 独立 Translation 模块

`translation` 只负责读取已经准备好的 Markdown/Text Artifact，执行语言转换、分段和安全 HTML
渲染。它不知道输入来自 DOC、MHTML、PDF、OCR 还是文本直通，也不得启动 LibreOffice、浏览器、
MinerU 或 OCR。

## 依赖方向

```text
Analysis / Weaponry Adapter
  -> Translation Application
      -> Translation Domain + Ports
      -> PreparedArtifactReaderPort
      -> TranslationEnginePort
      -> TranslationRendererPort
```

- `TranslationRequest` 只携带 TaskId、prepared ArtifactRef、目标语言、范围和冻结 Profile。
- `TranslatePreparedDocument` 通过窄 Reader Port 读取字节，不取得本地路径或删除权限。
- HYMT/Argos/Ollama 运行时和 HTML Renderer 位于 Adapter；格式转换实现不得进入本模块。
- 翻译引擎实例若非线程安全，只在单次 engine 调用周围加实例锁；Artifact 读取、分段和渲染不
  使用全局大锁。
- Analysis 全文翻译消费与 RAG/正文读取相同的 prepared Artifact；Weaponry 纯文本只调用
  `TranslationEnginePort`。Analysis 不再保留摘要翻译分支。

旧 Translation Service 已在阶段 1G-5A5 删除，旧 Translator 包与文件处理兼容 Facade 已在
阶段 1G-5B 删除。翻译和格式处理的新能力只能分别进入 `app/modules/translation/` 与
`app/modules/document_processing/`，禁止重新创建旧目录作为跨模块编排入口。
