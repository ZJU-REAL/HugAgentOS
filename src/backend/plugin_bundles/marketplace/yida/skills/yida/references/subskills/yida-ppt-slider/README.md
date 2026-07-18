
# 宜搭 PPT 幻灯片开发指南

## 适用场景

| 用户意图 | 触发条件 | 处理方式 |
|---------|---------|---------|
| 在宜搭内创建演示文稿 | "PPT"、"幻灯片"、"演示页面"、"产品路演" | 使用本技能 |
| 需要读取宜搭数据的演示 | 演示页要接入表单、权限或宜搭页面能力 | 使用本技能 |
| 纯静态演讲稿 | 不依赖宜搭发布、不读取宜搭数据 | 优先改用独立 HTML 幻灯片能力 |

## 核心规则

### 致命规则（FATAL）

1. **禁止 Hooks / import / require**：必须使用宜搭原生 `export function` 页面模式，第三方库通过 CDN 或内联代码接入。
2. **状态只走 `_customState + forceUpdate`**：状态变更后调用 `this.forceUpdate()`，不要用 Hooks，也不要把业务状态写进 React state。
3. **事件绑定必须真实可触发**：禁止 `onClick={foo()}`、`onClick={handleDotClick(i)}`、JSX 小写 `onclick`；可见 `<button>` 必须有 handler 或显式 `disabled`。
4. **生命周期必须清理**：键盘、触摸、鼠标、hash、fullscreen、定时器、ECharts 实例都要在 `didUnmount` 中清理。
5. **图片必须完整展示**：使用 `objectFit: 'contain'`，不要用 `cover` 裁剪演示素材。
6. **发布前必须校验**：执行 `openyida check-page <源文件>`，首次发布建议 `openyida publish ... --health-check`。

### 重要规则（IMPORTANT）

1. **发布前先确认**：展示页数、标题列表、主题方案，获得用户明确同意后再发布真实页面。
2. **幻灯片数据顶层维护**：用顶层 `SLIDES` 数组描述页面，不要把数据硬编码在 `renderJsx` 中。
3. **全屏覆盖宜搭容器**：页面根节点用 `position: fixed; top:0; left:0; right:0; bottom:0` 覆盖默认容器。
4. **移动端适配**：使用 `this.utils.isMobile()` 判断设备，调整字号、间距、图片高度和触控区域。
5. **演讲笔兼容**：键盘事件需支持方向键、`PageDown`、`PageUp`，数字键跳页用 300ms 缓冲。
6. **导航默认隐藏**：底部翻页导航默认隐藏，鼠标靠近底部或移动端触摸时显示。
7. **隐藏平台导航**：发布 PPT 后执行 `openyida update-form-config <appType> <formUuid> false "<页面标题>"`。

## 主题选择

生成 PPT 前先让用户选择视觉主题：

| theme | 风格 | 特色 | 适合场景 |
|-------|------|------|---------|
| `default` | 浅色简约 | 白底黑字、多 accent 主题色、数据驱动 `SLIDES` | 技术分享、产品路演、通用演示 |
| `dark-tech` | 深色科技风 | `#0B0F19` 背景、Canvas 粒子、电影级转场、玻璃态卡片 | 企业培训、产品发布、科技感演示 |

推荐话术：

> 我来帮你生成 PPT。请先选择视觉风格：A. default（浅色简约）；B. dark-tech（深色科技风）。

## 开发流程

```bash
# Step 1: 只读检测环境和登录态
openyida env --json
openyida login --check-only --json

# Step 2: 创建应用和页面（已有则跳过）
openyida create-app "<应用名>"
openyida create-page <appType> "<页面名>"

# Step 3: 编写页面源码
# 文件建议：project/pages/src/<页面名>.oyd.jsx

# Step 4: 本地校验
openyida check-page project/pages/src/<页面名>.oyd.jsx

# Step 5: 向用户展示配置摘要并确认后发布
openyida publish project/pages/src/<页面名>.oyd.jsx <appType> <formUuid> --health-check

# Step 6: 隐藏宜搭顶部导航
openyida update-form-config <appType> <formUuid> false "<页面名>"
```

## 技术栈与页面骨架

| 项 | 规范 |
|----|------|
| 框架 | React 16，宜搭原生 `export function` 页面模式 |
| 样式 | 内联 style；需要全局动画时用 `<style>` 注入 |
| 状态 | 顶层 `var _customState` + `getCustomState` / `setCustomState` / `forceUpdate` |
| 数据 | 顶层 `var SLIDES = [...]`，每页一个对象 |
| 生命周期 | `didMount` 注册事件，`didUnmount` 清理事件和定时器 |
| 文件名 | 推荐 `project/pages/src/<name>.oyd.jsx` |

## 幻灯片类型速查

