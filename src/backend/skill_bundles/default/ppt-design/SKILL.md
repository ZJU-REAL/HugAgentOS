---
name: ppt-design
display_name: PPT 演示文稿设计
description: "**设计、生成或编辑 PPT / 演示文稿 / 幻灯片 / deck / slides / .pptx** 时使用。既覆盖从零做一套（工作汇报·产品发布会·路演融资·数据/经营分析·行业研究·项目立项·政府汇报·培训课件·复盘总结），把提纲/大纲/文档内容扩写成整套演示文稿或课件；也覆盖对已有 .pptx 的编辑：改某页标题 / 加页删页 / 追加谢谢页 / 插图 / 换主题配色 / 成片质检 / 导出 PDF。只要用户提到 PPT / 幻灯片 / 演示文稿 / deck / slides / 路演 / 汇报材料 / 课件，即便没说用什么工具也走本技能产出 .pptx，别堆纯文字 bullets。仅当目标是 .pptx 时触发；把 ppt 内容总结成文字、推荐模板网站、问 PowerPoint 操作技巧、改一段文案、解释某汇报概念 时不要触发。"
license: MIT
tags: pptx,presentation,slides,deck,office,design
metadata:
  version: "2.0"
  category: productivity
  sources:
    - scripts/ppt-cli shim + scripts/ppt.py CLI (build + edit + render + export)
    - scripts/engine/ python-pptx & pptxgenjs engines (vendored)
---

# PPT 演示文稿设计

把内容做成一份**不像 AI 套模板**的演示文稿。核心工具是本技能预装的
`ppt-cli` 命令 —— 一次 `build` 子命令，传一份 JSON `spec`，产出完整 .pptx。
所有读、编辑、质检、导出 PDF 都是同一个 CLI 的子命令。

本技能不是"教你调 CLI"，而是教你**怎么想**：先谋篇，再选调性，再为每一页挑对版式，
最后用质检子命令确认成片是专业的。直接拿 bullets 平铺是这个工具存在的意义的反面。

> 不要派子代理。直接完成。最终一定要产出用户要的 .pptx 文件。

---

## 进入本技能前先停一秒：别走偏

进了这份 SKILL，**最终产物必须是 `.pptx`**（用 `ppt-cli build` 产出）。三件
"看上去完成了"实则失败的事：

1. **`pdf_create` / `word_*` 不可替代 `ppt-cli`**。用户说了"ppt/演示文稿/幻灯片"
   就必须出 .pptx——哪怕要的是"分析报告 ppt"。输出格式不符就是失败，不接受
   "PDF/Word 也能看"的推理。（pdf_create 的 spec 和 ppt-cli 的长得像，别混用。）
2. **别自己 bash + python-pptx 手搓**。引擎、调色板、富版式、缩略图质检闭环都做好了。
   固定版式表达不了视觉时用内置 `build-js`（freeform，见「两条构建路径」），仍不手搓。

**何时离开本 SKILL**：用户明确要 **PDF / 白皮书 / 简历 / Word 方案**且**没**带
"ppt/演示文稿"等词 → 走 `pdf_create`，别勉强用 ppt-cli。

---

## 终点：pin 到用户工作区才算交付（最重要的一条）

只要用户原话带 **生成 PPT / 做一份 deck / 出一版幻灯片 / 整理成演示文稿**，
这一轮的终点**不是**写完内容 / 写完 spec / build 出 .pptx，**而是
`pin_to_workspace` 调完、`.pptx` 卡片真的出现在用户对话区 / Canvas 里**。

三种"看上去做完了"实则没交付（本会话真实翻过车）：
- **停在 Markdown 简报**：回了一段漂亮 Markdown 就收尾，spec / build / pin 全没做。
- **停在 JSON spec**：build 过一次后回去改 spec，改完没重新 build、没重新 pin。
- **停在沙盒路径**：只回一句"路径是 `/workspace/xxx.pptx`"——沙盒对用户不可见。

### ✅ 正确的交付链（缺一不可，每次 spec 改动后都要重走）

