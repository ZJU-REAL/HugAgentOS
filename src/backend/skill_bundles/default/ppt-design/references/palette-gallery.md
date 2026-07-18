# 色板库 —— 扩展主题的配色值 + 版式母题

> ✅ **这 22 套已注册为 `build` 路径的命名主题**(见 `references/themes.md` 的扩展目录)。
> 走 `build` 时**直接写 `theme` 名**(如 `"theme":"glass_dashboard"`)即可,引擎自动
> 套配色 + 配套 pack,**不用**关心下面的 hex。CLI / `list-themes` 都认这些名字。
>
> 下面的 hex 表有两个用途:① 走 `build-js`(freeform)手写脚本时,照抄进脚本
> (freeform 不读命名主题);② 想理解每套主题的设计意图 / 版式母题。
> 每套是五色契约:`primary`(标题/深色)· `secondary`(次级)· `accent`(唯一锚点
> 强调色)· `light`(浅块/边框)· `bg`(页面底色)。多锚点的标了"+次强调"。
>
> **用色纪律照旧**(见 `design-principles.md`):一个色占 60-70% 视觉重量,
> 强调色只点睛;要深浅层次就调亮/调暗 accent,别引新色。

色值是从 `uploads/ppt-template/` 下 23 套开源 deck 逐个解析 + 看缩略图提炼的
(缩略图总览在 `uploads/ppt-template/_previews/`,源文件许可见
`uploads/ppt-template/INDEX.md`,主力 21 套为 MIT/需署名)。**这些不是 Coze /
千问 / Kimi 的模板**(那三家模板不可下载),是 GitHub 开源 deck 的设计语言提炼。

---

## A · 浅色专业(汇报 / 报告 / 数据 / 政企 —— 最常用)

| 盘子 | primary | secondary | accent(+次强调) | light | bg | 适用 · 版式母题 | 源 deck |
|------|---------|-----------|------------------|-------|-----|------------------|---------|
| `swiss_grid` | 1A1A1A | 666666 | D9251D | E8E8E8 | FFFFFF | 极简网格 / 编辑排版;大字封面、严格栅格、红点点睛。正式报告、研究 | swiss_grid_systems |
| `academic_blue` | 1A202C | 4A5568 | 3182CE +1A365D | E2E8F0 | F5F7FA | 学术 / 论文讲解;公式、图表、引用密集,蓝调克制 | attention_is_all_you_need |
| `consulting_navy` | 1A3A5C | 5D6D7E | E8A838 +2D8A4E | F0F2F5 | FFFFFF | 咨询 / 商业(麦肯锡感);KPI 卡、横向对比、折线柱状 | kimsoong_loyalty_programme |
| `tech_report_light` | 1B3A5C | 5B6776 | E8743B +3E7CB1 | D8DEE6 | F7F9FB | 浅色科技 / 数据报告;架构图、表格、指标块,干净理性 | lora_hu_2021 |
| `urban_project` | 2B3A4A | 5A6B7A | C2410C +6B7A4F | D6CFC0 | F5F2EC | 城市更新 / 政企项目 / 工程汇报;大图+图注、章节大序号 | high_rise_renewal |
| `warm_editorial` | 1F1B16 | 6A6258 | A44A3F | D8CBB8 | F6F1E8 | 暖米色编辑 / 人文报告;卡片三栏、时间线,亲和 | lin-huiyin-architect-revised |
| `bronze_premium` | 1C1C1C | 5C5852 | B8935A | D4CFC4 | F5F2EC | 高级灰金 / 建筑 / 奖项 / 评选;满版大图+留白,克制奢华 | pritzker_2026 |
| `ink_chinese` | 1A1A1A | 5C5852 | A52A2A | C8C0AE | F5F1E8 | 中式水墨 / 国风 / 文化政务;宣纸底、朱砂印章红、竖排标题 | cangzhuo |
| `nature_soft` | 3A3530 | 7A7068 | C99E62 | EDE5D3 | F7F2E8 | 自然草木 / 生活方式 / 柔和;色卡陈列、留白多,温润 | liziqi-plant-dye-colors |
| `corp_multi`(去品牌) | 231F20 | 50798A | 61A150 +367FB9 | E7E6E1 | FFFFFF | 通用企业 / 机构;多彩中性,适合分类内容。字体 Bitter/Rubik | GBIF-2023-corporate.potx |
| `academic_teal`(去品牌) | 1A1A1A | 50798A | 005C69 +D73371 | E7E6E6 | FFFFFF | 学术 / 科研海报;青墨+品红点睛,Arial,4:3 也适配 | lrkrol-academic-velis.potx |

## B · 深色科技 / 高级(技术 / 产品 / 路演 / 发布会)

