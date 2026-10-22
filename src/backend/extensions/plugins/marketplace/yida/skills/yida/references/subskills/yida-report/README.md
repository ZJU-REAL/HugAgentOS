
# 宜搭原生报表技能

## 严格禁止 (NEVER DO)

- 不要在前端直接聚合表单数据，必须通过宜搭原生报表的 `getDataAsync.json` 或 `getCacheData.json` 接口获取聚合数据
- 不要编造 `reportId`、`datasetId`、`fieldId`，必须从报表 Schema 或 URL 中提取
- 不要将本技能与 `yida-chart` 混淆：本技能负责创建原生报表（数据源），`yida-chart` 负责 ECharts 可视化
- 不要在没有原生报表的情况下直接使用 ECharts，ECharts 必须依赖原生报表作为数据源
- 不要用 shell heredoc、`cat`/`echo`/`printf`/`tee` 或重定向生成报表配置 JSON

## 严格要求 (MUST DO)

- **创建/发布前必须确认**：执行报表创建或发布操作前，必须向用户展示报表配置摘要（图表类型、数据源、字段映射），获得用户明确同意后再执行
- 普通"报表"、"统计"需求默认使用本技能，不要直接跳到 `yida-chart`
- 参考官方示例时先确认 schema 证据：只有 `formType: "report"` 或组件树出现 `Youshu*` 报表组件时才按原生报表处理；`report` 标签但默认页是 `receipt` 或自定义页时，先判断是否只是数据准备页或看板页
- 调用报表数据 API 前必须确认 `reportId` 和 `datasetId` 来自真实报表
- 解析报表数据时必须处理 `data.rows` 为空的情况，避免页面崩溃
- 报表配置 JSON 需要落盘时，必须用结构化文件写入工具创建到 `<projectRoot>/.cache/openyida/<项目名或任务名>/`，例如 `<projectRoot>/.cache/openyida/pm/pm-report-team.json`；不要在仓库根目录、系统临时目录或 `.cache/` 顶层生成 `*-report*.json`
- 本技能不读写 memory，报表配置通过 `openyida create-report` 命令写入宜搭平台，不依赖跨会话的 memory 状态

## 适用场景

| 用户意图 | 触发条件 |
|---------|---------|
| 普通报表/统计需求 | "报表"、"统计"、"数据分析"（默认使用本技能） |
| 读取报表聚合数据 | 调用 `getDataAsync.json` / `getCacheData.json` |
| 为 ECharts 提供数据源 | 先用本技能创建原生报表，再用 `yida-chart` 可视化 |

## 触发条件

**正向触发**：
- "报表"、"统计"、"数据分析"（默认使用本技能）
- "创建报表"、"生成统计图表"
- 为 ECharts 可视化提供数据源

> ⚠️ 严禁在前端直接聚合表单数据，必须通过原生报表 `getDataAsync.json` 或 `getCacheData.json` 获取聚合数据。

## 异常处理

| 异常场景 | 处理方式 |
|---------|----------|
| reportId/datasetId 不存在 | 不得编造，必须从报表 URL 或 Schema 中提取 |
| 报表数据 rows 为空 | 必须处理空数据情况，显示"暂无数据"而非页面崩溃 |
| 前端直接聚合表单数据 | 严禁，必须通过 `getDataAsync.json` 或 `getCacheData.json` 获取聚合数据 |
| 命令执行失败 | 检查登录态（`openyida env`），确认 appType 和 formUuid 正确 |

---


## 概述

本技能记录了通过宜搭原生报表（YoushuTable）的聚合数据驱动 ECharts 图表看板的完整实践经验，包含 API 调用方式、数据解析、踩坑记录和最佳实践。

---

## 核心架构

```
宜搭表单（数据源）
    ↓ 数据写入
宜搭原生报表（服务端聚合）
    ↓ 报表 API
ECharts 自定义页面（前端渲染）
```

官方示例中心的报表范式是“原生报表先聚合，自定义页面后增强”。因此，除非用户明确要做高级视觉看板，否则先创建或复用原生报表；只有在已有报表提供聚合数据后，再让 `yida-chart` 或 `yida-custom-page` 承担展示层。

**为什么不用 `searchFormDatas` 前端聚合？**

