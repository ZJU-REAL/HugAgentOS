# 看板视觉主题预设

> 3 套现成主题，根据业务域直接 copy THEME 常量到看板源码顶部。
>
> 选型决策树（按关键词顺序匹配，命中即止）：
> - 奢侈品零售 / 高端酒店 / 航空集团 / 金融私行 / 国际化总部高管汇报 → **金蓝奢华**
> - 供应链 / 物流 / 制造 / 政务 / 零售品牌（非奢侈非高端）/ 快消 / 餐饮连锁 / 科技感需求 → **深色紫蓝**（DEFAULT）
> - 日常业务看板 / 财务月报 / 医疗健康 / 内部管理 / 白天大屏强光场景 → **白底商务**
>
> **避坑**：用户只说"零售/餐饮/快消"未加"高端/奢侈/精品"时，默认走深色紫蓝，不要一看到"零售"就跳金蓝；金蓝奢华只服务明确的"高奢/私行/国际化"语境。

---

## 主题 1：深色紫蓝科技风（DEFAULT）

**典型业务**：供应链、物流、制造、能源、政务大屏、零售品牌（非奢侈非高端）、快消、餐饮连锁
**视觉关键词**：夜色太空、数据流动感、发光线、霓虹蓝紫
**标杆样本**：`project/pages/src/supply-chain-dashboard.js`

```javascript
var THEME = {
  // 背景层
  bg: '#0a0e27',                              // 页面主背景（近黑靛蓝）
  bgCard: 'rgba(15, 23, 60, 0.85)',           // 卡片背景（半透明蓝）
  bgCardHover: 'rgba(30, 41, 80, 0.95)',
  bgMask: 'rgba(4, 8, 30, 0.6)',              // 模态遮罩

  // 边框
  border: 'rgba(99, 179, 237, 0.25)',
  borderLight: 'rgba(99, 179, 237, 0.45)',

  // 主色（蓝）
  primary: '#3b82f6',
  primaryLight: '#60a5fa',
  primaryDark: '#2563eb',

  // 强调色（紫）
  accent: '#8b5cf6',
  accentLight: '#a78bfa',

  // 功能色
  success: '#10b981',
  warning: '#f59e0b',
  danger: '#ef4444',

  // 文字
  textPrimary: '#f1f5f9',
  textSecondary: '#cbd5e1',
  textMuted: '#64748b',

  // 渐变
  gradient: {
    primary: 'linear-gradient(135deg, #3b82f6 0%, #06b6d4 100%)',
    accent:  'linear-gradient(135deg, #8b5cf6 0%, #ec4899 100%)',
    success: 'linear-gradient(135deg, #10b981 0%, #06b6d4 100%)',
    danger:  'linear-gradient(135deg, #ef4444 0%, #f97316 100%)',
    card:    'linear-gradient(135deg, rgba(59,130,246,0.12) 0%, rgba(139,92,246,0.08) 100%)',
    royal:   'linear-gradient(135deg, #1e1b4b 0%, #0a0e27 100%)'
  }
};

var FONT_STACK = '"Orbitron", "Segoe UI", -apple-system, BlinkMacSystemFont, sans-serif';
```

**发光效果要点**：
- Logo / KPI 数字加 `textShadow: '0 0 16px rgba(59,130,246,0.6)'`
- 卡片加 `boxShadow: '0 0 30px rgba(59,130,246,0.12), inset 0 1px 0 rgba(255,255,255,0.05)'`
- 标题用 `WebkitBackgroundClip: text` + 渐变填充

**可选粒子背景**：Canvas 粒子见 `yida-ppt-slider` 的 dark-tech 主题实现。

---

## 主题 2：金蓝奢华风

**典型业务**：酒店、航空、奢侈品零售、金融私行、国际化集团高管汇报
**视觉关键词**：黑夜金光、克制奢华、海蓝配金、Orbitron 字体
**标杆样本**：`project/pages/src/shangri-la-executive-dashboard.js`

```javascript
var THEME = {
  // 背景层
  bg: '#0a1628',                               // 主背景（深海蓝）
  bgLight: '#1a2847',                          // 卡片背景（偏亮深蓝）
  bgCard: 'rgba(26, 40, 71, 0.85)',
  bgMask: 'rgba(5, 12, 26, 0.7)',

  // 边框
  border: 'rgba(0, 212, 255, 0.22)',
  borderLight: 'rgba(0, 212, 255, 0.35)',
  borderGold: 'rgba(212, 175, 55, 0.35)',

  // 主色（青蓝）
  primary: '#00d4ff',
  primaryLight: '#5eead4',
  primaryDark: '#0891b2',

  // 强调色（金）
  accent: '#d4af37',
  accentLight: '#facc15',
  accentDark: '#b8860b',

  // 功能色
  success: '#00e676',
  warning: '#f59e0b',
  danger:  '#ff5252',

  // 文字
  textPrimary: '#e0f2fe',
  textSecondary: '#cbd5e1',
  textMuted: '#64748b',

  // 渐变
  gradient: {
    primary: 'linear-gradient(135deg, #00d4ff 0%, #06b6d4 100%)',
    accent:  'linear-gradient(135deg, #d4af37 0%, #f59e0b 100%)',
    gold:    'linear-gradient(135deg, #d4af37 0%, #facc15 100%)',
    success: 'linear-gradient(135deg, #00e676 0%, #06b6d4 100%)',
    danger:  'linear-gradient(135deg, #ff5252 0%, #f97316 100%)',
    purple:  'linear-gradient(135deg, #a78bfa 0%, #ec4899 100%)',
    royal:   'linear-gradient(135deg, #1a2847 0%, #0a1628 100%)'
  }
};

var FONT_STACK = '"Orbitron", "Playfair Display", "Segoe UI", -apple-system, sans-serif';
```

