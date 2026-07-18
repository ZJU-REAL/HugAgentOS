# 官方示例中心 Schema 范式

> 来源：2026-06-01 对宜搭「开发者赋能平台 / 示例中心」capacity 标签下 156 个示例做只读 schema 抽取。本文只沉淀脱敏后的结构规律，不保存真实 cookie、CSRF token、用户信息或可直接复用的业务密钥。

## 对 OpenYida 技能的核心启发

官方示例中心反复出现的不是“每个需求都写一整页自定义代码”，而是以下范式：

1. **Schema 类型先行**：先看 `formType`、组件树、`dataSource.online`，再决定技能；不要只按卡片标签或标题选择技能。
2. **低代码配置优先**：能用表单字段、字段公式、字段联动、原生报表、流程规则、集成自动化表达的能力，不要默认落到自定义页面 JS。
3. **页面三层结构**：页面型能力通常拆成 `状态 VALUE 数据源`、`REMOTE/连接器数据源`、`薄动作/渲染层`，而不是把状态、请求和 UI 全塞进一个函数。
4. **单能力薄示例**：示例应用通常围绕一个能力闭环展开，先做可学习、可启用、可验证的小闭环，再扩展为完整业务应用。
5. **数据源可见可审计**：连接器和远程 API 通过设计器数据源进入 schema，页面代码只通过 `this.dataSourceMap.<name>.load()` 或 OpenYida 封装 API 调用。
6. **原生报表服务端聚合**：报表类示例优先用 `Youshu*` 原生报表组件和服务端聚合；自定义看板只负责展示和交互增强。

把这些范式反哺技能时，核心不是复制官方 schema 字段，而是改进 Agent 的默认决策：先分类、再选择低代码承载面、最后只在必要处写自定义代码。

## 只读抓取链路

当用户要求“蒸馏示例中心”“参考官方示例”“把示例 schema 反哺技能”时，优先抓 schema，而不是只看截图或卡片文案。

1. 示例中心页面本身是宜搭自定义页面，可通过只读接口获取页面 schema：

```text
GET /bench/getLatestFormWithNavNew.json?formUuid=coe
```

2. 示例列表来自模板中心数据源：

```text
GET /query/loginFreeFormData/listFormDataByType.json?type=templateCenter
```

关键参数：

```json
{
  "pageSize": 24,
  "currentPage": 1,
  "userLanguage": "zh_CN",
  "searchFieldJson": "{\"isShow\":\"n\",\"tags\":\"capacity\",\"templateTitle\":\"\",\"description\":\"\"}",
  "dynamicOrder": "{\"orderNum\":\"+\"}"
}
```

3. 通过模板 ID 获取源模板应用：

```text
GET /query/appTpl/getAppTplComplexInfo.json?appTplUuid=TPL_XXX
```

读取 `content.latestOnlineAppTpl.appType`，不要调用 `copyTemplateApp`，那会创建真实应用。

4. 打开模板只读预览页，从 HTML 的 `window.pageConfig.formUuid` 读取默认展示页：

```text
https://template.aliwork.com/{APP_TYPE}/workbench/?appTplUuid=TPL_XXX&__yida_hide_enable_template__=true
```

5. 通过模板域名读取页面 schema：

```text
GET https://template.aliwork.com/alibaba/web/{APP_TYPE}/query/formdesign/getSchemaWithAllNavs.json?formUuid={FORM_UUID}
```

只读抓取可以落到 `.cache/openyida/<项目名>/`，不要把原始 schema、cookie、token 或模板详情提交进仓库。

## 本轮样本分布

156 个 capacity 示例全部能通过上述链路拿到默认访问页 schema。默认页类型分布：

| 类型 | 数量 | 说明 |
| --- | ---: | --- |
| `receipt` | 80 | 普通表单页，常见于表单、公式、集成和连接器示例 |
| 自定义展示页 | 60 | `content.pages[0].formType` 为空，通常是自定义页面/数据看板/工具页 |
| `report` | 10 | 原生报表页，组件多为 `Youshu*` |
| `process` | 5 | 流程表单页 |
| `view` | 1 | 视图页 |

