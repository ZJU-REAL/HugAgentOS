# Freeform —— 手写 pptxgenjs，摆脱固定版式

`build` 子命令把 JSON spec 喂给固定模板引擎：安全、便宜，但被那约 40 个内置
版式**框住**——每张 `kpi_cards` 页长得一模一样。`build-js` 是另一条路：你直接
写原生 pptxgenjs 的 Node 脚本，**逐英寸**摆每一个文本框/形状/图片/图表，没有
任何版式是做不出来的。

这份文档教你走这条路。读完前先想清楚：**你真的需要它吗？**

---

## 1 · 什么时候走 freeform（先做这个判断）

| 用 `build`（spec 引擎，默认） | 用 `build-js`（freeform） |
|------------------------------|---------------------------|
| 常规工作汇报、政府材料、数据/行业报告 | 用户明确要"有设计感 / 特别 / 不要像模板" |
| 内容是标准结构（KPI、对比、流程、清单） | 版式诉求超出内置版式所能表达的 |
| 要快、要稳、要文件 100% 有效 | 封面/单页需要定制视觉、异形排布、品牌感 |
| 用户没对"长什么样"提任何要求 | 用户给了参考图 / 具体的视觉描述 |

**默认仍然是 `build`。** freeform 更贵（你要手写每一页的代码）、更易出错
（坐标错位、重叠、溢出），且**必须**配第 6 节的视觉 QA 才能交付。只有当
spec 引擎确实表达不了用户想要的视觉时，才切到这里。

一份 deck 也可以**混着来**：主体页用 `build` 出一版，再对封面等少数几页用
freeform 单独做、最后用 `add-slide` / 编辑命令拼起来——但多数情况要么整份
freeform、要么整份 spec，别过度工程。

---

## 2 · build-js 怎么调（契约）

把你写好的脚本落成一个 `.js` 文件，然后：

```bash
ppt-cli build-js --script /workspace/deck.js --output /workspace/deck.pptx [--timeout 90]
```

CLI 会：解析 pptxgenjs（容器里全局装了）→ 用 `node` 跑你的脚本（cwd = 脚本
所在目录，所以脚本里**图片用相对路径**也能解析）→ 校验产物。

**脚本侧的硬契约——必须遵守：**

1. 脚本结尾**必须**把 deck 写到环境变量 `PPT_OUT_PATH`：
   ```js
   await pres.writeFile({ fileName: process.env.PPT_OUT_PATH });
   ```
   不要自己 hard-code 文件名。CLI 通过这个变量把产物落到 `--output`。
2. 出错就 `process.exit(1)`（见骨架里的 `.catch`），别静默吞掉。

成功返回：
```json
{"ok": true, "output": "/workspace/deck.pptx",
 "meta": {"mode": "freeform", "size_bytes": 58210, "node_stdout": "..."}}
```
失败时 `error.message` 里是 node 的完整堆栈——按它定位脚本 bug。

> 产物是标准 `.pptx`，所以 `info` / `thumbnails` / `check-placeholders` /
> `to-pdf` / `set-title` 等所有其它子命令照样能作用在它上面。

---

## 3 · 脚本骨架（从这个改起，别从空白起）

```js
const pptxgen = require("pptxgenjs");

async function main() {
  const pres = new pptxgen();
  pres.layout = "LAYOUT_16x9";          // 画布 10″ × 5.625″（英寸坐标）
  pres.title = "标题";

  // —— 一个贯穿全篇的视觉母题：这里用左上角的强调圆点 ——
  const P = { ink: "0A0A0A", paper: "F5F5F5", accent: "002FA7", muted: "555555" };

  // 封面（深色）
  const cover = pres.addSlide();
  cover.background = { color: P.ink };
  cover.addShape(pres.shapes.OVAL, { x: 0.6, y: 0.55, w: 0.22, h: 0.22, fill: { color: P.accent } });
  cover.addText("演示文稿标题", {
    x: 0.6, y: 1.8, w: 8.8, h: 1.2, fontSize: 40, bold: true, color: "FFFFFF", margin: 0,
  });
  cover.addText("一句话定调的副标题", {
    x: 0.6, y: 3.0, w: 8.8, h: 0.6, fontSize: 18, italic: true, color: "9AA7C7", margin: 0,
  });

  // …更多 slide…

  await pres.writeFile({ fileName: process.env.PPT_OUT_PATH });
  console.log("written:", process.env.PPT_OUT_PATH);
}

main().catch((e) => { console.error(e); process.exit(1); });
```

