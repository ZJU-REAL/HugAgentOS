# UI 设计规范参考

本文档基于 UI 团队出品的设计规范（`img/设计规范/`），是前端开发的视觉对齐依据。

---

## 1. 颜色系统

### 1.1 主色 (Primary Blue)

| 状态 | 色值 | 用途 |
|------|------|------|
| 常规 | `#126DFF` | 主按钮、链接、选中态 |
| 悬浮 | `#3C87FF` | hover 状态 |
| 按下 | `#0862F3` | active/pressed 状态 |
| 禁用、描边 | `#A0C5FF` | disabled 按钮描边 |
| 背景 | `#DBE9FF` | 选中项背景、badge 背景 |
| 浅色/白色悬浮 | `#EBF2FF` | 列表 hover、浅色背景 |

### 1.2 边框与填充 (Neutral Gray)

| 状态 | 色值 | 用途 |
|------|------|------|
| 重（填充或 icon） | `#8C94A2` | 实心 icon 填充 |
| 深/悬浮 | `#B3BAC8` | 深边框、placeholder |
| 一般 | `#D8DBE2` | 常规分割线、边框 |
| 描边 | `#E3E6EA` | 轻量描边 |
| 灰色背景上的 hover | `#EBEDEE` | 灰背景区域的 hover |
| 背景灰 | `#F5F6F7` | 次级背景、侧边栏底色 |

### 1.3 文字色

| 色值 | 用途 |
|------|------|
| `#262626` | 强调文字、正文标题 |
| `#4D4D4D` | 次强调、大段正文内容 |
| `#808080` | 次要信息 |
| `#B3B3B3` | 置灰/说明文字 |
| `#FFFFFF` | 纯白文字（深色背景上） |

### 1.4 语义色 — 成功 (Success Green)

| 状态 | 色值 |
|------|------|
| 常规 | `#02B589` |
| 悬浮 | `#11D4A4` |
| 按下 | `#04A87F` |
| 禁用、描边 | `#80DAC4` |
| 背景 | `#CFF8EE` |
| 浅色/白色悬浮 | `#E6FFF9` |

### 1.5 语义色 — 警告 (Warning Orange)

| 状态 | 色值 |
|------|------|
| 常规 | `#F8AB42` |
| 悬浮 | `#FFC16C` |
| 按下 | `#F69D24` |
| 禁用、描边 | `#FBD5A0` |
| 背景 | `#FFECD1` |
| 浅色/白色悬浮 | `#FFF5E8` |

### 1.6 语义色 — 失败/危险 (Error Red)

| 状态 | 色值 |
|------|------|
| 常规 | `#FC5D5D` |
| 悬浮 | `#FF7676` |
| 按下 | `#F95555` |
| 禁用、描边 | `#FCAAAA` |
| 背景 | `#FFDFDF` |
| 浅色/白色悬浮 | `#FFF0F0` |

### 1.7 Icon 专用色

按需取色，保证页面**蓝色系占比最多**。

| 颜色 | 色值 |
|------|------|
| 蓝色 | `#126DFF` |
| 红色 | `#F25149` |
| 紫色 | `#7655FA` |
| 绿色 | `#02B589` |
| 橙色 | `#F8AB42` |

---

## 2. 文字排版

### 2.1 字体栈

| 平台 | 字体 | 字重 |
|------|------|------|
| macOS | PingFang SC | 正常 (400) / 加粗 (600) |
| Windows | 微软雅黑 | 正常 (400) / 加粗 (700) |
| 特殊数字/汉字 | 钉钉进步体 / DingTalk Sans | 正常 (400) |

**CSS font-family 推荐写法：**
```css
font-family: "PingFang SC", "Microsoft YaHei", "微软雅黑", "DingTalk Sans", sans-serif;
```

### 2.2 字号层级

| 字号 | 字重 | 用途 |
|------|------|------|
| 12px | 常规 | 辅助文案、标签 |
| 14px | 常规 | 正文 - 常规（默认正文） |
| 14px | 加粗 | 小标题 |
| 16px | 常规 | 正文 - 大字号 |
| 16px | 加粗 | 常规标题 |
| 18px | 加粗 | 特殊标题和强调 |
| 22px | 加粗 | 页面一级标题 |
| 44px | 加粗 | 页面 banner、运营等标题 |

### 2.3 日期格式

- 用 `/` 分隔：`2025/08/18 14:08`
- 到最末右对齐

---

## 3. 图标

