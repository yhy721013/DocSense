# 使用快速翻译模式的说明

## 1、pip安装必要翻译库

    pip install argostranslate

## 2、下载翻译包（翻译包是独立于翻译库的，无法使用pip下载）

    在联网状态下，运行一次run.py的测试（目前已经是默认快速翻译模式），会自动下载翻译包。

我这个代码里面实现了自动检测本地是否存在翻译包，第一次使用的时候会自动下载翻译包，不过下载需要翻墙

默认下载到C:\Users\xxx\.local\share\argos-translate\packages
里面可以看到translate-en_zh-1_9和translate-zh_en-1_9两个文件夹

## 3、(重要！没有这个步骤无法完成离线翻译)

找到pip 安装之后包所在的位置，例如conda中包的位置为：D:\ProgramData\anaconda3\envs\anythingllm\Lib\site-packages\argostranslate

在包里面打开sbd.py，在第154行之后再加入一行，结果如下：

    def lazy_pipeline(self):
        if self.stanza_pipeline is None:
            self.stanza_pipeline = stanza.Pipeline(
                lang=self.stanza_lang_code,
                dir=str(self.pkg.package_path / "stanza"),
                processors="tokenize",
                use_gpu=settings.device == "cuda",
                logging_level="WARNING",
                download_method=None,
            )

这会告诉 Stanza：“不要尝试下载任何东西，只管去本地找”。

如果没有这个步骤，那么在翻译的时候会出现如下错误：

    [INFO] ('Splitting sentences using SBD Model: (en) StanzaSentencizer',)
      [错误] ArgoTranslate 翻译失败：HTTPSConnectionPool(host='raw.githubusercontent.com', port=443): Max retries exceeded with url: /stanfordnlp/stanza-resources/main/resources_1.10.0.json (Caused by SSLError(SSLEOFError(8, '[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol (_ssl.c:1017)')))”

## 4、然后就可以运行离线快速翻译了






# 使用ollama加载模型的说明

## 下载GGUF格式

我下载的是4bit量化版（这个链接是未量化版的）
https://huggingface.co/Tencent-HunYuan/HY-MT1.5-1.8B-GGUF

## 创建 Modelfile

创建的Modelfile 文件内容如下：

    # 指定GGUF模型文件（确保路径正确）
    FROM HY-MT1.5-1.8B_bf16_Q4_K_M.gguf
    
    # 设置基础参数
    PARAMETER num_ctx 4096
    PARAMETER num_gpu 50
    PARAMETER num_thread 8
    
    # 定义模板提示词（可选）
    TEMPLATE """{{ if .System }}{{ .System }}
    {{ end }}{{ if .Prompt }}Translate the following text according to these rules:
    - Preserve original formatting (tags, line breaks, timestamps)
      - Use domain-specific terminology when applicable
      - Maintain context coherence across sentences
      Input: {{ .Prompt }}
      Output:{{ end }}"""


## 把GGUF格式模型和 Modelfile文件放在ollama的 models/blobs目录下

在powershell中运行：

    ollama create tencent-hy-mt:1.8b-q4 -f Modelfile

如果要删除模型，请运行：

    ollama rm tencent-hy-mt:1.8b-q4

如果要查看已安装的模型，请运行：

    ollama list


## 完成之后就可以删除GGUF格式模型了和Modelfile了


# 部署 hy_mt_translator 翻译包

基于腾讯 HY-MT1.5-1.8B 模型的文档翻译工具，支持 PDF/DOCX/TXT 格式，输出双语 HTML。


---

## 1. 项目结构

| 文件                    | 功能说明 |
|-----------------------|---------|
| `core.py`             | 核心翻译引擎，加载模型并提供文本翻译能力 |
| `document_handler.py` | 统一接口，自动识别文档类型并分发处理 |
| `pdf_handler.py`      | PDF 处理器，保留排版布局，支持表格识别 |
| `docx_handler.py`     | Word 处理器，支持合并单元格和嵌套表格 |
| `txt_handler.py`      | 文本文件处理器，按段落分割翻译 |
| `utils.py`            | 工具函数，包含提示词构建、输出清理和进度追踪 |
| `demo_usage.py`       | 测试运行入口 |

---

## 2. hy_mt_translator 封装调用说明

---

```python
from hy_mt_translator import HYMTTranslator, DocumentTranslator

# 步骤 1: 初始化翻译模型
translator = HYMTTranslator()

# 步骤 2: 创建文档翻译器
doc_translator = DocumentTranslator(translator)

# 步骤 3: 翻译文档（自动输出 HTML）
result_path = doc_translator.convert_to_html(
    file_path="your_document.pdf/docx/txt",  # 支持 .pdf, .docx, .txt
    output_dir="./output", # 存放 HTML 和 对应images文件夹 的目录
    target_lang="Chinese", # 目标语言
    show_bilingual=True,  # True=中英对照，False=仅译文
    translate_all= N  # 翻译前 N 段或翻译全文(N=0时翻译全文)
)
```

---

## 3. **HTML 输出样式说明**

所有处理器生成的 HTML 都采用统一的视觉风格：

**特性**:
- 背景色：`#f5f5f5` (浅灰)
- 容器：白色背景 + 阴影效果
- 原文颜色：`#000000` (黑色)
- 译文颜色：`#0066cc` (蓝色加粗)

**功能**:
- 流式相对定位保持原位置
- 表格带边框和蓝色高亮
- 字体大小和颜色继承原文档
- 响应式布局
- 段落间虚线分隔

---

### 4. 性能优化建议

1. **首次运行会加载模型**（约几秒），后续会使用缓存
2. **大文件建议分段处理**，避免内存占用过高 (尚未实现此优化)
3. **GPU 加速**: 默认使用ollama的GPU加速策略
4. **批量处理**: 多个文件共用一个 translator 实例更高效

```python
# 高效做法：一个 translator 处理多个文件
translator = HYMTTranslator()  # 只加载一次
doc_translator = DocumentTranslator(translator)

for file in file_list:
    doc_translator.convert_to_html(file_path=file, ...)
```

---

这就是 `hy_mt_translator` 包的完整封装调用说明！如有任何问题，请参考上述示例代码。

