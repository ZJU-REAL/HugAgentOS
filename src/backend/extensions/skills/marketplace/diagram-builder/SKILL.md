---
name: diagram-builder
description: "专业架构图与流程图绘制 skill。绘制系统架构图、微服务架构、网络拓扑图、业务流程图、数据流图、时序图、状态机、ER图等。支持 show_widget 内联 SVG 渲染与 Mermaid 代码块两种方式。触发关键词：架构图、流程图、时序图、状态机、拓扑图、ER图、数据流图、draw、diagram、flowchart、architecture。"
agent_created: true
---

# diagram-builder — 架构图与流程图绘制 Skill

## 概述

本 skill 提供两种图表绘制方式：

1. **内联可视化（推荐）**：调用 `read_me` + `show_widget` 工具，在对话中直接渲染精美 SVG/HTML 图表。
2. **Mermaid 代码块**：在 Markdown 中输出 Mermaid 语法，适合需要嵌入文档的场景。

---

## 方式一：内联 SVG/HTML（show_widget，推荐）

### 触发时机

凡用户明确要求"绘制"、"画出"、"可视化"某类图表，或对话内容用图表呈现比文字更清晰时，优先使用此方式。

### 操作步骤

1. **先调用 `read_me`**，加载 `diagram` 模块（获取 CSS 变量、颜色、排版规则）。
2. **再调用 `show_widget`**，传入原始 SVG 或 HTML 片段。
3. SVG 必须以 `<svg>` 标签开头，viewBox 格式为 `0 0 680 <height>`，宽度固定 680px。
4. 禁止包含 `<!DOCTYPE>`、`<html>`、`<head>`、`<body>` 标签。

### 图表类型与示例

参考 `references/diagram-types.md`，其中包含以下图表类型的 SVG 骨架与最佳实践：

- 系统架构图（分层盒子 + 箭头）
- 微服务 / 云原生架构图（服务网格、API Gateway、数据库集群）
- 网络拓扑图（节点 + 连线）
- 业务流程图（泳道图、决策菱形）
- 数据流图（数据源 → 处理 → 存储 → 输出）
- 时序图（角色 + 时序线 + 消息箭头）
- 状态机图（圆角矩形状态 + 转换箭头）
- ER 图（实体矩形 + 关系线）

### SVG 设计规范

- **颜色**：加载 `read_me diagram` 后按其 CSS 变量定义使用，保持整体一致性。
- **字体**：系统默认无衬线字体，中文内容使用 `font-family="sans-serif"`。
- **间距**：组件间距不少于 16px，文字与边框间距不少于 8px。
- **箭头**：使用 SVG `<marker>` 定义 arrowhead，保持风格统一。
- **分组**：用 `<g>` 元素对同类节点分组，复杂图表使用 `<rect>` 作为容器背景框。
- **图例**：复杂图表右下角添加图例说明颜色/形状含义。

### 箭头 Marker 规范（关键！）

参考 `references/svg-marker-guide.md` 获取完整箭头定义规范。核心要点：

- **方向规则**：marker 的路径必须朝右（+x 方向），三角形尖端在 x 最大值处。`orient="auto"` 会自动沿线的方向旋转，所以朝右的路径在竖线上会自然旋转为朝下。
- **尺寸规则**：`markerWidth` 和 `markerHeight` 必须 **≥ viewBox 宽高**，否则视口裁切会导致箭头只剩一半或方向错乱。
- **refX/refY**：`refX` 放到路径尖端 x 坐标、`refY` 放 y 中心，确保尖端精确对齐线尾。
- **图例中的箭头**：图例行内的水平箭头线同样使用向下 marker，`orient="auto"` 会自动适配水平方向。

### 分层架构图布局规范

参考 `references/architecture-layout-guide.md` 获取完整分层布局指南。核心要点：

- **层级分组框**：每层用 `stroke-dasharray` 虚线圆角矩形包裹所有同层组件（标题条 + 子组件），左上角标注层号。
- **层间箭头间距**：至少 40px，箭头上端距上层框底 8px、下端距下层框顶 8px，确保箭头完整可见。
- **viewBox 高度**：先计算所有内容底部坐标 + 图例高度 + 16px 余量，再四舍五入取整。宁大勿小，避免末尾元素被裁切。
- **图例位置**：固定在最底部独立区域，与最下层内容保持 ≥16px 间距，横排紧凑布局。

---

## 方式二：Mermaid 代码块

当用户需要在文档中嵌入图表，或明确要求 Mermaid 语法时使用。

### Mermaid 常用图表类型

```
flowchart TD        # 自上而下流程图
flowchart LR        # 从左到右流程图
sequenceDiagram     # 时序图
stateDiagram-v2     # 状态机
erDiagram           # ER 图
classDiagram        # 类图
gantt               # 甘特图
graph               # 通用图
```

参考 `references/mermaid-cheatsheet.md` 获取完整语法速查与示例模板。

---

## 绘图原则

1. **先理解，再绘制**：完整理解用户意图后再动手，必要时确认组件和关系。
2. **分层清晰**：架构图按"前端 → 后端 → 数据层 → 基础设施"等层次布局，避免交叉。
3. **信息密度适中**：单张图不超过 20 个节点；复杂系统拆成多张图分步展示。
4. **中文友好**：节点标签支持中文，保证编码和字体兼容。
5. **复杂图分多个 show_widget**：一个 show_widget 表示一个聚焦视角，多图配合文字解释形成完整叙述。

---

## 快速参考

| 用户意图 | 推荐方式 | 参考文件 |
|---------|---------|---------|
| 系统/微服务架构图 | show_widget SVG | references/diagram-types.md |
| 业务流程 / 泳道图 | show_widget SVG 或 Mermaid flowchart | references/diagram-types.md |
| 时序图 | show_widget SVG 或 Mermaid sequenceDiagram | references/mermaid-cheatsheet.md |
| 状态机 | show_widget SVG 或 Mermaid stateDiagram-v2 | references/mermaid-cheatsheet.md |
| ER 图 | show_widget SVG 或 Mermaid erDiagram | references/mermaid-cheatsheet.md |
| 嵌入 Markdown 文档 | Mermaid 代码块 | references/mermaid-cheatsheet.md |