标签不能直接等同于 schema 类型。例如 `report` 标签下既有原生报表，也有表单数据准备页和自定义看板；`integration` 标签下大多默认打开表单页，自动化逻辑流本体不在默认页面 schema 内。选择 OpenYida 子技能时先看 `formType`、组件树和数据源，再看标签。

组件分布反映了官方默认承载方式：

| 类型 | 高频组件 | 范式解释 |
| --- | --- | --- |
| 表单页 | `FormContainer`、`TextField`、`NumberField`、`DateField`、`EmployeeField`、`TableField`、`RichText` | 字段结构、公式和少量说明文本承载能力 |
| 自定义页 | `Div`、`Text`、`Image`、`Button`、`PageSection`、`Dialog`、`TablePc`、`Pagination` | 列表、看板、工具页、弹窗和分页承载交互 |
| 报表页 | `YoushuPageHeader`、`YoushuTopFilterContainer`、`YoushuSelectFilter`、`YoushuTable`、`YoushuPieChart` | 原生报表负责筛选、聚合和图表 |
| 流程页 | `FormContainer` + 表单字段 | 表单只承载数据，审批节点由流程规则承载 |

## 示例中心自身的页面模式

示例中心 `coe` 页面是一个典型“模板列表 + 筛选 + 搜索 + 分页 + 点赞 + 启用弹窗”的自定义页面。它的 schema 规律适合反哺列表页、资源中心、模板中心类页面：

- `VALUE` 数据源保存页面状态：`currentPage`、`pageSize`、`searchWord`、`filterValue`、`tags`、`loading`、`tplItem`、`tplInfo` 等。
- 列表数据源调用 `listFormDataByType.json?type=templateCenter`，参数由 `state` 组合并 JSON 化。
- 搜索条件使用 `searchFieldJson`，例如 `isShow`、`tags`、`templateTitle`、`description`。
- 排序使用 `dynamicOrder`，例如 `{"orderNum":"+"}`。
- 详情弹窗先按 `appTplUuid` 查模板记录，再查 `getAppTplComplexInfo.json`。
- 点赞和启用记录通过独立数据源写表；启用模板通过 `copyTemplateApp`，属于有副作用接口，未经用户明确确认不要调用。

## 自定义页面三层范式

官方自定义页反复使用三层模型：

| 层 | schema 形态 | OpenYida 生成时的落点 |
| --- | --- | --- |
| 状态层 | `VALUE` 数据源：`loading`、`pageSize`、`currentPage`、`tableData`、`searchFieldJson`、`selectedRowKeys` | `_customState` 固定字段，`setCustomState()` 更新 |
| 数据层 | `REMOTE` / 连接器数据源：表单查询、任务、流程、保存、删除、连接器动作 | 优先 `this.dataSourceMap.<name>.load()`；否则用 `this.utils.yida.*` |
| 交互层 | `Div/Text/Button/TablePc/Dialog/Pagination/Tabs/Collapse` + 少量 JS 方法 | `renderJsx` 只做展示和事件分发，业务方法独立 `export function` |

因此，生成列表/看板/工具页时，默认先设计状态字典和数据源清单，再写 JSX。不要一开始就写一个巨大的 `renderJsx`。

## 自定义页面数据源模式

官方自定义页面 schema 中常见 `dataSource.online` 结构：

```json
{
  "name": "getData",
  "protocal": "REMOTE",
  "dpType": "REMOTE",
  "type": "legao",
  "isInit": true,
  "requestHandler": {
    "type": "JSExpression",
    "value": "this.utils.legaoBuiltin.dataSourceHandler"
  },
  "options": {
    "method": "GET",
    "url": {
      "type": "variable",
      "variable": "`/${window.pageConfig.appType || window.g_config.appKey}/v1/form/searchFormDatas.json`"
    },
    "params": {
      "type": "variable",
      "variable": "{ formUuid: state.formUuid, pageSize: state.pageSize, currentPage: state.currentPage }"
    },
    "didFetch": {
      "source": "function didFetch(content) { return content; }"
    },
    "onError": {
      "source": "function onError(error) { this.utils.toast({ title: error.message, type: 'error' }); }"
    }
  },
  "dataHandler": {
    "source": "function(data, err) { this.setState({ getData: data }); return data; }"
  }
}
```

