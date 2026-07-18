# 视觉风格 —— 主题(配色)× 设计包(版式性格)

**风格 = `theme`(配色) + `pack`(封面/章节/装饰的视觉性格)的成套搭配。**
每个主题都**自带默认 pack**——spec 里只写 `"theme":"<名字>"`,引擎自动套上配套
pack,整份 deck 一致。不用、也不该自己拆开混搭(除非你很清楚要干嘛)。

## 怎么选(决策树)

1. **用户没提任何风格诉求** → 什么都不写(= `swiss_klein` 默认通用风,白底克莱因蓝)。
   这覆盖绝大多数工作汇报 / 数据 / 政务 / 商业 / 内部汇报。
2. **用户要"科技感/深色/高对比/发布会"** → `"theme":"navy_gold"`(深蓝金,深浅三明治)。
3. **用户给了参考图、点名某种调性、或内容有强场景属性**(中式 / 学术 / 财经 / 路演 /
   建筑 / 文创…)→ 从下面的**扩展主题目录**挑最贴的一个,直接写它的名字。

> 这些是 `build`(spec 引擎)路径**直接可用的命名主题**——CLI / `list-themes` 都认。
> 每个主题源自一套真实开源 deck 的设计语言(见 `references/palette-gallery.md` /
> `uploads/ppt-template/`)。`ppt-cli list-themes` 打印当前全部主题 + 中文别名。

## 核心 2 套(起手骨架)

| 风格 | spec | 长什么样 | 起手骨架 |
|------|------|----------|----------|
| **默认 / 通用风** `swiss_klein` | 都省 | 白底 + 克莱因蓝单锚点,克制专业 | `assets/skeleton-default.json` |
| **技术风格** `navy_gold` | `"theme":"navy_gold"`(默认带 `dark_gold` pack) | 深蓝底 + 金强调,封面/章节深、内容浅 | `assets/skeleton-tech.json` |

## 扩展主题目录(写 `theme` 名即可,自动配 pack)

**A · 浅色专业**(pack: `swiss` 网格 / `default` / `editorial` 编辑)

| theme | pack | 适合 | 别名 |
|---|---|---|---|
| `swiss_grid` | swiss | 极简排版网格、正式报告/研究,大字大留白 | 瑞士网格 |
| `academic_blue` | swiss | 学术/论文/科研汇报,蓝调克制 | 学术蓝 |
| `consulting_navy` | default | 咨询/商业(麦肯锡感),KPI+图表 | 咨询蓝 |
| `tech_report_light` | default | 浅色科技/数据报告,理性干净 | 浅色科技 |
| `corp_multi` | swiss | 通用企业/机构,多彩中性 | — |
| `academic_teal` | swiss | 学术/科研海报,青墨+品红 | — |
| `urban_project` | editorial | 城市更新/政企工程,大图+大序号 | 城市更新 |
| `warm_editorial` | editorial | 暖米色人文报告,卡片+时间线 | 暖色编辑 |
| `bronze_premium` | editorial | 高级灰金/建筑/评选,满版图留白 | 高级灰金 |
| `nature_soft` | editorial | 自然草木/生活方式,柔和 | 自然草木 |
| `ink_chinese` | ink | 中式水墨/国风/文化政务,宣纸+朱砂印章 | 中式水墨、国风 |

**B · 深色科技 / 高级**(pack: `dark_gold` / `glass` 玻璃拟态 / `blueprint` 蓝图 / `editorial`)

| theme | pack | 适合 | 别名 |
|---|---|---|---|
| `glass_dashboard` | glass | 玻璃拟态深色仪表盘,霓虹青紫、数据大屏 | 玻璃拟态 |
| `terminal_dark` | dark_gold | 暗夜终端/工程科技,暖金强调 | 暗夜终端 |
| `agent_dark` | dark_gold | 深色 Agent/技术方案,陶橙强调 | 深色科技卡 |
| `blueprint` | blueprint | 系统架构/蓝图,网格线+琥珀线框 | 蓝图 |
| `capital_dark` | dark_gold | 深色财经/资本报告,炭黑+红 | 深色财经 |
| `editorial_gold` | editorial | 深色金 人物/传记/专题,图文叙事 | 深色金编辑 |
| `luxe_interior` | editorial | 深色奢华/室内/高端品牌,香槟金 | 深色奢华 |
| `magazine_black` | editorial | 纯黑杂志/时尚/艺术,大图陈列 | 黑底杂志 |

