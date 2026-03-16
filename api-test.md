# 大模型文件处理接口文档

## 目录

1. [大模型文件解析接口]
2. [大模型报告生成接口]
3. [检测大模型任务接口]
4. [大模型任务进度推送接口]
5. [大模型回调接口]

---

## 1. 大模型文件解析接口

**接口路径：** `/llm/analysis`  
**请求方法：** `POST`  
**功能说明：** 提交文件进行大模型解析，支持多维度业务参数配置

### 请求参数说明

| 参数 | 类型 | 说明 | 必填 |
|------|------|------|------|
| businessType | String | 业务类型标识，固定值："file" | 是 |
| params | Array | 请求参数列表（项目兼容单项与多项；多项时按顺序串行处理） | 是 |

### params参数结构

| 字段 | 类型 | 说明 | 示例值 |
|------|------|------|--------|
| architectureList | Array | 领域体系列表（范围） | 见下方示例 |
| channel | Array | 渠道列表（范围）（字典编码） | `[{"value":"装发","key":"02"}]` |
| country | Array | 国家列表（范围）（字典编码） | `[{"value":"美国","key":"02"}]` |
| fileName | String | 文件对象名（业务主键） | `"bded228dc94440519d87f97cfb6b520b.pdf"` |
| filePath | String | 文件下载地址（格式 application/octet-stream） | `"http://localhost:8080/file-download/..."` |
| format | Array | 原生格式列表（范围）（字典编码） | `[{"value":"文档类","key":"03"}]` |
| maturity | Array | 成熟度列表（范围）（字典编码） | `[{"value":"阶段成果","key":"02"}]` |

### architectureList元素结构

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Long | 领域体系唯一标识 |
| level | Integer | 节点层级 |
| name | String | 领域名称 |
| path | String | 节点路径（ID链） |
| pathName | String | 节点路径名称 |
| sort | Integer | 排序值 |

### 请求示例

```json
{
  "businessType": "file",
  "params": [
    {
      "architectureList": [
        {
          "id": 1768464916588441,
          "level": 1,
          "name": "测试",
          "path": "1768464916588441",
          "pathName": "测试",
          "sort": 1
        }
      ],
      "channel": [
        {
          "value": "装发",
          "key": "02"
        }
      ],
      "country": [
        {
          "value": "美国",
          "key": "02"
        }
      ],
      "fileName": "bded228dc94440519d87f97cfb6b520b.pdf",
      "filePath": "http://localhost:8080/file-download/bded228dc94440519d87f97cfb6b520b.pdf",
      "format": [
        {
          "value": "文档类",
          "key": "03"
        }
      ],
      "maturity": [
        {
          "value": "阶段成果",
          "key": "02"
        }
      ]
    }
  ]
}
```

> 项目兼容扩展说明：除单文件请求外，也支持 `params` 中放入多个文件对象；后端会按数组顺序逐个处理，并为每个 `fileName` 分别维护状态、进度和回调。

---

## 2. 大模型报告生成接口

**接口路径：** `/llm/generate-report`  
**请求方法：** `POST`  
**功能说明：** 基于多文件内容生成结构化分析报告

### 请求参数说明

| 参数 | 类型 | 说明 | 必填 |
|------|------|------|------|
| businessType | String | 业务类型标识，固定值："report" | 是 |
| params | Array | 请求参数列表（仅使用第一个元素） | 是 |

### params参数结构

| 字段 | 类型 | 说明 | 示例值 |
|------|------|------|--------|
| filePathList | Array | 文件下载地址列表（支持多文件） | `["http://.../file1.sql", "http://.../file2.sql"]` |
| reportId | Long | 报告唯一标识（业务主键） | `132` |
| templateDesc | String | 模板说明信息 | `"1231231"` |
| templateOutline | String | 模板大纲内容 | `"12321313"` |
| requirement | String | 业务需求描述 | `"要求"` |

### 请求示例

```json
{
  "businessType": "report",
  "params": [
    {
      "filePathList": [
        "http://localhost:8080/file/download/ad92572918564809bdeb61af1a06486a.sql",
        "http://localhost:8080/file/download/d472f05fa3344738953d027ad598ff67.sql"
      ],
      "reportId": 132,
      "templateDesc": "1231231",
      "templateOutline": "12321313",
      "requirement": "业务需求描述，报告xxxx要求"
    }
  ]
}
```

---

## 3. 检测大模型任务接口

**接口路径：** `/llm/check-task`  
**请求方法：** `POST`  
**功能说明：** 检测大模型任务状态，对已解析成功但未回调的任务触发回调

### 文件类型请求参数

| 参数 | 类型 | 说明 | 必填 |
|------|------|------|------|
| businessType | String | 固定值："file" | 是 |
| params | Array | 仅含fileName参数的对象数组 | 是 |

### params元素结构（文件类型）

| 字段 | 类型 | 说明 |
|------|------|------|
| fileName | String | 文件对象名（业务主键） |

### 文件类型请求示例

```json
{
  "businessType": "file",
  "params": [
    {
      "fileName": "bded228dc94440519d87f97cfb6b520b.pdf"
    }
  ]
}
```

> 项目兼容扩展说明：`/llm/check-task` 支持一次传入多个 `fileName`；单项查询保持原返回结构，批量查询时 `data` 返回数组。

### 报告类型请求参数

| 参数 | 类型 | 说明 | 必填 |
|------|------|------|------|
| businessType | String | 固定值："report" | 是 |
| params | Array | 仅含reportId参数的对象数组 | 是 |

### params元素结构（报告类型）

| 字段 | 类型 | 说明 |
|------|------|------|
| reportId | Long | 报告唯一标识（业务主键） |

