# 基于已有报表创建 ECharts 页面指南

> 本文档包含方案 C 的完整执行流程、Schema 解析规范、filterKey 提取规则和数据源完整性校验。
> 由 `yida-chart` 主技能拆分而来，执行方案 C 时必须读取本文档。

---

## 概述

当用户提供了一个已有的宜搭原生报表 URL（如 `https://www.aliwork.com/APP_XXX/admin/REPORT-XXX`），应以该原生报表作为数据源，创建 ECharts 自定义页面实现更美观的展示效果。**最终输出是 ECharts 自定义页面，而非优化后的原生报表**。

## 触发条件

用户消息中包含符合以下格式的报表 URL：
```
https://www.aliwork.com/{appType}/admin/{formUuid}
```
其中 `formUuid` 以 `REPORT-` 开头。

## 执行流程

```
Step 1: 从 URL 中解析 appType 和 formUuid
    ↓
Step 2: 执行 openyida env 检测环境和登录态
    ↓
Step 3: 执行 openyida get-schema <appType> <formUuid> 获取现有报表 Schema
    ↓  需要文件时，使用结构化文件写入工具保存 stdout，或用 --all --keyword --output-dir 输出到 project .cache
    ↓
Step 4: 解析 Schema，提取每个图表组件的 5 个核心参数
    ↓  详见下方「Schema 解析规范」
    ↓
Step 5: 数据源完整性校验
    ↓  详见下方「数据源完整性校验流程」
    ↓
Step 6: 生成 REPORT_COMPONENTS 配置（每个组件独立的 filterKey）
    ↓
Step 7: 编写 ECharts 自定义页面代码
    ↓
Step 8: 发布页面并验证数据接口
    ↓
Step 9: 隐藏原生报表页面（双端隐藏）
    ↓
Step 10: 记录关联关系到 .cache/<项目名>-report-bindding.json
    ↓
Step 11: 输出 ECharts 自定义页面访问链接
```

## URL 解析规则

```javascript
// URL 格式: https://www.aliwork.com/{appType}/admin/{formUuid}
// 示例: https://www.aliwork.com/APP_KNILKT41DC5XXR5D4QEC/admin/REPORT-QA666SC1J3U3TFO9GM9MJ5400RIW3W83SUYMM5
// 解析结果:
//   appType  = APP_KNILKT41DC5XXR5D4QEC
//   formUuid = REPORT-QA666SC1J3U3TFO9GM9MJ5400RIW3W83SUYMM5
```

## Schema 获取注意事项

1. **不要用 shell 重定向保存 Schema**：报表 Schema 通常非常大（数千行），需要文件时用 CLI 自带输出目录或 agent 结构化文件写入工具
   ```bash
   openyida get-schema <appType> --all --keyword <报表名称> --output-dir .cache/openyida/<项目名或任务名>/schemas
   ```

2. **单表回读时保存 stdout**：如必须按 `formUuid` 回读，先执行命令，再用 create_file / Write / file edit tool 保存纯 JSON 到 `<projectRoot>/.cache/openyida/<项目名或任务名>/report-schema.json`
   ```bash
   openyida get-schema <appType> <formUuid> --json
   ```

---

## ⚠️ Schema 解析规范（必须遵守）

### 1. 组件参数提取规则

从报表 Schema 中提取 ECharts 页面所需的 **5 个核心参数**，每个图表组件都有独立的一组参数：

| 参数 | 来源位置 | 说明 |
|------|---------|------|
| **cid** | `componentsTree[0].children[N].id` | 组件唯一标识，`node_xxx` 格式 |
| **className** | `componentsTree[0].children[N].componentName` | 组件类型名，如 `YoushuPieChart` |
| **dataSetKey** | `componentsTree[0].children[N].props.dataSetModelMap` 的第一个 key | 数据集标识 |
| **filterKey** | 见下方「filterKey 提取规则」 | 每个组件独立的筛选 key |
| **cname** | `componentsTree[0].children[N].props.componentTitle.zh_CN` | 组件中文标题 |