```bash
# 1. 写 spec → 构建（必须）
ppt-cli build --spec /workspace/spec.json --output /workspace/deck.pptx
# 2. 把沙盒文件注册成 artifact，拿到 file_id（必须）
sandbox_get_artifact(src_path="/workspace/deck.pptx")
# 3. pin 到工作区，文件才会出现在用户对话区 / Canvas（缺这步 = 没交付）
pin_to_workspace(file_ids=["<file_id>"])
```
一次 chat 多份产物（.pptx + 缩略图 + .pdf）把 `file_id` 一次性放进 `file_ids` 列表 pin。

---

## 怎么调 CLI（先把这一段读完）

`ppt-cli` 已经装到 sandbox / mcp 容器的 `/usr/local/bin/` 下，所有子命令都是同步执行 + JSON 输出，
直接 bash 一行：

```bash
ppt-cli <subcommand> [args...]
```

它是一层 shim，运行时自动定位到 `scripts/ppt.py`（落到 `/workspace/skills/ppt-design/scripts/` 或
仓库内对应路径，看你在哪个容器里）。本地排查时也可以直接跑 `python <skill_dir>/scripts/ppt.py`，
行为完全一致。

| 阶段       | 子命令                                                                    |
|-----------|--------------------------------------------------------------------------|
| 主构建     | `build` —— 传 JSON spec，产出 .pptx（默认路径）                            |
| 自由构建   | `build-js` —— 跑你手写的 pptxgenjs 脚本，逐像素定制版式（见 `references/freeform.md`） |
| 看一眼     | `info` / `slide-count` / `extract` —— 概览、页数、单页文本               |
| 质检       | `check-placeholders` / `thumbnails` —— 扫占位符 / 渲缩略图              |
| 单页改     | `set-title` / `add-text` / `insert-image` / `add-slide` / `delete-slide` |
| 输出       | `to-pdf` —— 走 LibreOffice headless 转 PDF                              |
| 内省       | `list-themes` / `list-styles` / `list-slide-types`                       |

CLI 全部返回 JSON：成功打到 stdout（`{"ok": true, "output": "...", "meta": {...}}`），
失败到 stderr 且 exit 非零。**所有 path 参数既支持绝对路径也支持相对路径**——CLI 内部
会自己拷进临时 workdir 再调引擎。`--help` 与 `<subcommand> --help` 是权威帮助。

---

## 两条构建路径：spec 引擎（默认）vs freeform

本技能有两种产出 .pptx 的方式，**先选对路再开工**：

| | `build`（spec 引擎，**默认**） | `build-js`（freeform） |
|---|---|---|
| 你做什么 | 写 JSON spec，给每页选一个内置版式 | 手写 pptxgenjs 的 Node 脚本，逐像素摆 |
| 适合 | 常规工作/政府/数据/行业汇报；标准结构内容 | 用户明确要"有设计感/特别/不像模板"、或给了参考图 |
| 优点 | 快、稳、文件 100% 有效、便宜 | 版式无上限，每页可定制 |
| 代价 | 受约 40 个内置版式约束 | 更贵、易错位，**必须**配视觉 QA |

**默认走 `build`。** 95% 的汇报需求 spec 引擎足够好。只有当用户明确表达了
超出内置版式的视觉诉求、而 spec 引擎确实做不出来时，才切到 `build-js`——
这时**先读 `references/freeform.md`**（脚本契约、pptxgenjs API、配色复用、
致命踩坑全在里面），不要凭记忆硬写。

下面的四步法（PPMQ）描述的是 **`build` 路径**。freeform 路径同样要走第 1 步
（Plan 谋篇）和第 2 步（Palette 选调性）的**思考**，只是第 3 步不再"选版式"
而是直接写代码、第 4 步用 `build-js` 而非 `build`——QA 第 4 步两条路一样必做。

---

## 四步法（PPMQ）

每做一份 PPT，按顺序走这四步。**不要跳过第 1 步直接写 spec** —— 没有提纲的
spec 必然是一页页孤立的 bullets，那正是要避免的。

### 1 · Plan —— 先列提纲，再动 spec

先用三五句话和自己确认这份 PPT 的**叙事弧**，再决定页数与每页讲什么：