**奢华效果要点**：
- 标题用 **金 → 金黄 → 青蓝** 三段渐变：`'linear-gradient(90deg, #d4af37, #facc15, #00d4ff)'`
- Logo 用 gold 渐变 + `boxShadow: '0 0 20px rgba(212,175,55,0.4)'`
- carbonBar 背景用金+蓝+紫三色横向渐变：
  ```css
  linear-gradient(90deg, rgba(212,175,55,0.15) 0%, rgba(0,212,255,0.10) 50%, rgba(167,139,250,0.10) 100%)
  ```
- 关键动作按钮用金色 bg + 深蓝文字，提升品牌仪式感

---

## 主题 3：白底商务风

**典型业务**：日常业务看板、财务看板、医疗健康、内部管理、HR 分析
**视觉关键词**：克制、可读、大屏投屏友好、打印友好
**标杆样本**：`yida-chart` SKILL.md 的"报表设计规范"章节

```javascript
var THEME = {
  // 背景层
  bg: '#f8fafc',           // 页面背景（极浅灰，避免纯白晃眼）
  bgCard: '#ffffff',
  bgCardHover: '#f1f5f9',
  bgMask: 'rgba(15, 23, 42, 0.35)',

  // 边框
  border: '#e2e8f0',
  borderLight: '#f1f5f9',

  // 主色（深蓝）
  primary: '#1e40af',
  primaryLight: '#3b82f6',
  primaryDark: '#1e3a8a',

  // 强调色（天蓝）
  accent: '#0ea5e9',
  accentLight: '#38bdf8',

  // 功能色
  success: '#059669',
  warning: '#d97706',
  danger:  '#dc2626',

  // 文字
  textPrimary: '#0f172a',
  textSecondary: '#475569',
  textMuted: '#94a3b8',

  // 渐变（克制使用）
  gradient: {
    primary: 'linear-gradient(135deg, #1e40af 0%, #3b82f6 100%)',
    accent:  'linear-gradient(135deg, #0ea5e9 0%, #38bdf8 100%)',
    card:    'linear-gradient(135deg, #ffffff 0%, #f8fafc 100%)'
  }
};

var FONT_STACK = '-apple-system, BlinkMacSystemFont, "SF Pro Display", "Segoe UI", Roboto, sans-serif';
```

**商务风要点**：
- 卡片无阴影或极淡阴影（`0 1px 3px rgba(15,23,42,0.06)`）
- 边框 `1px solid #e2e8f0`，不用色带装饰
- KPI 数字 `font-feature-settings: "tnum"` 启用等宽数字
- tooltip 用深色反色 `rgba(15,23,42,0.92)` 提升可读性
- 分割线用 `#f1f5f9` 虚线（`type: [4,4]`）

---

## 通用字体与排版

所有主题共用：

```javascript
var TYPO = {
  titleL:  { fontSize: 20, fontWeight: 700, letterSpacing: '1px' },  // 看板主标题
  titleM:  { fontSize: 16, fontWeight: 600 },                         // 卡片标题
  titleS:  { fontSize: 13, fontWeight: 600 },                         // section 小标题
  kpiNum:  { fontSize: 28, fontWeight: 800, fontFeatureSettings: '"tnum"' },
  body:    { fontSize: 13, fontWeight: 400, lineHeight: 1.5 },
  caption: { fontSize: 11, fontWeight: 400, color: 'textMuted' }
};
```

**移动端统一缩小 2px**：`titleL → 16px, titleM → 14px, kpiNum → 22px`。

---

## ECharts 主题对齐

所有图表的 `itemStyle.color` / `axisLine.lineStyle.color` / `splitLine.lineStyle.color` 统一引用 `THEME.primary / THEME.accent / THEME.border`。

**深色主题下的 tooltip**：
```javascript
tooltip: {
  backgroundColor: 'rgba(10, 14, 39, 0.95)',
  borderColor: THEME.border,
  borderWidth: 1,
  textStyle: { color: THEME.textPrimary, fontSize: 12 }
}
```

**白底主题下的 tooltip**：
```javascript
tooltip: {
  backgroundColor: 'rgba(15, 23, 42, 0.92)',
  borderWidth: 0,
  textStyle: { color: '#ffffff', fontSize: 12 }
}
```

---

## 选型快速决策

用户只说"做个看板"，不说业务域，默认用 **主题 1（深色紫蓝）**。

用户提到以下关键词 → 切到对应主题：

| 关键词 | 主题 |
|-------|------|
| 酒店 / 奢华 / 高端 / 尊享 / 高净值 / 集团高层 / 国际 | 金蓝奢华 |
| 供应链 / 物流 / 智能 / 科技 / 大屏 / 指挥中心 / 驾驶舱 | 深色紫蓝 |
| 财务 / 预算 / 医疗 / HR / 日报 / 月报 / 周报 / 打印 | 白底商务 |

**禁止混搭**：同一个看板内不要让 KPI 用深色、图表用白底。THEME 选定后贯穿所有 section。
