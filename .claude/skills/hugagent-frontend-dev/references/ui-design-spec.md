# HugAgentOS UI 设计规范 v2（开发落地版）

> 来源：《HugAgentOS 设计规范 v2.0 · 2026.10》（`HugAgentOS-UI设计规范/HugAgentOS设计规范-v2.html` + `assets/`）。
> v2 相对 v1 经过一次对抗性审查，修了 25 项问题（6 项 P0 可访问性/语义错误、14 项 P1 层级一致性、5 项 P2 品牌表达）。
> 本文只保留写代码要用的规则与数值；品牌物料、宣传版式见原规范。
>
> **代码是唯一事实来源。** 改令牌时 `styles/variables.css`、`utils/motionTokens.ts`、`appTheme.ts` 三处同步改。
> v2 令牌参考实现在本 skill 的 `assets/design-v2/tokens/`，**尚未全部合入仓库**——合入状态和落地方式见
> `css-variables.md` 的「v1 → v2 对照」与本文末尾「迁移清单」。

---

## 0. 设计原则（冲突时按顺序取舍）

1. **内容为先**：界面是容器，报告、表格、答案才是主角。界面元素用中性色，颜色只表达含义和**唯一**的主要操作。能用留白分组就不加边框和底色。
2. **清晰的层级**：每个视图只有一个主要操作。层级靠字号、字重、间距建立，不靠盒子套盒子。
3. **一致而自然**：同样的东西永远长一样、动一样。动效有来处、有去处、可打断。照顾深浅外观、字号放大、减弱动效。
4. **过程安静可见**：智能体在思考/检索/调用工具时，用**一行安静的文字**说清在做什么、用了多久；可展开看细节，默认不打扰。不要多处同时闪动的指示器。

---

## 1. 色彩

### 1.1 品牌色

| 名称 | 色值 | 用途 |
|---|---|---|
| 环抱蓝 Hug Blue | `#126DFF`（白字 4.51:1） | **唯一强调色**：主要操作、链接、选中、焦点 |
| 火花绿 Spark | `#19D3A7` | **只属于标志和品牌动画，界面中禁用**（与成功色亮度对比仅 1.37:1，分不清） |
| 墨 | `#0B0D12` | 文字、深色底（宣传） |
| 纸 | `#FFFFFF` | 浅色底 |

- 深色模式主色**填充仍用 `#126DFF`**（v1 的 `#3E8BFF` 白字只有 3.31:1，P0）；深色下的链接/强调文字改用 `--color-primary-text`（`#5C9BFF`）。

### 1.2 语义色：每种只有三个令牌

| 语义 | 填充（状态点、实心按钮） | 前景（文字/图标，≥4.5:1） | 浅底（提示条、标签） |
|---|---|---|---|
| 强调 | `--color-primary` #126DFF | `--color-primary-text` #0F62E6 | `--color-primary-light` #EBF2FF |
| 成功 | `--color-success` #02B589 | `--color-success-text` #067A5E | `--color-success-bg` #E6FFF9 |
| 警告 | `--color-warning` #F8AB42 | `--color-warning-text` #B45309 | `--color-warning-bg` #FFF5E8 |
| 错误 | `--color-error` #FC5D5D | `--color-error-text` #D92D20 | `--color-error-bg` #FFF0F0 |
| 破坏性实心按钮 | `--color-danger-fill` #D92D20（白字 4.83:1） | — | — |

规则：
- **语义色不能直接当文字色用**（v1 的 success/warning/error 当文字只有 2.63/1.93/3.06:1，P0）。文字、图标一律用 `*-text`。
- **取消 hover/active/disabled 色阶**（v1 的 `--color-*-hover/-active/-disabled` 不再用于新代码）。状态改为统一叠加层，见 1.3。
- 浅底上的文字用对应的 `*-text`，不要用填充色压浅底（v1 危险按钮红字压浅红底 2.76:1，P0）。

### 1.3 状态叠加层（hover / press / disabled）

颜色只会**变深**，白字对比度不会下降：

| 状态 | 中性控件（透明/浅底） | 彩色填充（主按钮等） |
|---|---|---|
| 悬停 | 叠 `--state-hover`（浅 `rgba(16,24,40,.04)` / 深 `rgba(255,255,255,.06)`） | 叠 `--state-on-fill-hover`（黑 8% / 深色黑 12%） |
| 按下 | 叠 `--state-press`（.08 / .10），并 `scale(.97)` | 叠 `--state-on-fill-press`（黑 16% / 深色黑 22%），并 `scale(.97)` |
| 禁用 | 整体 `opacity: .4` | 整体 `opacity: .4` |