坐标系：左上角为原点，单位**英寸**。16:9 画布 = `10 × 5.625`。安全边距 ≥ 0.5″。

---

## 4 · pptxgenjs API 速查

### 文本
```js
slide.addText("文字", {
  x: 1, y: 1, w: 8, h: 1, fontSize: 24, fontFace: "Microsoft YaHei",
  color: "363636", bold: true, italic: false, align: "center", valign: "middle",
  charSpacing: 2,            // 字间距（letterSpacing 会被静默忽略，别用）
  margin: 0,                 // 文本框内边距；要和形状/图标对齐时设 0
});

// 富文本 / 多行：每段除最后一段都要 breakLine: true
slide.addText([
  { text: "第一行", options: { breakLine: true } },
  { text: "第二行" },
], { x: 0.5, y: 0.5, w: 8, h: 2 });
```

### 项目符号（要用就用真 bullet）
```js
slide.addText([
  { text: "第一条", options: { bullet: true, breakLine: true } },
  { text: "第二条", options: { bullet: true } },
], { x: 0.5, y: 0.5, w: 8, h: 3 });
// ❌ 绝不要 addText("• 第一条") —— 会出双重符号
```

### 形状
```js
slide.addShape(pres.shapes.RECTANGLE, {
  x: 0.5, y: 0.8, w: 3, h: 2, fill: { color: "FFFFFF" },
  line: { color: "DDDDDD", width: 1 },
});
slide.addShape(pres.shapes.OVAL, { x: 4, y: 1, w: 1, h: 1, fill: { color: "002FA7" } });
slide.addShape(pres.shapes.LINE, { x: 1, y: 3, w: 5, h: 0, line: { color: "999999", width: 2 } });
slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
  x: 1, y: 1, w: 3, h: 2, fill: { color: "FFFFFF" }, rectRadius: 0.1,  // 仅圆角矩形支持 rectRadius
});

// 阴影（克制用——一页一个视觉焦点）
slide.addShape(pres.shapes.RECTANGLE, {
  x: 1, y: 1, w: 3, h: 2, fill: { color: "FFFFFF" },
  shadow: { type: "outer", color: "000000", blur: 6, offset: 2, angle: 135, opacity: 0.12 },
});
// offset 必须 ≥ 0（负值会损坏文件）；要阴影朝上用 angle: 270
```
渐变填充不原生支持——需要渐变就用一张渐变图当背景。

### 图片
```js
slide.addImage({ path: "/workspace/chart.png", x: 1, y: 1, w: 5, h: 3 });
slide.addImage({ data: "image/png;base64,iVBOR...", x: 1, y: 1, w: 5, h: 3 });
// 保持比例：calcW = maxH * (origW / origH)
slide.addImage({ path: "x.png", x: 1, y: 1, w: 4, h: 3, rounding: true /*圆形裁剪*/ });
```
图片用**绝对路径**最稳；相对路径以脚本所在目录为基准。

### 图表（默认样式偏旧，套下面这组让它现代）
```js
slide.addChart(pres.charts.BAR, [
  { name: "销售额", labels: ["Q1","Q2","Q3","Q4"], values: [45,55,62,71] },
], {
  x: 0.5, y: 1, w: 9, h: 3.5, barDir: "col",
  chartColors: ["002FA7", "5B7FD4", "A9BEE8"],     // 用你的盘子
  catAxisLabelColor: "64748B", valAxisLabelColor: "64748B",
  valGridLine: { color: "E2E8F0", size: 0.5 }, catGridLine: { style: "none" },
  showValue: true, dataLabelPosition: "outEnd", dataLabelColor: "1E293B",
  showLegend: false,
});
// 图类型：BAR / LINE / PIE / DOUGHNUT / SCATTER / RADAR
```

### 表格
```js
slide.addTable([
  [{ text: "表头", options: { fill: { color: "002FA7" }, color: "FFFFFF", bold: true } }, "表头2"],
  ["单元格", "单元格"],
], { x: 1, y: 1.5, w: 8, colW: [4, 4], border: { pt: 1, color: "E2E8F0" } });
```

---

## 5 · 配色 —— 复用本 skill 的盘子