| 类型 | 用途 | 关键字段 |
|------|------|---------|
| `cover` | 封面页 | `eyebrow`, `title`, `subtitle`, `meta`, `tags` |
| `toc` | 目录页 | `title`, `items` |
| `chapter` | 章节过渡页 | `partNum`, `title`, `subtitle`, `desc` |
| `key-points` | 要点列表页 | `chapter`, `title`, `subtitle`, `points` |
| `image-text` | 图文混排页 | `chapter`, `title`, `subtitle`, `body`, `imageUrl` |
| `scene-image` | 场景展示页 | `chapter`, `sceneNum`, `title`, `subtitle`, `body`, `imageUrl`, `tag` |
| `scene-image-top` | 顶部大图场景页 | `chapter`, `sceneNum`, `title`, `subtitle`, `body`, `imageUrl`, `tag` |
| `two-images` | 双图对比页 | `chapter`, `title`, `subtitle`, `leftImage`, `rightImage` |
| `echarts-race` | ECharts 动态柱状图 | `title`, `subtitle`, 数据和渲染函数见参考文档 |
| `ending` | 结束页 | `title`, `subtitle`, `quote`, `cta`, `tags`, `contacts` |

## 必备能力清单

| 能力 | 实现要点 | 详细位置 |
|------|---------|---------|
| 键盘 / 演讲笔翻页 | 方向键、`PageDown`、`PageUp` | [核心示例](references/examples.md) |
| 数字键跳页 | `numBuffer` + 300ms 延迟，支持双位数 | [核心示例](references/examples.md) |
| 底部导航隐藏 | `navVisible` + 底部 80px 鼠标区域 | [核心示例](references/examples.md) |
| 全屏切换 | Fullscreen API，必须用户手势触发 | [核心示例](references/examples.md) |
| 中英文切换 | `I18N[state.lang]` 动态读取 | [核心示例](references/examples.md) |
| 深浅色切换 | `THEME_CONFIG` 动态取色 | [核心示例](references/examples.md) |
| URL hash 定位 | `#页码`，翻页同步 hash | [核心示例](references/examples.md) |
| dark-tech 主题 | 转场、粒子、玻璃态、背景层 | [dark-tech 主题](references/dark-tech-theme.md) |
| ECharts race | ECharts 5.6.0、插值动画、清理实例 | [ECharts race 示例](references/echarts-race-example.md) |

## 样式速查

| 场景 | 规则 |
|------|------|
| 白色主题 | 背景 `#ffffff`，主文字 `#1a1a2e`，边框 `rgba(26,26,46,0.08)` |
| 推荐 accent | 琥珀 `#d97706`、蓝色 `#0089ff`、紫色 `#c084fc` |
| dark-tech | 背景 `#0B0F19`，详见参考文档 |
| 图片 | `maxWidth/maxHeight: 100%` + `objectFit: 'contain'` |
| 移动端 | 用 `this.utils.isMobile()` 调整字号、padding 和图片高度 |
| 调试工具 | 可用 style 隐藏 `#__lowcode_devtool_switch__` |

## 异常处理

| 异常场景 | 处理方式 |
|---------|----------|
| 键盘翻页无响应 | 检查 `didMount` 是否注册键盘事件，是否支持 `PageDown` / `PageUp` |
| 切换页面后仍触发事件 | 检查 `didUnmount` 是否清理所有事件监听和定时器 |
| 图片显示不完整 | 改为 `objectFit: 'contain'`，限制 `maxWidth/maxHeight` |
| 按钮点不了 | 运行 `openyida check-page <file>`，重点检查 handler 绑定方式 |
| 数字键跳错页 | 检查 300ms 缓冲清空逻辑，页码从 1 开始、索引从 0 开始 |
| 全屏按钮无效 | 确认 Fullscreen API 在真实用户点击事件里调用 |
| 中英文切换未刷新 | 确认文案来自 `I18N[state.lang]`，切换后调用 `forceUpdate()` |
| 顶部导航遮挡 | 发布后执行 `openyida update-form-config ... false ...` |

## 参考文档

| 文档 | 覆盖范围 | 何时阅读 |
|------|---------|---------|
| [核心示例](references/examples.md) | SLIDES、状态、生命周期、渲染、导航、全屏、i18n、主题、hash | 编写页面代码前必读 |
| [dark-tech 主题](references/dark-tech-theme.md) | CSS 动画库、转场配置、样式预设、Canvas 粒子、背景层、主框架 | 用户选择 `dark-tech` 时必读 |
| [ECharts race 示例](references/echarts-race-example.md) | 动态柱状图完整实现、数据结构、插值动画、清理逻辑 | 需要 `echarts-race` 类型时必读 |
| `yida-custom-page` 子技能 | 宜搭 React 16 页面模式、编码限制、发布前检查 | 不确定页面运行时规则时调用 `use_skill("yida-custom-page", "确认宜搭自定义页面运行时规则")` |
