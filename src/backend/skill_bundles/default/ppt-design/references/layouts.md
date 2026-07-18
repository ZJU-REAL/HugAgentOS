# Layout Catalog —— content 页版式精确手册

写 `content` 页前读这份。每个版式给出：**何时用 · 精确 JSON schema · 条数上下限 ·
踩坑点**。`cover` / `toc` / `section` / `summary` / `closing` 是固定页型，不走版式判定，schema 见末尾。

## 目录

- [字段优先级判定顺序（最重要）](#字段优先级判定顺序最重要)
- [指标类](#指标类)：stat_callout · kpi_cards · big_number · bar_chart_kpi · horizontal_bars · kpi_ledger
- [结构类](#结构类)：icon_rows · grid · icon_cards · concentric · hub_spoke · pyramid · matrix
- [流程类](#流程类)：timeline · roadmap_vertical · process · journey · funnel
- [对比类](#对比类)：two_col · comparison_table · highlights
- [人物 / 社会证明 / 合作](#人物--社会证明--合作)：team · testimonial · logo_wall
- [复合 / 富版式](#复合--富版式)：split_feature · compare_panels · card_list · milestones · commands
- [喘息页](#喘息页)：quote · big_number · single
- [进阶版式（auto-detect，保持结构简单）](#进阶版式)：progress · waterfall · venn · pros_cons · pillars · cycle · gantt · swot
- [固定页型](#固定页型)：cover · toc · section · summary · closing
- [插图字段 image](#插图字段-image)
- [进阶组件（按风格家族选用）](#进阶组件从-23-套开源模板提炼按风格家族选用)：donut · trend · gauges · numbers · data_table · statement · specimen · defs · vertical · article · pricing · stack

---

## 字段优先级判定顺序（最重要）

`content` 页没写 `"layout"` 时，引擎按**字段是否存在**自上而下判定，命中即停。
顺序如下（这就是为什么"一页只放一种版式的字段"是硬规则）：

```
显式 layout
→ bento → team → testimonial(s) → logos(logo_wall)
→ feature(split_feature) → panels(compare_panels) → cards(card_list)
→ milestones → tiles(icon_cards) → journey → commands
→ quote → bigNumber/metric(big_number) → kpis(kpi_cards) → funnel → roadmap(roadmap_vertical)
→ hub.nodes/spokes(hub_spoke) → progress → waterfall → venn → pros/cons → pillars
→ cycle → gantt → swot → quadrants(matrix) → flow(process) → pyramid
→ columns+rows(comparison_table) → items[全带icon/glyph](icon_rows)
→ stats(stat_callout) → steps(timeline) → bars(≤4 bar_chart_kpi / >4 horizontal_bars)
→ rings(concentric) → ledger(kpi_ledger) → items(grid)
→ leftBullets/rightBullets(two_col) → highlights → 否则 single
```

复合 / 富版式（`bento` / `team` / `split_feature` / `panels` / `cards` /
`milestones` / `tiles` / `journey` / `commands` …）的触发字段都很专一，且**排在
最前面命中**——所以这些字段一旦出现就压过后面的 `bullets`/`items`/`stats`。
这也是"一页只放一种版式的字段"为什么是硬规则。

推论：
- 同页既有 `stats` 又有 `bullets` → 判定为 `stat_callout`，`bullets` **被丢弃**。
- 想要 `grid` 但 items 里带了 `icon` → 会判成 `icon_rows`。要 `grid` 就别给 icon。
- **要确定性，显式写 `"layout":"xxx"`**，它压过一切自动判定。

---

## 指标类

### stat_callout —— 1–3 个英雄指标
**何时**：一页只想砸 1–3 个最重要的数。多于 3 个改用 `kpi_cards`。
```json
{"type":"content","title":"三大核心指标",
 "stats":[
   {"value":"12 亿","label":"营收","tagline":"同比 +20%"},
   {"value":"850 万","label":"DAU"},
   {"value":"62","label":"NPS"}
 ]}
```
`value` 必填（短，含单位）；`label` 必填；`tagline` 可选（一句注解）。
**坑**：value 不要超过 6–7 字符，否则排版拥挤。

### kpi_cards —— 4–8 个指标卡片墙（经营总览）
**何时**：总览页，一次铺多个指标。
```json
{"type":"content","title":"经营总览",
 "kpis":[
   {"value":"12 亿","label":"营收","desc":"同比 +20%"},
   {"value":"850 万","label":"DAU"},
   {"value":"62","label":"NPS"},
   {"value":"99.2%","label":"可用性"}
 ]}
```
4–8 张为宜；`desc` 可选。多于 8 张拆两页。

### bento —— 现代仪表盘（一个主块 + 若干小块）
**何时**：开篇"核心格局/总览"页，或想把**一句核心结论 + 几个支撑指标**放在一页、
做出"设计过的仪表盘"感。比 `kpi_cards` 更有层次（主次分明），是数据/研究报告
开篇与政企汇报"一页看懂"的首选富版式。
```json
{"type":"content","title":"2026 产业链核心格局",
 "bento":[
   {"kicker":"核心结论","value":"万亿级","title":"硬科技下一个超级赛道",
    "desc":"2030 年全球市场规模预计突破 2400 亿美元，CAGR 38%"},
   {"value":"45%","title":"核心零部件价值占比","desc":"伺服电机/谐波减速器/力觉","icon":"chart"},
   {"value":"320万台","title":"2030 出货量预测","icon":"growth"},
   {"value":"$30K","title":"目标单机成本","icon":"money"},
   {"value":"67%","title":"头部整机毛利率","icon":"target"}
 ]}
```
- **第 1 项 = 主块**（左侧大色块，强调色填充）：可带 `kicker`（顶部小标签）、
  `value`（巨大英雄数）、`title`、`desc`。没有 `value` 时 `title` 自动放大成主标题。
- **第 2–5 项 = 小块**（右侧白卡）：`value`（强调色）+ `title` + `desc` + 可选 `icon`。
- **2–5 项**为宜（主块 + 1~4 小块）。小块 ≥4 时右侧自动两列，否则单列竖排。
- `icon` 填语义概念名（`chart`/`growth`/`money`/`target`/`settings`/`cpu`…），引擎自动渲 Tabler 矢量。

### big_number —— 单一英雄数字（喘息页）
**何时**：用一个超大数字制造冲击，打断密集页节奏。
```json
{"type":"content",
 "bigNumber":{"value":"3.2 亿","label":"年度活跃用户","sublabel":"较去年 +47%"}}
```
不需要 `title`。一页只一个数。

### bar_chart_kpi —— 3–4 个 KPI（视觉高度对比）
**何时**：3–4 个指标且想让观众一眼比出高低。`bars` 长度 ≤4 时命中此版式。
```json
{"type":"content","title":"四大核心指标",
 "bars":[
   {"value":"68%","label":"渗透率","percentage":68},
   {"value":"42%","label":"复购率","percentage":42},
   {"value":"95%","label":"满意度","percentage":95},
   {"value":"30%","label":"增长率","percentage":30}
 ]}
```
`percentage`（0–100）决定柱高，必给；`value` 是显示文案。

### horizontal_bars —— 5–10 个排名 / 份额
**何时**：`bars` 长度 >4 自动切到横向条。排名、市场份额、占比榜。
```json
{"type":"content","title":"细分市场份额",
 "bars":[
   {"label":"产品 A","value":"32%","percentage":32},
   {"label":"产品 B","value":"24%","percentage":24},
   {"label":"产品 C","value":"18%","percentage":18},
   {"label":"产品 D","value":"14%","percentage":14},
   {"label":"产品 E","value":"12%","percentage":12}
 ]}
```
按 `percentage` 降序排列数据，观感最好。5–10 条；超 10 条拆页或聚合。

### kpi_ledger —— 4–6 行财务式 KPI 台账
**何时**：财报风格的纵向指标清单（收入/利润/用户/NPS…）。
```json
{"type":"content","title":"关键经营指标",
 "ledger":[
   {"value":"12.8亿","label":"营业收入","desc":"同比 +28%"},
   {"value":"3.6亿","label":"净利润","desc":"同比 +52%"},
   {"value":"850万","label":"月活用户","desc":"创历史新高"},
   {"value":"62","label":"NPS","desc":"行业平均 38"}
 ]}
```
4–6 行；`desc` 放同比/注解。

---

## 结构类

### icon_rows —— 3–5 个特性 / 能力 / 支柱
**何时**：列特性、能力、优势。`items` 里每项都带 `icon` 或 `glyph` 才判为此版式。
```json
{"type":"content","title":"四大特性",
 "items":[
   {"icon":"chart","title":"智能推荐","desc":"召回率提升 35%"},
   {"icon":"rocket","title":"实时搜索","desc":"响应 < 300ms"},
   {"icon":"shield","title":"内容审核","desc":"准确率 99.2%"},
   {"icon":"users","title":"个性推送","desc":"转化率 +88%"}
 ]}
```
`icon` 填语义概念名（见 design-principles 的图标一节），引擎渲染 Tabler 矢量图标；
也可用 `glyph` 填 unicode 字符（◆●▲★）。3–5 行最佳。
**坑**：要 `grid` 卡片版式就**不要**给 icon/glyph，否则被判成 icon_rows。

### grid —— 4–6 个分类 / 选项卡片
**何时**：并列的场景/分类/选项，无主次。引擎自动 2×2 或 2×3。
```json
{"type":"content","title":"应用场景",
 "items":[
   {"title":"智能制造","desc":"工业质检 / 预测性维护"},
   {"title":"智慧交通","desc":"自动驾驶 / 车路协同"},
   {"title":"智慧医疗","desc":"辅助诊断 / 药物研发"},
   {"title":"智慧金融","desc":"风控 / 量化交易"}
 ]}
```
`items` **不带** icon/glyph。4–6 张；2、3、4、6 张排版最匀。

### icon_cards —— 图标卡片格（带图标圈，可选警示条 / 备注）
**何时**：能力卡 / 模块清单 / 功能矩阵。比 `grid` 多一个图标圈、比 `icon_rows`
更有"卡片"分量；还能在顶部挂一条深色 banner（提示 / 警示）或在底部挂一条 footer 备注。
```json
{"type":"content","title":"四大核心能力",
 "banner":"以下能力均已上线并通过验收",
 "tiles":[
   {"icon":"robot","title":"智能体编排","sub":"Module A","desc":"多智能体协同调度"},
   {"icon":"database","title":"知识引擎","sub":"Module B","desc":"向量 + 图谱双检索"},
   {"icon":"cloud","title":"弹性算力","sub":"Module C","desc":"按需扩缩容"},
   {"icon":"lock","title":"安全合规","sub":"Module D","desc":"全链路加密"}
 ],
 "footer":"数据来源：内部评测 2026"}
```
`tiles` 2–6 张，自动排格（≤2 单排、≤4 两列、5–6 三列）。每张：`icon`（语义概念名）+
`title` + 可选 `sub`（强调色小标，多用作英文模块名）+ 可选 `desc`。`banner`/`footer`
均可选，可传字符串或 `{text}`。

### concentric —— 3 层生态（核心/中间/外延）
**何时**：圈层结构、产业链生态、技术栈分层。固定 3 个 ring。
```json
{"type":"content","title":"产业链生态",
 "rings":[
   {"title":"核心层","items":["基础大模型","算力硬件"]},
   {"title":"中间层","items":["训练框架","推理引擎"]},
   {"title":"外延层","items":["应用集成","行业方案"]}
 ]}
```
`rings[0]` 是最内核心。每 ring 的 `items` 控制在 2–4 个。

### hub_spoke —— 中心 + 环绕要素
**何时**：波特五力、以一个核心辐射的生态/要素图。
```json
{"type":"content","title":"AI 产业生态",
 "hub":{"center":"AI 产业",
        "nodes":[{"title":"算力"},{"title":"算法"},{"title":"数据"},
                 {"title":"场景"},{"title":"政策"}]}}
```
`nodes` 4–6 个，匀称；每个 title 尽量 2–4 字。

### pyramid —— 金字塔 / 层级（顶→底）
**何时**：愿景→战略→执行→基础 这类自上而下的层级。
```json
{"type":"content","title":"能力金字塔",
 "pyramid":[
   {"title":"愿景"},
   {"title":"战略","desc":"3 大主线"},
   {"title":"执行","desc":"季度 OKR"},
   {"title":"基础设施"}
 ]}
```
`pyramid[0]` 是塔尖。3–5 层；`desc` 可选。

### matrix —— 2×2 战略象限 / SWOT
**何时**：优先级矩阵、波士顿矩阵、SWOT。`quadrants` 必须正好 4 个。
```json
{"type":"content","title":"优先级矩阵",
 "axisX":["低成本","高成本"],"axisY":["低收益","高收益"],
 "quadrants":[
   {"title":"快赢","desc":"立即投入"},
   {"title":"战略级","desc":"重点资源倾斜"},
   {"title":"放弃","desc":"不予投入"},
   {"title":"评估","desc":"小步验证"}
 ]}
```
`axisX`/`axisY` 各 2 个（轴两端标签）。象限顺序：左下→右下→左上→右上的语义由轴定义。

---

## 流程类

### timeline —— 3–5 个横向编号步骤
```json
{"type":"content","title":"实施路线图",
 "steps":[
   {"step":"01","title":"调研","desc":"市场与用户研究"},
   {"step":"02","title":"设计","desc":"产品方案评审"},
   {"step":"03","title":"开发","desc":"快速迭代+A/B"},
   {"step":"04","title":"推广","desc":"渠道协同放大"}
 ]}
```
3–5 步；`step` 是编号文案（"01"/"Q1"/"阶段一"皆可）。

### roadmap_vertical —— 纵向里程碑路线图
**何时**：带时间相位的里程碑（季度/年度推进）。
```json
{"type":"content","title":"实施路线图",
 "roadmap":[
   {"phase":"2025 Q2","title":"立项调研","desc":"市场与需求"},
   {"phase":"2025 Q3","title":"方案设计","desc":"架构评审"},
   {"phase":"2025 Q4","title":"试点落地","desc":"3 个区县"},
   {"phase":"2026 Q1","title":"全面推广","desc":"全市覆盖"}
 ]}
```
3–6 个相位。`phase` 是时间/阶段标签。

### process —— 横向箭头流程
**何时**：线性流程，强调"一步接一步"。与 timeline 的区别：process 更强调流转箭头，
无编号；timeline 有编号、偏"时间线"。
```json
{"type":"content","title":"交付流程",
 "flow":[
   {"title":"需求","desc":"对齐目标"},
   {"title":"设计","desc":"方案评审"},
   {"title":"开发","desc":"迭代交付"},
   {"title":"验收","desc":"上线复盘"}
 ]}
```
3–5 环。

### journey —— 图标流程（图标圈 + 箭头）
**何时**：演进路径、生命周期、用户旅程。与 `process`/`timeline` 的区别：journey
每一步是一个**图标圆圈**、用箭头串联，视觉更"流动"，适合强调"一段历程"。
```json
{"type":"content","title":"数据生命周期",
 "journey":[
   {"icon":"search","title":"采集","desc":"多源接入"},
   {"icon":"settings","title":"处理","desc":"清洗与标注"},
   {"icon":"database","title":"存储","desc":"分层入湖"},
   {"icon":"rocket","title":"应用","desc":"对外赋能"}
 ]}
```
3–5 步；每步 `icon`（语义概念名）+ `title` + 可选 `desc`。超过 5 步拆页。

### funnel —— 3–5 级转化漏斗
```json
{"type":"content","title":"转化漏斗",
 "funnel":[
   {"label":"曝光","value":"120 万"},
   {"label":"点击","value":"38 万"},
   {"label":"注册","value":"9.5 万"},
   {"label":"付费","value":"1.2 万"}
 ]}
```
按漏斗自然顺序从大到小排。3–5 级。

---

## 对比类

### two_col —— A vs B 并排对比
```json
{"type":"content","title":"Q3 vs Q4",
 "leftTitle":"Q3","rightTitle":"Q4",
 "leftBullets":["指标 a","指标 b"],
 "rightBullets":["指标 a","指标 b"]}
```
左右条数尽量等长（视觉对称）。`leftTitle`/`rightTitle` 可选但建议给。

### comparison_table —— 多行特性对比表
```json
{"type":"content","title":"方案对比",
 "columns":["维度","方案 A","方案 B"],
 "rows":[
   ["成本","高","低"],
   ["周期","2 周","6 周"],
   ["风险","低","中"]
 ]}
```
`rows` 每行长度必须等于 `columns` 长度。列 ≤4、行 ≤6 时观感最佳。

### highlights —— 叙述 + ≤3 个关键结论高亮
**何时**：有一段叙述要点，外加 1–3 个最该被记住的数字/结论做成 pill。
```json
{"type":"content","title":"重点结论",
 "bullets":["叙述要点 1","叙述要点 2"],
 "highlights":["核心数据 1","核心数据 2","核心数据 3"]}
```
`highlights` **最多 3 个**（超过会被截或挤）。这是"想用 bullets 又不想廉价"的最佳替代。

---

## 喘息页

低密度、高冲击，每 3–4 页插一个，打破"满页模板脸"。

### quote —— 一句金句定调
```json
{"type":"content","quote":"真正的护城河是组织能力，而非单点技术",
 "attribution":"—— 战略评审会议纪要"}
```
不需要 `title`。`quote` 一句话，别超两行。

### big_number
见上"指标类"。也是优秀的喘息页。

### single —— 最后手段的纯 bullets
```json
{"type":"content","title":"…","bullets":["要点 1","要点 2","要点 3"]}
```
**仅当**内容实在没有任何结构时用，且**连续不得超过 2 页**。能用 highlights /
icon_rows / grid 就别用 single。

---

## 进阶版式

以下版式由对应字段自动判定，文档化程度较低，**保持结构简单**（每项只给
`title`/`label`/`value`/`desc` 这类短字段），不确定就回退到上面的主版式。

| 版式 | 触发字段 | 用途 | 最简形状 |
|------|----------|------|----------|
| `progress` | `progress:[…]` | 进度条组 | `[{"label":"模块A","percentage":80}]` |
| `waterfall` | `waterfall:[…]` | 瀑布增减图 | `[{"label":"期初","value":100},{"label":"增长","value":30}]` |
| `venn` | `venn:[…]` | 交集关系 | `[{"title":"A"},{"title":"B"}]` |
| `pros_cons` | `pros:[…]` / `cons:[…]` | 利弊对照 | `"pros":["优点1"],"cons":["缺点1"]` |
| `pillars` | `pillars:[…]` | 支柱列 | `[{"title":"支柱1","desc":"…"}]` |
| `cycle` | `cycle:[…]` | 循环流程 | `[{"title":"环节1"},{"title":"环节2"}]` |
| `gantt` | `gantt:[…]` | 甘特图（`start`/`end` 必须是**数字**，如月份 1–12，非 "Q1") | `[{"task":"任务A","start":1,"end":3}]` |
| `swot` | `swot:{…}` | SWOT | `{"S":["…"],"W":["…"],"O":["…"],"T":["…"]}` |

`swot` 与 `quadrants` 都能做 SWOT；要可控就用上面的 `matrix`（quadrants）。

---

## 人物 / 社会证明 / 合作

讲"谁在做、客户怎么说、和谁合作"的页，别再用纯 bullets——用下面三个专用版式。
字段唯一，自动判定（在 `bento` 之后、`quote` 之前命中）。

### team —— 团队 / 人物卡片
**何时**：核心团队、专家阵容、组织成员。每张卡 = 头像图标 + 姓名 + 职务 + 一句简介。
```json
{"type":"content","kicker":"团队","title":"核心团队",
 "team":[
   {"name":"张三","role":"CEO","desc":"15 年产业经验","icon":"briefcase"},
   {"name":"李四","role":"CTO","desc":"前大厂算法负责人","icon":"cpu"},
   {"name":"王五","role":"COO","desc":"供应链与运营","icon":"settings"},
   {"name":"赵六","role":"CFO","desc":"投融资与财务","icon":"finance"}
 ]}
```
3–8 人；≤4 人单排、5+ 自动换行。`icon` 填语义概念（不填默认 person 图标）。
`desc` 仅在单排（≤4 人）时显示；多排自动省略简介以免拥挤。

### testimonial —— 客户证言 / 背书卡
**何时**：客户口碑、专家评价、媒体引述。1–2 张大引号卡，底部带署名身份。
```json
{"type":"content","kicker":"客户之声","title":"客户证言",
 "testimonials":[
   {"quote":"接入后交付效率提升一倍。","name":"某制造企业","role":"信息化负责人","icon":"factory"},
   {"quote":"政策匹配很到位，省了大量申报时间。","name":"某科技公司","role":"总经理","icon":"award"}
 ]}
```
也可用单数 `"testimonial":{...}`（渲染成单张满宽卡）。最多 2 张。

### logo_wall —— 合作伙伴 / 客户 logo 墙
**何时**：合作伙伴、客户、生态成员的并列展示。每格一个 logo 图或"图标 + 名称"芯片。
```json
{"type":"content","kicker":"生态","title":"合作伙伴",
 "logos":[
   {"name":"光伏龙头","icon":"solar"},{"name":"物流集团","icon":"logistics"},
   {"name":"银行","icon":"bank"},{"name":"高校","icon":"school"},
   {"name":"医院","icon":"hospital"},{"name":"研究院","icon":"microscope"}
 ]}
```
4–12 格，自动排网格。有真实 logo 图时给 `data_base64`（裸 base64）渲染图片，
否则按 `icon`（或从 `name` 自动推断）+ 名称渲染。

---

## 复合 / 富版式

信息更密、视觉更"做过设计"的多区块版式。适合周报/产品/汇报的重点页。字段唯一，自动判定。

### split_feature —— 三区复合（左详情 + 右上深色指标 + 右下要点）
**何时**：一页讲透一个重点功能/项目：左边叙述 + 右上几个硬指标 + 右下几条价值点。
```json
{"type":"content","kicker":"FEATURE · 重磅功能","title":"项目工作空间 MVP",
 "feature":{"title":"功能介绍 · What's new","desc":"一段话介绍…",
            "bullets":["要点一","要点二","要点三"]},
 "metrics":[{"value":"3","label":"数据表"},{"value":"11","label":"API"},
            {"value":"12","label":"组件"},{"value":"23","label":"测试"}],
 "points":[{"icon":"shield","title":"数据安全","desc":"物理隔离"},
           {"icon":"cpu","title":"上下文聚焦","desc":"更精准"}]}
```
`feature`（左卡，可只给 title+bullets）必填触发；`metrics`（右上深色块，2–4 个）、
`points`（右下要点，2–4 条）可选。三块都给时信息量最大。

### compare_panels —— 对照面板（迁移前/后、现状/目标、优势/劣势）
**何时**：两到三栏并排对照，每栏带色头 + 要点。比 `two_col` 更有"面板感"。
```json
{"type":"content","title":"能力体系大迁移",
 "panels":[
   {"label":"迁移前 · 旧方案","tone":"neg","bullets":["问题一","问题二"]},
   {"label":"迁移后 · 新方案","tone":"pos","bullets":["改进一","改进二"]}
 ]}
```
`tone`：`neg/前/旧/劣/风险`→红，`pos/后/新/优/收益`→绿，其它→主强调色；也可 `color` 指定。2–3 栏。

### card_list —— 纵向卡片清单（左色条 + 图标 + 标题 + 说明）
**何时**：资质清单、能力清单、产品/模块列表。比 `icon_rows` 更有卡片分量，每条带左侧彩色条。
```json
{"type":"content","title":"四大能力",
 "cards":[
   {"icon":"file-text","title":"Word → word-editing","desc":"18 工具 / 含模板套样式"},
   {"icon":"presentation","title":"PPT → ppt-design","desc":"多版式与主题库"},
   {"icon":"chart","title":"Excel → excel-editing","desc":"字节级 patch"}
 ]}
```
3–6 条；`desc`/`meta` 可选；左色条自动多色轮换（可用 `color` 固定单条）。

### milestones —— 里程碑横条（大号值/标签 + 标题 + 说明）
**何时**：关键成果、三大里程碑、重点结论。每条 = 左色边 + 大号 value/label + 标题 + 说明。
```json
{"type":"content","title":"本周三大里程碑",
 "milestones":[
   {"value":"MVP","title":"项目工作空间","desc":"三位一体"},
   {"value":"4 → 0","title":"能力全部下线","desc":"链路缩短"},
   {"value":"12+","title":"稳定性机制","desc":"成体系"}
 ]}
```
3–5 条；`value` 可以是数字、比值或短词（MVP / 4→0 / 12+）。

### commands —— 命令 / 接口 / 操作清单（标签 + 等宽码）
**何时**：技术介绍里的命令行、API、配置项或操作步骤清单。每行 = 彩色标签 +
**等宽字体**的命令/代码 + 说明，专为代码/接口排版而设（普通要点别用它）。
```json
{"type":"content","title":"常用命令",
 "commands":[
   {"label":"构建","code":"ppt-cli build --spec spec.json --output deck.pptx","desc":"由 spec 生成 .pptx"},
   {"label":"质检","code":"ppt-cli thumbnails deck.pptx --output-dir thumbs/","desc":"渲缩略图逐页看"},
   {"label":"导出","code":"ppt-cli to-pdf deck.pptx --output deck.pdf","desc":"转 PDF"}
 ]}
```
3–6 行；`label`（短标签，可省）+ `code`（命令/代码，等宽渲染）+ 可选 `desc`。

---

## 固定页型

### cover —— 封面
```json
{"type":"cover","title":"演示文稿标题",
 "subtitle":"副标题（可选）",
 "tagline":"—— 一句话定调（可选，斜体）",
 "body":"作者 / 日期（可选，底部）",
 "kicker":"眉标（可选，标题上方大写）"}
```
默认浅色。要深色发布会感：加 `"cover_style":"dark"`（仅用户明确要求时）。

### toc —— 目录
```json
{"type":"toc","title":"目录","items":["一、行业概览","二、竞争格局","三、趋势研判","四、投资建议"]}
```
`title` 可选（默认"目录"）。**`items` 必须是这份 deck 里真实 `section` 章节的标题，
逐字照抄**——不要写 `"第一章 …"`、`"XXXX"`、`"待补充"`、`"…"` 这类占位/填充符
（那正是"目录全是填充符"的来源）。先把各 `section` 标题定下来，再回填 `toc`。
> 兜底：若 `items` 留空或写成了占位/填充符，引擎会自动用本 deck 的 `section`
> 标题重建目录；但不要依赖兜底——`section` 标题本身也得是真的。

### section —— 章节分隔
```json
{"type":"section","title":"第二部分 · 市场分析","subtitle":"规模 / 格局 / 趋势"}
```
每个主体模块前放一个。`subtitle` 可选。本身也是一种节奏调剂（低密度）。
默认在背景里有一个**淡淡的章节序号水印**。想把序号变成**醒目设计元素**时加
`"section_style":"numbered"`（左侧大号强调色编号 + 右侧标题）；`"number":"03"`
可自定义编号文案（默认用页码），例：
```json
{"type":"section","section_style":"numbered","number":"02","title":"解决方案","subtitle":"我们做了什么"}
```

### summary —— 总结
```json
{"type":"summary","title":"总结",
 "bullets":["结论 1","结论 2","结论 3"],
 "body":"—— 完。"}
```
`bullets` 渲染成打勾卡片。`title`/`body` 可选（默认"总结"/"—— 完。"）。
深色版加 `"summary_style":"dark"`（仅用户明确要求时）。

### closing —— 致谢 / 收尾页（可选）
```json
{"type":"closing","title":"谢谢观看","subtitle":"欢迎批评指正"}
```
想在 `summary` 之后再放一页"谢谢/致谢/Thank you"时**必须用 `type:"closing"`**，
**不要用一张没有正文字段的 `content` 空页来当收尾**——那会渲染成一张白板
（即"最后一页是空的"）。`title` 默认"谢谢观看"，`subtitle`/`body` 可选，
深色版加 `"closing_style":"light"` 可切浅色（默认品牌深色）。还可带联系方式与二维码：
```json
{"type":"closing","title":"谢谢观看","subtitle":"XX 单位",
 "contact":[{"icon":"phone-call","label":"电话","value":"0XXX-XXX-XXX"},
            {"icon":"mail","label":"邮箱","value":"contact@xx.com"}],
 "qr":"<二维码图片裸 base64，可选>"}
```
`contact` 最多 4 项（图标 + 文案，居中排布）；`qr` 给裸 base64 时居中渲染小图。
> 兜底：任何 `content` 页若没有任何可渲染内容（无 bullets/数据/图等），
> 引擎会自动按 closing 收尾页渲染，绝不留白板；但正常情况请显式建页。

---

## 插图字段 image

任意 slide 可带 `"image"`，提供 `artifact_id`（如 generate_chart 产出的图、
用户上传的图）或 `data_base64`（裸 base64，无 `data:` 前缀），再给 slot 定位：

```json
{"type":"content","title":"全球市场份额","bullets":["要点1","要点2"],
 "image":{"artifact_id":"<id>","slot":"right","caption":"数据来源：xxx 2025"}}
```

| slot | 效果 |
|------|------|
| `right` | 图在右半，文字挤到左半 |
| `left`  | 镜像，文字在右 |
| `below_title` | 图占满正文区（无其他内容） |
| `hero` | 大图占据大部分（封面/发布会） |
| `full` | 满屏背景 |
| `{x,y,w,h}`（英寸） | 显式坐标，覆盖 slot |

`caption` 可选（图下小号斜体）。单图原始体积上限 12MB，超了先压缩/裁剪。
画布是 16:9（10″ × 5.625″）。

---

## 进阶组件（从 23 套开源模板提炼，按风格家族选用）

让不同风格的 deck 用各自的组件词汇——别每套都堆 kpi_cards。每页只放一种组件的字段。
风格→该多用哪些组件见 `recipes.md`「组件随风格变化」。

### 数据类

**`donut`** — 环形图 + 右侧图例（占比/构成）。
```json
{"type":"content","title":"结构占比","donut":[{"label":"核心部件","value":42},{"label":"系统集成","value":28}]}
```
2–6 项；`value` 数值（可带单位字符串）。配 consulting/capital/glass。

**`trend`** — 折线/面积趋势（时间序列）。
```json
{"type":"content","title":"增长趋势","trend":[{"label":"2021","value":30},{"label":"2022","value":45}]}
```
3–14 点；单系列。配 capital/newsprint/glass/tech_report。

**`gauges`** — 环形仪表盘（达成率/完成度），1–4 个。
```json
{"type":"content","title":"关键达成率","gauges":[{"value":92,"label":"目标达成"},{"value":78,"label":"满意度","unit":"%"}]}
```
`value` 0–100；可选 `display`/`unit`。配 glass/memphis。

**`numbers`** — 大数字带（最醒目的几个数,无卡片,带分隔线），2–5 个。
```json
{"type":"content","title":"核心指标","numbers":[{"value":"37.5%","label":"渗透率"},{"value":"1,240亿","label":"总产值"}]}
```
比 `kpi_cards` 更大更克制。几乎所有风格都适用。

**`data_table`** — 数据表/排名/台账，支持状态点（`green`/`yellow`/`red` 自动转● 着色）。
```json
{"type":"content","title":"任务进度","table":{"headers":["任务","进度","状态"],"rows":[["强链","85%","green"],["扩量","60%","yellow"]]}}
```
配 consulting/capital/newsprint/terminal。

### 结构 / 文字类

**`statement`** — 大宣言/金句页（比 quote 更大、可无出处）。
```json
{"type":"content","statement":"把不确定的未来,拆成今天能落地的一步。","attribution":"战略研究部"}
```
配 swiss/ink/newsprint/editorial 的喘息页。

**`specimen`** — 字体大样（巨字 + 字重样本），视觉规范页。
```json
{"type":"content","title":"字体体系","specimen":{"glyph":"产","weights":["黑体/标题","宋体/正文"],"note":"主字族"}}
```
配 swiss/编辑风。

**`defs`** — 规格/定义清单（左术语↔右值,发丝线），2–8 行。
```json
{"type":"content","title":"统计口径","defs":[{"term":"统计口径","value":"规上工业企业"},{"term":"覆盖范围","value":"全市10区县"}]}
```
配 swiss/学术/蓝图/编辑。

**`vertical`** — 竖排文字/对联（国风）,1–4 列右起 + 朱砂印章。
```json
{"type":"content","vertical":["本秀于林","风必摧之"],"seal":"印"}
```
配 ink_chinese。

**`article`** — 多栏正文（报刊版面）,2–3 栏。
```json
{"type":"content","title":"要闻综述","article":["第一栏正文…","第二栏…","第三栏…"]}
```
配 newsprint/magazine/riso。

**`pricing`** — 定价档位卡,2–4 档（`featured:true` 高亮主推档）。
```json
{"type":"content","title":"参与档位","pricing":[{"plan":"基础","price":"¥799","period":"/年","features":["核心功能"]},{"plan":"专业","price":"¥1999","featured":true,"features":["全部功能","优先支持"]}]}
```
配 memphis/consulting。

**`stack`** — 架构分层堆叠（每层名 + 该层组件 chip）,2–5 层。
```json
{"type":"content","title":"技术架构","stack":[{"layer":"应用层","items":["门户","API"]},{"layer":"数据层","items":["库","缓存"]}]}
```
配 blueprint/terminal/tech_report。

---

## 进阶组件·第二批（图表 + 仪表盘/编辑控件）

让一份 deck 能像真实模板那样铺满 20+ 种不同样式。新增图表类（原生 pptxgenjs 图表）与控件类。

### 图表类

**`pie`** — 饼图（份额,含图例）。`pie:[{label,value}]`,2–6 项。
**`radar`** — 雷达图（能力画像/多维评估）。`radar:[{label,value}]`,3–8 维。
**`scatter`** — 散点图（投入产出/分布）。`scatter:[{x,y}]`,≤40 点。
**`lines`** — 多系列折线（交叉/对比趋势）。`lines:{labels:[...],series:[{name,values:[...]}]}`,≤4 系列。
**`grouped_bars`** — 分组柱（多期/多区域对比）。`groupedBars:{labels:[...],series:[{name,values:[...]}]}`,≤4 系列。

### 控件类

**`kpi_delta`** — 带升降箭头的指标卡（▲绿/▼红 + 变化值）,≤4。
```json
{"type":"content","title":"同比变化","deltas":[{"value":"15%","label":"复购率","change":"3.2pct","dir":"up"}]}
```
**`sparkcards`** — 指标 + 迷你趋势线卡片,≤4。`sparkcards:[{value,label,spark:[数值…]}]`。
**`ribbons`** — 要素流向（左→右连线,桑基简化）。`ribbons:{left:[...],right:[...]}`,各≤5。
**`status_list`** — 状态清单（行 + 绿/黄/红状态标签）。`statusList:[{label,value,status:"green|yellow|red"}]`,≤6。
**`callout`** — 强调横幅 / 金句 / CTA。`callout:{text,sub?}`（或直接 `callout:"…"`)。
**`checklist`** — 勾选清单（✓,1–2 列）。`checklist:["…"]`,≤8。
**`calendar`** — 周历 / 日程（每日一列）。`calendar:[{day,items:[...]}]`,≤7 天。
**`def_cards`** — 概念/定义卡格（术语+释义,带色边）。`defCards:[{term,desc}]`,≤6。
**`numbered`** — 大序号栏（01/02/03 + 标题 + 描述）。`numbered:[{title,desc}]`,≤4。

> 这两批进阶组件共 26 个。**一份 deck 至少混用 8–10 种不同组件、20+ 页内尽量不重复版式**；
> 各风格家族该优先用哪些见 `recipes.md`「组件随风格变化」。
