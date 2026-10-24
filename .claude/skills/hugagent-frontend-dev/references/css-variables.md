# CSS 变量、命名与样式模式

真源：`src/frontend/src/styles/variables.css`（`:root` 浅色 + 文件末尾 `:root[data-theme="dark"]` 深色覆盖 +
`:root[data-font-platform=…]` 字体优先级）。TS 镜像：`utils/motionTokens.ts`（动效）、`appTheme.ts`（antd token）。
三处同步改。设计规则见 `ui-design-spec.md`（v2）。

---

## 1. v1 → v2 令牌对照（写新代码先查这张表）

v2 令牌**尚未全部合入** `variables.css`。写代码前先 `grep -n -- '--令牌名' src/frontend/src/styles/variables.css` 确认：

- **已存在** → 直接用；
- **不存在、而你的改动需要它** → 按迁移清单第 1 项把它加进 `variables.css`（浅色 `:root` 和深色覆盖块**两处都加**，
  值取自 `assets/design-v2/tokens/hugagentos-tokens.v2.css`），同步 `appTheme.ts`（如涉及 antd），然后再用。
  **不要**写死色值、**不要**引用未定义的变量名、**不要**写 `var(--x, #fff)` 兜底（深色门禁会拦）。

| 用途 | v1（现行代码） | v2（目标） | 说明 |
|---|---|---|---|
| 强调填充 | `--color-primary` #126DFF（深色 #3E8BFF） | `--color-primary`（深色也 #126DFF） | 深色值待迁移 #6 |
| 强调文字 / 链接 | `--color-primary` | `--color-primary-text` #0F62E6 / 深 #5C9BFF | 新增 |
| 主色上的文字 | `--color-text-white` | `--color-on-primary` | 新增 |
| 悬停 / 按下 | `--color-*-hover` / `--color-*-active` 色阶 | `--state-hover` / `--state-press` / `--state-on-fill-hover` / `--state-on-fill-press` 叠加层 | **新代码禁用色阶** |
| 禁用 | `--color-*-disabled` | `opacity: .4` | |
| 成功/警告/错误 文字 | `--color-success` 等直接当文字 | `--color-success-text` / `--color-warning-text` / `--color-error-text` | P0 对比度 |
| 成功/警告/错误 浅底 | `--color-*-light`（v1 的 `*-bg` 是更深一档） | `--color-success-bg` #E6FFF9 等 | **注意：v2 的 `*-bg` 取 v1 `*-light` 的值** |
| 破坏性实心按钮 | — | `--color-danger-fill` #D92D20 | 新增 |
| 占位符 | `--color-text-placeholder` #98A2B3 | 同名，值改为 #667085（= 三级文字） | 迁移 #7 |
| 分隔线 | `--color-border` #E3E8EF | `--color-separator` | 改名，值相同 |
| 输入框边框 | `--color-border` / `--color-fill` | `--color-control-border` #C5CCD8 | 新增 |
| 次要按钮 / 胶囊 / 用户气泡 | `--color-bg-gray` / `--color-fill-hover` | `--color-fill-quiet` #F2F4F7 | 新增 |
| 浮层材质 | 各处自写半透明 | `--material` + `backdrop-filter: blur(20px)` | 新增 |
| 模态遮罩 | antd 默认 | `--scrim` | 新增 |
| 静止卡片投影 | `--shadow-card` | **无投影** | 迁移 #9 |
| 悬停卡片 / 对话输入区 | `--shadow-card-hover` / `--shadow-card` | `--shadow-raised` | 调整 |
| 菜单 / 弹窗 | `--shadow-dropdown` | `--shadow-floating` | 调整 |
| 分类色 | tint × 7（purple/cyan/teal/amber/green/red/blue） | tint × 4（blue/purple/teal/amber） | cyan/green/red 不新增引用 |
| 胶囊圆角 | 写 `999px` | `--radius-full` | 新增 |
| 间距 | `--space-xs/sm/md/lg/xl` = 4/8/16/**20**/**40** | `--space-1/2/3/4/6/8/12/16` = 4/8/12/16/24/32/48/64 | 20/40 不在栅格上 |
| 字号 | `--font-size-xs/sm/md/lg/xl/xxl` = 12/14/16/18/22/44（px） | 九级角色：display / title-1/2/3 / headline / body-lg / body / footnote / caption（rem） | 见 ui-design-spec §2 |
| 品牌火花绿 | — | `--color-brand-spark` | 仅标志，界面禁用 |
| 弹簧 | `SPRING.pop / ink / panel` | `SPRING.smooth / snappy` | 迁移 #2 |