- **开场**：`cover`（标题 + 可选副标题/一句话定调 tagline）→ 通常接 `toc` 目录
- **主体**：用 `section` 分隔每个大模块；模块内每页是一个 `content`，**讲一件事**
- **节奏**：每 3–4 页穿插一个"喘息页"（`quote` 金句 / `big_number` 单一英雄指标 /
  低密度 `section`）打破"满页模板脸"
- **收尾**：`summary`（要点打勾卡片）；要再加一页"谢谢/致谢"就用 `closing` 页

一份 8–20 页的 deck 是常态。页数由内容决定，不要为凑页数注水，也不要把
五件事塞进一页。

> **两条内容纪律（这两条最近真的翻过车）：**
> 1. **目录(`toc`)的 `items` 必须逐字等于这份 deck 真实的 `section` 章节标题**，
>    禁止写 `"第一章 …"`、`"XXXX"`、`"待补充"`、`"…"`、点点点这类占位/填充符——
>    那就是"目录全是填充符"。做法：先把各 `section` 标题定下来，再回填 `toc`。
> 2. **每一页都要有真实内容，绝不留白板。最后一页尤其要检查**：收尾页要么是有真实
>    结论的 `summary`，要么是 `closing`（谢谢页）。**不要**用一张没填任何正文字段的
>    `content` 空页来收尾——它会渲染成一张白板（即"最后一页是空的"）。

详见 `references/design-principles.md`（叙事弧、密度、节奏的"为什么"）。

### 2 · 选风格 —— 主题(配色)× 设计包(版式性格)

每个 `theme` 自带配套 `pack`(封面/章节/装饰的视觉性格)——通常只写 `theme` 名即可：

- **默认 / 通用风**（绝大多数情况）—— `theme` / `pack` **都不写**(= `swiss_klein`)。白底 +
  克莱因蓝、克制专业，适配工作汇报、数据 / 行业研究、政务、商业、内部汇报。
  起手骨架 `assets/skeleton-default.json`。
- **技术风格** —— `"theme":"navy_gold"`（默认带 `dark_gold` pack）。深蓝 + 金、高对比，
  封面 / 章节深色。适配技术 / 产品 / 方案 / 路演 / 周报。起手骨架 `assets/skeleton-tech.json`。
- **场景化扩展主题**（用户给参考图 / 点名调性 / 内容有强场景属性时）—— 从 `references/themes.md`
  的**扩展目录**挑一个直接写名字:中式 `ink_chinese`、玻璃拟态 `glass_dashboard`、
  蓝图 `blueprint`、咨询 `consulting_navy`、城市工程 `urban_project`、报刊 `newsprint_brutal`…
  共 22 套(浅色专业 / 深色科技 / 创意撞色三档),每套自带配套 pack。

**怎么判**：没诉求 → 默认；要"科技/深色/发布会" → `navy_gold`；给了参考图或点名某种
场景调性 → 从扩展目录挑最贴的一个。拿不准就用默认。完整主题目录 + 每套的配套 pack /
适用 / 别名见 `references/themes.md`，`ppt-cli list-themes` 打印当前全部主题。

> 技术风格的深色观感来自 `dark_gold` **设计包**——它把封面 / 章节 / 眉标整套换成深蓝金
> （双语金方块眉标 + 多色 KPI 卡 + 里程碑条等），不是简单换个底色。`pack` 别名
> `技术风格` / `技术风` / `科技风` / `深色科技` / `深蓝金` / `tech` / `dark` 都等价；
> 仍需同时配 `theme:navy_gold` 才拿到完整深蓝金配色。

**`--style`（视觉性格，与风格正交）** 默认 `soft`。四选一，两套风格都能搭：

| `--style` | 何时用                                   |
|-----------|------------------------------------------|
| `sharp`   | 数据密集、零投影、紧凑 —— 正式报告、财报   |
| `soft`    | **默认** —— 商务/汇报，轻投影             |
| `rounded` | 更松、更透气、实心图标 —— 产品介绍、营销   |
| `pill`    | 留白最大、上浮卡片 —— 发布会、高端品牌     |

