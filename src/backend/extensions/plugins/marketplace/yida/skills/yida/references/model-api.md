# 大模型 AI 接口

## txtFromAI

**描述**：输入提示词，调用大模型接口返回文本

**接口地址**：`/query/intelligent/txtFromAI.json`

**请求方式**：POST

**Content-Type**：`application/x-www-form-urlencoded`

### 请求参数

| 参数名 | 类型 | 是否必填 | 描述 | 示例 |
| --- | --- | --- | --- | --- |
| _csrf_token | String | 是 | CSRF 令牌，从 `window.g_config._csrf_token` 获取 | `06109173-62b8-461a-89b8-a9baca5d9c86` |
| prompt | String | 是 | 提示词内容 | `请生成一段描述` |
| maxTokens | String | 否 | 最大返回 token 数 | `3000` |
| skill | String | 是 | 技能类型 | `ToText` |

### 返回值

| 字段 | 类型 | 描述 |
| --- | --- | --- |
| success | Boolean | 请求是否成功 |
| errorCode | String | 错误码 |
| errorMsg | String | 错误信息 |
| content | Object | 返回内容对象 |
| content.content | String | 大模型生成的文本内容 |
| content.damo_requestId | String | 请求唯一标识 |
| content.usage | Object | token 用量统计 |
| content.usage.input_tokens | Number | 输入 token 数 |
| content.usage.onput_tokens | Number | 输出 token 数 |

### 请求示例

```javascript
fetch('/query/intelligent/txtFromAI.json', {
  method: 'POST',
  headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
  body: new URLSearchParams({
    _csrf_token: window.g_config._csrf_token,
    prompt: '提示词内容',
    maxTokens: '3000',
    skill: 'ToText',
  }).toString(),
}).then((res) => res.json())
  .then((data) => {
    console.log('AI返回内容', data.content.content);
  });
```

### 返回值示例

```json
{
  "content": {
    "usage": {
      "onput_tokens": 11,
      "input_tokens": 91
    },
    "damo_requestId": "chatcmpl-26a868b5-e3e1-9ddb-a310-d1336299f9b9",
    "content": "大模型生成的文本内容"
  },
  "errorCode": "",
  "errorExtInfo": null,
  "errorLevel": "",
  "errorMsg": "",
  "success": true,
  "throwable": ""
}
```