写法（叠加而不是换色）：

```css
.jx-xxx-btn { background: var(--color-primary); color: var(--color-on-primary); }
.jx-xxx-btn:hover  { background-image: linear-gradient(var(--state-on-fill-hover), var(--state-on-fill-hover)); }
.jx-xxx-btn:active { background-image: linear-gradient(var(--state-on-fill-press), var(--state-on-fill-press)); transform: scale(.97); }
.jx-xxx-btn:disabled { opacity: .4; }
```

antd 侧：`colorPrimaryHover` / `colorPrimaryActive` 设为**更深**的 `#0F62E6` / `#0A56CC`（见 `assets/design-v2/tokens/appTheme.v2.ts`）。

### 1.4 文字与背景

文字三级（**占位符 = 三级文字色**，不再用 2.58:1 的 `#98A2B3`）：

| 令牌 | 浅色 | 深色 | 用途 |
|---|---|---|---|
| `--color-text` | #101828 | #E8ECF4 | 正文、标题 |
| `--color-text-secondary` | #475467 | #B3BDCD | 说明 |
| `--color-text-tertiary` | #667085 | #8792A4 | 辅助、时间戳、**占位符** |

背景与线（深色模式层级越高表面越亮，不靠加重投影）：

| 令牌 | 浅色 | 深色 | 用途 |
|---|---|---|---|
| `--color-bg-base` | #FFFFFF | #0F141B | 页面底色 |
| `--color-bg-layout` | #F5F7FB | #12171F | 侧栏、分区底 |
| `--color-bg-container` | #FFFFFF | #161C25 | 卡片、面板 |
| `--color-bg-elevated` | #FFFFFF | #1C2330 | 浮层、弹窗 |
| `--color-bg-chat` | #FDFDFC | #111720 | 对话区底 |
| `--color-separator` | #E3E8EF | #2B3442 | 分隔线（装饰性；即 v1 的 `--color-border`） |
| `--color-control-border` | #C5CCD8 | #3A4556 | 输入框边框（配合可见标签） |
| `--color-fill-quiet` | #F2F4F7 | #1F2631 | 次要按钮、胶囊、用户气泡 |
| `--material` | rgba(255,255,255,.82) | rgba(28,35,48,.80) | 浮层材质，配合 `backdrop-filter: blur(20px)` |
| `--scrim` | rgba(16,24,40,.32) | rgba(0,0,0,.56) | 模态遮罩 |

### 1.5 分类色：从 7 组收敛到 4 组

只用于区分类别，**类别优先用图标和文字区分，颜色只做辅助**：

| 组 | 示例 | 令牌（浅底 + 前景成对使用） |
|---|---|---|
| 蓝 | MCP / 连接器 | `--color-tint-blue-bg` + `--color-tint-blue` |
| 紫 | 技能 | `--color-tint-purple-bg` + `--color-tint-purple` |
| 青 | 自动化 | `--color-tint-teal-bg` + `--color-tint-teal` |
| 琥珀 | 计划模式 | `--color-tint-amber-bg` + `--color-tint-amber` |

新代码不要再用 `cyan / green / red` 三组 tint（存量保留，不新增引用）。

---

## 2. 文字层级

按**角色**命名（参考 Apple 文字样式），九级覆盖所有场景。**字重只用 400 / 500 / 600**（不再出现 700/900）。中文不加负字距；西文 28px 以上收紧。**字号一律用 rem**，跟随浏览器缩放。