- **推荐图标库**: IconPark（字节跳动）
- **最小切图尺寸**: 16px，其他尺寸为 4 的倍数

### 3.1 线性图标

| 尺寸 | 描边宽度 | 圆角 | 端点 |
|------|---------|------|------|
| 32px | 2.4px 居中 | 2px | 圆形 |
| 16px | 1.2px 居中 | 1px | 圆形 |

> 使用时只根据 32px / 16px 两种基准尺寸，通过缩放功能来变化大小。

### 3.2 面性图标 — 单色

- 尺寸 32px，背景圆角 8px

### 3.3 面性图标 — 双色

- 同色系，主颜色 100%，浅色 50%

---

## 4. 间距

以 **4px 为基本单位**，使用 4 的倍数作为间距。

| 值 | 常见用途 |
|-----|---------|
| 4px | 紧凑间距（标签内边距、小图标间距） |
| 8px | 元素间常规间距 |
| 16px | 卡片内间距、段落间距 |
| 20px | 模块间距、表单行距 |
| 40px | 大区域分隔 |

---

## 5. 圆角

系统使用**大圆角**表达 AI、时尚、亲近的品牌感。

| 圆角值 | 用途 | 示例 |
|--------|------|------|
| 4px | 小面积元素 | 标签、小卡片 |
| 8px | 常用组件 | 按钮、输入框、下拉框、菜单 |
| 12px | 分割模块 | 卡片视图、背景卡片 |
| 20px | 大容器 | 弹窗、大输入框 |

---

## 6. 投影

为整体性统一限制投影层级，分为两种状态：

- **常态**: 轻微投影（几乎无感知）
- **悬停/hover**: 加深投影（凸显层级）

可根据具体场景微调投影颜色。

---

## 7. 按钮

最小切图尺寸 **36px**，其他为 4 的倍数。

### 7.1 按钮类型

| 类型 | 说明 |
|------|------|
| 主要按钮 | 蓝色填充（`#126DFF`），白色文字 |
| 次要按钮 | 白色填充、蓝色描边 |
| 文字按钮 | 无边框、无背景，文字色即操作色 |
| 图标按钮 | 仅图标，无文字 |
| 成功按钮 | 绿色填充（`#02B589`） |
| 危险按钮 | 红色填充（`#FC5D5D`） |

### 7.2 按钮尺寸

| 尺寸 | 高度（参考） |
|------|------------|
| 大 | 40px |
| 中（默认） | 36px |
| 小 | 28px |

### 7.3 按钮状态

每种按钮均有 4 种状态：常规 → 悬浮(hover) → 按下(active) → 禁用(disabled)。

---

## 8. 导航

### 8.1 导航菜单

- 垂直列表，图标 + 文字
- 选中项高亮

### 8.2 Tabs / 标签页

- **一级**: 页面或模块的一级导航，有选中样式、默认样式、hover 样式
- **二级**: 页面或模块的二级导航
- **三级**: 紧凑标签切换

### 8.3 面包屑 / 返回

- **面包屑**: 显示页面在系统层级结构中的位置，并提供上返回，本系统中较少使用
- **返回**: 用于二级页面，能向上返回

---

## 9. 数据录入

### 9.1 输入框

- 标签对齐方式在页面内统一（左对齐 / 右对齐 / 顶对齐）
- 默认高度 36px
- 前后间距 20px（页面范围统一）
- 前后间距需要在整个页面范围统一
- 可附带前缀 icon

### 9.2 下拉选择

- 包含下拉选择 + 搜索
- 基础样式有正文类型、hover 状态
- 有单选和多选模式
- 支持复选框列表

### 9.3 日期选择器

- 标准日历面板
- 支持日期范围选择
- 支持快捷选项（本周/上周/本月等）

### 9.4 多选 / 单选

- 单选: 圆形 radio，选中为蓝色填充
- 多选: 方形 checkbox，选中为蓝色勾选
- 有横排和竖排两种布局

### 9.5 开关

- 默认灰色背景
- 开启状态为主色蓝（`#126DFF`）
- 带有开/关文字标识

---

## 10. 数据展示

### 10.1 标签 (Tag)

- 慎重使用标签，保证页面干净有序
- 同一信息前台展现阶段对比最多使用 2 种颜色样式
- 类型：基础标签、可关闭标签、表态标签

### 10.2 卡片 (Card)

- padding: 16–20px（可根据内容调整，特殊情况为 4 的倍数）
- 类型一：图标 + 标题 + 描述 + 标签
- 类型二：大面积内容卡片（标题 + 正文段落）
- 圆角 12px