**自动质感升级（无需你做任何事，引擎已内建）**：
> - **封面 / 章节 / 总结页自带克制的对角渐变背景**（不再是平板色块）——想关掉用 `"bg_gradient": false`。
> - **所有图表/层级版式（柱图、漏斗、金字塔、同心圆、甘特…）的颜色按数值深浅排序、且不会被冲淡成近白**——次要数据元素始终清晰可读。
> - 这些是默认行为，写 spec 时不用特意处理。

**顺手定一个视觉母题**：选完 theme/style，给这份 deck 认定**一个**反复出现
的视觉元素（一个强调圆点、图标统一进同色圆圈、卡片统一单边粗边条…），
之后每页都带上它。这是 deck 看起来"设计过"而非"拼出来"的关键。还要记住
两条底线——**色彩要有主导色**（一个色占 60–70%，别四色平均用力）、
**每页都要有视觉元素**（别出纯文字页）。详见 `references/design-principles.md`
原则 5–7 与「避免清单」。

### 3 · Map —— 为每一页挑对版式，再写 spec

`content` 页靠 spec 里的字段**自动判定版式**。这一步的关键是：
**先看这页内容是什么形态，再选版式，再填对应字段**。连续 3 页以上纯 `bullets`
是被禁止的——它廉价、且浪费了这个工具的全部价值。

按内容形态选版式（每页独立决定）：

| 这一页的内容是…                     | 用版式             | 关键字段                         |
|------------------------------------|--------------------|----------------------------------|
| 1–3 个英雄指标 / KPI               | `stat_callout`     | `stats:[{value,label,tagline?}]` |
| 4–8 个指标卡片墙（总览）            | `kpi_cards`        | `kpis:[{value,label,desc?}]`     |
| 核心结论 + 几个支撑指标（仪表盘）   | `bento`            | `bento:[{kicker?,value?,title,desc?,icon?}]`（首项=主块） |
| 3–5 个特性 / 能力 / 支柱           | `icon_rows`        | `items:[{icon,title,desc}]`      |
| 4–6 个分类 / 选项卡片              | `grid`             | `items:[{title,desc}]`           |
| 4–6 个图标卡片格（可带顶部警示条 / 底部备注）| `icon_cards`  | `tiles:[{icon,title,sub?,desc}]` + `banner?` + `footer?` |
| 3–5 个阶段 / 步骤（横向）          | `timeline`         | `steps:[{step,title,desc}]`      |
| 纵向里程碑路线图                   | `roadmap_vertical` | `roadmap:[{phase,title,desc}]`   |
| A vs B 对比                        | `two_col`          | `leftBullets` / `rightBullets`   |
| 多行特性对比表                     | `comparison_table` | `columns:[…]`, `rows:[[…]]`      |
| 叙述 + ≤3 个关键结论高亮           | `highlights`       | `bullets`, `highlights:[≤3]`     |
| 3–4 个需视觉对比高度的 KPI         | `bar_chart_kpi`    | `bars:[≤4 {value,label,percentage}]` |
| 5–10 个排名 / 份额                 | `horizontal_bars`  | `bars:[>4 {label,value,percentage}]` |
| 3 层生态（核心/中间/外延）         | `concentric`       | `rings:[3 {title,items}]`        |
| 中心 + 环绕要素（波特五力/生态）    | `hub_spoke`        | `hub:{center,nodes:[{title}]}`   |
| 4–6 行财务式 KPI 台账              | `kpi_ledger`       | `ledger:[{value,label,desc?}]`   |
| 2×2 战略象限 / SWOT               | `matrix`           | `axisX`,`axisY`,`quadrants:[4]`  |
| 横向箭头流程                       | `process`          | `flow:[{title,desc}]`            |
| 3–5 步图标流程（图标圈 + 箭头）     | `journey`          | `journey:[{icon,title,desc}]`    |
| 金字塔 / 层级（顶→底）            | `pyramid`          | `pyramid:[{title,desc?}]`        |
| 3–5 级转化漏斗                     | `funnel`           | `funnel:[{label,value}]`         |
| 一句金句定调（喘息页）             | `quote`            | `quote`, `attribution?`          |
| 单一英雄数字（喘息页）             | `big_number`       | `bigNumber:{value,label,sublabel?}` |
| 团队 / 人物阵容（头像+姓名+职务）   | `team`             | `team:[{name,role,desc?,icon?}]` |
| 客户证言 / 背书（带署名）          | `testimonial`      | `testimonials:[{quote,name,role?}]` |
| 合作伙伴 / 客户 logo 墙            | `logo_wall`        | `logos:[{name,icon?/data_base64?}]` |
| 一页讲透一个重点（详情+指标+要点）  | `split_feature`    | `feature:{title,desc,bullets}` + `metrics:[…]` + `points:[…]` |
| 对照面板（迁移前后/现状目标/优劣）  | `compare_panels`   | `panels:[{label,tone,bullets}]`  |
| 纵向卡片清单（左色条+图标+说明）    | `card_list`        | `cards:[{icon,title,desc}]`      |
| 里程碑横条（大号值+标题+说明）      | `milestones`       | `milestones:[{value,title,desc}]` |
| 命令 / 接口 / 操作清单（标签+等宽码）| `commands`        | `commands:[{label,code,desc}]`   |
| 实在没结构的 3–5 条要点（最后手段）| `single`           | `bullets:[…]`                    |