### 2. 图表组件类型白名单

解析 Schema 时，**必须过滤掉非图表组件**：

| componentName | 类型 | dataSetKey |
|--------------|------|------------|
| `YoushuSimpleIndicatorCard` | 指标卡 | `youshuData` |
| `YoushuPieChart` | 饼图 | `chartData` |
| `YoushuGroupedBarChart` | 分组柱状图 | `chartData` |
| `YoushuLineChart` | 折线图 | `chartData` |
| `YoushuFunnelChart` | 漏斗图 | `chartData` |
| `YoushuComboChart` | 组合图 | `chartData` |
| `YoushuRadarChart` | 雷达图 | `chartData` |
| `YoushuTable` | 明细表 | `chartData` |

**必须排除的组件**：

| componentName | 说明 |
|--------------|------|
| `YoushuPageHeader` | 页面标题容器 |
| `YoushuTopFilterContainer` | 筛选器容器 |
| `YoushuSelectFilter` | 下拉筛选器 |
| `YoushuTimeFilter` | 时间筛选器 |
| `YoushuInputFilter` | 区间筛选器 |

> ⚠️ **常见错误**：把 `YoushuPageHeader` 容器的 `id` 当作指标卡的 cid。指标卡是 `YoushuPageHeader` 的子组件。

### 3. Schema 组件层级结构

```
componentsTree[0]
  └── children[]
        ├── { componentName: "YoushuPageHeader", id: "node_xxx" }    ← 容器（排除）
        │     └── children[]
        │           ├── { componentName: "YoushuTopFilterContainer" } ← 筛选器容器（排除）
        │           ├── { componentName: "YoushuSimpleIndicatorCard", id: "node_aaa" }  ← ✅ 指标卡
        │           ├── { componentName: "YoushuPieChart", id: "node_bbb" }             ← ✅ 饼图
        │           └── { componentName: "YoushuTable", id: "node_ddd" }                ← ✅ 明细表
        └── ...
```

**解析时必须递归遍历 `children`**，不能只看第一层。

### 4. ⚠️ filterKey 提取规则（最易出错）

> **每个图表组件有独立的 filterKey，绝对不能共用同一个 filterKey 请求所有图表。**

filterKey 嵌套在组件的 `props.dataSetModelMap.<dataSetKey>.dataViewQueryModel.filterList` 中：

```
组件节点
  └── props
        └── dataSetModelMap
              └── <dataSetKey>
                    └── dataViewQueryModel
                          └── filterList[]
                                ├── { filterKey: "filter-aaa-bbb-ccc-ddd", paramId: "node_xxx-selectFilter" }
                                └── { filterKey: "filter-eee-fff-ggg-hhh", paramId: "node_yyy-selectFilter" }
```

**提取步骤**：
1. 找到图表组件节点（通过 `componentName` 白名单过滤）
2. 获取 `props.dataSetModelMap` 的第一个 key 作为 `dataSetKey`
3. 进入 `dataSetModelMap[dataSetKey].dataViewQueryModel.filterList`
4. 遍历 `filterList`，根据 `paramId` 匹配目标筛选器
5. 取对应条目的 `filterKey` 值

> ⚠️ **关键点**：同一个筛选器在不同图表组件中的 `filterKey` 是**不同的**！

### 5. REPORT_COMPONENTS 配置模板

```javascript
// ✅ 正确：每个组件有独立的 filterKey
var REPORT_COMPONENTS = {
  indicator: {
    cid: 'node_aaa', cname: '客户总数',
    className: 'YoushuSimpleIndicatorCard',
    dataSetKey: 'youshuData',
    filterKey: 'filter-k2wu-qq5l-fibv-dthq',
  },
  pie: {
    cid: 'node_bbb', cname: '行业分布',
    className: 'YoushuPieChart',
    dataSetKey: 'chartData',
    filterKey: 'filter-7fs7-jqi4-rkz3-qvqi',  // 与指标卡不同！
  },
};

var _buildFilterValueMap = function(component) {
  var filterValueMap = {};
  if (_customState.filterValue && component.filterKey) {
    filterValueMap[component.filterKey] = [_customState.filterValue];
  }
  return filterValueMap;
};
```

