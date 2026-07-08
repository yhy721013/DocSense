# 多语言翻译功能说明

本项目支持 **8种语言** 的快速机器翻译（基于 Argo Translate），包括：
- **中文 (zh)** ↔ **英文 (en)** （双向）
- **日文 (ja)** → 英文 → 中文
- **俄文 (ru)** → 英文 → 中文
- **韩文 (ko)** → 英文 → 中文
- **法文 (fr)** → 英文 → 中文
- **德文 (de)** → 英文 → 中文
- **意文 (it)** → 英文 → 中文

> **注意**: 非中文语言通过英文中转翻译到中文，这是由翻译包的设计决定的。

---

## 一、快速开始（自动模式）

### 1. 安装依赖库

```bash
pip install argostranslate
```

### 2. 配置环境变量

在 `.env` 文件中设置翻译包目录路径：

```bash
# 翻译包存储目录（建议放在项目内便于管理）
ARGOS_PACKAGES_DIR=D:\2026\DocSense\models\argos-translate\packages
```

### 3. 首次运行（自动下载翻译包）

在联网状态下，首次运行应用会自动检测并下载所有必需的翻译包：

```bash
python run.py
```

或者运行集成测试脚本验证翻译功能（推荐）：

```bash
python -m tests.test_multilingual_translation_integration
```

**系统会自动完成以下操作**：
- ✅ 检测已安装的翻译包
- ✅ 下载缺失的翻译包（需要网络连接）
- ✅ 初始化翻译引擎
- ✅ 验证语言识别能力

默认下载位置：`ARGOS_PACKAGES_DIR` 指定的目录

### 4. 离线模式配置（重要！）

为避免每次翻译时尝试联网下载 Stanza 资源，需要修改 `sbd.py` 文件：

**找到文件位置**：
```
<conda_env_path>\Lib\site-packages\argostranslate\sbd.py
```

例如：`D:\ProgramData\anaconda3\envs\DocSense\Lib\site-packages\argostranslate\sbd.py`

**修改内容**：
在 `lazy_pipeline` 方法中（约第154行），添加 `download_method=None` 参数：

```python
def lazy_pipeline(self):
    if self.stanza_pipeline is None:
        self.stanza_pipeline = stanza.Pipeline(
            lang=self.stanza_lang_code,
            dir=str(self.pkg.package_path / "stanza"),
            processors="tokenize",
            use_gpu=settings.device == "cuda",
            logging_level="WARNING",
            download_method=None,  # ← 添加这一行，禁止联网下载
        )
```

**如果不修改会出现的错误**：
```
[错误] ArgoTranslate 翻译失败：HTTPSConnectionPool(host='raw.githubusercontent.com', port=443): 
Max retries exceeded with url: /stanfordnlp/stanza-resources/main/resources_1.10.0.json
```

### 5. 验证安装

运行集成测试脚本，全面验证多语言翻译功能：

```bash
python -m tests.test_multilingual_translation_integration
```

测试内容包括：
- ✅ 翻译包完整性检查
- ✅ 7种语言到中文的单语言翻译
- ✅ 混合多语言段落处理
- ✅ 中转翻译机制验证
- ✅ 语言自动识别准确性（目标：100%）

---

## 二、离线部署（生产环境推荐）

如果服务器无法联网，可以预先准备好所有翻译包：

### 1. 在有网络的环境下载翻译包

运行一次集成测试或主程序，让系统自动下载所有翻译包到指定目录。

### 2. 复制翻译包到目标环境

将整个 `packages` 目录复制到目标服务器的相同路径。

### 3. 确认 sbd.py 已修改

确保目标环境的 `sbd.py` 已添加 `download_method=None`。

### 4. 验证离线模式

断开网络后运行测试，应能正常翻译而不报错。

---

## 三、使用示例
## 四、API 调用方式

### 方式1: 直接文本翻译

```python
from app.services.translator.core import HYMTTranslator

# 初始化翻译器（check_ollama=False 跳过 Ollama 检查，加速启动）
translator = HYMTTranslator(check_ollama=False)

# 翻译单段文本（默认使用快速机器翻译）
text_ja = "空母は悪天候でも航空機を発進させることができます。"
result = translator.translate_text(text_ja, target_lang="Chinese", fast_translate=True)
print(f"译文: {result}")

# 切换到 LLM 大模型翻译模式
result_llm = translator.translate_text(text_ja, target_lang="Chinese", fast_translate=False)
```

### 方式2: 文档翻译（支持 PDF/DOCX/TXT/MHTML）

```python
from app.services.translator.document_handler import DocumentTranslator
from app.services.translator.core import HYMTTranslator

# 创建翻译器
translator = HYMTTranslator(check_ollama=False)
doc_translator = DocumentTranslator(translator)

# 翻译文档，生成双语 HTML 和单语 HTML
bilingual_html, monolingual_html = doc_translator.convert_to_html(
    file_path="document.pdf",      # 支持 .pdf, .docx, .txt, .mhtml
    output_dir="./output",          # 输出目录
    target_lang="Chinese",          # 目标语言
    show_bilingual=True,            # True=中英对照，False=仅译文
    fast_translate=True             # True=机器翻译，False=LLM翻译
)

print(f"双语HTML: {bilingual_html}")
print(f"单语HTML: {monolingual_html}")
```