| 角色 | 令牌前缀 | 字号 / 行高 / 字重 / 字距 | 用途 | 与 v1 |
|---|---|---|---|---|
| 展示 display | `--font-size-display` | 2.5rem(40) / 48 / 600 / -0.02em | 欢迎语、宣传 | 调整（原 xxl 44） |
| 标题一 title-1 | `--font-size-title-1` | 1.75rem(28) / 36 / 600 / -0.01em | 页面标题 | 新增 |
| 标题二 title-2 | `--font-size-title-2` | 1.375rem(22) / 30 / 600 | 分区标题 | 沿用 xl |
| 标题三 title-3 | `--font-size-title-3` | 1.125rem(18) / 26 / 600 | 弹窗、卡片标题 | 沿用 lg |
| 重点 headline | `--font-size-headline` | 0.9375rem(15) / 22 / 600 | 列表项标题 | 新增 |
| 阅读 body-lg | `--font-size-body-lg` | 1rem(16) / 28 / 400 | 对话、文档正文 | 沿用 md |
| 正文 body | `--font-size-body` | 0.875rem(14) / 22 / 400 | **界面默认**：按钮、输入、列表、表格 | 沿用 sm |
| 注释 footnote | `--font-size-footnote` | 0.8125rem(13) / 20 / 400 | 过程行、说明 | 新增 |
| 说明 caption | `--font-size-caption` | 0.75rem(12) / 18 / 400 | 时间戳、标签 | 沿用 xs |

每级同时有 `--line-height-*` / `--font-weight-*` / `--letter-spacing-*` 四件套（见 v2 token CSS）。

字体（已在代码中落地，`platformTypography.ts` 在挂载前给 `<html>` 打 `data-font-platform`）：

| 场景 | 字体 |
|---|---|
| 界面中文 | Apple: PingFang SC · Windows: Microsoft YaHei UI · 其他: Noto Sans CJK SC（`--font-family`） |
| 数字 | DingTalk Sans → 系统字体（`--font-family-number`）；**表格、计时、金额一律 `font-variant-numeric: tabular-nums`** |
| 代码 | ui-monospace, SF Mono, Menlo, Consolas（`--font-family-mono`） |
| 宣传 | 思源黑体（不进产品界面） |

---

## 3. 间距与尺寸

**8 栅格**：`4 / 8 / 12 / 16 / 24 / 32 / 48 / 64`。4 只用于图标和文字之间的半步。v1 的 20 与 40 不在栅格上，**新代码不再使用**。

| v2 令牌 | 值 | 对应 v1 |
|---|---|---|
| `--space-1` | 4 | `--space-xs` |
| `--space-2` | 8 | `--space-sm` |
| `--space-3` | 12 | （v1 缺失） |
| `--space-4` | 16 | `--space-md` |
| `--space-6` | 24 | 替代 `--space-lg` 20 |
| `--space-8` | 32 | （v1 缺失） |
| `--space-12` | 48 | 替代 `--space-xl` 40 |
| `--space-16` | 64 | — |

尺寸规则：
- **点击区**：桌面 ≥ 32×32，触屏 ≥ 44×44（真实尺寸）。紧凑控件最小 28，用伪元素/padding 外扩点击区。
- **控件高度**：28（紧凑，仅桌面表格和工具栏）· 32（默认）· 40（主要操作、移动端）。不再出现 24px 高的按钮。
- **阅读宽度**：对话与文档正文最大 **720px**（每行约 40 个汉字）。
- **页面边距**：桌面 24 / 32，移动端 16 + 安全区（`env(safe-area-inset-*)`）。

---

## 4. 圆角

值不变，规则变严：**圆角跟随尺寸，且同心——内层圆角 = 外层圆角 − 内边距**。

| 令牌 | 值 | 用于 |
|---|---|---|
| `--radius-xs` | 6 | 28px 控件、代码块 |
| `--radius-sm` | 10 | 32–40px 控件、菜单项 |
| `--radius-md` | 16 | 卡片、菜单、弹窗 |
| `--radius-lg` | 24 | 对话输入区、面板 |
| `--radius-full` | 999px | 标签、头像、开关、独立图标按钮（胶囊） |

例：菜单外 16、内边距 6 → 选项 10（v1 外 10 / 内边距 6 / 选项 6 不同心，P1）。

---

## 5. 层级与材质

一共四层。**投影只给浮在内容之上的东西。**

| 层 | 背景 | 内容 | 投影 |
|---|---|---|---|
| L0 底面 | `--color-bg-layout` / `--color-bg-base` | 页面、侧栏 | 无 |
| L1 内容 | `--color-bg-container` | 卡片、面板 | **静止无投影**，悬停出现 `--shadow-raised` 并上浮 1px |
| L2 浮层 | `--material` + `backdrop-filter: blur(20px)` | 菜单、弹出框、提示 | `--shadow-floating` |
| L3 模态 | `--color-bg-elevated` + `--scrim` 遮罩 | 对话框、表单抽屉 | `--shadow-floating` |

