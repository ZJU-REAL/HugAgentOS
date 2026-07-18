# 看板结构与布局规范

> 本文档定义看板页面的**单屏控制塔结构**——从 header 到 marquee 的 9 个必备 section，以及响应式断点、样式对象骨架。
>
> 实战样本：
> - 深色紫蓝：`project/pages/src/supply-chain-dashboard.js`（2356 行）
> - 金蓝奢华：`project/pages/src/shangri-la-executive-dashboard.js`（1796 行）

---

## 单屏 9 Section 结构

从上到下、从左到右：

```
┌─────────────────────────────────────────────────────────┐
│ 1. header          [Logo] 标题 · 副标题        [时间]    │
├─────────────────────────────────────────────────────────┤
│ 2. filterBar       [视图][财年][季度][维度][品类][重置]   │
├─────────────────────────────────────────────────────────┤
│ 3. carbonBar       [核心指标 × 5 条]                     │
├─────────────────────────────────────────────────────────┤
│ 4. 控制塔（3 列 grid）                                   │
│   ┌────────────┬────────────┬────────────┐              │
│   │ 健康指数    │ 4 经营模块  │ 今日动作    │              │
│   │ 雷达图/环形 │ (2×2 grid) │ + 风险水位  │              │
│   └────────────┴────────────┴────────────┘              │
├─────────────────────────────────────────────────────────┤
│ 5. KPI 条（5 列）[KPI×5，hover 弹派单菜单]                │
├─────────────────────────────────────────────────────────┤
│ 6. 图表网格（2 行 × 3 列）                               │
│   [区域营收地图] [品牌/产品矩阵] [客群/区域分布]         │
│   [渠道贡献桑基] [NPS/服务趋势 ] [营收/利润趋势]         │
├─────────────────────────────────────────────────────────┤
│ 7. 审批流水 + 数字化项目（2 列）                         │
├─────────────────────────────────────────────────────────┤
│ 8. AI 快讯 marquee（底部滚动字幕）                       │
├─────────────────────────────────────────────────────────┤
│ 9. 派单弹窗（modal，默认 hidden）                        │
└─────────────────────────────────────────────────────────┘
```

---

## 响应式断点

```javascript
var isMobile = window.innerWidth < 768;
var isTablet = window.innerWidth >= 768 && window.innerWidth < 1024;
var isPC     = window.innerWidth >= 1024;
```

| Section | PC (≥1024) | 平板 (768~1023) | 手机 (<768) |
|---------|-----------|----------------|-------------|
| header | 横向 flex | 横向 flex | 标题换行 |
| filterBar | 一行 6 控件 | 一行 3 控件 + 折行 | 纵向堆叠 |
| carbonBar | 5 列 | 3 列折行 | 2 列 |
| 控制塔 | 3 列 | 2 列 + 1 列 | 1 列堆叠 |
| KPI | 5 列 | 3 列折行 | 2 列 |
| 图表网格 | 3 列 × 2 行 | 2 列折行 | 1 列 |
| 审批+数字化 | 2 列 | 2 列 | 1 列 |
| marquee | 单行滚动 | 单行滚动 | 单行滚动 |

**规则**：所有栅格用 `display: grid; grid-template-columns: repeat(N, 1fr); gap: 16px`，移动端改 `repeat(1, 1fr)` + `gap: 12px`。

---

## renderJsx 骨架