| 对比项 | `searchFormDatas` 前端聚合 | 原生报表 API |
|--------|--------------------------|-------------|
| 数据准确性 | ❌ pageSize 最大 100，数据量大时不完整 | ✅ 服务端聚合，数据 100% 准确 |
| 性能 | ❌ 需要分页拉取全部数据再前端计算 | ✅ 服务端直接返回聚合结果 |
| 适用场景 | 数据量 < 100 条的简单统计 | 任意数据量的聚合统计 |

---

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

> 📖 请求示例、返回数据结构、数据解析方法、踩坑记录（8条）、聚合函数、常见问题详见 [references/report-api-guide.md](references/report-api-guide.md)，按需读取。

---

## 原生报表 Schema 构建（vc-yida-report）

### 概述

宜搭提供了原生报表组件库 `vc-yida-report`，包含 **16 种**开箱即用的报表组件，涵盖图表、表格、筛选器、指标卡等。Agent 应通过 `openyida create-report` 传入结构化图表配置，由 CLI 内部构建并发布 Schema，不要尝试读取或手写 `build-yida-report-schema.js`。

- **组件库地址**：`//g.alicdn.com/code/npm/@ali/vc-yida-report/1.0.101/pc.js`
- **全局挂载**：`window.YidaReport`
- **创建入口**：`openyida create-report <appType> "<报表名称>" <配置JSON文件路径>`
- **字段配置参考**：[`report-field-config-guide.md`](../../references/report-field-config-guide.md)

### 组件总览

| 组件名 | 中文名 | 构建函数 | 类型 |
|--------|--------|---------|------|
| `YoushuSimpleIndicatorCard` | 指标卡 | `buildSchema.simpleIndicatorCard()` | KPI 展示 |
| `YoushuLineChart` | 折线图 | `buildSchema.lineChart()` | 图表 |
| `YoushuPieChart` | 饼图 | `buildSchema.pieChart()` | 图表 |
| `YoushuGroupedBarChart` | 分组条形图 | `buildSchema.groupedBarChart()` | 图表 |
| `YoushuFunnelChart` | 漏斗图 | `buildSchema.funnelChart()` | 图表 |
| `YoushuGauge` | 仪表盘 | `buildSchema.gauge()` | 图表 |
| `YoushuRadarChart` | 雷达图 | `buildSchema.radarChart()` | 图表 |
| `YoushuHeatmap` | 热力图 | `buildSchema.heatmap()` | 图表 |
| `YoushuCalendarHeatmap` | 日历热力图 | `buildSchema.calendarHeatmap()` | 图表 |
| `YoushuComboChart` | 组合图 | `buildSchema.comboChart()` | 图表 |
| `YoushuWordCloud` | 词云图 | `buildSchema.wordCloud()` | 图表 |
| `YoushuMap` | 地图 | `buildSchema.map()` | 图表 |
| `YoushuCrossPivotTable` | 交叉透视表 | `buildSchema.crossPivotTable()` | 表格 |
| `YoushuTable` | 基础表格 | `buildSchema.table()` | 表格 |
| `YoushuPageHeader` | 页面标题栏 | `buildSchema.pageHeader()` | 布局 |
| `YoushuTopFilterContainer` | 顶部筛选容器 | `buildSchema.topFilterContainer()` | 筛选 |
| `YoushuSelectFilter` | 下拉筛选器 | `buildSchema.selectFilter()` | 筛选 |
| `YoushuTimeFilter` | 时间筛选器 | `buildSchema.timeFilter()` | 筛选 |
| `YoushuInputFilter` | 区间筛选器 | `buildSchema.inputFilter()` | 筛选 |

### 核心工具函数

Schema 构建脚本提供以下核心函数，使用前需先引入：

```javascript
const {
  buildSchema,           // 各组件 Schema 构建器
  buildDataSetEntry,     // 构建数据集配置
  buildFieldDefinition,  // 构建字段定义
  buildFilterItem,       // 构建过滤条件
  buildOrderByItem,      // 构建排序项
  generateComponentId,   // 生成唯一组件 ID
} = require('./build-yida-report-schema');
```

### 数据集配置（dataSetModelMap）

所有报表组件通过 `dataSetModelMap` 配置数据源，每个数据集由 `buildDataSetEntry` 构建：

