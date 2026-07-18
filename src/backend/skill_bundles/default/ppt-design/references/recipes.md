# Deck Recipes —— 起手骨架

骨架按**真实参考**逐个重建。每份富骨架都是 **20+ 不同组件 / ~26 页**的完整 deck,
直接复制改 `theme` + 内容即可,**别再每页同一种版式**：

| skeleton | 何时用 | 组件特征 |
|---|---|---|
| `assets/skeleton-default.json` | 常规汇报通用起点 | 默认风格,基础富版式 |
| `assets/skeleton-tech.json` | 技术/产品/方案/架构/周报 | 深蓝金 `dark_gold` |
| `assets/skeleton-data.json` | **数据/咨询/经营分析** | kpi_delta·donut·trend·table·sparkcards·funnel·status_list·pie·numbered… |
| `assets/skeleton-glass.json` | **数据大屏/科技产品(深色)** | gauges·donut·radar·lines·sparkcards·pie(玻璃拟态) |
| `assets/skeleton-blueprint.json` | **系统架构/技术蓝图** | stack·hub·ribbons·process·matrix·status_list(蓝图网格) |
| `assets/skeleton-editorial.json` | **人文/报告/政企叙事** | timeline·statement·numbered·team·quote·milestones |
| `assets/skeleton-ink.json` | **中式/文化/国风** | vertical·statement·defs·timeline·numbered(宣纸朱砂) |
| `assets/skeleton-newsprint.json` | **简报/资讯/复盘(报刊感)** | article·table·trend·scatter·callout·status_list |

> 复制某份骨架后,改 `theme` 名即可换配色 + 配套 pack（骨架的 theme 字段可直接替换）。
> 每份骨架已铺 20+ 种不同组件,是"组件不要千篇一律"的现成范例。

---

## 没有合适骨架时怎么起手（这才是常态）

骨架只是省事的起点，**不是必须**。没有贴合的骨架时，直接按下面两步自己谋篇：

1. **保留用户原话的框架与标题**——用户说"周报 / 政策解读 / 路演 / 课件"，就照那个结构走，
   不要硬塞进某个模板的分类。
2. **按每页内容形态选版式**（详见 `layouts.md`）：
   - 数字/KPI → `kpi_cards` / `stat_callout` / `bar_chart_kpi` / `numbers`(大数字带) / `milestones`
   - 占比/构成 → `donut`(环形图) / `concentric`
   - 趋势/时间序列 → `trend`(折线面积图) / `bar_chart_kpi`
   - 达成率/完成度 → `gauges`(环形仪表盘) / `progress`
   - 表格/排名/台账 → `data_table`(带状态点) / `comparison_table` / `kpi_ledger`
   - 并列要素 → `icon_rows` / `grid` / `icon_cards`
   - 流程/步骤 → `timeline` / `process` / `journey`
   - 架构/分层/技术栈 → `stack`(架构分层) / `hub_spoke`(拓扑)
   - 对比 → `two_col` / `comparison_table` / `compare_panels`
   - 规格/口径/参数 → `defs`(规格清单)
   - 定价/档位 → `pricing`(定价档位)
   - 多栏正文/报刊 → `article`(多栏)
   - 竖排/对联/国风 → `vertical`
   - 字体/视觉规范 → `specimen`(字体大样)
   - 份额/构成饼 → `pie`(饼图) / `donut`
   - 多维评估/能力 → `radar`(雷达图)
   - 分布/相关 → `scatter`(散点)
   - 多系列趋势/交叉 → `lines`(多线) / 多期对比 → `grouped_bars`(分组柱)
   - 升降/同比 → `kpi_delta`(带箭头) / 指标+迷你趋势 → `sparkcards`
   - 状态台账 → `status_list`(状态标签) / 流向 → `ribbons`
   - 强调横幅/CTA → `callout` / 勾选清单 → `checklist`
   - 日程/周历 → `calendar` / 概念释义 → `def_cards` / 大序号 → `numbered`
   - 喘息 → `quote` / `big_number` / `statement`(大宣言) / `section`

> 组件库共 ~70 种（基础 ~45 + 进阶 26,schema 见 `layouts.md`）。
> **一份完整 deck(20+ 页)应铺 10–20 种不同组件**,参考 `assets/skeleton-*.json`(每份已示范 20+ 组件)。

## 组件随风格变化（**最重要**：别每套都用同一批组件）

不同风格的 deck 内部**组件词汇**不一样。选定 `theme` 后，**优先从该家族的特征组件里挑**，
让内容形态贴合那一类模板的"长相"，而不是无论什么风格都堆 kpi_cards + 时间线。

| 风格家族（theme） | 该多用的特征组件 |
|---|---|
| 瑞士网格 `swiss_grid` / 学术 `academic_*` | `statement`、`specimen`、`defs`、`numbers`、`two_col`、`trend` |
| 咨询/数据 `consulting_navy` `tech_report_light` | `kpi_delta`、`donut`、`pie`、`trend`、`lines`、`grouped_bars`、`data_table`、`status_list`、`funnel`、`sparkcards`、`callout` |
| 编辑/人文 `warm_editorial` `bronze_premium` `urban_project` `editorial_gold` | `timeline`、`statement`、`numbered`、`two_col`、`numbers`、`team`、`quote`、`milestones` |
| 中式 `ink_chinese` | `vertical`、`statement`、`defs`、`timeline`、`numbered`、`quote` |
| 玻璃仪表盘 `glass_dashboard` | `gauges`、`donut`、`pie`、`radar`、`trend`、`lines`、`sparkcards`、`numbers`、`status_list` |
| 蓝图/架构 `blueprint` 终端 `terminal_dark` `agent_dark` | `stack`、`hub_spoke`、`ribbons`、`process`、`matrix`、`data_table`、`status_list`、`numbered` |
| 财经 `capital_dark` | `trend`、`lines`、`donut`、`pie`、`numbers`、`horizontal_bars`、`grouped_bars`、`scatter`、`ribbons`、`kpi_delta` |
| 报刊 `newsprint_brutal` | `article`、`data_table`、`trend`、`scatter`、`numbers`、`statement`、`callout`、`status_list` |
| 孟菲斯/Riso `memphis_pop` `riso_zine` | `numbers`、`pricing`、`gauges`、`icon_cards`、`calendar`、`checklist`、`funnel`、`callout`、`numbered` |

> 这不是硬绑定——按真实内容选最达意的组件即可；但**同一份 deck 内要混用 3–5 种不同组件**，
> 避免连续多页同一版式。新组件 schema 见 `layouts.md`。

## 选风格（见 SKILL「选风格」节 / `themes.md` 完整目录）

- 默认通用风 → `theme` / `pack` 都不写（白底 + 克莱因蓝）
- 技术风格 → `"theme":"navy_gold"`（深蓝金，封面/章节深色）
- 场景化 → 从 `themes.md` 扩展目录挑一个直接写 `theme` 名（自动带配套 pack）

## 页数甜区

8–12 页是大多数汇报的甜区；周报 / 进展类 12–16；研究 / 年终 15–20。
按内容定，不要为凑页数注水，也不要把多件事塞进一页。
