
# yida-page-uiux — 自定义页面视觉方向决策

## 定位（先读这段）

本技能是**视觉方向决策层**，不是编码层：

- **产出**：一个「视觉方向决策块」（纯文字方向 + 差异化说明），交给 `yida-custom-page` / `yida-canvas-custom-page` 按各自 `design-system.md` 的 token/组件落地。
- **不产出**：JSX 代码、具体像素数值表（那是 `design-system.md` 的职责）。
- **为什么存在**：`design-system.md` 是「安全平庸」的 token 层——直接套用会得到千篇一律的 AI 模板脸。本技能在写码前先强制做**差异化视觉决策**，让每个页面有自己的性格。

> **一句话分工**：`yida-page-uiux` 定**方向与差异** → `design-system.md` 提供**数值与组件** → `yida-custom-page` / `yida-canvas-custom-page` 写 **JSX**。

## 重要边界：自定义页面不做表单/录入 UI

- 自定义页面**只做展示、工具、看板、详情**，**不手写表单控件**（输入框群、字段校验、提交表单）。
- 需要录入/提交 → 一律走宜搭**原生表单**（`yida-create-form-page` 建普通表单 / `yida-create-process` 建带审批表单）。
- 详情页 / 列表页需要「新建 / 编辑」入口时，用既有 iframe 方式嵌入原生表单：`workbench/{formUuid}?iframe=true`（禁用 `formDetail`），不要在自定义页里重写表单。
- 因此本技能**不含「表单页」场景**。页面类型只覆盖展示/工具/看板/详情/落地页这类自定义页。

## 核心工作流（严格按顺序，不跳步）

每一步的判定法、checklist、代码/模板都在 `workflow/` 对应文件里，**按需只读命中的那一个**。

```
Step 0 导航形态判定 → Step 1 页面类型 → Step 2 意图解码 → Step 3 路由 scene
   → Step 4 视觉方向决策 → Step 5 图标与素材 → Step 6 去 AI 味自检 → 输出决策块
```

| 步骤 | 做什么 | 详细文件 | 产出 |
|---|---|---|---|
| **Step 0** 导航形态判定 | 应用导航是否隐藏（`isRenderNav`）？决定要不要跟应用框架融合、要不要自带导航壳 | [workflow/step-0-nav-shape.md](workflow/step-0-nav-shape.md) | 导航形态（可见/隐藏 + 壳型） |
| **Step 1** 页面类型判定 | 锁定属于哪一类（workbench/dashboard/list/detail/landing），决定后续所有决策 | [workflow/step-1-page-type.md](workflow/step-1-page-type.md) | 页面类型 + 判定依据 |
| **Step 2** 意图解码 | 提取 2-3 个气质关键词 + 3-5 条项目特定设计原则 | [workflow/step-2-intent-decode.md](workflow/step-2-intent-decode.md) | 气质关键词 + 设计原则 |
| **Step 3** 路由到 scene | 只读命中的那一个 `references/scenes/*.md`，拿骨架/密度/焦点/组件套餐 | [workflow/step-3-scene-routing.md](workflow/step-3-scene-routing.md) | 布局骨架 + 密度 + 焦点 |
| **Step 4** 视觉方向决策 | 调用 `visual-decision-engine.md`，做 5 个差异化维度 + 反默认自检 | [workflow/step-4-visual-decision.md](workflow/step-4-visual-decision.md) | 差异化 5 维 + 反默认说明 |
| **Step 5** 图标与素材 | 默认内联 SVG 语义集；iconfont 仅用户提供项目 URL 时 opt-in | [workflow/step-5-icon-and-assets.md](workflow/step-5-icon-and-assets.md) | 图标策略 |
| **Step 6** 去 AI 味自检 | 逐条扫黑名单 + 8 问自检，任一命中即回对应 Step 修正 | [workflow/step-6-deai-selfcheck.md](workflow/step-6-deai-selfcheck.md) | 自检通过 |
| **输出** | 汇总成「视觉方向决策块」，提示交码落地 | [workflow/output-decision-block.md](workflow/output-decision-block.md) | 视觉方向决策块 |

## 核心规则（红线，任何步骤都适用）

- 🚫 **严禁 emoji（FATAL）**：页面渲染的任何位置一律禁止 emoji，需要图标走功能性内联 SVG。详见 [Step 6](workflow/step-6-deai-selfcheck.md)。
- **主色策略**：导航可见时主色跟随平台品牌 `var(--color-brand1-*)`，不自由换主色相；导航隐藏（`isRenderNav=false`）时主色相可自立。**语义色（成功/警告/错误）永远固定**。详见 [Step 0](workflow/step-0-nav-shape.md) / [Step 4](workflow/step-4-visual-decision.md)。
- **不做表单**：见上「重要边界」。
- **只讲方向不写码**：本技能只产出决策块，JSX 与像素数值归 `design-system.md` + `yida-custom-page`。

## 参考文档

| 文档 | 覆盖范围 | 何时阅读 |
|------|---------|---------|
| [视觉决策引擎](references/visual-decision-engine.md) | 5 个风格意图 + 反默认组合表 + 5 个差异化维度 + 强制第二选择 + 差异化自检 | Step 4 做视觉决策时（所有场景共用） |
| `yida-nav-shell` 子技能 | 页面隐藏应用导航后用 JSX 自建侧边/顶部/混合/浮动/标签导航壳：选型表 + 骨架 + native/Canvas 代码 + 多视图切换 | Step 0 判定导航隐藏、需自建导航壳时调用 `use_skill("yida-nav-shell", "设计页面内自绘导航壳")` |
| [scene-workbench](references/scenes/workbench.md) | 工作台/门户首页的骨架/密度/焦点/组件套餐/去 AI 味要点 | 页面类型 = 工作台时 |
| [scene-dashboard](references/scenes/dashboard.md) | 数据看板/驾驶舱 | 页面类型 = 看板时 |
| [scene-list](references/scenes/list.md) | 列表/管理页 | 页面类型 = 列表时 |
| [scene-detail](references/scenes/detail.md) | 详情/展示页（叠加详情页叙事纪律） | 页面类型 = 详情时 |
| [scene-landing](references/scenes/landing.md) | 官网/落地页/品牌展示/产品介绍页：Section 构图、素材锚点、页面节奏 | 页面类型 = 官网落地页时 |
| [设计规范](../yida-custom-page/references/design-system.md) | token/组件实现层：色彩/圆角/字体/间距/组件样式的具体数值 | 方向定完、交码落地时 |
| [素材资源](../yida-custom-page/references/assets-guide.md) | 图片/图标/音效素材库、renderIcon 骨架、CDN 安全规范 | 需要图标/图片/音效时 |
| [字段与 URL 参考](../../references/field-and-url-reference.md) | 隐藏导航 `isRenderNav=false`、各页面类型 URL 拼接模板、跨页跳转 | Step 0 判定导航形态、自带导航壳拼跳转 URL 时 |