```javascript
buildDataSetEntry({
  cubeCode: 'your_cube_code',        // 数据集 cubeCode（必填）
  fieldDefinitionList: [              // 字段定义列表
    buildFieldDefinition({
      alias: 'date',                  // 字段别名（fieldKey）
      aliasName: '日期',              // 字段显示名称
      isDim: true,                    // true=维度，false=度量
      dataType: 'DATE',              // STRING | NUMBER | DATE | BOOLEAN | ARRAY
      aggregateType: 'NONE',         // NONE | SUM | AVG | COUNT | MAX | MIN | COUNT_DISTINCT
      timeGranularityType: 'DAY',    // YEAR | QUARTER | MONTH | WEEK | DAY | HOUR | MINUTE | null
    }),
    buildFieldDefinition({
      alias: 'sales',
      aliasName: '销售额',
      isDim: false,
      dataType: 'NUMBER',
      aggregateType: 'SUM',
    }),
  ],
  fieldList: ['date', 'sales'],       // 查询字段别名列表
  filterList: [],                     // 过滤条件列表
  orderByList: [                      // 排序列表
    buildOrderByItem('date', 'ASC'),  // ASC | DESC
  ],
  limit: 1000,                        // 数据条数限制
  // 以下为筛选器专用参数
  cubeCodes: ['your_cube_code'],      // cubeCode 数组（筛选器使用）
  valueField: ['field_alias'],        // 值字段数组（筛选器使用）
  labelField: ['field_alias'],        // 显示字段数组（筛选器使用）
})
```

### 常用组件 Schema 构建示例

#### 指标卡

```javascript
buildSchema.simpleIndicatorCard({
  dataSetModelMap: {
    kpi_dataset: buildDataSetEntry({
      cubeCode: 'your_cube_code',
      fieldDefinitionList: [
        buildFieldDefinition({ alias: 'total_sales', aliasName: '总销售额', isDim: false, dataType: 'NUMBER', aggregateType: 'SUM' }),
        buildFieldDefinition({ alias: 'order_count', aliasName: '订单数', isDim: false, dataType: 'NUMBER', aggregateType: 'COUNT' }),
      ],
      fieldList: ['total_sales', 'order_count'],
    })
  },
  settings: { columnCount: 4, columnCountForH5: 2 }
})
```

#### 折线图

```javascript
buildSchema.lineChart({
  dataSetModelMap: {
    line_dataset: buildDataSetEntry({
      cubeCode: 'your_cube_code',
      fieldDefinitionList: [
        buildFieldDefinition({ alias: 'date', aliasName: '日期', isDim: true, dataType: 'DATE', timeGranularityType: 'DAY' }),
        buildFieldDefinition({ alias: 'sales', aliasName: '销售额', isDim: false, dataType: 'NUMBER', aggregateType: 'SUM' }),
      ],
      fieldList: ['date', 'sales'],
      orderByList: [buildOrderByItem('date', 'ASC')],
    })
  },
  settings: {
    titleConfig: { label: '销售趋势' },
    height: 400,
    smooth: true,
    drillDown: false,
  }
})
```

#### 饼图（环形图）

```javascript
buildSchema.pieChart({
  dataSetModelMap: {
    pie_dataset: buildDataSetEntry({
      cubeCode: 'your_cube_code',
      fieldDefinitionList: [
        buildFieldDefinition({ alias: 'category', aliasName: '类别', isDim: true, dataType: 'STRING' }),
        buildFieldDefinition({ alias: 'amount', aliasName: '金额', isDim: false, dataType: 'NUMBER', aggregateType: 'SUM' }),
      ],
      fieldList: ['category', 'amount'],
    })
  },
  settings: {
    titleConfig: { label: '销售占比' },
    innerRadius: 0.6,  // >0 为环形图
  }
})
```

#### 基础表格（明细表）

```javascript
buildSchema.table({
  dataSetModelMap: {
    table: buildDataSetEntry({
      cubeCode: 'your_cube_code',
      fieldDefinitionList: [
        buildFieldDefinition({ alias: 'name', aliasName: '姓名', isDim: true, dataType: 'STRING' }),
        buildFieldDefinition({ alias: 'dept', aliasName: '部门', isDim: true, dataType: 'STRING' }),
        buildFieldDefinition({ alias: 'sales', aliasName: '销售额', isDim: false, dataType: 'NUMBER', aggregateType: 'SUM' }),
      ],
      fieldList: ['name', 'dept', 'sales'],
      orderByList: [buildOrderByItem('sales', 'DESC')],
    })
  },
  settings: {
    fixedHeader: true,
    theme: 'border',
    maxBodyHeight: 500,
    pagination: {
      pageSize: 20,
      showPageSelect: true,
      pageSizeList: [10, 20, 50, 100],
    }
  }
})
```

