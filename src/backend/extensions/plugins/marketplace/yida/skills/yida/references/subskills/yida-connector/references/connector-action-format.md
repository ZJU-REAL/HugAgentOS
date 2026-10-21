# 连接器执行动作配置文件格式

本文档详细说明宜搭连接器执行动作配置文件的格式规范。

## 字段说明

| 字段 | 说明 |
|------|------|
| `label` | 字段在宜搭界面上显示的"显示名称"，应根据接口文档含义填写中文名称 |
| `desc` | 字段的详细描述，用于 hover 提示 |
| `__level` | 字段层级，顶层字段填 `0` |
| `hidden` | 是否在界面上隐藏该字段，默认 `false` |

## inputs 分组规则

inputs 中的字段按照以下规则分组：

| 分组 | 说明 | 包含内容 |
|------|------|----------|
| `Headers` | 请求头参数 | `Content-Type` 等 |
| `Query` | URL 查询参数 | GET 接口的参数、POST 接口中需要放在 query 的参数如 `access_token` |
| `Path` | 路径变量 | URL 中 `{variable}` 形式的参数 |
| `Body` | 请求体参数 | POST/PUT 接口的 JSON body |

### GET 接口处理规则

GET 接口没有 Body，所有业务参数放在 `Query` 分组中：
- `inputs` 只包含 `Headers` 和 `Query`
- `parameters` 只有 `header` 和 `query` 字段，无 `body`

### access_token 在 query 的处理

当接口的 `access_token` 通过 URL query 传递时：
- 若连接器已配置 `ApiKeyAuth`（`in: query`），则 `access_token` 由鉴权账号自动注入
- **不需要**在 inputs 中重复添加
- 若接口有其他 query 参数（如 `pubaccId`），则单独放在 `Query` 分组中

## 连接器描述规则

- 描述应为**一句话总结**，概括连接器的核心用途
- **不是**列出动作名称
- 示例：`支持向 diwork 群组发送文本消息` 而非 `动作列表: 群组发送文本消息`
- 描述由 `buildConnectorDesc` 函数根据 operations 自动生成，无需手动填写

## 完整的 JSON 格式示例

```json
[
  {
    "id": "operation-id",
    "operationId": "actionName",
    "summary": "动作名称",
    "description": "动作描述",
    "url": "v1.0/api/path",
    "method": "post",
    "inputs": [
      {
        "childList": [
          {
            "componentName": "TextField",
            "defaultValue": "application/json",
            "desc": "Content-Type",
            "name": "Content-Type",
            "required": false
          }
        ],
        "desc": "请求头",
        "name": "Headers",
        "paramType": "Object",
        "required": false
      },
      {
        "defaultValue": "{}",
        "desc": "请求体",
        "name": "Body",
        "paramType": "Object",
        "required": false,
        "childList": [
          {
            "componentName": "TextField",
            "name": "fieldName",
            "label": "字段显示名称",
            "desc": "字段含义描述",
            "required": true,
            "__level": 0,
            "hidden": false
          }
        ]
      }
    ],
    "parameters": {
      "header": [
        {
          "name": "Content-Type",
          "value": "application/json"
        }
      ],
      "body": {
        "default": "{}"
      }
    },
    "responses": {
      "type": "object",
      "properties": {
        "fieldName": { "type": "string", "description": "fieldName" }
      }
    },
    "outputs": [
      {
        "defaultValue": "{\n    \"fieldName\": \"value\"\n}",
        "desc": "响应体结构",
        "name": "Response",
        "paramType": "Object",
        "required": false,
        "childList": [
          {
            "_key": "actionName%fieldName",
            "name": "fieldName",
            "paramType": "String",
            "children": [],
            "childList": [],
            "__level": 0,
            "hidden": false,
            "label": "字段显示名称"
          }
        ]
      }
    ],
    "origin": true
  }
]
```

## outputs childList 字段说明

| 字段 | 说明 |
|------|------|
| `_key` | 格式为 `operationId%fieldName`，如 `sendServiceTxt%flag` |
| `paramType` | 字段类型，`String` / `Number` / `Boolean`（注意：outputs 用 `paramType` 而非 `componentName`，两者不能混用） |
| `children` | 固定为空数组 `[]` |
| `childList` | 固定为空数组 `[]`（叶子节点无子字段） |
| `defaultValue` | 应填写接口返回的真实响应示例 JSON 字符串 |

## responses JSON Schema 说明

### 基本格式

```json
{
  "type": "object",
  "properties": {
    "fieldName": { "type": "string", "description": "fieldName" }
  }
}
```

### 规则说明

- `type` 固定为 `"object"`（小写）
- `properties` 中每个字段的 `type` 也用小写：`"string"` / `"number"` / `"boolean"`

### 示例

**有返回字段时**：
```json
{
  "type": "object",
  "properties": {
    "msg": { "type": "string", "description": "msg" },
    "flag": { "type": "string", "description": "flag" }
  }
}
```

**无返回字段时**：
```json
{ "type": "object", "properties": {} }
```