> 深色盘子封面/章节用 `bg` 深底,内容页可反过来用浅卡片(深浅三明治)。
> `navy_gold`(技术风)已覆盖"深蓝金"主流诉求,下面是它表达不了的其它深色调性。

| 盘子 | primary | secondary | accent(+次强调) | light | bg | 适用 · 版式母题 | 源 deck |
|------|---------|-----------|------------------|-------|-----|------------------|---------|
| `glass_dashboard` | FFFFFF | A8B0D0 | 3DDDFC +A26BFA | 1A2150 | 0A0E27 | 玻璃拟态仪表盘;半透卡片、霓虹青紫、数据大屏 | glassmorphism_demo |
| `terminal_dark` | E6EDF3 | 8B949E | D4A574 +60A5FA +34D399 | 30363D | 1C2333 | 暗夜终端 / 工程科技;代码块、状态卡、三态色(暖金/蓝/绿) | claude-code-auto-mode |
| `agent_dark` | E8E8EC | 9CA3AF | D4845A +5B9BD5 | 2D3348 | 1A1D27 | 深色 Agent / 技术方案;流程图、对比双栏,陶橙+蓝 | building_effective_agents |
| `blueprint` | F0F4F8 | A0B8D0 | FFB627 +5BA3E0 | 2D4A6B | 0E2A47 | 蓝图 / 系统架构;线稿框图、等距示意,琥珀点睛 | kubernetes_blueprint_2026 |
| `capital_dark` | E8E6E1 | 8A857E | E63946 +F4A261 | 2A2F36 | 0E1116 | 深色资本 / 财经报告;炭黑底、红橙数据可视化,罗马数字章节 | global_ai_capital_2026 |
| `editorial_gold` | E8E8E8 | A0A0B0 | C9A96E | 2A2A4A | 1A1A2E | 深色金 人物 / 传记 / 专题;深底大图+金标题,叙事 | lin-huiyin-architect |
| `luxe_interior` | E8E0D4 | B0A08E | C4A882 | 7A6E60 | 1A1714 | 深色奢华 / 室内 / 高端品牌;暖近黑底、香槟金、满版图 | home-design-trends-2026 |
| `magazine_black` | FFFFFF | 9E9690 | C9A96E | 2A2520 | 0A0A0A | 黑底杂志 / 时尚 / 艺术;纯黑底、大图陈列、衬线刊头(小众) | fashion-weekly-digest |

## C · 创意 / 小众撞色(默认别用,只在用户明确要"活泼/潮/手作感"时)

> 高饱和、强个性,放错场景会显廉价。用前确认用户诉求,且全篇统一。

| 盘子 | primary | secondary | accent(+次强调) | light | bg | 适用 · 版式母题 | 源 deck |
|------|---------|-----------|------------------|-------|-----|------------------|---------|
| `newsprint_brutal` | 111111 | 6B6B6B | C8102E | CBC8B7 | F4F1EA | 野兽派 / 报纸排版;粗黑分栏、报头、网点图(编辑/宣言感) | brutalist_ai_newspaper_2026 |
| `riso_zine` | 1A1A1A | 5A5A5A | FF5C8A +1E4DBC +E8A02E | F5EFE0 | F5EFE0 | Riso / Zine 撞色;粉蓝叠印、手作拼贴(文创/青年向) | indie_bookstore_zine_guide |
| `memphis_pop` | 1A1A2E | 5C5C7A | FF3DA5 +FFD93D +00C896 +00B8D9 | FFF8EE | FFF8EE | 孟菲斯 / 高饱和活泼;几何撞色、贴纸感(活动/快消/少儿) | sugar_rush_memphis |

---

## 版式参考(非配色)

- **`image-text-showcase`**(uploads 里 20 页)是一份**图文版式合集**,不是单一配色——
  深底/浅底/自然/紫调混排。当 freeform 要找"大图配文怎么摆"的版式灵感时,翻它的
  缩略图 `_previews/image-text-showcase.png`。配色不统一,**不收进上面的盘子表**。

## 不收录 / 慎用说明(交代清楚,无遗漏)

23 套全部已过目;映射结果:
- A/B/C 三表共收 **22 套**的配色(浅色 11 + 深色 8 + 创意 3)。
- `image-text-showcase` 只作**版式**参考,配色不收(见上)。
- 两个 `.potx`(GBIF / lrkrol)自带真主题色板,已**去品牌**后收入 A 表;其源文件
  含机构 logo / "Delete before presenting" 提示页,**只抄色板别直接套用文件**。
- C 表三套属小众,**默认流程不要选**;A 表前五套(swiss_grid / academic_blue /
  consulting_navy / tech_report_light / urban_project)覆盖绝大多数汇报/报告需求,
  优先从这里挑。
