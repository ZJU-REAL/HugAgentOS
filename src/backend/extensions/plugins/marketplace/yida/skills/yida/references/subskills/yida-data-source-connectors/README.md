
# 宜搭连接器数据源接入规范

## 核心规则

在宜搭应用页面里调用连接器操作或远程 API 时，必须先在设计器“数据源”面板创建对应的数据源，再在页面代码里通过 `this.dataSourceMap.<数据源名>.load()` 调用。

禁止在自定义页面里直接用 `fetch`、`XMLHttpRequest`、`/query/newconnector/testConnector.json`、`ConnectorFactory.testConnector` 或手写远程 URL 绕过设计器数据源。例外只允许用于一次性本地诊断，不得发布到正式页面 Schema。

官方示例中心的 schema 回读规律见 [官方示例中心 Schema 范式](../../references/official-example-schema-patterns.md)。其中连接器调用有时会被平台归一为 `REMOTE + /query/publicService/invokeService.json + serviceInfo.connectorInfo`，因此验收时既看设计器数据源是否存在，也要识别这种归一形态。

## 适用场景

- 自定义页面、Dashboard、数据大屏读取外部系统接口。
- 页面需要调用宜搭 HTTP 连接器动作，例如获取 token、查询设备列表、查询状态、提交指令。
- 用户要求“把连接器操作添加到页面数据源”“左侧数据源里要能看到连接器”“远程 API 不要写死在 JSX 里”。
- 修复页面一直卡在“加载中”，且原因是代码绕过数据源直接请求连接器或外部域名。

> ⚠️ 子表内嵌明细只返回 50 行，应使用 `openyida data query subform` 按 `formInstId + tableFieldId` 分页查询，不要为此新建连接器数据源。

## 与其他技能的分工

- 创建 HTTP 连接器本体、账号、动作列表：使用 `yida-connector`。
- 从 API 文件或后端 Controller 生成安全动作 JSON：使用 `yida-connector-safe-actions`。
- 查询表单、流程、任务或子表数据：使用 `yida-data-management`。
- 编写自定义页面 UI 和生命周期：使用 `yida-custom-page`。
- 发布页面：使用 `yida-publish-page`，发布后确认数据源仍被保留或补回。

## 实施流程

1. 确认连接器动作存在。

```bash
openyida connector detail <connector-id>
openyida connector list-actions <connector-id>
```

2. 为页面规划数据源名称。

命名使用业务语义，建议小驼峰，例如：

- `tricolorGetToken`
- `tricolorGetUserDtuSns`
- `tricolorGetDtuSnStateList`

官方示例里高频数据源类别包括表单查询、任务列表、流程发起、保存/更新/删除、连接器动作。OpenYida 生成时应把每个远程能力设计为独立数据源，并为页面状态保留明确字段：

| 能力 | 数据源命名建议 | 状态字段建议 |
| --- | --- | --- |
| 查询列表 | `get<Biz>List` | `loading`、`tableData/list`、`currentPage`、`pageSize`、`totalCount`、`filters/searchFieldJson` |
| 查询详情 | `get<Biz>ById` | `detailLoading`、`currentRecord` |
| 保存/更新 | `save<Biz>` / `update<Biz>` | `submitting`、`dialogVisible` |
| 删除/批量删除 | `delete<Biz>` / `batchDelete<Biz>` | `selectedRowKeys`、`deleting` |
| 连接器动作 | `<service><Action>` | 与动作结果同名的 `result` / `rawResult` |

3. 在页面 Schema 的 Page 根节点 `dataSource.online` 中登记连接器数据源。

数据源必须满足：

- `dpType: "YIDACONNECTOR"`
- `protocal: "REMOTE"`
- `requestHandler.value: "this.utils.legaoBuiltin.dataSourceHandler"`
- `options.connector` 指向连接器名，例如 `Http_xxx`
- `options.connectorAction.value` 使用动作 `operationId`
- `options.params.inputs` 包含 `Headers`、`Query`、`Body`
- `options.shouldFetch: false`，由页面代码按需触发
- `options.didFetch` 必须返回处理后的 content；返回结构不稳定时做归一化
- `options.onError` 必须 toast 具体数据源/动作名，并让页面加载态恢复

发布后回读 schema 时，平台可能把连接器数据源归一成以下只读形态；这是可接受的，但不要在源码里写死 `_csrf_token`：

- `dpType: "REMOTE"` / `protocal: "REMOTE"`
- `options.url` 为 `/query/publicService/invokeService.json?...`
- `options.params.serviceInfo` 内含 `connectorInfo.connectorId`、`actionId`、`type`、`connection`
- `requestHandler.value` 仍是 `this.utils.legaoBuiltin.dataSourceHandler`

4. 页面代码只调用数据源。

```javascript
export function loadConnectorDataSource(dataSourceName, headers, query, body) {
  var dataSource = this.dataSourceMap && this.dataSourceMap[dataSourceName];
  if (!dataSource || !dataSource.load) {
    return Promise.reject(new Error('页面数据源不存在：' + dataSourceName));
  }
  return dataSource.load({
    inputs: JSON.stringify({
      Headers: headers || {},
      Query: query || {},
      Body: body || {}
    })
  });
}
```

5. 所有调用必须有可恢复的加载态。

- 请求失败或超时后必须 `loading: false`。
- 错误必须显示到页面或 toast，不能只写 `console.log`。
- 对连接器调用包一层超时控制，避免页面永久停在“加载中”。

## 发布和回读验证

发布后必须回读 Schema，确认数据源仍在 Page 根节点：

```bash
openyida publish <src> <appType> <formUuid> --health-check
openyida get-schema <appType> <formUuid>
```

如需保存回读 Schema，使用 create_file / Write / file edit tool 创建 `<projectRoot>/.cache/openyida/<项目名或任务名>/<page>-schema.json`；从 workspace 根执行后续命令时路径加 `project/` 前缀。不要使用 shell 重定向。

检查点：

- 设计器左侧“数据源”能看到新增连接器数据源。
- `dataSource.online` 中能看到显式连接器数据源，或回读为 `REMOTE + publicService/invokeService + serviceInfo.connectorInfo` 的平台归一形态。
- `actions.module.source` 中没有 `ConnectorFactory.testConnector`、`newconnector/testConnector`、外部 API 域名直连代码。
- 页面运行时使用 `this.dataSourceMap.<name>.load()`。

## 反模式

不要发布以下写法：

```javascript
fetch('https://api.example.com/data');
new XMLHttpRequest();
postYidaForm('/query/newconnector/testConnector.json?_api=ConnectorFactory.testConnector', payload);
```

这些写法会导致设计器数据源不可见、权限和参数不可审计，也容易出现跨域、CSRF、预览态卡死或“加载中”无法恢复的问题。

## PR/验收清单

- 页面 Schema 已包含连接器数据源。
- 页面代码通过 `this.dataSourceMap` 调用。
- 本地执行 `openyida check-page` 和 `openyida compile` 通过。
- 发布后执行 `openyida get-schema` 回读验证数据源存在。
- 若页面仍报错，错误文案应暴露具体数据源名称或连接器动作名。
