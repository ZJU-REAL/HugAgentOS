# 宜搭报表 API 详解与踩坑记录

> 本文档是 `yida-report` 技能的参考文档，记录报表 API 调用方式、数据解析、踩坑记录和最佳实践。

## 报表 API 详解

### 接口地址

```
POST /alibaba/web/{appType}/visual/visualizationDataRpc/getDataAsync.json
```

### 关键参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `pageName` | String | 是 | 固定值 `"report"` |
| `prdId` | String | 是 | 报表的 prdId（从报表 URL 中获取） |
| `cid` | String | 是 | 报表组件 ID（如 `YoushuTable_mmx9ha6ar`） |
| `cname` | String | 是 | 组件名称（如 `"按状态统计"`） |
| `className` | String | 是 | 组件类名（如 `"YoushuTable"`、`"YoushuSimpleIndicatorCard"`） |
| `dataSetKey` | String | 是 | 数据集 key（表格用 `"table"`，指标卡用 `"youshuData"`） |

### 请求示例

```javascript
var requestBody = {
  pageName: "report",
  prdId: "13085982",
  cid: "YoushuTable_mmx9ha6ar",
  cname: "按状态统计",
  className: "YoushuTable",
  dataSetKey: "table",
};

this.utils.yida.request({
  url: "/alibaba/web/" + APP_TYPE + "/visual/visualizationDataRpc/getDataAsync.json",
  method: "POST",
  data: requestBody,
});
```

### 返回数据结构

```json
{
  "content": {
    "data": [
      ["进行中", 8],
      ["已完成", 5],
      ["规划中", 4],
      ["已延期", 2],
      ["已取消", 1]
    ],
    "meta": [
      {
        "alias": "项目状态",
        "dataType": "STRING",
        "type": "DIMENSION"
      },
      {
        "alias": "项目数量",
        "dataType": "LONG",
        "type": "MEASURE"
      }
    ]
  }
}
```

### 数据解析方法

```javascript
function parseTableData(responseData) {
  var dataArray = responseData.data || [];
  var metaArray = responseData.meta || [];

  var dimensionIndex = -1;
  var measureIndex = -1;
  var dimensionAlias = "";
  var measureAlias = "";

  metaArray.forEach(function(m, i) {
    if (m.type === "DIMENSION") {
      dimensionIndex = i;
      dimensionAlias = m.alias;
    } else if (m.type === "MEASURE") {
      measureIndex = i;
      measureAlias = m.alias;
    }
  });

  // 如果 meta 没有 type 字段，按顺序推断：第一个是维度，第二个是度量
  if (dimensionIndex === -1 && metaArray.length >= 2) {
    dimensionIndex = 0;
    measureIndex = 1;
    dimensionAlias = metaArray[0].alias;
    measureAlias = metaArray[1].alias;
  }

  return dataArray.map(function(row) {
    return {
      name: String(row[dimensionIndex] || ""),
      value: parseFloat(row[measureIndex]) || 0,
    };
  });
}
```

---

## 报表组件类型

### YoushuSimpleIndicatorCard（指标卡）

- **用途**：显示单个聚合数值（如项目总数、总预算）
- **dataSetKey**：`"youshuData"`
- **返回格式**：`{ content: { data: [[42]], meta: [...] } }`
- **取值方式**：`data[0][0]`

### YoushuTable（统计表格）

- **用途**：按维度分组统计（如按状态统计项目数）
- **dataSetKey**：`"table"`
- **返回格式**：`{ content: { data: [["进行中", 8], ["已完成", 5]], meta: [...] } }`
- **取值方式**：遍历 `data` 数组，每行 `[维度值, 度量值]`

---

## 🔥 踩坑记录

### 坑 1：数值聚合必须使用 NumberField

**现象**：报表对"项目预算"字段做 SUM 聚合时返回 0 或报错。

**原因**：预算字段使用了 `TextField`（文本组件），文本类型无法进行数值聚合（SUM、AVG 等）。

**解决**：将字段类型从 `TextField` 改为 `NumberField`。

```bash
openyida create-form update <appType> <formUuid> '[
  {"action":"delete","fieldId":"textField_j2xeja4e"},
  {"action":"add","field":{"type":"NumberField","label":"项目预算","placeholder":"请输入预算金额"}}
]'
```

⚠️ 注意：改字段类型后，旧数据中该字段的值会丢失，需要重新写入。

### 坑 2：报表 API 路径

**错误路径**：
```
❌ /yida-report/data/queryReportData.json
❌ /alibaba/web/{appType}/query/reportData.json
```

**正确路径**：
```
✅ /alibaba/web/{appType}/visual/visualizationDataRpc/getDataAsync.json
```