- 静止卡片不加投影，用底色和留白分组（替代 v1 `--shadow-card`）。
- **对话输入区只保留投影（`--shadow-raised`），不再同时加边框**。
- 投影令牌：`--shadow-raised`（原 card）、`--shadow-floating`（原 dropdown），深浅两档已定义。

---

## 6. 图标

参考 SF Symbols：**图标是文字的一部分**。

- 24px 画板，四周留 2px，**线宽 1.5px**，圆头圆角（`stroke-linecap/linejoin: round`），`stroke="currentColor"`。
- 尺寸跟随字号、线宽跟随字重、颜色继承文字色：13px 常规 → 线宽 1.5；15px 中粗 → 2；20px 标题 → 2。
- 默认线性，**选中态换面性**。
- 风格样例：`assets/design-v2/icons-ui/`（12 个：new-chat / search / knowledge / skill / automation / my-space / attachment / settings / send / chevron-right / check / close）。
- 既有 IconPark / `@ant-design/icons` 可继续用，但要满足上面的线宽与 currentColor 规则；不要给图标单独上品牌色。

---

## 7. 组件

以 Ant Design 为底，用 v2 令牌覆盖（ConfigProvider token，不覆写组件内部 DOM 样式）。

### 7.1 按钮

- **一个视图只有一个主要按钮**（`type="primary"`）。
- 次要操作用**浅灰填充**（`--color-fill-quiet`），**不再用描边按钮**。
- 低频操作用文字按钮（`type="text"` / `type="link"`）。
- 破坏性操作：**确认框里用实心红**（`--color-danger-fill` + 白字），**列表里用红色文字按钮**（`--color-error-text`）。
- 尺寸 28 / 32 / 40；加载中按钮文案写进行时（"生成中"）。
- 文案写动作："生成报告"，不写"立即开启智能分析之旅"。

### 7.2 输入框与对话输入区

- 输入框**必须有可见标签**，不能只靠占位符说明用途。占位符写示例（"例如：Q3 华东区销售分析"）。
- 出错时**写清原因和改法**："请输入 00:00 到 23:59 之间的时间"。
- 边框用 `--color-control-border`。
- **对话输入区**是最重要的控件：`--radius-lg` 大圆角，**只有投影没有边框**；内容为空时发送按钮是中性色，**有内容后才变主色**（主色只在能操作时出现）。

### 7.3 对话与过程

- 用户消息：浅灰气泡（`--color-fill-quiet`）。助手回复**不加气泡**。
- 思考、工具调用：**默认一行安静的文字**——做了什么、结果多少、用了多久，点击展开细节。
  例："已思考 12 秒"、"已检索知识库「销售数据 2026」14 条 · 0.8s"、"正在生成 Excel 报表"。
- **只有用户会打开的产物**（文件、表格、网页）才做成卡片。v1 把每次工具调用做成卡片（过程比答案显眼，P1），新代码不要这样。
- 正文宽度 ≤ 720px，阅读字号 body-lg（16/28）。

### 7.4 状态

状态**同时用颜色和形状**表达。**一屏只有一个持续动画。**

| 状态 | 表现 |
|---|---|
| 运行中（当前那一项） | 文字微光（shimmer），一屏一个 |
| 运行中（其他项） | 静止蓝点 |
| 已完成 | 成功色勾，`statusIn` 落定 |
| 等你确认 | 警告色点 + **扩散涟漪**（唯一允许涟漪的状态，因为它在叫人） |
| 失败 | 错误色 |
| 排队中 / 已暂停 | 中性色静止 |
| 计数徽标 | 超过 99 显示 `99+` |

### 7.5 卡片与浮层

- 卡片静止无投影，悬停出现 `--shadow-raised` 并 `translateY(-1px)`。
- 菜单用材质背景，**从触发点展开**（`transform-origin` 指向触发点），`--radius-md` + 选项 `--radius-sm`。
- 提示条（toast）：**反色胶囊**，出现在**底部中间**，3 秒后自动消失，新提示替换旧提示。
- 快捷键提示（⌘N / ⌘K）用 caption 字号、三级文字色。

---

## 8. 动效

### 8.1 原则

1. **有来处**：菜单从按钮展开，面板从边缘滑入，消息从输入框升起；返回沿原路离开。
2. **可打断**：手势、拖拽、展开收起、布局变化都用弹簧；中途打断从当前位置和速度继续。动画期间界面始终可操作。
3. **克制**：产品界面弹簧不回弹或只轻微回弹；一次性过渡 ≤ 320ms；除折叠展开外**只动 `opacity` 和 `transform`**，不动 filter（含 blur）和阴影。
4. **进行中常在**：表示"正在运行"的动画在减弱动效时也继续；装饰性位移在减弱动效时换成淡入淡出。