在 v2 间距令牌合入前：4/8/16 用 `--space-xs/sm/md`，12/24/32/48/64 可直接写 px（门禁只管颜色），
**不要**在新代码里用 `--space-lg`(20) / `--space-xl`(40)。

---

## 2. 现行令牌速查（variables.css 当前值）

### 背景四级 + 对话底

| 令牌 | 浅色 | 深色 |
|---|---|---|
| `--color-bg-base` | #FFFFFF | #0F141B |
| `--color-bg-container` | #FFFFFF | #161C25 |
| `--color-bg-elevated` | #FFFFFF | #1C2330 |
| `--color-bg-layout` | #F5F7FB | #12171F |
| `--color-bg-chat` | #FDFDFC | #111720 |

### 文字

`--color-text` #101828 · `--color-text-secondary` #475467 · `--color-text-tertiary` #667085 ·
`--color-text-placeholder` #98A2B3（v2 改 #667085）· `--color-text-white` #FFFFFF

### 主色与语义

- 主色：`--color-primary` #126DFF、`-hover` #3C87FF、`-active` #0862F3、`-disabled` #A0C5FF、`-bg` #DBE9FF、`-light` #EBF2FF
- 语义：`--color-{success|warning|error}` + `-hover/-active/-disabled/-bg/-light`（v1 六档，新代码按上表换 v2）
- 线与填充：`--color-border` #E3E8EF、`--color-fill` #DDE3EC、`--color-fill-hover` #EEF1F5、`--color-bg-gray` #F4F6F9、
  `--color-fill-deep` #98A2B3、`--color-fill-heavy` #8C94A2
- 分类 tint：`--color-tint-{purple,cyan,teal,amber,green,red,blue}-bg` + `--color-tint-{…}`（深浅两档已定义）

### 圆角 / 间距 / 字号 / 字体

- `--radius-xs/sm/md/lg` = 6 / 10 / 16 / 24
- `--space-xs/sm/md/lg/xl` = 4 / 8 / 16 / 20 / 40
- `--font-size-xs/sm/md/lg/xl/xxl` = 12 / 14 / 16 / 18 / 22 / 44 px
- `--font-family`（`--font-family-cjk` 按 `data-font-platform` 切换 PingFang SC / Microsoft YaHei UI / Noto Sans CJK SC）、
  `--font-family-mono`、`--font-family-number`（DingTalk Sans）

### 投影

`--shadow-card` / `--shadow-card-hover` / `--shadow-dropdown`（深浅两档）

### 动效

`--motion-duration-instant/fast/normal/slow` = 100 / 160 / 240 / 320ms ·
`--motion-ease-brand-out` / `--motion-ease-standard` / `--motion-ease-exit` · `--motion-stagger-step` 60ms

### 局部域令牌（只在对应模块内用）

- `--delivery-*`：交付文件卡片（文件类型图标色等）
- `--color-personal-*` / `--shadow-personal-composer`：Personal Agent 对话皮肤
- `--module-*`：模块导航轨与二级边栏
- `--scrollbar-*`：滚动条
- `--bg / --text / --muted / --border / --primary / --primary2 / --accent / --shadow1`：**旧别名，新代码禁用**

---

## 3. 命名规则

```css
.jx-chatArea { }            /* Block：.jx- 前缀 + camelCase */
.jx-chatArea-header { }     /* Element：连字符 */
.jx-chatArea--empty { }     /* Modifier：双连字符 */
```