**全页跨 deck 至少混用 3–5 种不同版式。** 每个版式的完整 JSON schema、条数上下限、
踩坑点，以及"字段共存时谁优先"的判定顺序，见 `references/layouts.md` —— 写
spec 前如果对某个版式的字段没把握，**先读它**。

**组件要随风格变化,别每套都堆同一批。** 组件库共 ~70 种。除上表外,从真实模板提炼的有:
图表 `donut`/`pie`/`trend`/`lines`(多线)/`grouped_bars`/`radar`/`scatter`/`gauges`(仪表盘);
数据 `numbers`(大数字带)/`kpi_delta`(升降箭头)/`sparkcards`(迷你趋势)/`data_table`/`status_list`;
版面 `statement`/`specimen`/`defs`/`def_cards`/`vertical`(竖排)/`article`(多栏)/`pricing`/`stack`(架构)/
`ribbons`(流向)/`callout`(横幅)/`checklist`/`calendar`/`numbered`(大序号)。
**一份 20+ 页 deck 应铺 10–20 种不同组件**,且**优先从该 `theme` 家族的特征组件里挑**
(咨询/财经→donut/trend/lines/data_table/kpi_delta;蓝图/终端→stack/hub_spoke/ribbons;
中式→vertical;报刊→article;瑞士/学术→specimen/defs/statement;玻璃→gauges/radar;孟菲斯→pricing/calendar)。
对照表见 `recipes.md`「组件随风格变化」;schema 见 `layouts.md`「进阶组件」;
现成 20+ 组件范例见 `assets/skeleton-{data,glass,blueprint,editorial,ink,newsprint}.json`。

写 spec 的硬规则（来自 `references/layouts.md` 的判定顺序）：

1. **一页只放一种版式的字段。** 同时写 `stats` 和 `bullets`，引擎按优先级选
   `stats`，`bullets` 被丢弃。保持每页字段干净。
2. **要确定性就显式写 `"layout":"xxx"`。** 它最高优先级，覆盖自动判定。
   不确定自动判定会选什么时，显式声明。
3. **每个 `content` 页都必须带可渲染数据,严禁只有标题的空白页。** 数据字段名按
   `layouts.md` 的 schema 写(如 `bars` / `ledger` / `kpis` / `tiles` / `steps`)。
   引擎对"用版式名当字段名"是容错的(写 `horizontal_bars`/`kpi_ledger`/`kpi_cards`
   也能识别),但**最稳的是照 schema 字段名**。哪怕只有一段话,也用 `body` 或 `bullets`
   写进去——引擎会把它渲成正文,绝不要留空。

默认通用风的封面/章节/总结都是浅色，**不要**逐页改深色。要深色高对比的整体
观感就走**技术风格**（`theme:navy_gold` + `pack:dark_gold`，封面/章节自动深色）；
仅个别页要反色时才加 `"cover_style":"dark"` / `"summary_style":"dark"`。