### 8.2 弹簧（Motion 库，用时长 + 回弹描述）

| 预设 | 参数 | 用于 |
|---|---|---|
| `SPRING.smooth` | `visualDuration: .35, bounce: 0` | 面板、抽屉、折叠展开、布局变化（替代 v1 `panel`） |
| `SPRING.snappy` | `visualDuration: .25, bounce: .15` | 标签页指示条、开关、徽标确认（替代 v1 `pop` / `ink`） |

v1 的 `pop` / `panel` 回弹 33%（面板会晃，P1），`ink` 一并标记弃用；**新代码只用 smooth / snappy**，`bouncy` 不进产品界面。

### 8.3 时长与曲线（不适合弹簧的一次性过渡）

| 令牌 | 时长 | 用于 |
|---|---|---|
| `--motion-duration-instant` | 100ms | 悬停着色、按下反馈 |
| `--motion-duration-fast` | 160ms | 状态切换、菜单弹出、图标落定 |
| `--motion-duration-normal` | 240ms | 入场、出场、消息发送、列表编排 |
| `--motion-duration-slow` | 320ms | 面板转场、折叠展开（能用弹簧优先弹簧） |

曲线三条：`--motion-ease-brand-out`（入场/展开/升起）、`--motion-ease-standard`（双向状态切换）、`--motion-ease-exit`（退场）。
**退场时长 = 入场 × 0.7**（如菜单 160 → 112ms，提示条 240 → 168ms）。
**禁止**在代码里写 `ease` / `ease-out` / 新的 cubic-bezier 值，TS 侧用 `utils/motionTokens.ts` 的 `DUR` / `EASE`。

### 8.4 编排

- 静态列表入场逐项错开 60ms（`--motion-stagger-step` / `STAGGER_STEP`），**第 8 项起不再增加延迟**（`STAGGER_CAP`）。
- 超过 20 项的动态列表不做布局动画（`LAYOUT_ANIM_MAX_ITEMS`）。
- 翻看历史记录不播任何入场动画，只对 **2 秒内新产生**的内容播放（`FRESH_ENTER_WINDOW_MS`）。

### 8.5 原语库（新增动画前先找同语义的原语）

命名：关键帧 `jx-kf-名称`，工具类 `jx-anim-名称`（定义在 `styles/motion.css`）。**入场类一律 `animation-fill-mode: backwards`。**

| 原语 | 工具类 | 参数 | 用于 | 减弱动效时 |
|---|---|---|---|---|
| fadeInUp | `.jx-anim-fadeInUp` | 240ms · brand-out · 上移 8px | 卡片、确认条入场 | 淡入 |
| fadeIn | `.jx-anim-fadeIn` | 240ms · standard | 大面积区域 | 淡入 |
| scaleIn | `.jx-anim-scaleIn` | 160ms · brand-out · .97 → 1 | 菜单、弹出框（原点指向触发点） | 淡入 |
| statusIn | `.jx-anim-statusIn` | 160ms · brand-out · .7 → 1 | 状态图标落定 | 淡入 |
| flash | `.jx-anim-flash` | 1.2s · standard | 跳转定位后的一次性高亮 | 保持 |
| shake | `.jx-anim-shake`（`jx-kf-shakeX`） | 300ms · ±3px | 校验失败 | 停止 |
| shimmer 文字 | 关键帧 `jx-kf-shimmer` | 2s linear 循环 | 运行中的那一行（一屏一个） | 保持（`motion-keep`） |
| ripple | `.jx-anim-ripple` | 1.6s 循环 | **只用于"等你确认"** | 停止 |
| stagger | `.jx-anim-stagger` + `staggerStyle(i)` | 60ms 步进，封顶第 8 项 | 静态列表入场 | 淡入 |

> 代码里另有 `.jx-anim-pulse`（`motion-keep` 的通用"进行中"脉冲）与 `.jx-anim-flash-row`。v2 要求一屏只有一个持续动画：
> 运行中的当前项优先用文字微光，其他运行项保持静止蓝点，不要为每一项都挂 pulse。

### 8.6 智能体动效（一次任务的完整过程）

