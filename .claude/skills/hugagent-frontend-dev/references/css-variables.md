# CSS 变量与样式规范参考

> **重要**: 颜色、圆角、间距等视觉规范以 `references/ui-design-spec.md` 为权威来源。本文件侧重 CSS 实践模式。

## 全局变量 (variables.css)

以下变量需与 UI 设计规范对齐（参见 `ui-design-spec.md` 第 12 节的完整映射）。

```css
:root {
  /* 主色 */
  --color-primary: #126DFF;
  --color-primary-hover: #3C87FF;
  --color-primary-active: #0862F3;
  --color-primary-disabled: #A0C5FF;
  --color-primary-bg: #DBE9FF;
  --color-primary-light: #EBF2FF;

  /* 边框与填充 */
  --color-fill-heavy: #8C94A2;
  --color-fill-deep: #B3BAC8;
  --color-fill: #D8DBE2;
  --color-border: #E3E6EA;
  --color-fill-hover: #EBEDEE;
  --color-bg-gray: #F5F6F7;

  /* 文字 */
  --color-text: #101828;
  --color-text-secondary: #475467;
  --color-text-tertiary: #667085;
  --color-text-placeholder: #98A2B3;
  --color-text-white: #FFFFFF;

  /* 语义色 */
  --color-success: #02B589;
  --color-warning: #F8AB42;
  --color-error: #FC5D5D;

  /* 圆角 */
  --radius-xs: 6px;   /* 标签等小面积 */
  --radius-sm: 10px;  /* 按钮、输入框、下拉框 */
  --radius-md: 16px;  /* 卡片视图、背景卡片 */
  --radius-lg: 24px;  /* 弹窗、大输入框 */

  /* 间距 (4px 基准) */
  --space-xs: 4px;
  --space-sm: 8px;
  --space-md: 16px;
  --space-lg: 20px;
  --space-xl: 40px;

  /* 字体（后一条定义生效：系统栈优先，中文回退 PingFang/雅黑） */
  --font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue",
    "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
  --font-family-number: "DingTalk Sans", "PingFang SC", "Microsoft YaHei", sans-serif;

  /* Motion 令牌（TS 侧镜像在 utils/motionTokens.ts） */
  --motion-duration-instant: 100ms;
  --motion-duration-fast: 160ms;
  --motion-duration-normal: 240ms;
  --motion-duration-slow: 320ms;
  /* easing: --motion-ease-brand-out cubic-bezier(.16,1,.3,1) / --motion-ease-standard cubic-bezier(.4,0,.2,1) */
}
```

> 阴影令牌：`--shadow-card` / `--shadow-card-hover` / `--shadow-dropdown`（双层 ambient + key-light）。
> 底部还有 legacy 别名（`--primary` / `--bg` / `--text` / `--muted` / `--border` 等），新代码不要使用。

## CSS 类名前缀

所有自定义类使用 `.jx-` 前缀，避免与 Ant Design 冲突。

## 命名规则 (BEM-like)

| 类型 | 格式 | 示例 |
|------|------|------|
| Block | `.jx-blockName` | `.jx-chatArea` |
| Element | `.jx-blockName-element` | `.jx-chatArea-header` |
| Modifier | `.jx-blockName--modifier` | `.jx-chatArea--empty` |
| Sub-element | `.jx-blockName-element-sub` | `.jx-fileCard-icon-bg` |

## 常用尺寸

| 用途 | 值 |
|------|-----|
| 侧边栏宽度 | 260px (展开) / 0 (收起) |
| 输入框最大宽度 | 840px |
| 消息最大宽度 | 840px |
| 面板内边距 | 20px (桌面) / 16px (移动) |
| 卡片间距 | 8px - 16px |
| 卡片圆角 | 16px (`--radius-md`) |
| 输入框圆角 | 10px (`--radius-sm`) |
| 按钮圆角 | 10px (`--radius-sm`) |
| 弹窗圆角 | 24px (`--radius-lg`) |
| 按钮最小高度 | 36px |

## 常用样式模式

### 卡片

```css
.jx-card {
  padding: var(--space-md) var(--space-lg);  /* 16px 20px */
  border-radius: var(--radius-md);           /* 16px */
  border: 1px solid var(--color-border);
  background: #fff;
  transition: box-shadow 0.2s, border-color 0.2s;
}
.jx-card:hover {
  border-color: var(--color-primary-hover);
  box-shadow: 0 4px 12px rgba(18, 109, 255, 0.08);
}
```

### 按钮

```css
.jx-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 18px;
  border-radius: var(--radius-sm);  /* 10px */
  font-size: 14px;
  cursor: pointer;
  transition: all 0.2s;
  min-height: 36px;
}
```

### 输入区

```css
.jx-inputArea {
  width: 100%;
  max-width: 840px;
  margin: 0 auto;
  border-radius: var(--radius-lg);   /* 24px */
  border: 1.5px solid var(--color-border);
  background: #fff;
}
```

## 文件索引

| 文件 | 内容 |
|------|------|
| `variables.css` | CSS 变量 + body 背景 + AntD 覆写 |
| `chat.css` | 聊天区域、输入框、消息气泡、空状态 |
| `sidebar.css` | 侧边栏、导航、历史列表 |
| `catalog.css` | Catalog 面板、搜索、列表项 |
| `tool.css` | 工具输出面板、展开/折叠 |
| `common.css` | 全局工具类、弹窗覆写 |
