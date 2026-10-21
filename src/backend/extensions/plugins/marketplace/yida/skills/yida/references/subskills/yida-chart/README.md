
# 宜搭 ECharts 高级报表技能

## 适用场景

| 用户意图 | 正确处理 |
|---------|---------|
| 普通"报表"、"统计"、标准图表 | 改用 `yida-report` |
| "更美观"、"高级"、"定制化"、"ECharts"、"数据大屏" | 使用本技能 |
| 用户提供已有报表 URL，希望美化展示 | 使用本技能的方案 C |
| 多表关联分析 | 当前不支持，先澄清或拆成单表报表 |

## 与 yida-report 的分工

| 技能 | 定位 | 产物 |
|------|------|------|
| `yida-report` | 创建宜搭原生报表，配置聚合图表和接口 | 原生报表页面 |
| `yida-chart` | 基于原生报表数据，用 ECharts 做自定义可视化 | ECharts 自定义页面 |

> ECharts 高级报表必须先有原生报表作为数据源；没有原生报表时，先调用 `yida-report` 创建。

## 核心规则

### 致命规则（FATAL）

1. **聚合统计禁止前端聚合**：KPI、分组、求和、平均、趋势、占比都必须走原生报表 `getDataAsync.json` / `getCacheData.json`。
2. **必须依赖原生报表**：不能在没有原生报表数据源的情况下直接创建 ECharts 聚合页面。
3. **不要混用 `cid` 和 `fieldId`**：`cid` 是 `node_xxx`，用于报表数据请求；`fieldId` 是组件标识，不能作为 API 请求参数。
4. **不要编造报表参数**：`reportId`、`cid`、`dataSetKey`、`filterKey` 必须来自 URL 或 Schema。
5. **ECharts 通过 CDN 加载**：使用 `this.utils.loadScript` 或等价动态脚本加载，禁止 `import` / `require`。
6. **数据失败必须有错误态**：接口失败、无权限、空数据都要展示可理解提示，不要静默空白。

### 重要规则（IMPORTANT）

1. **原生报表双端隐藏**：创建 ECharts 页面后，原生报表作为数据源应在 PC 和移动端导航中隐藏。
2. **记录绑定关系**：写入 `.cache/<项目名>-report-bindding.json`，记录 ECharts 页面、原生报表和组件映射。
3. **更新时双页面同步**：需求变化时先更新原生报表 Schema，再更新 ECharts 页面和绑定关系。
4. **明细表走表单数据接口**：仅数据明细表可用 `this.utils.yida.searchFormDatas`，聚合图表仍走报表接口。
5. **遵循自定义页面规范**：状态、生命周期、事件绑定、发布前校验按 `yida-custom-page` 执行。

## 方案选择

| 场景 | 方案 | 流程 |
|------|------|------|
| 用户有原生报表 URL | 方案 C：基于已有报表创建 ECharts 页面 | 解析 URL → get-schema → 提取组件参数 → 校验数据源 → 写 ECharts 页面 |
| 用户无原生报表但要 ECharts 高级报表 | 方案 B：从头创建 | `yida-report` 创建原生报表 → get-schema → 创建 ECharts 页面 |
| 用户只要标准报表 | 不使用本技能 | 交给 `yida-report` |

## 开发流程

```bash
# Step 1: 只读检测环境和登录态
openyida env --json
openyida login --check-only --json

# Step 2: 准备原生报表数据源
# 无原生报表时先使用 yida-report 创建；已有 URL 时解析 appType/formUuid

# Step 3: 获取报表 Schema；需要文件时用 CLI 输出目录或结构化文件写入工具保存 stdout
openyida get-schema <appType> --all --keyword <报表名称> --output-dir .cache/openyida/<项目名或任务名>/schemas

# Step 4: 解析 cid / className / dataSetKey / filterKey / cname

# Step 5: 创建 ECharts 自定义页面并编写源码
openyida create-page <appType> "<ECharts页面名>" --mode dashboard
openyida check-page project/pages/src/<页面名>.oyd.jsx

# Step 6: 发布并验证
openyida publish project/pages/src/<页面名>.oyd.jsx <appType> <echartsFormUuid> --health-check
```

## 报表参数速查

| 参数 | 来源位置 | 用途 | 注意 |
|------|---------|------|------|
| `cid` | `componentsTree[].children[].id` | `getDataAsync.json` 请求参数 | 必须是 `node_xxx` |
| `className` | 组件 `componentName` | 请求 `componentClassName` | 如 `YoushuPieChart` |
| `dataSetKey` | `props.dataSetModelMap` 的 key | 请求数据集 | 指标卡常见 `youshuData`，图表常见 `chartData` |
| `filterKey` | `dataViewQueryModel.filterList[]` | 构造筛选参数 | 每个组件独立，不能共用 |
| `cname` | `props.componentTitle.zh_CN` | 页面展示和调试 | 可为空但建议保留 |
| `prdId` / `topicId` | 导航接口 `getFormNavigationListByOrder` | 报表接口必填 | 不能硬编码 |

