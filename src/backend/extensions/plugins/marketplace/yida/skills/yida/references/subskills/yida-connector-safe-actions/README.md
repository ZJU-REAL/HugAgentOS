
# 宜搭 HTTP 连接器执行动作安全生成

## 适用场景

当用户需要把已有系统接口接入宜搭 HTTP 连接器时使用本技能，尤其适用于：

- 已有连接器，需要继续添加“执行动作”
- 用户提供前端 API 文件和后端 Controller/接口定义文件
- 从 Vue/React API wrapper、ASP.NET Controller、Spring Controller 等代码中提取接口
- 点击宜搭连接器测试面板时报错，刷新后动作列表为空
- `openyida connector list-actions <connector-id>` 返回 0 个动作，但连接器仍存在
- 需要说明“如何正确使用 OpenYida 生成 HTTP 连接器动作”

如果只是创建连接器、配置鉴权、管理连接器账号，优先使用 `yida-connector`。

## 核心原则

- 先读源码再生成动作，不要凭空编造接口路径、参数或 action-id。
- 默认只生成前端 API 文件实际暴露/调用的接口，除非用户明确要求覆盖后端全部接口。
- 对未知响应结构保持保守，先只输出根对象 `Response`，避免宜搭测试面板解析复杂输出结构时报错。
- Windows PowerShell 读取中文 JSON 时必须显式使用 UTF-8。
- 修改后必须执行“添加动作 -> 列表验证 -> CLI 测试 -> 再次列表验证”的闭环。
- 如果一次错误配置导致动作被清空，应重建完整动作列表，而不是只追加缺失动作。
- 动作 JSON 必须使用当前 agent 的结构化文件写入工具创建；不要用 shell heredoc、`cat`/`echo`/`printf`/`tee` 或重定向写文件。

## 推荐流程

### 1. 读取接口来源

同时读取用户提供的前端 API 文件和后端接口定义文件。

前端 API 文件用于确认“哪些接口真的要暴露给宜搭连接器”；后端文件用于确认 method、route、query/path/body 参数和默认值。

### 2. 查看连接器状态

```bash
openyida connector detail <connector-id>
openyida connector list-actions <connector-id>
```

记录以下信息：

- 连接器 ID
- 连接器域名、协议、基础路径、鉴权方式
- 当前已有动作列表
- 是否已经出现动作被清空

### 3. 生成动作 JSON 文件

建议放在 OpenYida project 工作目录的任务子目录：

```text
<projectRoot>/.cache/openyida/<项目名或任务名>/connector-actions/<业务名>-actions.json
```

从 workspace 根执行命令时传 `project/.cache/openyida/<项目名或任务名>/connector-actions/<业务名>-actions.json`；从 project 工作目录内执行时传 `.cache/openyida/<项目名或任务名>/connector-actions/<业务名>-actions.json`。

动作 ID 建议使用顺序编号：

```json
"id": "operation-1"
```

动作调用名使用前端函数名或后端 Action 名：

```json
"operationId": "getUserDtuSns"
```

### 4. 校验 JSON 编码

Windows PowerShell 必须加 `-Encoding UTF8`：

```powershell
Get-Content -Raw -Encoding UTF8 .cache\openyida\<项目名或任务名>\connector-actions\<业务名>-actions.json | ConvertFrom-Json | Out-Null
```

不要使用默认 `Get-Content` 校验中文 JSON，默认编码可能导致误判或乱码。

### 5. 添加动作

```bash
openyida connector add-action --operations .cache/openyida/<项目名或任务名>/connector-actions/<业务名>-actions.json --connector-id <connector-id> --confirm
```

### 6. 验证动作存在

```bash
openyida connector list-actions <connector-id>
```

### 7. 用 CLI 测试动作

CLI 测试时 `--action` 使用 `operationId`，不是顺序编号 `operation-1`：

```bash
openyida connector test --connector-id <connector-id> --action <operationId>
```

测试后再次查询动作列表，确认动作没有被清空：

```bash
openyida connector list-actions <connector-id>
```

## 安全动作格式

### 无参数 GET 动作

```json
{
  "id": "operation-1",
  "operationId": "getAccessToken",
  "summary": "获取三色灯 Token",
  "description": "获取三色灯 Token",
  "url": "api/TriColorLamp/GetAccessToken",
  "method": "get",
  "inputs": [],
  "parameters": {},
  "responses": {
    "type": "object",
    "properties": {}
  },
  "outputs": [
    {
      "defaultValue": "{}",
      "desc": "响应体结构",
      "name": "Response",
      "paramType": "Object",
      "required": false
    }
  ],
  "origin": true
}
```