```javascript
export function renderJsx() {
  var self = this;
  var state = this.getCustomState();
  var isMobile = window.innerWidth < 768;
  var isTablet = window.innerWidth >= 768 && window.innerWidth < 1024;

  // 样式对象（响应式），详见下文 s.XXX 结构
  var s = buildStyles(isMobile, isTablet);

  // Loading 分支也必须含 timestamp div
  if (state.loading) {
    return (
      <div>
        <div style={{ display: 'none' }}>{this.state.timestamp}</div>
        <div style={s.loadingWrap}>加载中…</div>
      </div>
    );
  }

  return (
    <div style={s.page}>
      <div style={{ display: 'none' }}>{this.state.timestamp}</div>

      {/* 1. header */}
      <div style={s.header}>
        <div style={s.headerLeft}>
          <div style={s.logo}>Y</div>
          <div>
            <div style={s.title}>集团经营驾驶舱</div>
            <div style={s.subtitle}>FY26 · 全球 · 实时</div>
          </div>
        </div>
        <div style={s.headerRight}>
          <span style={s.currentTime}>{state.currentTime}</span>
        </div>
      </div>

      {/* 2. filterBar */}
      <div style={s.filterBar}>
        {renderFilterControls(self, state, s)}
      </div>

      {/* 3. carbonBar */}
      <div style={s.carbonBar}>
        {CARBON_BAR_ITEMS.map(function(item) {
          return renderCarbonItem(item, s);
        })}
      </div>

      {/* 4. 控制塔 */}
      <div style={s.dashboardTriangle}>
        <div style={s.healthCard}>{renderHealthIndex(state, s)}</div>
        <div style={s.modulesGrid}>
          {DASHBOARD_MODULES.map(function(m) {
            return renderModuleCard(self, m, s);
          })}
        </div>
        <div style={s.actionsAndRisks}>
          <div style={s.actionsPanel}>
            {DASHBOARD_ACTIONS.map(function(a) { return renderAction(self, a, s); })}
          </div>
          <div style={s.risksPanel}>
            {DASHBOARD_RISKS.map(function(r) { return renderRisk(self, r, s); })}
          </div>
        </div>
      </div>

      {/* 5. KPI */}
      <div style={s.kpiGrid}>
        {state.kpiList.map(function(k) {
          return renderKpiCard(self, k, s);
        })}
      </div>

      {/* 6. 图表网格 */}
      <div style={s.chartGrid}>
        <div id="chart-region" style={s.chartCard} />
        <div id="chart-brand" style={s.chartCard} />
        <div id="chart-segment" style={s.chartCard} />
        <div id="chart-channel" style={s.chartCard} />
        <div id="chart-nps" style={s.chartCard} />
        <div id="chart-trend" style={s.chartCard} />
      </div>

      {/* 7. 审批 + 数字化 */}
      <div style={s.twoColGrid}>
        <div style={s.approvalCard}>{renderApprovals(state, s)}</div>
        <div style={s.digitalCard}>{renderDigitalProjects(state, s)}</div>
      </div>

      {/* 8. marquee */}
      <div style={s.marquee}>
        <span style={s.marqueeIcon}>AI</span>
        <span style={s.marqueeText}>{state.marqueeText}</span>
      </div>

      {/* 9. 派单弹窗 */}
      {state.todoModal && state.todoModal.visible ? renderTodoModal(self, state, s) : null}
    </div>
  );
}
```

---

## 样式对象 s 的必备键清单

```
s.page                ── 全局背景、字体、padding
s.header              ── 顶部横幅
s.headerLeft / headerRight
s.logo                ── 圆角/渐变/发光
s.title / subtitle
s.currentTime

s.filterBar           ── 横向 flex，wrap
s.filterLabel / filterSelect / filterBtn / filterBtnReset

s.carbonBar           ── grid 5 列
s.carbonItem / carbonValue / carbonLabel / carbonDelta

s.dashboardTriangle   ── 3 列 grid
s.healthCard / healthScore / healthRing
s.modulesGrid         ── 2×2 grid
s.moduleCard / moduleHeader / moduleMetric / moduleAction
s.actionsAndRisks     ── 上下堆叠
s.actionsPanel / actionItem / actionTitle / actionPriority
s.risksPanel / riskItem / riskScore / riskHint

s.kpiGrid             ── grid 5 列
s.kpiCard / kpiLabel / kpiValue / kpiTrend / kpiActionMenu

s.chartGrid           ── grid 3 列
s.chartCard           ── 卡片容器，含固定高度（移动端 280，PC 400）

s.twoColGrid          ── grid 2 列
s.approvalCard / approvalRow / approvalStatus
s.digitalCard / digitalProgressBar

s.marquee             ── 底部条
s.marqueeIcon / marqueeText

s.modalMask / modalCard / modalTitle / modalForm / modalBtns
```

**关键约束**：
- `s.chartCard` **必须显式设置 height**，否则 ECharts 容器 0 高度不渲染。PC 400px / 移动 280px
- `s.page` 的 background 应与 THEME.bg 一致，避免滚动时露白
- 所有卡片用相同 `borderRadius`（推荐 12px），统一视觉节奏

---

## 控制塔（Section 4）内部结构

这是看板的**产品化差异点**——不只是"图表堆叠"，而是把经营要素拆成"健康指数 + 4 模块 + 动作 + 风险"的决策面板。

| 子区域 | 内容 | 交互 |
|--------|------|------|
| **健康指数** | 单一综合分数（0-100），雷达/环形展示 4-6 个维度 | 点击打开"指数说明"弹窗 |
| **4 经营模块** | 4 个业务域（如品牌/营收/服务/可持续），每模块 1 核心指标 + 1 操作按钮 | 按钮 → 派单弹窗预填"优化 XX 模块" |
| **今日动作** | 3 条今日必做事项，带优先级标签和状态时间 | 任意一条点击 → 派单 |
| **风险水位** | 3 条当前风险，红黄绿打分 + 建议动作 | 点击 → 派单"风险应对" |