## 图表组件白名单

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

必须排除 `YoushuPageHeader`、`YoushuTopFilterContainer`、`YoushuSelectFilter`、`YoushuTimeFilter`、`YoushuInputFilter` 等容器或筛选器组件。

## ECharts 页面必备结构

| 项 | 要求 |
|----|------|
| 状态 | 顶层 `var _customState` |
| 状态方法 | `getCustomState`、`setCustomState`、`forceUpdate` |
| 生命周期 | `didMount` 初始化，`didUnmount` 清理 chart 实例和 resize 事件 |
| 渲染入口 | `renderJsx` 每个 return 分支都包含隐藏的 `this.state.timestamp` |
| CDN | `https://g.alicdn.com/code/lib/echarts/5.6.0/echarts.min.js` |
| 工具函数 | 不需要 `this` 的函数用 `var _xxx = function () {}` |
| 组件方法 | 需要宜搭运行时 `this` 的方法用 `export function xxx() {}` |

## 数据接口速查

| 场景 | 接口 | 约束 |
|------|------|------|
| 聚合统计 | `/visual/visualizationDataRpc/getDataAsync.json` | 由原生报表接口返回，禁止浏览器端聚合 |
| 缓存聚合 | `/visual/visualizationDataRpc/getCacheData.json` | 可按报表配置复用缓存数据 |
| 明细表 | `this.utils.yida.searchFormDatas` | 只用于展示原始记录，不做分组求和 |
| 详情跳转 | `/APP_xxx/formUuid/formInstId` | 明细表记录需保留 `formInstId` |
| 地图 GeoJSON | `https://geo.datav.aliyun.com/areas_v3/bound/100000_full.json` | ECharts 5 地图需 `registerMap('china', geoJson)` |

## 生成前自检

- [ ] 已确认需求是 ECharts 高级可视化，而非普通原生报表
- [ ] 已存在或已创建原生报表数据源
- [ ] 已通过 `openyida get-schema` 获取 Schema 文件
- [ ] 已提取每个图表组件的 `cid`、`className`、`dataSetKey`、`filterKey`、`cname`
- [ ] `prdId` 通过导航接口动态获取，不硬编码
- [ ] 聚合图表不使用 `searchFormDatas` 做前端聚合
- [ ] `renderJsx` 所有分支都有隐藏 timestamp div
- [ ] `didUnmount` 清理 ECharts 实例、resize 监听和定时器
- [ ] 发布前执行 `openyida check-page <源文件>`
- [ ] 记录 `.cache/<项目名>-report-bindding.json`

## 常见问题

**Q：用户说"优化已有报表"，是不是改原生报表？**  
A：不是。本技能最终产物是 ECharts 自定义页面，原生报表只作为数据源。

**Q：为什么不能前端聚合？**  
A：权限、过滤器、数据量和统计口径都由宜搭原生报表保证；前端聚合容易越权、慢且口径不一致。

**Q：多个图表能共用同一个 `filterKey` 吗？**  
A：不能。同一个筛选器在不同组件里也可能有不同 `filterKey`，必须逐组件提取。

**Q：页面点击筛选后怎么刷新？**  
A：更新 `_customState.filterValueMap`，重新调用报表数据请求函数，对已有 chart 执行 `setOption`，不要频繁 dispose 重建。

## 参考文档

| 文档 | 覆盖范围 | 何时阅读 |
|------|---------|---------|
| [ECharts 页面代码模板](references/echarts-code-template.md) | 必备函数、CDN、`prdId` 动态获取、报表请求模板、meta 解析 | 编写 ECharts 页面代码前必读 |
| [ECharts 视觉规范](references/echarts-design-spec.md) | 默认风格、配色、卡片、图表模板、多端适配、表格样式 | 设计页面 UI 时必读 |
| [已有报表绑定指南](references/echarts-bindding-guide.md) | 方案 C、Schema 解析、filterKey、数据源完整性校验 | 用户提供报表 URL 时必读 |
| [示例](references/examples.md) | 命令示例、页面代码示例、常见图表实现 | 需要参考完整写法时阅读 |
| `yida-report` 子技能 | 原生报表创建、追加图表、Schema 生成 | 需要先创建或补齐原生报表时调用 `use_skill("yida-report", "创建或补齐原生报表")` |
| `yida-custom-page` 子技能 | 宜搭 React 16 自定义页面规则 | 不确定页面运行时限制时调用 `use_skill("yida-custom-page", "确认宜搭自定义页面运行时限制")` |
