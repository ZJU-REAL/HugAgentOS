# 插件 UI 素材库（plugin-ui）

宿主提供**有限的一组 view 素材**，插件在自己的 `plugin.json` 里声明「哪个工具用哪个 view、
字段怎么映射」。宿主的渲染链路里因此**没有任何一处按插件名 / 工具名分支**——加一个插件不需要
改前端，卸载一个插件它的界面自动消失。

设计背景与完整契约见 `internal design docs`。

## 目录结构

```
plugin-ui/
├── README.md            ← 本文件：素材清单 + 如何扩展
├── index.ts             ← 对外出口（宿主只从这里 import）
├── types.ts             ← 契约类型，与后端 core/services/plugin_ui_contract.py 一一对应
├── pointer.ts           ← 受限 pointer 求值器（白名单解析，无 eval）
├── i18n.ts              ← 贡献文案解析（字符串或 {zh-CN, en} 映射）
├── registry.ts          ← ★ view kind → 组件 的唯一注册表
├── PluginView.tsx       ← 渲染宿主：负责 actions 生命周期、分页、嵌套渲染
├── styles.css           ← 素材库自带样式（jx-pv-* 命名空间，主题变量驱动）
├── ViewProps.ts         ← 所有 view 共用的 props 契约
├── views/
│   ├── document/        A 组 · 文档型（7）
│   ├── analytic/        B 组 · 分析型（6）
│   ├── container/       C 组 · 容器与交互型（6）
│   ├── svg/             四个图表图元（折线 / 条形 / 环形 / 雷达）
│   └── shared/          view 之间共用的小组件
└── module/
    ├── PluginModuleFrame.tsx   L2：插件自带前端模块的 iframe 宿主
    └── bridge.ts               L2：postMessage 桥（grants 白名单 + 限频）
```

## 素材清单（19 种）

| 组 | view kind | 用途 |
|---|---|---|
| A 文档型 | `badge` | 一行完成态徽章 |
| | `kv` | 键值详情表（接受 record 或 `{key,value}` 数组两种上游形态） |
| | `list` | 卡片列表（标题 + 摘要 + 元信息 + 引用角标） |
| | `table` | 表格，列可由 map 指定或从数据推断 |
| | `markdown` | Markdown 正文 |
| | `sections` | 分组卡片，点开看完整分块 |
| | `metrics` | 指标卡（大数字 + 同比） |
| B 分析型 | `timeseries` | 折线 / 柱状趋势 |
| | `ranking` | 榜单（名次 + 热度条 + 升降） |
| | `comparison` | 多主体 × 多指标对比矩阵，可高亮最优 |
| | `distribution` | 占比分布（环形或条形，按类目数自动选） |
| | `score` | 多维评价（≥3 维用雷达，否则条形） |
| | `timeline` | 时间轴事件流 |
| C 容器交互 | `tree-graph` | 层级图谱画布：布局 + 缩放平移 + 展开折叠 + 节点下钻 |
| | `gallery` | 图标卡片网格 |
| | `status-list` | 带状态徽章与行内动作的列表 |
| | `trace` | 调用链 / 耗时视图 |
| | `link-card` | 外链卡片 |
| | `tabs` | **容器**：把多个 view 组合成页签（嵌套深度上限 2） |

## 如何新增一种 view（三步）

1. **写组件**：在 `views/<组>/XxxView.tsx` 里导出一个接收 `ViewProps` 的组件。
   只读 `map` 指定的字段（用 `pointer.ts` 的 `readText` / `readRecords` 等），
   要触发动作就调 `ctx.runAction`——**不要自己发请求**，数据一律走 L1 代理。
2. **登记**：在 `registry.ts` 的 `VIEW_REGISTRY` 里加一行。
3. **放行**：在后端 `core/services/plugin_ui_contract.py` 的 `VIEW_KINDS` 里加上同名字符串，
   否则安装时会被当作未知类型丢弃（这是有意的：拼错的 view 名会出现在导入报告里，
   而不是运行时变成一张空卡片）。

样式统一写在 `styles.css`，类名前缀 `jx-pv-`，颜色一律用文件顶部的 CSS 变量，
这样深浅色主题不需要任何 JS 参与。

## 两条硬规矩

- **契约是数据，不是代码。** `pointer.ts` 是白名单解析器，不接受表达式、不 `eval`。
  需要执行代码的场景走 L2 模块沙箱，不要给 L0 开口子。
- **插件的自定义前端不进这个目录。** L2 模块的代码放在插件包自己的 `web/` 目录里，
  由后端 `/v1/plugins/{slug}/web/{path}` 托管、在 null origin 的 iframe 里运行。
  这里只有承载它的 `PluginModuleFrame` 和桥。

## 图表为什么是手写 SVG

前端没有引入任何图表库。B 组三种绘图 view 用 `views/svg/primitives.tsx` 里四个图元实现，
理由：这几种数据形态是固定的，不需要通用图表引擎的表达力；自绘能保证主题、字体、
配色与产品一致；而一旦引入图表库，插件迟早会要求"透传图表库的全部配置项"，
那 L0 声明式就退化成了图表库 DSL 透传，契约会失控。真需要任意图表时走 L2。
