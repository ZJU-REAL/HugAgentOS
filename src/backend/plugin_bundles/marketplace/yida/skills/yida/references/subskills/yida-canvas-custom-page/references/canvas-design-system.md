# Code Canvas 主色对齐与视觉落地

本文件是 Code Canvas 页面的**实现层**引导：怎么让首次生成的页面跟随宿主 App 的品牌主题色，而不是永远一片 antd 默认蓝。视觉方向怎么定（页面类型、差异化、去 AI 味）是**栈无关**的，走共用的决策层技能 `yida-page-uiux`，本文件只讲 Canvas（React18 + antd + Tailwind）这套栈怎么把主色落地。

> 决策层：需要视觉方向时调用 `use_skill("yida-page-uiux", "确定 Code Canvas 页面视觉方向")`（先做 Step 0 导航形态判定，再定工作台/仪表盘/列表/详情、5 维差异化、去 AI 味、禁 emoji）。
> 实现层：本文件负责把「主色跟随 App 品牌」落到 antd token / Tailwind / 图表。

> **前提是导航可见**：跟随品牌主色是为了跟应用框架融合。若页面隐藏了应用导航（`isRenderNav=false`，沉浸/独立/门户/大屏，由 `yida-page-uiux` Step 0 判定），主色相可自立、不必严格跟品牌——此时把下文 `readBrandColor` 的取值换成自定的主色即可（antd `colorPrimary` / 图表色都改喂自定色），**语义色仍固定、去 AI 味红线仍生效**。拿不准就按「跟随品牌」这个更安全的默认走。

## 核心事实：CSS 变量能穿透，antd token 不能

Canvas 的 `runtimeCode` 在**宿主页真实 `window`** 里 `new Function` 执行（见 SKILL.md「运行时事实」），组件挂在宿主 DOM 树内。由此得到主色落地的分界：

| 消费方 | 品牌色怎么给 | 原因 |
| --- | --- | --- |
| 普通 DOM / Tailwind 元素（`style` / `className`） | **直接用 CSS 变量** `var(--color-brand1-6)` | CSS 变量沿 DOM 树级联，Canvas 节点在宿主树内，能读到平台注入的 `--color-brand1-*` |
| antd 组件（Button / Table / Tabs…） | **JS 解析成真实色值**喂 `ConfigProvider.theme.token.colorPrimary` | antd 的色板（hover/active/disabled）由 JS 算法从一个真实颜色推导，`var(...)` 是字符串塞不进算法 |
| JS 消费的颜色：recharts `stroke`/`fill`、canvas 绘制、图表配色数组 | **JS 解析成真实色值** | 传给库的是运行时字符串，不走 CSS 级联 |

所以只有「JS 要拿到真实颜色」的场景才需要读值，其余直接用 CSS 变量最省事。

## 读品牌色的 helper（JS 消费场景用）

因为跑在真 window，直接读根节点计算样式即可。带兜底，读不到时退平台默认蓝。

```jsx
// 品牌色阶：1 最浅 → 6 主色 → 10 最深，与平台 --color-brand1-* 对齐
function readBrandColor(level, fallback) {
  try {
    var el = document.documentElement;
    var v = getComputedStyle(el).getPropertyValue('--color-brand1-' + (level || 6)).trim();
    return v || fallback;
  } catch (e) {
    return fallback;
  }
}

// hook 形式：首帧同步取值，无闪烁
function useBrandColor(level, fallback) {
  var s = React.useState(function () { return readBrandColor(level, fallback); });
  return s[0];
}
```

> ⚠️ **变量作用域**：若平台把 `--color-brand1-*` 定义在某个容器而非 `:root`，`document.documentElement` 可能读到空串 → 命中 fallback。更稳的做法是给组件根节点挂 `ref`，在 `useEffect` 里读 `getComputedStyle(rootRef.current)`（组件节点一定在变量作用域内），读到后 `setState` 触发一次重渲染。先用 `documentElement` 同步取，空串再降级到 ref 方案即可。

