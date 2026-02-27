# MongoDB存储方案设计

## 1. 项目架构修改

### 添加数据库配置模块
- 创建 `database_config.py`：管理MongoDB连接配置
- 添加环境变量支持：`MONGODB_URI`、`DATABASE_NAME`等
- 实现连接池管理

### 设计数据模型
- 创建 `DocumentResult` 模型：存储文档路径、处理结果、分类信息
- 设计索引策略：按分类、时间、用户ID建立索引

## 2. 修改现有处理流程

### 分离关注点
- **数据访问层**：新建 `database_service.py`，封装MongoDB CRUD操作
- **业务逻辑层**：修改 [pipeline.py](file:///D:/2026/LLM-%E58%88%EF%BC%9Av1/pipeline.py) 中的 [run_anythingllm_rag](file:///D:/2026/LLM-%E58%88%EF%BC%9Av1/pipeline.py#L55-L145) 函数，添加结果存储逻辑
- **API层**：更新 [web_ui.py](file:///D:/2026/LLM-%E58%88%EF%BC%9Av1/web_ui.py) 中的上传接口

### 修改核心函数
- 在 [run_anythingllm_rag](file:///D:/2026/LLM-%E58%88%EF%BC%9Av1/pipeline.py#L55-L145) 函数返回后，添加MongoDB存储逻辑
- 修改 [process_file_with_rag](file:///D:/2026/LLM-%E58%88%EF%BC%9Av1/pipeline.py#L148-L169) 函数，使其返回更完整的处理信息

## 3. 接口设计优化

### 数据库服务接口
- `save_document_result(file_path, result_json, metadata)`：保存文档处理结果
- `get_document_result_by_id(result_id)`：根据ID获取结果
- `get_results_by_category(category)`：按分类查询结果
- `update_document_result(result_id, updates)`：更新已有结果

### 业务逻辑接口
- 保持现有的 [process_file_with_rag](file:///D:/2026/LLM-%E58%88%EF%BC%9Av1/pipeline.py#L148-L169) 函数签名不变
- 在内部调用数据库服务进行存储

## 4. 实现步骤

### MongoDB存储实现方案

### 1. 创建数据库配置模块

```python
# database_config.py

```
创建数据库配置模块，用于管理MongoDB连接配置。
创建完成后，直接运行该文件，测试能否正常连接MongoDB。

### 2. 创建数据库服务模块

```python
# database_service.py

```
创建数据库服务模块，用于封装MongoDB的CRUD操作。
创建完成后，直接运行，测试能否正常连接数据库并进行CRUD操作。


### 3. 修改web_ui.py以支持数据库存储

```python
# 修改后的web_ui.py相关部分（只需要修改import和处理逻辑）

# ... HTML和其他常量保持不变 ...

# 修改：
def move_file_to_category_folder(file_path: Path, category: str) -> Tuple[bool, str]
def upload_file()
def upload_folder()

# 新增：添加新的API端点用于数据库查询
def get_result_by_id(result_id: str)
def get_result_by_file_path(file_path: str)
def get_results_by_category(category: str)
def search_results()
```

### 4. 修改pipeline.py以集成数据库存储

```python
# 修改后的pipeline.py
# 主要增加了以下4个参数：
    store_in_db,
    additional_metadata,
    final_file_path, 
    store_original_file,
# 并且增加了当store_in_db为True时，则调用数据库服务进行存储
```


### 5. 修改rag_with_ocr.py以支持数据库存储

```python
# 修改后的rag_with_ocr.py
# 主要增加了以下3个参数：
    store_in_db,
    additional_metadata,
    store_original_file,
# 以及对应处理逻辑
```

## 5. 运行说明

1、运行 `web_ui.py` 即可启动Web UI，并上传文件进行处理。

2、打开MongoDB 客户端，查看存储结果。

### MongoDB连接配置说明

### 1. **理解MongoDB Compass与代码配置的关系**

- **MongoDB Compass**：可视化MongoDB管理工具，用于查看和操作数据库
- **代码配置**：[database_config.py](file:///D:/2026/LLM-%E58%88%EF%BC%9Av1/database_config.py) 中的配置决定了应用程序连接到哪个MongoDB实例和数据库

### 2. **关键配置参数**

#### [DatabaseConfig](file:///D:/2026/LLM-%E58%88%EF%BC%9Av1/database_config.py#L7-L27) 类中的参数：
- [mongodb_uri](file://D:\2026\LLM-分类v1\database_config.py#L9-L9)：MongoDB连接地址，默认 `mongodb://localhost:27017/`
- [database_name](file://D:\2026\LLM-分类v1\database_config.py#L10-L10)：数据库名称，默认 `document_classification` 
- [collection_name](file://D:\2026\LLM-分类v1\database_config.py#L11-L11)：集合名称，默认 `document_results`

### 3. **确保Compass与代码连接同一数据库**

#### 在MongoDB Compass中操作：
1. **启动Compass**，连接字符串使用：`mongodb://localhost:27017/`
2. **连接后**，在左侧导航栏查找名为 `document_classification` 的数据库
3. **展开数据库**，找到名为 `document_results` 的集合

#### 验证配置一致性：

```python
# 检查当前应用使用的配置
from database_service.database_config import DatabaseConfig

config = DatabaseConfig()
print(f"应用连接的URI: {config.mongodb_uri}")
print(f"应用使用的数据库: {config.database_name}")
print(f"应用使用的集合: {config.collection_name}")
```

### 4. **连接步骤**

#### 步骤1：在Compass中连接
- 打开MongoDB Compass
- 在连接字符串中输入：`mongodb://localhost:27017/`
- 点击Connect

#### 步骤2：验证数据库和集合
- 在左侧数据库列表中找到 `document_classification`
- 展开后查看 `document_results` 集合
- 上传文件后，数据会出现在此集合中

### 5. **常见问题解决**

#### 如果在Compass中看不到数据库：
- **确认MongoDB服务运行**：确保MongoDB服务已启动
- **检查配置一致性**：确认Compass连接的URI与代码中的[mongodb_uri](file:///D:/2026/LLM-%E58%88%EF%BC%9Av1/database_config.py#L9-L9)相同
- **检查数据库名称**：确认Compass中查看的是[database_name](file:///D:/2026/LLM-%E58%88%EF%BC%9Av1/database_config.py#L10-L10)指定的数据库

#### 如果仍然找不到数据：
- **上传文件后刷新**：在Compass中右键点击集合名称选择"Refresh"
- **检查数据是否存在**：运行诊断脚本来确认数据是否真的写入了数据库

### 6. MongoDB存储 json 格式举例
```python
{
  "_id": {
    "$oid": "697318151a607e5f7e42aaa7"
  },
  "original_file_path": "uploads\\20250919凯瑟琳·萨顿被任命为美国防部网络政策负责人.docx",
  "final_file_path": "uploads\\05_作战指挥\\02_组织机构\\20250919凯瑟琳·萨顿被任命为美国防部网络政策负责人_1769150485.docx",
  "original_file_name": "20250919凯瑟琳·萨顿被任命为美国防部网络政策负责人.docx",
  "final_file_name": "20250919凯瑟琳·萨顿被任命为美国防部网络政策负责人_1769150485.docx",
  "original_file_size": 0,
  "final_file_size": 25244,
  "result_json": "{\n  \"outline\": [\"标题\", \"内容\", \"原文链接\", \"原文\"],\n  \"category\": \"作战指挥\",\n  \"sub_category\": \"组织机构\",\n  \"extract\": {\n    \"org_name\": \"国防部网络政策助理部长办公室\",\n    \"country\": \"美国\",\n    \"military_branch\": \"国防部\",\n    \"commander\": \"凯瑟琳·萨顿\",\n    \"function\": \"负责制定并监督国防部网络空间政策和战略的实施；将国家网络空间政策和指导与该部门的网络空间政策相结合；为国防部网络空间活动提供指导和监督，这些活动涉及外国网络空间威胁、国际合作、与外国伙伴和国际组织的接触，以及国防部网络空间战略和计划的实施，包括与网络空间力量、能力及其使用相关的活动。\",\n    \"subordinate_units\": \"\",\n    \"mechanism\": \"该办公室于2024年根据国会要求正式设立。\"\n  }\n}",
  "parsed_result": {
    "outline": [
      "标题",
      "内容",
      "原文链接",
      "原文"
    ],
    "category": "作战指挥",
    "sub_category": "组织机构",
    "extract": {
      "org_name": "国防部网络政策助理部长办公室",
      "country": "美国",
      "military_branch": "国防部",
      "commander": "凯瑟琳·萨顿",
      "function": "负责制定并监督国防部网络空间政策和战略的实施；将国家网络空间政策和指导与该部门的网络空间政策相结合；为国防部网络空间活动提供指导和监督，这些活动涉及外国网络空间威胁、国际合作、与外国伙伴和国际组织的接触，以及国防部网络空间战略和计划的实施，包括与网络空间力量、能力及其使用相关的活动。",
      "subordinate_units": "",
      "mechanism": "该办公室于2024年根据国会要求正式设立。"
    }
  },
  "metadata": {
    "upload_timestamp": {
      "$numberLong": "1769150474743"
    },
    "source": "web_ui_single_upload",
    "original_filename": "20250919凯瑟琳·萨顿被任命为美国防部网络政策负责人.docx"
  },
  "created_at": {
    "$date": "2026-01-23T06:41:25.863Z"
  },
  "updated_at": {
    "$date": "2026-01-23T06:41:25.863Z"
  },
  "category": "作战指挥",
  "sub_category": "组织机构",
  "full_category": "作战指挥/组织机构"
}
```

## 6. 安装依赖

在 `requirements.txt` 中添加：

```txt
pymongo>=4.0.0
```


## 7. 主要改进点

1. **清晰的模块分离**：数据库配置、服务、业务逻辑完全分离
2. **统一的数据模型**：定义了标准的文档存储格式
3. **错误处理机制**：数据库操作失败不会影响主流程
4. **灵活的接口**：支持启用/禁用数据库存储
5. **丰富的查询接口**：支持多种查询方式
6. **自动索引**：为常用查询字段创建索引
7. **元数据扩展**：支持自定义元数据存储

这样的设计使得数据存储功能与原有业务逻辑解耦，同时提供了完整的CRUD操作接口。

## 

## 8. 错误处理与日志

### 异常处理
- MongoDB连接失败时的降级处理
- 存储失败不影响主流程
- 添加重试机制

### 日志记录
- 记录存储操作的成功/失败状态
- 记录处理时间和性能指标

## 9. 配置管理

### 环境变量
- `MONGODB_URI`：MongoDB连接字符串
- `DATABASE_NAME`：数据库名称  
- `COLLECTION_NAME`：集合名称

### 配置文件
- 在 [config.py](file:///D:/2026/LLM-%E58%88%EF%BC%9Av1/config.py) 中添加数据库配置选项
- 支持本地开发和生产环境的不同配置

当前数据库实现既保持了现有代码结构的完整性，又清晰地分离了数据存储逻辑，使整个处理流程更加模块化和可维护。