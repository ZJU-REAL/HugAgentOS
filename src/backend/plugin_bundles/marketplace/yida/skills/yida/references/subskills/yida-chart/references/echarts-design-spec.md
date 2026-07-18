# ECharts 报表视觉风格规范

> 本文档包含 ECharts 报表页面的默认视觉风格、配色方案、图表配置模板等。
> 由 `yida-chart` 主技能拆分而来，设计 ECharts 页面样式时参考本文档。

---

## 默认风格：白底简洁商务风

> ⚠️ **ECharts 报表页面默认使用白底简洁商务风**，除非用户明确要求"暗色主题"、"科技风"、"大屏风格"等关键词。

### 风格选择规则

| 用户关键词 | 使用风格 |
|-----------|----------|
| 无特殊要求 / "报表" / "看板" / "Dashboard" | **白底商务风**（默认） |
| "酷炫" / "美观" / "高级" | **白底商务风**（默认，"酷炫"不等于暗色） |
| "暗色" / "深色" / "科技风" / "大屏" / "监控大屏" | 暗色科技风 |

### 默认配色方案

```javascript
var COLORS = {
  primary: '#1e40af',
  primaryLight: '#3b82f6',
  accent: '#0ea5e9',
  success: '#059669',
  warning: '#d97706',
  danger: '#dc2626',
  neutral: '#64748b',
  bg: '#f8fafc',
  cardBg: '#ffffff',
  textPrimary: '#0f172a',
  textSecondary: '#475569',
  textMuted: '#94a3b8',
  border: '#e2e8f0',
};

var CHART_COLORS = ['#3b82f6', '#0ea5e9', '#059669', '#d97706', '#dc2626', '#8b5cf6', '#ec4899', '#14b8a6'];
```

### 卡片样式

```javascript
var cardStyle = {
  background: COLORS.cardBg,
  borderRadius: 8,
  padding: 20,
  marginBottom: 16,
  border: '1px solid ' + COLORS.border,
  boxShadow: '0 1px 3px rgba(0, 0, 0, 0.06)',
};
```

### 设计要素

| 要素 | 规范 |
|------|------|
| 页面背景 | `#f8fafc`（极浅灰），避免纯白 |
| 卡片 | 白底 + `border-radius: 10px` + `1px solid #e2e8f0` 细边框 |
| 字体 | `-apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", Roboto, sans-serif` |
| KPI 数字 | `font-size: 26px` + `font-weight: 700` + `font-feature-settings: "tnum"` |
| 图表 tooltip | 深色半透明背景 `rgba(15, 23, 42, 0.92)` + 圆角 8px |
| 分割线 | `#f1f5f9` 虚线（`type: [4, 4]`） |
| 标签文字 | `12-13px`，颜色用 `textSecondary` 或 `textMuted` |

---

## 报表必备组件

| 组件 | 作用 | 必要性 |
|------|------|--------|
| **KPI 指标卡** | 展示核心数字 | ✅ 必须 |
| **图表区域** | 展示趋势和分布 | ✅ 必须 |
| **数据明细表格** | 展示每条数据详情 | ✅ 必须 |
| **全局筛选栏** | 按维度筛选数据 | ✅ 推荐 |

## 全局筛选器位置

筛选器应放在**页面顶层**（标题栏下方、KPI 卡片上方），作为全局筛选器。

## 刷新状态 UI 提示

```javascript
// 标题旁刷新提示
{_customState.refreshing && <span style={{ marginLeft: 8, color: '#3b82f6' }}>🔄 刷新中...</span>}

// 顶部进度条
{_customState.refreshing && (
  <div style={{
    position: 'fixed', top: 0, left: 0, right: 0,
    height: 3, background: 'linear-gradient(90deg, #3b82f6 0%, #60a5fa 50%, #3b82f6 100%)',
    zIndex: 9999,
  }}></div>
)}
```

---

## 常用图表配置模板

> ⚠️ `label.formatter` 返回 rich text 模板在宜搭自定义页面环境不稳定。需要强调标签时，优先使用普通 formatter 字符串、`label.color/fontWeight` 等样式，或在数据处理阶段预先拼好展示文本。

### 柱状图

```javascript
export function renderBarChart(categories, values, title) {
  var chart = this.createChart('chart-bar');
  if (!chart) return;
  chart.setOption({
    title: { text: title || '', left: 'center' },
    tooltip: { trigger: 'axis' },
    xAxis: {
      type: 'category', data: categories,
      axisLabel: { rotate: categories.length > 8 ? 30 : 0, fontSize: this.utils.isMobile() ? 10 : 12 },
    },
    yAxis: { type: 'value' },
    series: [{ type: 'bar', data: values, itemStyle: { color: '#0089FF' }, barMaxWidth: 40 }],
    grid: { left: '3%', right: '4%', bottom: '10%', containLabel: true },
  });
}
```

## 多端适配

```javascript
export function getChartHeight(chartType) {
  var isMobile = this.utils.isMobile();
  var heightMap = {
    bar: isMobile ? '280px' : '400px',
    line: isMobile ? '280px' : '400px',
    pie: isMobile ? '300px' : '380px',
    gauge: isMobile ? '250px' : '300px',
  };
  return heightMap[chartType] || (isMobile ? '280px' : '400px');
}
```

布局建议：
- **PC 端**：`flexWrap: 'wrap'`，每个图表占 `48%` 宽度
- **移动端**：单列布局，每个图表占 `100%` 宽度

---

## 表格样式规范

```javascript
var thStyle = {
  padding: '10px 12px', textAlign: 'left', fontWeight: 600,
  color: '#475569', fontSize: 12,
  borderBottom: '2px solid #e2e8f0',
  background: '#f8fafc', whiteSpace: 'nowrap', cursor: 'pointer',
};

var tdStyle = {
  padding: '10px 12px', borderBottom: '1px solid #e2e8f0',
  color: '#475569', fontSize: 13,
};

var statusBadgeStyle = {
  display: 'inline-block', padding: '2px 8px', borderRadius: 4,
  fontSize: 11, fontWeight: 600, lineHeight: '18px',
};

var detailLinkStyle = {
  color: '#3b82f6', fontSize: 12, textDecoration: 'none',
  cursor: 'pointer', fontWeight: 500,
};
```

### 表格功能要求

| 功能 | 说明 |
|------|------|
| **列排序** | 点击表头切换升序/降序 |
| **分页** | 每页 10 条，底部分页器 |
| **详情链接** | 项目名称可点击跳转详情页 |
| **状态标签** | 彩色 badge 展示 |
| **斑马纹** | 奇偶行交替背景色 |
| **筛选联动** | 跟随全局筛选器 |