### 10.3 气泡提示卡片

- 点击/鼠标移入元素时弹出气泡式卡片浮层

### 10.4 Tips 提示

- 用于展示不完全的内容，鼠标移入时展示完整内容
- 有浅色和深色两种样式

### 10.5 表格 (Table)

- 标准表格：表头 + 数据行 + 分页
- 特殊表格：带颜色标记行

---

## 11. 数据提示

### 11.1 Alert 警告提示

4 种语义，每种有可关闭和不可关闭两种形态：

| 类型 | 颜色 |
|------|------|
| 成功 | 绿色系 (`#02B589`) |
| 注意 | 橙色系 (`#F8AB42`) |
| 失败 | 红色系 (`#FC5D5D`) |
| 提示信息 | 蓝色系 (`#126DFF`) |

### 11.2 全局提示 (Message)

- 顶部居中弹出，自动消失
- 类型：成功 / 注意 / 失败

### 11.3 弹窗 (Modal)

- 标题 + 内容区 + 底部操作栏（取消 + 确认/保存）
- 确认按钮为主色蓝，危险操作为红色
- 圆角 20px

---

## 12. CSS 变量映射（推荐）

以下为设计规范到 CSS 变量的推荐映射，应用在 `variables.css` 中：

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
  --color-text: #262626;
  --color-text-secondary: #4D4D4D;
  --color-text-tertiary: #808080;
  --color-text-placeholder: #B3B3B3;
  --color-text-white: #FFFFFF;

  /* 成功 */
  --color-success: #02B589;
  --color-success-hover: #11D4A4;
  --color-success-active: #04A87F;
  --color-success-disabled: #80DAC4;
  --color-success-bg: #CFF8EE;
  --color-success-light: #E6FFF9;

  /* 警告 */
  --color-warning: #F8AB42;
  --color-warning-hover: #FFC16C;
  --color-warning-active: #F69D24;
  --color-warning-disabled: #FBD5A0;
  --color-warning-bg: #FFECD1;
  --color-warning-light: #FFF5E8;

  /* 错误/危险 */
  --color-error: #FC5D5D;
  --color-error-hover: #FF7676;
  --color-error-active: #F95555;
  --color-error-disabled: #FCAAAA;
  --color-error-bg: #FFDFDF;
  --color-error-light: #FFF0F0;

  /* Icon 色 */
  --color-icon-blue: #126DFF;
  --color-icon-red: #F25149;
  --color-icon-purple: #7655FA;
  --color-icon-green: #02B589;
  --color-icon-orange: #F8AB42;

  /* 圆角 */
  --radius-xs: 4px;
  --radius-sm: 8px;
  --radius-md: 12px;
  --radius-lg: 20px;

  /* 间距 */
  --space-xs: 4px;
  --space-sm: 8px;
  --space-md: 16px;
  --space-lg: 20px;
  --space-xl: 40px;

  /* 字体 */
  --font-family: "PingFang SC", "Microsoft YaHei", "微软雅黑", sans-serif;
  --font-family-number: "DingTalk Sans", "PingFang SC", "Microsoft YaHei", sans-serif;

  /* 字号 */
  --font-size-xs: 12px;
  --font-size-sm: 14px;
  --font-size-md: 16px;
  --font-size-lg: 18px;
  --font-size-xl: 22px;
  --font-size-xxl: 44px;
}
```

---

## 13. Ant Design 主题映射

使用 Ant Design ConfigProvider 时，按以下规范配置 token：

```typescript
const theme = {
  token: {
    colorPrimary: '#126DFF',
    colorSuccess: '#02B589',
    colorWarning: '#F8AB42',
    colorError: '#FC5D5D',
    colorInfo: '#126DFF',
    colorText: '#262626',
    colorTextSecondary: '#4D4D4D',
    colorTextTertiary: '#808080',
    colorTextQuaternary: '#B3B3B3',
    colorBorder: '#E3E6EA',
    colorBorderSecondary: '#D8DBE2',
    colorBgContainer: '#FFFFFF',
    colorBgLayout: '#F5F6F7',
    borderRadius: 8,
    borderRadiusLG: 12,
    borderRadiusSM: 4,
    fontFamily: '"PingFang SC", "Microsoft YaHei", "微软雅黑", sans-serif',
    fontSize: 14,
    fontSizeSM: 12,
    fontSizeLG: 16,
  },
};
```