- 所有自定义类 `.jx-` 前缀；关键帧 `jx-kf-名称`，动效工具类 `jx-anim-名称`
- 组件私有的自定义属性用 `--jx-<block>-<prop>`，**值也必须引令牌**（`css-var-literal-color` 规则）
- 样式按功能放 `styles/` 对应文件或子目录（`styles/chat/`、`styles/catalog/`、`styles/myspace/`、`styles/automation/`、
  `styles/mobile/`）；`styles/index.ts` 统一 import，组件专属样式可在组件内直接 import

---

## 4. 常用样式模式（v2 写法）

```css
/* L1 卡片：静止无投影，悬停浮起 */
.jx-xxx-card {
  background: var(--color-bg-container);
  border-radius: var(--radius-md);
  padding: var(--space-md);
  transition: transform var(--motion-duration-fast) var(--motion-ease-standard),
              box-shadow var(--motion-duration-fast) var(--motion-ease-standard);
}
.jx-xxx-card:hover { transform: translateY(-1px); box-shadow: var(--shadow-raised); }

/* 中性控件悬停：叠加层而非换色 */
.jx-xxx-item:hover { background: var(--state-hover); }
.jx-xxx-item:active { background: var(--state-press); }

/* L2 浮层：材质 + 模糊，从触发点展开 */
.jx-xxx-menu {
  background: var(--material);
  backdrop-filter: blur(20px);
  border-radius: var(--radius-md);   /* 外 16 */
  padding: 6px;                      /* 内边距 6 */
  box-shadow: var(--shadow-floating);
  transform-origin: var(--jx-menu-origin, top left);
}
.jx-xxx-menu-item { border-radius: var(--radius-sm); }  /* 内 10 = 16 − 6，同心 */

/* 语义提示条：浅底 + 前景文字成对 */
.jx-xxx-hint--warning { background: var(--color-warning-bg); color: var(--color-warning-text); }

/* 分类标签：tint 成对 */
.jx-xxx-tag--skill { background: var(--color-tint-purple-bg); color: var(--color-tint-purple); border-radius: var(--radius-full); }

/* 半透明面 / 墨色浮层（深色下自动正确） */
.jx-xxx-glass { background: color-mix(in srgb, var(--color-bg-container) 60%, transparent); }
.jx-xxx-ink   { background: color-mix(in srgb, var(--color-text) 5%, transparent); }

/* 数字列 */
.jx-xxx-num { font-family: var(--font-family-number); font-variant-numeric: tabular-nums; }

/* 小图标按钮：视觉 28，点击区外扩到 32 */
.jx-xxx-iconBtn { position: relative; width: 28px; height: 28px; border-radius: var(--radius-full); }
.jx-xxx-iconBtn::after { content: ''; position: absolute; inset: -2px; }
```

---

## 5. 布局约定（模块页面与容器响应式）

- **模块主页面**（项目、我的空间、站点、应用中心、能力中心、定时任务、设置、更新记录、实验室）：公共内容区是全宽滚动视口，
  几何集中在 `styles/workspace-layout.css`。模块面板用正常文档流，**不要**再给面板自己设 `height:100%` + `overflow:auto`
  造第二个滚动容器，也不要用 `overflow-x:hidden`（会隐式把 `overflow-y` 变成 auto）。限宽只给模块内容。
  回归：`npm run test:module-pages`。
- **容器响应式**：聊天工具栏、项目 / 能力目录 / 知识库卡片按所在面板的可用宽度排版——容器提供宽度，后代用容器查询响应；
  不要让元素查询自身，不要用 JS 重复维护 CSS 断点。入口 `components/chat/ComposerToolbar.tsx`、
  `styles/chat/toolbar-layout.css`、`styles/projects.css`、`styles/catalog/`。
  回归：`npm run test:container-layout-browser`、`npm run test:chat-width-browser`。
- 聊天、Canvas、插件工作区和真正的编辑器 / 限高预览保留专用布局。