**DASHBOARD_MODULES 数据结构**：
```javascript
var DASHBOARD_MODULES = [
  {
    key: 'revenue',
    label: '营收健康',
    metric: 'US$ 1,285',
    metricUnit: 'RevPAR',
    delta: '+8.4%',
    trend: 'up',         // 'up' | 'down' | 'flat'
    color: THEME.primary,
    actions: [
      { label: '拉齐各 BU 目标', priority: '高' },
      { label: '周会同步节奏',   priority: '中' }
    ]
  },
  // ... 共 4 项
];
```

**DASHBOARD_ACTIONS 数据结构**：
```javascript
var DASHBOARD_ACTIONS = [
  { title: '08:30 晨会简报', detail: '昨夜到离店 + 今日重点接待', status: '08:30 已生成', priority: '中', color: THEME.primary },
  // ... 共 3 项
];
```

**DASHBOARD_RISKS 数据结构**：
```javascript
var DASHBOARD_RISKS = [
  { name: 'MENA 扩张', level: '高', score: 87, hint: '本季开业节奏偏紧', color: THEME.danger },
  // ... 共 3 项，score 0-100，越高风险越大
];
```

---

## CARBON_BAR（Section 3）设计

"碳链指标条"来自供应链看板原型，在其他业务域应重命名为对应语义：

| 业务 | 命名 | 典型 5 项 |
|------|------|----------|
| 供应链 | 碳链指标 | 碳排/绿色占比/循环利用/合规/新能源 |
| 酒店 | 品牌/客群 | 会员规模/钻石客/新兴市场/绿色占比/RevPAR |
| 零售 | 商品/会员 | SKU 周转/VIP 占比/新客率/毛利率/线上占比 |
| 制造 | 产线/质量 | 良率/设备 OEE/交期准时率/能耗/安全天数 |

**CARBON_BAR_ITEMS 数据结构**：
```javascript
var CARBON_BAR_ITEMS = [
  { key: 'circle', label: 'Circle 会员', value: '1.65亿', delta: '+12.3%', trend: 'up', color: THEME.primary },
  // ... 共 5 项
];
```

---

## KPI 卡（Section 5）设计

5 个 KPI 卡每个都带"action menu"——hover（用 CSS :hover，不要 onMouseEnter）或点击时弹出 2-3 个派单快捷动作。

```javascript
var KPI_CARDS = [
  {
    key: 'revpar',
    label: 'RevPAR',
    value: 'US$ 1,285',
    delta: '+8.4% YoY',
    trend: 'up',
    icon: '$',
    actions: [
      { label: '拉齐品牌组目标', relatedKpi: 'RevPAR', priority: '高' },
      { label: '对齐竞品报告',   relatedKpi: 'RevPAR', priority: '中' }
    ]
  },
  // ... 共 5 项
];
```

每个 action 点击 → 调用 `openDashboardTodo(title, relatedKpi, priority, description)` 打开派单弹窗，详见 `interaction-patterns.md`。

---

## 图表网格（Section 6）6 图表常见组合

| 槽位 | 通用语义 | ECharts 类型 | 供应链 | 酒店 |
|------|---------|-------------|--------|------|
| 1 | 地理维度 | map / bar | 区域订单分布 | 区域营收地图 |
| 2 | 对象矩阵 | bar / radar | 品类业绩 | 品牌矩阵 |
| 3 | 人/客维度 | pie / radar | 客户画像 | 客群分布 |
| 4 | 流向图 | sankey | 直采链路 | 渠道贡献 |
| 5 | 过程指标 | line | 关键节点达成 | 服务 NPS |
| 6 | 趋势预测 | line | 营收趋势 + 预测 | RevPAR 趋势 + AI 预测 |

**每图表独立 init 函数**：`initRegionChart / initBrandChart / initSegmentChart / initChannelChart / initNpsChart / initTrendChart`，用 `renderAllCharts()` 统一编排。筛选变化时 **setOption 原地刷新**，不要 dispose 重建。

---

## 发布后检查清单

- [ ] PC / 平板 / 手机三端打开都无横向滚动
- [ ] 筛选任意维度，6 图表和 5 KPI 都实时刷新（不闪烁）
- [ ] 每个 KPI / 模块 / 动作 / 风险点击都能弹出派单弹窗
- [ ] 派单后钉钉客户端出现真实待办；不是仅收到工作通知（见 `interaction-patterns.md` 联调步骤）
- [ ] 每个卡片右上角"截图"按钮可用，有真实 `onClick`，截出来不包含按钮本身
- [ ] marquee 在底部平滑滚动不卡顿
- [ ] 组织内短链打开 → 无平台导航 → 看板顶满一屏