### 方式3: 在服务层使用（推荐）

```python
from app.services.llm_service.translation_service import LLMTranslationService

service = LLMTranslationService()

# 翻译纯文本
translated_text = service.translate_text_only(
    text="The aircraft carrier can launch aircraft.",
    target_lang="Chinese",
    fast_translate=True,  # 使用机器翻译
    as_html=False
)

# 翻译文档文件
bilingual_path, monolingual_path = service.translate_document(
    file_path="document.pdf",
    output_dir="./output",
    target_lang="Chinese",
    fast_translate=True
)
```

---

## 五、翻译模式配置

### 环境变量控制

在 `.env` 文件中配置：

```bash
# 翻译模式：machine（机器翻译）或 llm（大模型翻译）
DOCSENSE_TRANSLATION_MODE=machine

# 使用的 LLM 模型名称（仅在 llm 模式下生效）
DOCSENSE_TRANSLATION_MODEL=Qwen3-4B-Instruct-2507-Q4_K_M

# 翻译包目录
ARGOS_PACKAGES_DIR=D:\2026\DocSense\models\argos-translate\packages
```

### 运行时覆盖

可以在调用时临时覆盖全局配置：

```python
# 即使全局设置为 llm 模式，这里仍使用机器翻译
result = translator.translate_text(text, target_lang="Chinese", fast_translate=True)

# 即使全局设置为 machine 模式，这里仍使用 LLM 翻译
result = translator.translate_text(text, target_lang="Chinese", fast_translate=False)
```---

## 六、支持的翻译对

当前项目配置的翻译包列表（定义在 `core.py` 中）：

```python
_ARGOS_LANGUAGE_PACKAGES = [
    ("zh", "en", "中文→英文"),   # 双向
    ("en", "zh", "英文→中文"),   # 双向
    ("ja", "en", "日文→英文"),   # 单向，需中转
    ("ru", "en", "俄文→英文"),   # 单向，需中转
    ("ko", "en", "韩文→英文"),   # 单向，需中转
    ("fr", "en", "法文→英文"),   # 单向，需中转
    ("de", "en", "德文→英文"),   # 单向，需中转
    ("it", "en", "意文→英文"),   # 单向，需中转
]
```

**翻译路径说明**：
- **中文 ↔ 英文**: 直接翻译，质量较高
- **其他语言 → 中文**: 通过英文中转（如：日语→英语→中文）
- **中转机制自动触发**: 无需手动配置，系统会自动选择最佳路径

---

## 七、语言自动识别

系统能够自动检测输入文本的语言类型，支持：

### 字符集识别（100% 准确）
- **日语**: 检测平假名/片假名 (`\u3040-\u30ff`)
- **韩语**: 检测韩文字符 (`\uac00-\ud7af`)
- **俄语**: 检测西里尔字母 (`\u0400-\u04ff`)
- **中文**: 检测汉字 (`\u4e00-\u9fff`)

### 词频统计识别（拉丁语系）
- **法语**: 特征词（le, la, les, aujourd'hui 等）+ 特殊字符（à, é, è, ê 等）
- **德语**: 特征词（der, die, das, und 等）+ 特殊字符（ä, ö, ü, ß）
- **意大利语**: 特征词（il, lo, la, e, è 等）+ 特殊字符（à, è, é, ì 等）
- **英语**: 默认 fallback，当其他语言评分不高时使用

**识别准确率**: 在测试中达到 **100%**（8/8 语言）

---

## 八、性能与限制

### 优势
✅ **速度快**: 机器翻译比 LLM 快 10-100 倍  
✅ **离线可用**: 配置完成后无需网络连接  
✅ **资源占用低**: 不需要 GPU 或大量内存  
✅ **批量处理高效**: 适合大规模文档翻译  

### 局限性
⚠️ **专业术语**: 军事、技术等领域的术语可能翻译不准确  
⚠️ **混合语言**: 同一段落包含多种语言时效果有限  
⚠️ **上下文理解**: 无法像 LLM 那样理解长距离上下文  
⚠️ **翻译方向**: 非英语语言到中文需经过中转，可能损失部分语义  

### 典型翻译质量示例

| 原文语言 | 原文 | 译文 | 评价 |
|---------|------|------|------|
| 英语 | The aircraft carrier can launch aircraft in bad weather. | 航空母舰可以在恶劣天气下发射飞机。 | ✅ 准确 |
| 日语 | 空母は悪天候でも航空機を発進させることができます。 | 空降飞机即使在恶劣天气下也能发射。 | ⚠️ "空母"译为"空降飞机"不够准确 |
| 法语 | Le porte-avions peut lancer des avions par mauvais temps. | 航空母舰可以在恶劣天气下投掷飞机。 | ⚠️ "lancer"译为"投掷"不太恰当 |
| 德语 | Der Flugzeugträger kann bei schlechtem Wetter Flugzeuge starten. | 航空母舰可以在恶劣天气下启动飞机。 | ⚠️ "starten"译为"启动"可以更优化 |