**C · 创意 / 小众撞色**(默认别用,只在用户明确要"活泼/潮/手作/报刊感"时)

| theme | pack | 适合 | 别名 |
|---|---|---|---|
| `newsprint_brutal` | newsprint | 报纸/野兽派,报头+衬线大标题+分栏 | 报纸、野兽派 |
| `memphis_pop` | memphis | 孟菲斯高饱和,几何撞色+贴纸(活动/快消) | 孟菲斯 |
| `riso_zine` | memphis | Riso/Zine 撞色,文创/青年向 | — |

> 想覆盖自动配的 pack,在 spec 顶层显式写 `"pack":"<名字>"`。pack 取值:`default`、
> `dark_gold`(别名 技术风格/科技风/tech/dark…)、`swiss`、`editorial`、`ink`、`glass`、
> `blueprint`、`newsprint`、`memphis`。深色主题(bg 为深色)内容页**自动**用深色卡片+浅字。

## 配色值(freeform 手写脚本时抄这里;核心 2 套)

走 `build` 路径不用关心 hex,引擎自动套。只有 `build-js`(freeform)需要自己在脚本里定义颜色:

| 风格 | primary | secondary | accent | light | bg |
|------|---------|-----------|--------|-------|-----|
| 默认 `swiss_klein` | 0A0A0A | 1F1F1F | 002FA7 | E8EAF1 | F5F5F5 |
| 技术 `navy_gold` | 16294D | 5B6B8C | E6A92E | E7ECF4 | F4F6FA |

> 需要同色系深浅层次(数据系列、层级块),**别引入新色**——把 accent 调亮/调暗:
> 调亮 = 每通道 `c + (255-c)*p`;调暗 = `c * (1-p)`,`p` 取 0.15~0.4。

> 上面只列核心 2 套的 hex。扩展主题目录的 22 套(浅色/深色/创意)hex 见
> `references/palette-gallery.md`——`build` 路径直接写 `theme` 名即可,**无需** hex;
> 只有 `build-js`(freeform)手写脚本时才需要把 hex 抄进去。

## 深浅默认(别擅自改)

- 默认通用风:cover / content / summary 全程浅底,主色标题。
- 技术风格(`dark_gold`):封面 / 章节自动深色,内容页浅色——这是设计包内建的节奏,
  不用你逐页设 `cover_style`。

只有用户明确要个别页"反色"时,才在该页加 `"cover_style":"dark"` / `"summary_style":"dark"`,
属高级覆盖,不是默认。

## 页眉 / 页码(默认风自带的成套页面装饰,自动渲染,无需在 spec 里写)

默认通用风(`swiss_klein` / 默认 pack)的 content / toc / summary 页自带一套统一的页面
"边框"语言,让整份 deck 像一套精心设计的成品而非裸标题:

- **小标题(页眉)**:标题左侧一道**主色竖条 tab**(accent 色),紧跟深色粗体标题。
  有 `kicker` 时仍作为标题上方的小字 overline,与标题左对齐。
- **页码(页脚)**:底部一道**贯穿全宽的细分隔线(hairline)** + 右下角一个**小号灰色页码**,
  替代旧的灰色圆角药丸。深底主题自动反白。

这套页眉/页脚是引擎内建 chrome,**spec 不需要、也不应**手写 tab / 页码 / 分隔线。竖条 tab 仅默认
pack 出现;其它 pack(swiss / editorial / dark_gold / glass …)各有自己的页眉性格,但页码 hairline
是所有 pack 统一的。

## style(视觉性格,与风格正交)

`style` 控制圆角、阴影、字号比例、留白,四档差异明显,两套风格都能搭:

| style | 圆角 | 阴影 | 密度 | 何时 |
|-------|------|------|------|------|
| `sharp` | 0° | 无 | 密 | 数据密集、正式报告、财报 |
| `soft` | 小 | 轻 | 密 | **默认**,商务/汇报 |
| `rounded` | 中 | 轻 | 松 | 产品介绍、营销 |
| `pill` | 大 | 上浮 | 松 | 发布会、高端品牌 |

默认 `soft`;数据/财报偏 `sharp`;发布会偏 `pill`;常规汇报 `soft` 最稳。
