# Chart Type Selection Guide

Rules for matching data characteristics to ECharts chart types.

## Decision Rules

| Data Pattern | Chart Type | ECharts Series Type |
|---|---|---|
| 1 dimension × 1 metric (ranking) | Horizontal Bar | `bar` (xAxis/yAxis swapped) |
| 1 timeline × 1+ metrics (trend) | Line / Area | `line` with `areaStyle` |
| 1 dimension × 1 metric (proportion) | Pie / Donut | `pie` with `radius` |
| 2 metrics (relationship) | Scatter | `scatter` |
| 3+ metrics (correlation) | Heatmap | `heatmap` |
| 1 dimension × 2 metrics (comparison) | Grouped Bar | `bar` (multiple series) |
| 1 dimension × 1 metric (distribution) | Histogram/Box | Custom `bar` or `boxplot` |
| Hierarchical (part-to-whole) | Treemap / Sunburst | `treemap` / `sunburst` |
| 3 dimensions (flow/relationship) | Sankey | `sankey` |
| Geographic data | Map | `map` (requires geoJSON) |

## ECharts CDN

Use the following CDN for ECharts in HTML templates:
```html
<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js"></script>
```

## Chart Size Standards

- Full-width charts: height 400px, width 100%
- Side-by-side charts: height 350px, width 48%
- Summary cards: height 250px, width 30%
- Pie charts: height 400px
- Heatmaps: height 450px (need space for labels)

## Responsive Behavior

All charts must use `echarts.init(chartDom).resize()` on window resize:
```javascript
window.addEventListener('resize', function() {
    chart.resize();
});
```