### 坑 3：prdId 获取方式

**prdId 不是 formUuid**，而是报表页面 URL 中的数字 ID。

获取方式：
1. 在浏览器中打开报表页面
2. 打开开发者工具 → Network
3. 搜索 `getDataAsync` 请求
4. 在请求参数中找到 `prdId` 值

### 坑 4：组件 ID（cid）获取方式

每个报表组件都有唯一的 `cid`，格式为 `{组件类名}_{随机字符串}`。

获取方式：同上，在 Network 中查看 `getDataAsync` 请求的参数。

### 坑 5：dataSetKey 区分

| 组件类型 | dataSetKey |
|---------|-----------|
| `YoushuSimpleIndicatorCard`（指标卡） | `"youshuData"` |
| `YoushuTable`（统计表格） | `"table"` |

**用错 dataSetKey 会导致返回空数据。**

### 坑 6：searchFormDatas 没有 HTTP API

`searchFormDatas` 是宜搭前端 JS SDK 的接口（`this.utils.yida.searchFormDatas`），**不能**通过 HTTP 直接调用。

```
❌ POST /dingtalk/web/{appType}/query/punchFormDataProvider/searchFormDatas.json → 404
❌ POST /dingtalk/web/{appType}/v1/form/searchFormDatas.json → 超时
```

但 `saveFormData` 可以通过 HTTP 调用：
```
✅ POST /dingtalk/web/{appType}/query/punchFormDataProvider/saveFormData.json
```

### 坑 7：cookies.json 格式

openyida 的 `.cache/cookies.json` 中，cookies 是**数组**格式，不是字符串：

```json
{
  "cookies": [
    {"name": "cna", "value": "xxx", "domain": ".aliwork.com", "path": "/"},
    {"name": "tianshu_csrf_token", "value": "xxx", "domain": ".aliwork.com", "path": "/"}
  ],
  "base_url": "https://www.aliwork.com"
}
```

使用时需要拼接：
```javascript
var cookieStr = cookies.map(ck => ck.name + '=' + ck.value).join('; ');
```

### 坑 8：不要用 fallback 逻辑

**反模式**：报表 API 失败时回退到 `searchFormDatas` 前端聚合。

**问题**：`searchFormDatas` 的 pageSize 最大 100，数据量超过 100 条时聚合结果不准确。

**正确做法**：报表 API 失败时直接显示错误信息，方便排查问题。

---

## 可用的聚合函数

| 聚合函数 | 说明 | 适用字段类型 |
|---------|------|------------|
| `COUNT` | 计数 | 所有类型 |
| `COUNT_DISTINCT` | 去重计数 | 所有类型 |
| `SUM` | 求和 | `NumberField` |
| `AVG` | 平均值 | `NumberField` |
| `MIN` | 最小值 | `NumberField`、`DateField` |
| `MAX` | 最大值 | `NumberField`、`DateField` |

---

## 表单数据批量写入（HTTP API）

```javascript
const { httpPost } = require('lib/utils.js');
const querystring = require('querystring');

const postData = querystring.stringify({
  _csrf_token: csrfToken,
  formUuid: FORM_UUID,
  formDataJson: JSON.stringify({
    textField_xxx: '项目名称',
    numberField_xxx: 280,
    selectField_xxx: '进行中',
  }),
});

await httpPost(baseUrl, `/dingtalk/web/${APP_TYPE}/query/punchFormDataProvider/saveFormData.json`, postData, cookies);
```

**注意**：`cookies` 参数是数组格式 `[{name, value, domain, path}]`，`httpPost` 内部会自动拼接。

---

## 常见问题

**Q：报表 API 返回空数据？**
- 检查 `prdId` 是否正确
- 检查 `cid` 是否与报表中的组件匹配
- 检查 `dataSetKey` 是否正确（指标卡用 `youshuData`，表格用 `table`）

**Q：SUM 聚合返回 0？**
- 检查字段是否为 `NumberField` 类型
- `TextField` 无法进行数值聚合，必须改为 `NumberField`
- 改字段类型后旧数据会丢失，需要重新写入

**Q：如何调试报表 API？**
1. 在浏览器中打开报表页面
2. 打开 DevTools → Network → 搜索 `getDataAsync`
3. 查看请求参数和返回数据
4. 将参数复制到自定义页面代码中

**Q：ECharts 图表不显示？**
- 确认 ECharts CDN 加载成功：`https://g.alicdn.com/code/lib/echarts/5.5.0/echarts.min.js`
- 确认 DOM 元素已渲染后再调用 `echarts.init()`
- 使用 `setTimeout` 延迟初始化图表