## antd：ConfigProvider 注入 colorPrimary

用 `readBrandColor` 取主色，交给 `ConfigProvider`，antd 会自动推导 hover/active/disabled 整套色板。语义色（success/warning/error）用 antd 默认，不覆盖，保证语义稳定。

```jsx
import React from 'react';
import { ConfigProvider, Button, Table } from 'antd';

function readBrandColor(level, fallback) {
  try {
    var v = getComputedStyle(document.documentElement)
      .getPropertyValue('--color-brand1-' + (level || 6)).trim();
    return v || fallback;
  } catch (e) { return fallback; }
}

function YidaComp(props) {
  var colorPrimary = readBrandColor(6, '#1677ff'); // 缺失时退平台默认蓝
  return (
    <ConfigProvider
      theme={{
        token: {
          colorPrimary: colorPrimary,   // 主色跟随 App 品牌
          borderRadius: 8,              // 圆角等非主色 token 可按视觉方向调
        },
        // 不覆盖 colorSuccess/colorWarning/colorError，语义色保持固定
      }}
    >
      <div style={{ padding: 16 }}>
        <Button type="primary">主操作</Button>
      </div>
    </ConfigProvider>
  );
}

export default YidaComp;
```

**要点**：`ConfigProvider` 包在组件最外层，页面内所有 antd 组件才统一吃到品牌色。只设 `colorPrimary` 一个入口，不要逐组件手写颜色。

## Tailwind：CSS 变量直接用

Canvas 节点在宿主树内，Tailwind 运行时对普通元素直接用 arbitrary value 引用 CSS 变量即可，**不需要 JS**：

```jsx
// 主色文字 / 背景 / 边框，直接引平台变量，跟随 App 主题
<div className="text-[var(--color-brand1-6)] border border-[var(--color-brand1-3)]">…</div>
<button className="bg-[var(--color-brand1-6)] hover:bg-[var(--color-brand1-5)] text-white rounded-lg px-4 py-2">
  主操作
</button>
```

色阶对应（与 native `design-system.md` 一致）：主色 `brand1-6`、填充按钮 hover 亮一档 `brand1-5`、按下深一档 `brand1-7`、通用浅色 hover 底 `brand1-1`、选中/标签浅底 `brand1-2`。

## 图表 / recharts：用解析后的品牌色

图表颜色是 JS 传给库的字符串，必须用 `readBrandColor`，不能硬编码 `#1677ff`。

```jsx
import React from 'react';
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts';

function readBrandColor(level, fallback) {
  try {
    var v = getComputedStyle(document.documentElement)
      .getPropertyValue('--color-brand1-' + (level || 6)).trim();
    return v || fallback;
  } catch (e) { return fallback; }
}

function YidaComp(props) {
  var brand = readBrandColor(6, '#1677ff');
  var data = [
    { name: '1月', value: 120 }, { name: '2月', value: 200 },
    { name: '3月', value: 150 }, { name: '4月', value: 320 },
  ];
  return (
    <div style={{ width: '100%', height: 300, padding: 16 }}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data}>
          <XAxis dataKey="name" />
          <YAxis />
          <Tooltip />
          <Line type="monotone" dataKey="value" stroke={brand} />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

export default YidaComp;
```

多色系列需要区分时，用品牌色 + 中性色/语义色，不要整排饱和撞色（见 `yida-page-uiux` 去 AI 味清单）。

## 自查清单（主色相关）

- 页面最外层有 `ConfigProvider` 且 `token.colorPrimary` 来自 `readBrandColor`，不是硬编码色值。
- Tailwind 主色类用 `var(--color-brand1-*)`，没有散落的 `#1677ff` / `bg-blue-500`。
- 图表 / canvas 绘制颜色走 `readBrandColor`，无硬编码蓝。
- 语义色（成功/警告/错误）保持 antd 默认或平台语义变量，未被主色覆盖。
- 视觉方向已按 `yida-page-uiux` 决策：不是默认蓝 + 大圆角 + emoji 的 AI 味套版。