#### 筛选器（下拉 + 时间 + 区间）

```javascript
// 下拉筛选器
buildSchema.selectFilter({
  dataSetModelMap: {
    selectFilter: buildDataSetEntry({
      cubeCode: 'your_cube_code',
      cubeCodes: ['your_cube_code'],
      fieldDefinitionList: [
        buildFieldDefinition({ alias: 'dept_id', aliasName: '部门ID', isDim: true, dataType: 'STRING' }),
        buildFieldDefinition({ alias: 'dept_name', aliasName: '部门名称', isDim: true, dataType: 'STRING' }),
      ],
      fieldList: ['dept_id', 'dept_name'],
      valueField: ['dept_id'],
      labelField: ['dept_name'],
    })
  },
  settings: { labelConfig: { label: '部门' }, mode: 'multiple', showLabel: true }
})

// 时间筛选器
buildSchema.timeFilter({
  dataSetModelMap: {
    filterData: buildDataSetEntry({
      cubeCode: 'your_cube_code',
      cubeCodes: ['your_cube_code'],
      fieldDefinitionList: [
        buildFieldDefinition({ alias: 'create_date', aliasName: '创建日期', isDim: true, dataType: 'DATE', timeGranularityType: 'DAY' }),
      ],
      fieldList: ['create_date'],
      valueField: ['create_date'],
    })
  },
  settings: { labelConfig: { label: '日期范围' }, mode: 'range', dataConfig: { queryType: 'Between' } }
})
```

### 完整报表页面 Schema 构建示例

以下示例展示如何组合多个组件构建一个完整的报表页面：

```javascript
const {
  buildSchema, buildDataSetEntry, buildFieldDefinition, buildOrderByItem,
} = require('./build-yida-report-schema');

const CUBE_CODE = 'your_cube_code_here';

const reportPageSchema = buildSchema.pageHeader({
  titleContent: '销售数据看板',
  titleTip: '数据每日 08:00 更新',
  children: [
    buildSchema.topFilterContainer({
      showTag: true,
      children: [
        buildSchema.timeFilter({ /* 时间筛选器配置 */ }),
        buildSchema.selectFilter({ /* 下拉筛选器配置 */ }),
      ],
    }),
    buildSchema.simpleIndicatorCard({ /* 指标卡配置 */ }),
    buildSchema.lineChart({ /* 折线图配置 */ }),
    buildSchema.table({ /* 明细表格配置 */ }),
  ],
});

console.log(JSON.stringify(reportPageSchema, null, 2));
```

### 通用 settings 配置参考

| 字段 | 类型 | 说明 |
|------|------|------|
| `titleConfig.label` | `string` | 组件标题 |
| `height` | `number` | 组件高度（默认 300） |
| `colorType` | `string` | 颜色类型，`default` 或 `CUSTOM_COLOR` |
| `customColor` | `string` | 自定义颜色，逗号分隔 |
| `drillDown` | `boolean` | 是否开启下钻（默认 false） |
| `limit` | `number` | 数据条数限制（默认 1000） |

### 各图表组件特有配置

| 组件 | 特有配置 | 说明 |
|------|---------|------|
| **折线图** | `smooth`, `isStack`, `isPercent` | 平滑曲线、堆叠、百分比堆叠 |
| **饼图** | `innerRadius` | 内半径（>0 为环形图） |
| **条形图** | `isStack`, `isPercent` | 堆叠、百分比堆叠 |
| **仪表盘** | `min`, `max`, `range` | 最小值、最大值、区间配置 |
| **基础表格** | `fixedHeader`, `theme`, `pagination` | 固定表头、风格、分页 |

---

## 报表 Schema 构建关键规则（chart-builder.js）

### 命令调用格式

```bash
openyida create-report <appType> "<报表名称>" <配置JSON文件路径>
# 配置文件路径示例：.cache/openyida/<项目名或任务名>/<报表名>-report.json
```