freeform 不传 `--theme`（那是 spec 引擎的参数）。你**自己在脚本里定义颜色**。
但不要凭空选色——直接复用 skill 已调好的盘子，hex 抄进脚本即可。每个盘子五色：
`primary`（标题/深色）· `secondary`（次级）· `accent`（强调/唯一锚点色）·
`light`（浅色块/边框）· `bg`（页面底色）。

| 盘子 | primary | secondary | accent | light | bg | 适用 |
|------|---------|-----------|--------|-------|-----|------|
| `swiss_klein`（默认通用风） | 0A0A0A | 1F1F1F | 002FA7 | E8EAF1 | F5F5F5 | 95% 通用：工作/数据/政务/商业/研究 |
| `navy_gold`（技术风格） | 16294D | 5B6B8C | E6A92E | E7ECF4 | F4F6FA | 技术/产品/方案/路演（深蓝金，深色锚点页自配深底） |

> 这两套是 `build` 路径的全部主题（见 `references/themes.md`）。`ppt-cli list-themes` 可当下打印。
> 需要同色系深浅变化（数据系列、层级块），别引入新色——把 accent 调亮/调暗：
> 调亮 = 每个通道 `c + (255-c)*p`；调暗 = `c * (1-p)`，`p` 取 0.15~0.4。

**这两套不贴时（用户给了参考图 / 明确风格诉求）→ 翻 `references/palette-gallery.md`。**
那里有 22 套从开源 deck 提炼的扩展盘子（浅色专业 / 深色科技 / 创意撞色三类），
同样是五色契约，直接抄 hex 进 freeform 脚本。仍然**别凭空选色**：要么这两套，要么从
palette-gallery 挑一套，整篇贯彻到底。
（注：走 `build` 路径时这 22 套已是命名主题，直接写 `theme` 名即可、无需 hex；
这里抄 hex 只因为 freeform 自绘不读命名主题。）

**用色纪律**（来自 `design-principles.md`，freeform 同样适用）：
- **主导性**：一个色占 60-70% 视觉重量，1-2 个辅助色，一个强调色。不要均权。
- **深浅三明治**：封面 + 结尾页深色，中间内容页浅色；或整体深色走高级感。
- 选的色要"为这个主题而生"——把它换到另一个完全不同主题的 deck 上还"能用"，
  就说明选得不够具体。

---

## 6 · 致命踩坑（会损坏文件 / 出视觉 bug）

1. **hex 颜色绝不带 `#`** —— `color: "FF0000"` ✅ ／ `color: "#FF0000"` ❌ 会损坏文件。
2. **绝不把透明度编进 hex** —— 8 位 hex（`"00000020"`）损坏文件。用 `opacity`（0~1）。
3. **`bullet: true`，不要 unicode `•`** —— 会出双重符号。
4. 数组文本项之间要 `breakLine: true`，否则连成一行。
5. **不要在多次调用间复用同一个 options 对象** —— pptxgenjs 会原地改写它
   （把 shadow 值转成 EMU），第二次调用就拿到被污染的值。每次传新对象，
   或用工厂函数 `() => ({...})`。
6. `ROUNDED_RECTANGLE` 不要配矩形 accent 压条 —— 压条盖不住圆角。要 accent
   边条就用 `RECTANGLE`。
7. 每份 deck 用全新的 `new pptxgen()`，不要复用实例。
8. `shadow.offset` 必须 ≥ 0，负值损坏文件（要朝上的阴影用 `angle: 270`）。
9. **绝不在标题下加装饰线** —— 这是 AI 套模板的典型痕迹。用留白或底色区分，
   不要下划线/accent 横线。
10. 文本框默认有内边距：要让文字和形状/图标精确对齐时，给文本设 `margin: 0`。

---

## 7 · 构建后 —— 视觉 QA 是硬步骤

freeform 的代价就是**坐标全靠你算，第一版几乎一定有错位/溢出/重叠**。
build-js 跑完**不能直接交付**，必须走 `references/qa-and-edit.md` 的视觉
质检闭环：

```bash
ppt-cli thumbnails /workspace/deck.pptx --output-dir /workspace/thumbs/
ppt-cli check-placeholders /workspace/deck.pptx
```

然后**真的逐张看缩略图**，按 `qa-and-edit.md` 的检查清单找 bug（重叠、溢出、
低对比、边距不足、对齐不齐…）。发现问题 → 改脚本 → 重跑 `build-js` → 复检。
**至少完整跑一轮"改了再验"才能交付。**

交付仍是那两步：`sandbox_get_artifact` 拿 file_id → `pin_to_workspace`。