**建议**: 对于高质量要求的场景，建议使用 LLM 翻译模式（`fast_translate=False`）。

---

## 九、故障排查

### 问题1: 翻译包未下载

**现象**: 运行时报错 `未找到源语言 XX 的翻译包`

**解决**:
```bash
# 1. 检查网络连接
ping raw.githubusercontent.com

# 2. 删除缓存，重新触发下载
rm -rf D:\2026\DocSense\models\argos-translate\packages\*
python -m tests.test_multilingual_translation_integration

# 3. 或手动下载翻译包（见离线部署章节）
```

### 问题2: Stanza 联网错误

**现象**: `HTTPSConnectionPool(host='raw.githubusercontent.com')... Max retries exceeded`

**解决**: 按照「一、快速开始」第4步修改 `sbd.py`，添加 `download_method=None`。

### 问题3: 翻译结果为空或过短

**现象**: 返回空字符串或只有几个字符

**解决**:
```python
# 检查是否捕获了异常
try:
    result = translator.translate_text(text, target_lang="Chinese", fast_translate=True)
except RuntimeError as e:
    print(f"翻译失败: {e}")
    # 可以尝试切换到 LLM 模式
    result = translator.translate_text(text, target_lang="Chinese", fast_translate=False)
```

### 问题4: 混合语言段落未翻译

**现象**: 中英混合文本保持原样

**原因**: Argo Translate 主要针对单一语言设计，检测到混合语言时可能跳过翻译。

**解决**: 
- 方案1: 预处理文本，按语言分段后再分别翻译
- 方案2: 使用 LLM 模式（`fast_translate=False`），LLM 对混合语言支持更好

---

## 十、高级用法

### 批量处理多个文件

```python
from app.services.translator.core import HYMTTranslator
from app.services.translator.document_handler import DocumentTranslator

# 只初始化一次翻译器（高效）
translator = HYMTTranslator(check_ollama=False)
doc_translator = DocumentTranslator(translator)

# 批量处理
file_list = ["doc1.pdf", "doc2.docx", "doc3.txt"]
for file_path in file_list:
    try:
        bilingual, monolingual = doc_translator.convert_to_html(
            file_path=file_path,
            output_dir="./output",
            target_lang="Chinese",
            fast_translate=True
        )
        print(f"✅ {file_path} 翻译完成")
    except Exception as e:
        print(f"❌ {file_path} 翻译失败: {e}")
```

### 自定义进度回调

```python
def progress_callback(current, total):
    percentage = (current / total) * 100 if total > 0 else 0
    print(f"进度: {current}/{total} ({percentage:.1f}%)")

# 在翻译服务中使用
service = LLMTranslationService()
service.translate_document(
    file_path="large_document.pdf",
    output_dir="./output",
    progress_callback=progress_callback
)
```

### 术语校正后处理

```python
# 定义术语映射表
TERM_CORRECTIONS = {
    "空降飞机": "航空母舰",
    "猎人": "战斗机",
    "投掷飞机": "发射飞机",
    "启动飞机": "起飞飞机",
}

def correct_terms(text: str) -> str:
    """修正翻译后的术语"""
    for wrong, correct in TERM_CORRECTIONS.items():
        text = text.replace(wrong, correct)
    return text

# 使用
result = translator.translate_text(text, target_lang="Chinese", fast_translate=True)
corrected_result = correct_terms(result)
```

---

## 十一、相关文档与测试

- **集成测试脚本**: `tests/test_multilingual_translation_integration.py`
  - 全面测试多语言翻译功能
  - 自动验证翻译包完整性
  - 生成详细测试报告

- **单元测试**: `tests/test_translation_service.py`
  - 测试语言识别逻辑
  - 测试中转翻译机制
  - 测试配置优先级

- **核心实现**: `app/services/translator/core.py`
  - `_ARGOS_LANGUAGE_PACKAGES`: 支持的翻译对列表
  - `_detect_argos_source_language()`: 语言自动识别
  - `_auto_install_argos_packages()`: 自动下载安装

- **离线部署指南**: `OFFLINE.md` (第35-52行)

---

## 十二、总结

✅ **当前状态**: 多语言翻译功能已完全可用  
✅ **支持语言**: 8种（中、英、日、俄、韩、法、德、意）  
✅ **翻译包**: 已完整安装，可离线使用  
✅ **语言识别**: 准确率 100%  
✅ **中转机制**: 自动工作，无需手动配置  

**推荐使用场景**:
- 📄 大批量文档快速翻译
- 🔒 离线环境下的翻译需求
- ⚡ 对速度要求高于质量的场景
- 🌐 多语言内容的初步翻译

**不推荐使用场景**:
- 🎯 对翻译质量要求极高的专业文档
- 📚 需要深度上下文理解的复杂文本
- 🔬 高度专业化的技术文献

对于这些场景，建议使用 LLM 翻译模式（`fast_translate=False`）。