### 带 Query 参数的 GET 动作

```json
{
  "id": "operation-2",
  "operationId": "getDtuSnData",
  "summary": "获取单设备三色灯数据",
  "description": "根据 dtuSn 和日期获取三色灯数据",
  "url": "api/TriColorLamp/GetDtuSnData",
  "method": "get",
  "inputs": [
    {
      "childList": [
        {
          "componentName": "TextField",
          "desc": "设备 dtuSn",
          "name": "dtuSn",
          "queryDefaultValue": {
            "paramType": "fixedValue",
            "defaultValue": ""
          },
          "required": true
        },
        {
          "componentName": "TextField",
          "desc": "查询日期，可为空",
          "name": "date",
          "queryDefaultValue": {
            "paramType": "fixedValue",
            "defaultValue": ""
          },
          "required": false
        }
      ],
      "desc": "请求参数",
      "name": "Query",
      "paramType": "Object",
      "required": false
    }
  ],
  "parameters": {
    "query": [
      {
        "name": "dtuSn",
        "type": "string",
        "required": true,
        "description": "设备 dtuSn",
        "queryDefaultValue": {
          "paramType": "fixedValue",
          "defaultValue": ""
        }
      },
      {
        "name": "date",
        "type": "string",
        "required": false,
        "description": "查询日期，可为空",
        "queryDefaultValue": {
          "paramType": "fixedValue",
          "defaultValue": ""
        }
      }
    ]
  },
  "responses": {
    "type": "object",
    "properties": {}
  },
  "outputs": [
    {
      "defaultValue": "{}",
      "desc": "响应体结构",
      "name": "Response",
      "paramType": "Object",
      "required": false
    }
  ],
  "origin": true
}
```

## 字段规则

| 字段 | 推荐写法 |
| --- | --- |
| `id` | 使用 `operation-1`、`operation-2` 等顺序编号 |
| `operationId` | 使用前端函数名或后端 Action 名，例如 `getDtuSnData` |
| `summary` | 中文短名称，用于宜搭界面展示 |
| `description` | 一句话说明动作用途 |
| `url` | 不带域名，只写连接器域名后的相对路径 |
| `method` | 小写，例如 `get`、`post`、`put` |
| `inputs` | GET 参数只放 `Query`，不要放 `Body` |
| `parameters.query` | 与 `inputs[].childList[]` 中的 query 参数保持一致 |
| `queryDefaultValue` | Query 参数建议在 `inputs` 和 `parameters` 两处都写 |
| `outputs` | 修复或首次生成时只保留根对象 `Response` |

## 避免测试面板崩溃

以下写法容易导致宜搭连接器测试面板解析异常，应谨慎使用：

- 未确认平台兼容时，在输出字段里展开复杂 `Code`、`Message`、`Data` 子字段
- 给输入/输出叶子节点添加非必要的 `label`
- GET 动作配置 `Body`
- `inputs` 与 `parameters` 中的 query 参数不一致
- 中文 JSON 未按 UTF-8 读取或保存
- 只追加部分动作，遗漏原有 Token/Test 动作，导致重建后动作列表不完整

当用户反馈“点击测试后操作都不见了”，优先按修复流程处理，不要继续追加同一份风险 JSON。

## ASP.NET Controller 映射规则

对于 ASP.NET Controller：

```csharp
[Route("api/[controller]/[action]")]
public class TriColorLampController : ControllerBase
```

路径映射为：

```text
api/TriColorLamp/<ActionName>
```

规则：

- `[HttpGet]`、`[HttpPost]`、`[HttpPut]` 映射到对应小写 method
- `[FromQuery]` 参数映射为 `Query`
- 有默认值或可空参数，例如 `string date = null`，映射为 `required: false`
- 无默认值的必填参数映射为 `required: true`

## 故障修复流程

当连接器动作被清空时：

1. 执行 `openyida connector detail <connector-id>`，确认连接器仍存在。
2. 执行 `openyida connector list-actions <connector-id>`，确认动作是否为 0。
3. 从前端 API 文件和后端 Controller 重新生成完整动作列表。
4. 如果原来有 Token/Test 动作，也要一起放回 JSON。
5. 使用保守输出结构，不展开复杂响应字段。
6. 执行 `add-action --confirm` 重建动作。
7. 执行 `list-actions` 验证动作数量。
8. 用 `connector test --action <operationId>` 测试至少一个无参数动作。
9. 测试后再次执行 `list-actions`，确认动作没有再次消失。