> 配置 JSON 先用 create_file / Write / file edit tool 创建。上方路径默认从 OpenYida project 工作目录执行；从 workspace 根执行命令时传 `project/.cache/openyida/<项目名或任务名>/<报表名>-report.json`。

**⚠️ 第二个参数是报表名称，必须使用业务含义的中文名称**（如"任务管理数据报表"），不要传 formUuid。

### cubeCode 格式规则

报表引擎的 `cubeCode` 使用**下划线**分隔，而 `formUuid` 使用**连字符**分隔。代码中 `normalizeCubeCode()` 会自动转换，但配置文件中建议直接使用下划线格式：

```
formUuid:  FORM-AB4ACB9DD12C470D82047E05CDC19166CJSU
cubeCode:  FORM_AB4ACB9DD12C470D82047E05CDC19166CJSU  ← 连字符替换为下划线
```

### 配置文件字段格式

推荐使用**结构化格式**（`xField`/`yField`），而非简化的 `fields` 数组格式：

```json
{
  "reportName": "任务管理数据报表",
  "formUuid": "FORM-xxx",
  "charts": [
    {
      "title": "按优先级分布",
      "type": "pie",
      "cubeCode": "FORM_xxx",
      "xField": {
        "fieldCode": "selectField_xxx_value",
        "aliasName": "优先级",
        "dataType": "STRING",
        "aggregateType": "NONE"
      },
      "yField": [
        {
          "fieldCode": "pid",
          "aliasName": "数量",
          "dataType": "STRING",
          "aggregateType": "COUNT"
        }
      ]
    }
  ]
}
```

### 各图表类型的字段配置

| 图表类型 | 必填字段 | 说明 |
|---------|---------|------|
| `indicator` | `kpi`（数组） | 每个 kpi 字段需要 `fieldCode`、`aliasName`、`aggregateType` |
| `pie` | `xField`（单个）+ `yField`（数组） | xField 为分类维度，yField 为数值度量 |
| `bar`/`line`/`area` | `xField`（单个）+ `yField`（数组） | 可选 `groupField` 分组 |
| `table` | `columnFields`（数组） | 每列一个字段对象 |
| `combo` | `xField` + `leftYFields` + `rightYFields` | 柱线混合图 |
| `gauge` | `valueField`（单个） | 可选 `assitValueField` |
| `pivot` | `columnList`（数组） | 交叉透视表 |

### fieldCode 后缀规则

| 字段组件类型 | 报表中的 fieldCode | 示例 |
|------------|-------------------|------|
| `SelectField` | 加 `_value` 后缀 | `selectField_xxx` → `selectField_xxx_value` |
| `EmployeeField` | 加 `_value` 后缀 | `employeeField_xxx` → `employeeField_xxx_value` |
| `TextField` | 原样使用 | `textField_xxx` |
| `NumberField` | 原样使用 | `numberField_xxx` |
| `DateField` | 原样使用 | `dateField_xxx` |
| 内置字段 `pid` | 原样使用 | `pid`（用于 COUNT 计数） |

### dataSetModelMap 结构要点

报表引擎要求 `dataSetModelMap` 中每个数据集包含**两层字段定义**：

1. **`dataViewQueryModel.fieldDefinitionList`**：查询模型层，定义字段的 `alias`、`fieldCode`、`aggregateType` 等
2. **外层字段数组**（`xField`/`yField`/`fieldList`/`columnFields` 等）：展示层，每个字段对象包含 20+ 属性（`visible`、`isDimension`、`fieldKey`、`cubeCode`、`title`、`format`、`link`、`drillList`、`orderBy`、`measureType` 等）

两层都必须正确填充，否则报表图表会显示为空。

### userConfig 格式

报表引擎期望 `userConfig` 为**数组格式**（带 `ColumnFieldSetter` 配置器定义），而非简单对象格式：

```json
[
  {
    "name": "chartData",
    "title": "配置数据",
    "items": [
      {
        "setterName": "ColumnFieldSetter",
        "name": "xField",
        "title": "横轴",
        "setterProps": { "single": true, "showFormatTab": true }
      },
      {
        "setterName": "ColumnFieldSetter",
        "name": "yField",
        "title": "纵轴",
        "setterProps": { "showFormatTab": true, "showDataLink": true }
      }
    ]
  }
]
```

指标卡（`indicator`）的 `userConfig` 也是数组格式，`name` 为 `youshuData`。