落到 OpenYida 手写 `.oyd.jsx` 时，不要照抄低代码 schema 里的 `this.setState`。应转换为：

- 页面状态放在 `_customState`，用 `setCustomState()` 合并更新。
- 读取宜搭表单/流程/任务数据时优先用 `this.utils.yida.*` 或已建好的 `this.dataSourceMap.<name>.load()`。
- 远程连接器和第三方 API 必须走设计器数据源，不要在 JSX 里直接 `fetch` 外部地址。
- 列表页保持 `loading / list / currentPage / pageSize / totalCount / filters / selectedRowKeys` 的稳定状态结构，失败时把 `loading` 置回 `false` 并 toast。
- 需要搜索或筛选时优先形成 `searchFieldJson` 状态，提交前再 JSON 化；不要把散落的筛选字段硬写进多个请求函数。

## 连接器数据源回读形态

连接器类示例的默认页 schema 中，部分数据源会被平台归一为普通 `REMOTE` 数据源：

```json
{
  "protocal": "REMOTE",
  "dpType": "REMOTE",
  "type": "legao",
  "options": {
    "method": "POST",
    "url": "/query/publicService/invokeService.json?_csrf_token=***",
    "params": {
      "inputs": "{\"Headers\":{},\"Query\":{},\"Body\":{}}",
      "serviceInfo": "{\"connectorInfo\":{\"connectorId\":\"Http_xxx\",\"actionId\":\"operationId\",\"type\":\"httpConnector\",\"connection\":123}}"
    }
  }
}
```

因此：

- 新建页面数据源仍按 `yida-data-source-connectors` 技能规划，让设计器能看到数据源和连接器动作。
- 发布后回读 schema 时，既要接受显式 `YIDACONNECTOR` 形态，也要接受 `REMOTE + publicService/invokeService + serviceInfo.connectorInfo` 这种平台归一形态。
- 任何 `_csrf_token` 都来自运行时页面配置，不能写死到技能文档或页面源码。

## 原生报表与自定义看板边界

原生报表页 schema 通常满足：

- `formType: "report"`。
- 组件树包含 `YoushuPageHeader`、`YoushuTopFilterContainer`、`YoushuSelectFilter`、`YoushuTable`、`Youshu*Chart` 等。
- 标签为 `report` 但 `formType` 不是 `report` 时，往往只是报表数据准备页或自定义页面看板。

落地规则：

- 创建或改造原生报表优先用 `yida-report`，不要手写 `Youshu*` schema。
- 需要更强视觉和交互时，先用原生报表做服务端聚合，再用 `yida-chart`/`yida-custom-page` 渲染。
- 不要在自定义页面前端分页拉全量表单数据做大规模聚合。

## 表单、公式、流程与集成边界

- 公式示例主要是 `receipt` 或 `process` schema，公式通常落在字段属性 `valueType: "formula"`、`complexValue.formula`、`formula`，字段引用必须是 `#{fieldId}`。
- 流程示例默认页可能是表单页，也可能是自定义展示页；审批流节点和规则不一定在默认页面 schema 内，应改用 `yida-process-rule` 查询或配置。
- 集成自动化示例默认页多是触发表单或说明页；逻辑流本体不在默认页面 schema 内，应改用 `yida-integration` 查询/创建。
- 连接器示例默认页只能证明页面如何调用数据源，连接器本体和动作定义仍由 `yida-connector` / `yida-connector-safe-actions` 管理。

## 技能选择速查

| schema 观察 | 优先技能 |
| --- | --- |
| `formType: "receipt"`，组件多为 `TextField` / `NumberField` / `TableField` | `yida-create-form-page`，公式需求再用 `yida-formula` |
| `formType: "process"` | `yida-create-process` / `yida-process-rule` |
| `formType: "report"` 或组件为 `Youshu*` | `yida-report` |
| 自定义展示页，组件为 `Div` / `Text` / `Button` / `TablePc` / `Dialog` | `yida-custom-page` |
| 自定义页面里有 `dataSource.online` 的远程接口 | `yida-data-source-connectors` + `yida-custom-page` |
| `publicService/invokeService` 或 `serviceInfo.connectorInfo` | `yida-connector` / `yida-data-source-connectors` |
| 默认页看不到逻辑流，但标签是 `integration` | `yida-integration` |