---

## ⚠️ 数据源完整性校验流程（必须执行）

### 校验流程

```
Step 1: 分析用户需求，列出需要的所有图表类型
    ↓
Step 2: 解析报表 Schema，提取已有的图表组件列表
    ↓
Step 3: 对比需求与现有组件
    ↓  ✅ 全部存在 → 继续
    ↓  ❌ 有缺失 → 执行 Step 4
    ↓
Step 4: 调用 openyida append-chart 补充缺失图表
    ↓
Step 5: 重新获取 Schema，确认新增图表已生效
    ↓
Step 6: 编写 ECharts 页面代码
```

### 新增图表的必备字段

| 字段 | 说明 | 示例 |
|------|------|------|
| **type** | 图表类型 | `pie`、`bar`、`line`、`indicator`、`table` |
| **title** | 图表标题 | `"行业分布"` |
| **cubeCode** | 数据集编码 | `FORM_D399CAC7A21D4F43B8ED222FF67F96EESK3J` |
| **xField** | 横轴字段（维度） | `{ fieldCode: "selectField_xxx_value", title: "行业" }` |
| **yField** | 纵轴字段（度量） | `{ fieldCode: "textField_xxx", title: "数量", aggregateType: "COUNT" }` |
| **filterFields** | 关联筛选器字段 | 与已有筛选器一致 |

> ⚠️ `SelectField` 和 `EmployeeField` 在报表中的 `fieldCode` 需要加 `_value` 后缀。

### 校验自检清单

- [ ] 报表 Schema 中每种需要的图表类型都有对应组件
- [ ] 每个图表组件都提取了独立的 `cid`（`node_xxx` 格式）
- [ ] 每个图表组件都提取了独立的 `filterKey`（`filter-xxx` 格式）
- [ ] 不同图表组件的 `filterKey` 互不相同
- [ ] `dataSetKey` 正确（指标卡 `youshuData`，其他 `chartData`）
- [ ] `className` 与 Schema 中的 `componentName` 完全一致
- [ ] 如有缺失图表，已通过 `append-chart` 补充并重新获取 Schema

---

## 关键约束

1. **必须复用原有数据源**：`cubeCode`、`cubeTenantId`、`cid`、`componentClassName` 等必须从原报表 Schema 中提取
2. **不要修改原生报表和数据源表单**：只基于原报表的数据接口创建 ECharts 展示层
3. **数据获取必须通过 getDataAsync.json**：禁止前端聚合
4. **隐藏原生报表页面**：创建 ECharts 页面后，将原生报表设置为双端隐藏
5. **getDataAsync.json 接口参数必须从 Schema 中提取，禁止猜测**：
   - `prdId`：必须通过 `getFormNavigationListByOrder` 动态获取
   - `pageName`：固定为 `'report'`
   - `pageId`：使用报表的 `REPORT-xxx` formUuid
   - `cid`、`componentClassName`、`dataSetKey`：从报表 Schema 提取
6. **报表接口返回的是聚合数据，不含 `formInstId`**：明细表必须用 `searchFormDatas`
7. **筛选器的 `filterValueMap` key 必须用 `filterKey`**：不能用字段 ID
8. **纯工具函数必须用 `var` 声明**：不能用 `export function`

## ECharts 页面设计策略

| 设计维度 | 具体操作 |
|---------|----------|
| **布局设计** | 指标卡在上、图表居中、数据明细表在下 |
| **图表选择** | 根据原报表组件类型选择对应 ECharts 图表 |
| **筛选交互** | 通过 filterValueMap 参数实现筛选联动 |
| **样式定制** | 使用 ECharts 主题、渐变色、动画 |
| **响应式** | 监听 window resize，调用 `chart.resize()` |