同一时刻只有一个微光在动；文字按词块淡入，不做模糊，不逐字排队。

| 环节 | v2 | v1（不要再这样写） |
|---|---|---|
| 发送 | 用户气泡 `translateY(8px) + scale(.98) → 1`，240ms brand-out，原点右下 | scale .92 · 300ms ease-out |
| 思考 | 一行"正在思考 · 8 秒"，文字微光 2s linear，计时每秒更新；骨架屏只在加载历史时出现，不与微光同屏 | 跳点 + 呼吸 + 骨架三处同时动 |
| 调用工具 | 进行中：文字微光；完成：图标 `statusIn` 160ms，只留下一行可展开的记录 | 卡片 + 无限涟漪 |
| 流式输出 | **按词块淡入**，opacity 180ms standard，落后模型 ≤ 300ms | 逐字 350ms + 65ms 错开 + blur 3px（每秒约 15 字，越积越多） |

### 8.7 交互控件

| 控件 | 动效 |
|---|---|
| 开关 | 滑块 snappy 弹簧；底色 160ms standard |
| 标签页 | 指示条位置与宽度 snappy 弹簧；文字颜色 160ms standard |
| 面板 / 抽屉 | smooth 弹簧从边缘滑入；遮罩透明度跟随同一进度 |
| 菜单 | 打开 160ms brand-out `scale(.97 → 1)`；关闭 112ms exit；原点在按钮处 |
| 提示条 | 入场 240ms brand-out 上移 16px；退场 168ms exit；3 秒后消失 |
| 折叠展开 | 高度与透明度 smooth 弹簧；箭头旋转 240ms brand-out |

### 8.8 减弱动效（`prefers-reduced-motion: reduce`）

v2 改为 Apple 做法：**位移和缩放换成淡入淡出，时长不变**；循环装饰停止；运行指示器继续。

| 类型 | 减弱动效时 | 写法 |
|---|---|---|
| 入场位移 / 缩放 | 淡入，时长不变 | 同文件 `@media (prefers-reduced-motion: reduce)` 内写**同名 `@keyframes` 覆盖版，只保留 opacity**，标注 `motion-fade` |
| 弹簧（Motion 库） | 换成透明度过渡 | `useReducedMotion()` 为真时只动 opacity，时长取 `DUR.fast` |
| 循环装饰（涟漪、抖动） | 停止 | 覆盖版定格，标注 `motion-stop` |
| 工作指示器（微光、转圈、骨架） | 继续 | 使用它的规则上标注 `motion-keep: 理由` |
| CSS transition | 归零 | 保持现状（改成正数会让 antd 浮层定位也产生过渡） |

> 构建门禁 `scripts/check-loading-motion.mjs` 保留。**当前门禁仍按 v1 规则检查**：装饰性关键帧必须有同名
> 覆盖版（v1 为"定格终态"）或在使用处标注 `motion-keep:`。按 v2 写"仅 opacity"的同名覆盖版同样满足门禁；
> `motion-fade` / `motion-stop` 标注随迁移清单第 3 项一起被门禁识别。

---

## 9. 文案语气

像一位靠谱的同事说话：直接、具体、不卖弄。

| 场景 | ✅ | ❌ |
|---|---|---|
| 按钮 | 写动作：生成报告 | 立即开启智能分析之旅 |
| 进行中 | 写正在做什么：正在检索 3 个知识库 | AI 正在努力思考中… |
| 出错 | 写原因和办法：文件超过 50MB，请压缩后重新上传 | 哎呀，出错了！ |
| 完成 | 写结果在哪：报告已保存到我的空间 | 操作成功 |

所有用户可见文案仍须包 `t()`（见 SKILL.md i18n 节）。

---

## 10. 品牌标志（界面内用到时）

- **主标志是标准字**（A 里已含环线和火花点）。**禁止方形标与标准字并排**。产品顶栏这类横向空间用标准字。
- 方形标只用于方形容器（应用图标、头像、网页图标）。**24px 及以下用小尺寸版** `hugagentos-mark-small.svg` / `favicon.svg`。
- 彩色稿里火花点永远是火花绿；只有单色稿可以让火花点跟随主体变色。
- 禁止：拉伸压扁、换品牌色、旋转倾斜、投影/描边/渐变、火花点改色或移位。
- 对话中的助手头像：品牌蓝底 + 白色环线 + 火花绿点，环线占圆形头像宽度 60%。
- 标志动画 ≤ 1 秒（描边 0–600ms standard → 填实 500–700ms → 火花点 650ms 起 snappy 弹出）；启动页只播一次；减弱动效时整体 240ms 淡入。
- 素材：`assets/design-v2/logo/`（mark / mark-small / reverse / mono / glyph / favicon）。