### 报告类型请求示例

```json
{
  "businessType": "report",
  "params": [
    {
      "reportId": 132
    }
  ]
}
```

---

## 4. 大模型任务进度推送接口

**接口路径：** `/llm/progress`  
**通信方式：** `WebSocket`  
**触发时机：** 调用文件解析/报告生成接口后自动建立连接推送进度

### 消息请求参数（客户端→服务端）

> 项目兼容扩展说明：除首条消息直接发送订阅 JSON 外，也支持显式动作消息：
>
> - `{"action":"subscribe","businessType":"file","params":[...]}`
> - `{"action":"query","businessType":"file","params":[...]}`
> - `{"action":"unsubscribe","businessType":"file","params":[...]}`
>
> 一个 WebSocket 连接可同时管理多个任务订阅。

#### 文件类型请求结构

```json
{
  "businessType": "file",
  "params": [
    {
      "fileName": "bded228dc94440519d87f97cfb6b520b.pdf"
    }
  ]
}
```

#### 报告类型请求结构

```json
{
  "businessType": "report",
  "params": [
    {
      "reportId": 132
    }
  ]
}
```

### 消息响应参数（服务端→客户端）

#### 文件类型响应结构

| 字段 | 类型 | 说明 |
|------|------|------|
| businessType | String | 固定值："file" |
| data.fileName | String | 文件对象名（业务主键） |
| data.progress | Number | 解析进度（0.0～1.0） |

```json
{
  "businessType": "file",
  "data": {
    "fileName": "bded228dc94440519d87f97cfb6b520b.pdf",
    "progress": 0.69
  }
}
```

#### 报告类型响应结构

| 字段 | 类型 | 说明 |
|------|------|------|
| businessType | String | 固定值："report" |
| data.reportId | Long | 报告唯一标识 |
| data.progress | Number | 生成进度（0.0～1.0） |

```json
{
  "businessType": "report",
  "data": {
    "reportId": 132,
    "progress": 0.89
  }
}
```

---

## 5. 大模型回调接口

**接口路径：** `/llm/callback`  
**请求方法：** `POST`  
**功能说明：** 大模型处理完成后回调业务系统，返回结构化结果

### 文件解析回调参数

| 参数 | 类型 | 说明 |
|------|------|------|
| businessType | String | 固定值："file" |
| data.fileName | String | 文件对象名（业务主键） |
| data.country | String | 国家（解析结果） |
| data.channel | String | 渠道（解析结果） |
| data.maturity | String | 成熟度（解析结果） |
| data.format | String | 原生格式（解析结果） |
| data.status | String | 解析状态（0未解析 1解析中 2已解析 3解析失败）（解析结果） |
| data.architectureId | Long | 领域体系ID（解析结果） |
| data.fileDataItem | Object | 文件解析详细数据（见下表） |
| msg | String | 回调消息（如"解析成功"） |

### fileDataItem字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| fileName | String | 文件名(对象名) |
| dataTime | String | 资料年代（ISO8601）yyyy-MM-dd |
| keyword | String | 关键词 |
| summary | String | 摘要 |
| score | BigDecimal | 评分（一位小数） |
| fileNo | String | 文件编号 |
| source | String | 资料来源 |
| originalLink | String | 原文链接 |
| language | String | 语种 |
| dataFormat | String | 资料格式 |
| associatedEquipment | String | 所属装备 |
| relatedTechnology | String | 所属技术 |
| equipmentModel | String | 装备型号 |
| documentOverview | String | 文件概述 |
| originalText | String | 文件原文 |
| documentTranslationOne | String | 文件翻译单语 |
| documentTranslationTwo | String | 文件翻译双语 |

### 文件回调请求示例

#### 解析成功

```json
{
  "businessType": "file",
  "data": {
    "fileName": "string",
    "country": "string",
    "channel": "string",
    "maturity": "string",
    "format": "string",
    "status": "2",
    "architectureId": 0,
    "fileDataItem": {
      "fileName": "string",
      "dataTime": "2026-02-09",
      "keyword": "string",
      "summary": "string",
      "score": 3.6,
      "fileNo": "string",
      "source": "string",
      "originalLink": "string",
      "language": "string",
      "dataFormat": "string",
      "associatedEquipment": "string",
      "relatedTechnology": "string",
      "equipmentModel": "string",
      "documentOverview": "string",
      "originalText": "string",
      "documentTranslationOne": "string",
      "documentTranslationTwo": "string"
    }
  },
  "msg": "解析成功"
}
```

#### 解析失败（更新状态）

```json
{
  "businessType": "file",
  "data": {
    "fileName": "string",
    "status": "3"
  },
  "msg": "解析失败"
}
```

### 报告生成回调参数

| 参数 | 类型 | 说明 |
|------|------|------|
| businessType | String | 固定值："report" |
| data.reportId | Long | 报告唯一标识 |
| data.details | String | 报告内容（HTML富文本格式） |
| data.status | String | 报告状态（0生成中 1已生效 2生成失败） |
| msg | String | 回调消息（如"生成成功"） |

### 报告回调请求示例

#### 生成成功

```json
{
  "businessType": "report",
  "data": {
    "reportId": 132,
    "status": "1",
    "details": "<div class=\"report-title\">\n  <span style=\"color: #ff6b6b;\">📄</span>\n  <span style=\"margin-left: 8px; font-size: 20px;\">报告内容</span>\n</div>"
  },
  "msg": "生成成功"
}
```

#### 生成失败（更新状态）

```json
{
  "businessType": "report",
  "data": {
    "reportId": 132,
    "status": "2"
  },
  "msg": "生成失败"
}
```
