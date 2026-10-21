# SVG Marker 箭头定义指南

本文档提供 SVG `<marker>` 箭头的正确定义方式，避免常见的方向错误和裁切问题。

---

## 核心原则

### 1. 路径必须朝右（+x 方向）

`orient="auto"` 的工作原理：将 marker 的 **x 轴** 沿线的方向旋转。因此 marker 路径的尖端必须指向 +x 方向。

```
正确：尖端在右侧
  M1 2L8 5L1 8     ←—— 尖端在 x=8，朝右

错误：尖端在下方  
  M1 2L5 8L9 2     ←—— 尖端在 y=8，朝下
  这个路径在竖线上会旋转为朝左，而不是朝下！
```

**完整向下箭头定义**：

```svg
<marker id="arrow-down" viewBox="0 0 10 10" refX="8" refY="5"
  markerWidth="8" markerHeight="8" orient="auto">
  <path d="M1 2L8 5L1 8" fill="none" stroke="context-stroke"
    stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
</marker>
```

### 2. markerWidth/markerHeight ≥ viewBox

| viewBox | 错误的 markerWidth/Height | 正确的 | 后果 |
|---------|--------------------------|--------|------|
| 0 0 10 10 | 6 | 8 或 10 | 用 6 时 viewBox 被压缩，路径超出视口被裁切 |
| 0 0 10 10 | 12 | 正常但偏大 | 用 12 时留了 margin，也 OK |

**规则：markerWidth ≥ viewBox 宽度，markerHeight ≥ viewBox 高度。推荐相等或略大 2 单位。**

### 3. refX/refY 精确对齐

| marker 参数 | 值 | 含义 |
|------------|-----|------|
| refX | 8 | 尖端 x 坐标，与线尾对接 |
| refY | 5 | 箭头垂直中心，保持居中 |
| orient | "auto" | 按线的方向自动旋转 |

---

## 常见错误与修复

| 症状 | 根因 | 修复 |
|------|------|------|
| 箭头朝左 | 路径尖端朝下（y 轴），orient auto 旋转后方向错 | 路径改为朝右（+x），尖端 x=8 |
| 箭头只显示半边 | markerHeight < viewBox 高度，下半截被裁切 | markerHeight 改为 ≥10 |
| 箭头与线不对齐 | refX/refY 不在尖端坐标 | refX 对齐路径尖端 x，refY 取路径 y 中点 |
| 图例行内箭头方向错 | 行内水平线用了 `orient="auto-start-reverse"` 或未设 orient | 用 `orient="auto"`，路径朝右即可适配任何方向 |

---

## 完整示例：向下箭头 marker

```svg
<defs>
  <marker id="arrow-down"
    viewBox="0 0 10 10"
    refX="8"
    refY="5"
    markerWidth="8"
    markerHeight="8"
    orient="auto">
    <path d="M1 2L8 5L1 8"
      fill="none"
      stroke="context-stroke"
      stroke-width="1.5"
      stroke-linecap="round"
      stroke-linejoin="round"/>
  </marker>
</defs>

<!-- 竖线：箭头自动朝下 -->
<line x1="340" y1="168" x2="340" y2="206"
  stroke="#888" stroke-width="1.8" marker-end="url(#arrow-down)"/>

<!-- 横线（如图例行内）：orient="auto" 自动适配为朝右 -->
<line x1="526" y1="11" x2="544" y2="11"
  stroke="#888" stroke-width="1.8" marker-end="url(#arrow-down)"/>
```

---

## 箭头尺寸选择

| 图表类型 | 推荐 markerWidth/Height | stroke-width | 线 stroke-width |
|---------|------------------------|-------------|-----------------|
| 大型架构图（680×800+） | 8 | 1.5 | 1.8 |
| 中型图（680×400~600） | 8 | 1.5 | 1.5 |
| 小型图/流程图 | 6 | 1.2 | 1.2 |
| 图例中的微型箭头 | 8 | 1.5 | 1.8（与主图一致） |
