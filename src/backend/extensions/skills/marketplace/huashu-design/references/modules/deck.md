# 模块 · 幻灯片 / PPT / 演示

幻灯片、PPT、路演、课件、汇报材料都走这里。先过 SKILL.md 的窄桥（三方向门等），再按这份手册做。

## 交付链（开工不问格式）

HTML deck（基础产物，永远先做）→ 完成后**自动**出 PDF → 用户要才出可编辑 PPTX。

- PDF：多文件架构用 `scripts/export_deck_pdf.mjs`，单文件 deck-stage 用 `scripts/export_deck_stage_pdf.mjs`。文字保留矢量可搜，不问直接给。
- PPTX 有两条路，按 HTML 的状态选，不要混用：
  - HTML 还没写 → 按 4 条硬约束写，再走 `scripts/export_deck_pptx.mjs`（内部调 `html2pptx.js`）→ `references/editable-pptx.md`
  - HTML 已经写好且是视觉驱动的、或甲方要求继承他们的模板 → `scripts/pptx_from_rendered.py`，读渲染后坐标，零改造 → `references/pptx-from-rendered-html.md`。导出后会自动拿页面 innerText 比对，报告有没有丢字
  - 绝不为迁就 html2pptx 的约束去降级已经写好的设计——那正是第二条路存在的意义

## 架构（先定，错了会反复踩 CSS 作用域的坑）

- **默认：多文件＋概览墙**（几乎所有 deck）。每页一个独立 HTML（1920×1080 的 `<section>`），用 `assets/deck_index.html` 拼起来：直接复制为 `index.html`、编辑 MANIFEST 即用，**别重写概览逻辑**（倾斜、点击命中、裁切三个坑已内建解决）。自带两种自适应概览（网格 iframe / 无限画廊，画廊需先跑 `scripts/gen_deck_thumbs.mjs` 生成缩略图）、键盘翻页、缩放、统一页码、打印合并。
- **单文件**：只在 ≤5 页极简 pitch、明确不需要概览墙、或需要跨页共享 JS 状态时用 `assets/deck_stage.js`。script 必须放在 `</deck-stage>` 之后，section 的 `display: flex` 要写在 `.active` 上。
- 选单文件前先确认「真的 ≤5 页且不需要概览墙」——13 页 deck 选了单文件，丢掉概览墙，是实测踩过的坑。

细节、目录结构、页面骨架、tokens 放什么：`references/slide-decks.md`「先定架构」一节。

## 做的顺序

1. 三方向：每个方向出 **2 页代表页**（视觉结构差异最大的两种，如封面＋数据页），兼作 showcase。三版 deck 外壳统一，只换视觉。
2. 用户选定后，≥5 页的 deck 先按选定方向把 2 页 showcase 定成 grammar，再批量其余页。跳过这步 = 方向错时返工 N 页而不是 2 页。
3. 每页先答 form 五问（见 `references/workflow.md`），尤其「叙事角色」：封面、过渡、数据、引语、结尾，一页一个角色，版式跟着变。
4. 全部完成后合成 `index.html`，出 PDF。

## 硬规则

- **单页内容绝不自带页码或进度标记**，页码只由外壳承载（实测出过「02/03」和「6/16」两套页码打架）
- 正文 ≥24px，标签注释 ≥16px，标题 60–120px（完整字号表见 `references/content-guidelines.md`「Scale 规范」）
- 固定尺寸内容自己实现缩放＋letterbox（外壳已做）
- 播放位置存 localStorage，刷新不丢
- 体检时用 deck 的字号下限：`python3 "$HD/scripts/design_lint.py" check slides/*.html --viewport 1920x1080 --min-body 24 --min-label 16`。单文件 deck-stage 只能体检当前显示的那一页，这也是默认用多文件的原因之一

## 数据页别默认成「三个大数字＋条形图」

那是 deck 数据页的模板答案，换什么风格都长一样。先问这组数字要说的是什么关系：比例（按比例缩放的实物）、变化（一条叙事曲线）、构成（拆开的整体）、对比（左右对照）、规模（一个超大对象）。数据本身就可以是这一页的主角素材（`references/materials.md`）。

## 相关文件

| 要做什么 | 读 |
|---|---|
| 架构、目录、单页骨架、常见坑 | `references/slide-decks.md` |
| 可编辑 PPTX · 路线 A | `references/editable-pptx.md` + `scripts/html2pptx.js` |
| 可编辑 PPTX · 路线 B（已写好的视觉稿 / 甲方模板） | `references/pptx-from-rendered-html.md` + `scripts/pptx_from_rendered.py` |
| 演讲者备注、Tweaks 调参 | `references/slide-decks.md`、`references/tweaks-system.md` |
| 风格库与样张（秒数轮盘的 PPT 分区，外加跨媒介的中文与东亚分区） | `references/design-styles.md` |