任意 `content` 页可加 `"kicker":"小标题眉标"`（标题上方大写提示标签），
瞬间提升专业感。`item.icon` 可填语义概念名（`chart`/`users`/`shield`/
`rocket`/`idea`/`security`/`finance`/`eco`…），引擎自动渲染 Tabler 矢量图标。
插图见 `references/design-principles.md` 的 image slot 一节。

**起手骨架在 `references/recipes.md`** —— 目前只有两份（旧的一批"只换色、版式雷同"
的通用模板已删，后续按真实参考逐个补）：

| skeleton | 何时用 |
|---|---|
| `assets/skeleton-default.json` | 任何常规汇报的**通用起点**（默认风格） |
| `assets/skeleton-tech.json`    | **技术风格**：技术/产品介绍、方案宣讲、架构汇报、进展/周报（深蓝金 `dark_gold`，富版式齐全） |

**没有贴合骨架是常态**——别硬套。直接：① 保留用户原话的框架与标题；② 按每页内容形态
挑版式（见 `layouts.md` 与下面的版式表）；③ 需要某种视觉风格就加 `pack`（见「设计包」节）。
`cat` 一份骨架当结构参考即可，文字全换成用户真实内容。

**新增的人物 / 社会证明 / 收尾版式什么时候用**（让 deck 更像"真做过"而非套模板）：
- `team`：路演团队页、招商 / about-us 的人物阵容、专家委员会
- `testimonial`：招商已落户企业之声、产品客户口碑、项目背书——比纯 bullets 可信得多
- `logo_wall`：合作伙伴 / 客户 / 生态成员并列展示
- `closing`：`summary` 之后再来一页"谢谢 + 联系方式（电话/邮箱/二维码）"作正式收尾
- `section_style:"numbered"`：章节多、想要"第 01/02 章"强编号节奏感时给 `section` 加上

#### 构建命令

写好 spec 落到一个 .json 文件后跑：

```bash
# 默认通用风：theme/pack 都不传
ppt-cli build --spec /tmp/my-spec.json --output /tmp/deck.pptx [--style soft] [--engine pptxgenjs]

# 技术风格：传 navy_gold + dark_gold（也可写在 spec 顶层的 "theme"/"pack" 里）
ppt-cli build --spec /tmp/my-spec.json --output /tmp/deck.pptx --theme navy_gold --pack dark_gold
```

`--engine` 默认 `pptxgenjs`（视觉保真度高，需要镜像里装了 node + pptxgenjs 全局包），
找不到 Node/pptxgenjs 会抛错——这时退到 `--engine python-pptx`（纯 Python，
视觉略弱但绝对可用）。

成功输出形如：
```json
{"ok": true, "output": "/tmp/deck.pptx",
 "meta": {"engine":"pptxgenjs","theme":"...","style":"soft","slide_count":12,
          "size_bytes":...,"layout_warnings":[...],"font_warnings":[...]}}
```

### 4 · QA —— 构建后必须质检，不能盲发

build 完先看返回 JSON 里的 `meta.layout_warnings`：非空说明 deck 太多纯 bullets /
版式太单一 —— 按提示改 spec 重新构建（改完 spec 务必重走「终点」节那条交付链）。

然后跑质检闭环（细节见 `references/qa-and-edit.md`）：

```bash
# 1. 渲缩略图，真的去看每页排版是否溢出/空洞/错位（生成 slide-01.jpg .. slide-NN.jpg）
ppt-cli thumbnails /tmp/deck.pptx --output-dir /tmp/thumbs/

# 2. 扫占位符（xxxx / 待补充 / lorem 等）
ppt-cli check-placeholders /tmp/deck.pptx
# is_clean=false 必须补完所有 hits 才能交付

# 3. 整页重做 → 回到 build；微调单页 → 用下面的单页编辑命令
# 4. 用户要 PDF 时
ppt-cli to-pdf /tmp/deck.pptx --output /tmp/deck.pdf
```

---

## 编辑已有 .pptx

用户给一个已有 deck 要改时，**不要重建**。先 `info` 看页数和每页标题定位，
再用单页编辑子命令精确修改：