---

## 11. Ant Design 主题映射（v2 目标值）

真源 `src/frontend/src/appTheme.ts`，参考实现 `assets/design-v2/tokens/appTheme.v2.ts`：

| Token | 浅色 | 深色 |
|---|---|---|
| colorPrimary | #126DFF | **#126DFF**（v1 为 #3E8BFF） |
| colorPrimaryHover / Active | #0F62E6 / #0A56CC（变深） | 同左 |
| colorLink | #0F62E6 | #5C9BFF |
| colorSuccess / colorError | #02B589 / #D92D20 | #22C79D / #FF6B6B |
| colorSuccessText / WarningText / ErrorText | #067A5E / #B45309 / #D92D20 | #3DDBB0 / #FFC26B / #FF8A80 |
| colorText / Secondary / Tertiary | #101828 / #475467 / #667085 | #E8ECF4 / #B3BDCD / #8792A4 |
| colorTextPlaceholder | #667085 | #8792A4 |
| colorBorder / BorderSecondary | #C5CCD8 / #E3E8EF | #3A4556 / #2B3442 |
| colorBgContainer / Layout / Elevated | #FFFFFF / #F5F7FB / #FFFFFF | #161C25 / #12171F / #1C2330 |
| borderRadius / LG / SM | 10 / 16 / 6 | 同左 |
| fontSize / SM / LG | 14 / 12 / 16 | 同左 |

`AppThemeProvider.tsx` 在系统开启减弱动效时额外给 antd 传 `motion: false`（已实现，保持）。

---

## 12. 迁移清单（按风险从低到高）

前四项**只加不删、无视觉变化**，可以先合入；后面几项会改变现有界面，需要逐页走查 / 产品评审。

| # | 改动 | 文件 | 影响 |
|---|---|---|---|
| 1 | 新增 `*-text`、`danger-fill`、`state-*`、`fill-quiet`、`brand-spark`、`material`、`scrim`、`radius-full`、`space-1…16`、文字层级、`shadow-raised/floating` 令牌 | variables.css · appTheme.ts | 只加不删 |
| 2 | SPRING 增加 smooth / snappy，旧 pop / ink / panel 标记 `@deprecated` | utils/motionTokens.ts | 新代码用新预设 |
| 3 | 减弱动效：位移类覆盖版从"定格"改为"仅 opacity"，门禁识别 `motion-fade` / `motion-stop` | styles/motion.css · scripts/check-loading-motion.mjs | 只影响开启减弱动效的用户 |
| 4 | 新增小尺寸方形标，网页图标 16 / 32 换用 | public/ · index.html | 网页图标更清晰 |
| 5 | 语义色当文字处换成 `*-text` | 全局搜 `color: var(--color-(success|warning|error))` | 提示文字变深 |
| 6 | 主按钮悬停 / 按下改为叠加层；深色主色改回 #126DFF | appTheme.ts · variables.css | 悬停由变浅改为变深 |
| 7 | 占位符改为三级文字色；输入框补齐可见标签 | variables.css · 各表单 | 占位符变深 |
| 8 | `--space-lg` 20 → 24、`--space-xl` 40 → 48 | variables.css · 引用处 | 需逐页走查 |
| 9 | 静止卡片去掉 `shadow-card`；对话输入区去掉边框 | 各卡片样式 · composer | 需走查 |
| 10 | 用户气泡入场 .98 + 8px / 240ms；流式输出改为词块淡入，去掉 blur | chat/messages.css · chat/grapheme-reveal.css | 需体验评审 |
| 11 | 工具调用默认折叠为一行；运行中改为文字微光，涟漪只留给待确认 | tool.css · 工具调用组件 | 需产品评审 |
| 12 | 菜单圆角 16、选项 10（同心） | antd Dropdown 主题 · 自研菜单 | 视觉微调 |

做第 1、2 项时直接以 `assets/design-v2/tokens/` 下的 CSS / TS 为准，深浅两档一起加；第 5 项起每一项都是**会改变现有界面**的改动，没有用户明确要求不要顺手做。