```bash
# 概览：slide_count + 每页标题（用来定位 --slide N）
ppt-cli info <deck.pptx>

# 改某页标题
ppt-cli set-title <deck.pptx> --slide 3 --title "新标题" --output <new.pptx>

# 在某页加一个自由文本框（位置以英寸为单位）
ppt-cli add-text <deck.pptx> --slide 3 --text "补充说明" \
  --output <new.pptx> [--position 0.5,4.5] [--font-size 12] [--color 1A2B3C] [--bold]

# 在某页插图（图片路径自动拷入）
ppt-cli insert-image <deck.pptx> --slide 3 --image <img.png> \
  --output <new.pptx> [--position 5,1,4,3]

# 追加一页（type=cover|toc|section|content|summary）
ppt-cli add-slide <deck.pptx> --type section --title "新章节" \
  --output <new.pptx> [--content "副标题/正文"] [--theme ...] [--style ...]

# 删一页
ppt-cli delete-slide <deck.pptx> --slide 3 --output <new.pptx>
```

每个子命令都把改完的结果落到 `--output` 指定的新文件——**不会原地覆盖**。
完整参数表与"何时该重建 vs 该编辑"的判别，见 `references/qa-and-edit.md`。

---

## 资源索引

| 文件 | 何时读 |
|------|--------|
| `references/layouts.md` | 写 spec 前——每个版式的精确 schema、条数限制、字段优先级判定顺序、踩坑点 |
| `references/freeform.md` | 走 `build-js` freeform 路径前——脚本契约、pptxgenjs API、配色复用、致命踩坑 |
| `references/themes.md` | 选风格时——两套风格（默认通用 / 技术 dark_gold）的判定、配色 hex、`--style` 四档 |
| `references/palette-gallery.md` | 22 套扩展主题的配色 hex + 版式母题（已注册为 build 命名主题；`build` 直接写 theme 名，freeform 才抄 hex）|
| `references/design-principles.md` | 想理解"为什么"——叙事弧、信息密度、节奏、视觉层级、视觉母题、色彩主导性、避免清单（含"标题下不画线"）、字体、图标语义、插图 slot |
| `references/recipes.md` | 开工前——按 deck 类型给推荐配方与页序，指向可复用骨架 |
| `references/qa-and-edit.md` | 构建后质检、或要编辑已有 deck、或排查 layout_warnings/图过大/字体缺失 |
| `assets/skeleton-*.json` | 可复制改写的整份 spec 模板(每份 20+ 组件/~26 页)：`default`/`tech`/`data`(咨询数据)/`glass`(深色大屏)/`blueprint`(架构)/`editorial`(人文)/`ink`(中式)/`newsprint`(报刊)。改 `theme` 名即换配色+配套 pack |
| `ppt-cli --help` / `ppt-cli <subcommand> --help` | 子命令完整参数 + 默认值 |
| `scripts/engine/` | （仅维护时翻）vendored 引擎源码——builder/slide_types/themes/decorations 等 |

## 一句话自检（交付前）

- 用户要的是 **.pptx**，我交付的也是 .pptx 吗？（不是悄悄换成了 PDF / Word？
  也不是把 Markdown 简报 / JSON spec / 沙盒路径当成交付物了？）
- 有 cover、有 summary、主体被 section 分段了吗？
- **目录是真实章节标题吗**（逐字对应 `section`，不是"第一章…/XXXX/待补充/填充符"）？
- **最后一页是真实的 `summary`/`closing` 吗**，不是一张空白页？翻 `thumbnails` 确认每页都有内容。
- 跨 deck 混了 ≥3 种版式吗？有没有连续 >2 页纯 bullets？
- 风格选对了吗？默认通用风就 theme/pack 都不传；技术诉求才用 `navy_gold` + `dark_gold`。
- 跑过 `thumbnails` 真的看过、`check-placeholders` 干净吗？跑完了至少一轮"改了再验"吗？
- （`build` 路径）返回 JSON 的 `meta.layout_warnings` 空吗？（`build-js` freeform 路径无此字段，跳过此条）
- 如果中途改过 spec：**有没有改完之后重新 `ppt-cli build` 一次？** 用最新 .pptx pin？
- **`sandbox_get_artifact` 拿了 file_id 吗？`pin_to_workspace` 调了吗？**
  —— 这两步任意一个漏了，等于没交付。
