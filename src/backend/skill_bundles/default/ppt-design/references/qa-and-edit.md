# QA 闭环 与 编辑已有 deck

本文档假设你已读过 SKILL.md 的"怎么调 CLI"那一段。所有命令都是 `ppt-cli <subcommand> ...` 的形式。

## 构建调用

```
ppt-cli build \
  --spec /path/to/spec.json    \   # 必填：JSON 文件路径或内联 JSON 字符串
  --output /path/to/deck.pptx  \   # 必填：必须 .pptx 结尾
  --engine pptxgenjs           \   # 默认；视觉保真最好。备选 python-pptx
  --style soft                 \   # sharp | soft | rounded | pill
  --theme navy_gold --pack dark_gold   # 技术风格才传这两个；默认通用风都省略
```

stdout 返回：
```json
{"ok": true, "output": "...", "meta": {
  "engine": "...", "theme": "...", "style": "...",
  "slide_count": N, "size_bytes": ...,
  "layout_warnings": [...], "font_warnings": [...]
}}
```

## 质检闭环（构建后必做，不能盲发）

### Step 0 —— 先看 `meta.layout_warnings`
非空 = deck 太多纯 bullets / 版式太单一。**这是硬信号**：按提示把纯 `bullets`
页换成 `highlights`/`icon_rows`/`grid` 等富版式，重新 build。
不要带着 warnings 交付。

### Step 1 —— 视觉质检：假设它有 bug，你的任务是找出来
```bash
ppt-cli thumbnails /path/to/deck.pptx \
  --output-dir /path/to/thumbs/  [--dpi 120] [--quality 85]
```
生成 `slide-01.jpg .. slide-NN.jpg`。**逐页打开图片真的看**——只靠 spec
想象排版一定会漏。

心态很关键：**默认它有问题，第一版几乎从不是对的。** 把质检当成 bug 猎杀，
而不是确认走过场。如果你一眼"没发现问题"，那是没认真看。逐页对照这张清单：

- 元素重叠（文字压在形状上、线穿过字、卡片叠卡片）
- 文字溢出 / 被边界截断 / 顶到页边
- 标题换行成两行了，但为单行标题摆的装饰元素没跟着动
- 页脚 / 数据来源和上方内容碰在一起
- 元素挨太近（间距 < 0.3″）或卡片几乎贴边
- 间距不均（一处大片留白、一处挤成一团）
- 离页边太近（< 0.5″ 边距）
- 该对齐的列 / 卡片没对齐
- 低对比文字（浅灰字压在米色底上之类）
- 低对比图标（深图标压在深底上、且没有反差圆圈托底）
- 文本框太窄导致频繁换行
- 残留占位符内容
- 标题底下有没有装饰横线（有就是错——见 `design-principles.md` 避免清单）

逐页把问题记下来——哪怕很小也写下来。一条都挑不出，就更挑剔地再看一遍。

### Step 2 —— 扫占位符
```bash
ppt-cli check-placeholders /path/to/deck.pptx
```
扫 `xxxx`/`lorem`/`占位`/`待补充`/`请填写`/`TODO` 等。`is_clean: false`
说明有没填的坑——回填真实内容，别交付带占位符的 deck。

### Step 3 —— 修正 + 复检循环

1. 把 Step 1 / Step 2 找到的问题全部列出来。
2. 修：
   - **整页版式不对 / 内容要大改** → 改 spec 重跑 `build`；freeform 路径则改
     脚本重跑 `build-js`。
   - **个别小瑕疵**（错字、补一句、加一张图、删一页、加一页）→ 用下方单页
     编辑命令，避免整份重建。
3. **重渲改过的页，再看一遍**——改一处经常带出新问题（一个元素挪了位，
     可能压到相邻元素）。
4. 重复 2–3，直到完整看一轮挑不出新问题为止。

**没跑完至少一轮"改了再验"，不算质检完成，不能交付。**

### Step 4 —— 导出（仅当用户要 PDF）
```bash
ppt-cli to-pdf /path/to/deck.pptx \
  --output /path/to/deck.pdf
```
走 LibreOffice headless 转换。

### 其它只读 / QA 命令

| 想干什么 | 命令 |
|---------|------|
| 页数 + 每页标题（定位用，最常用） | `ppt-cli info FILE.pptx` |
| 仅页数（便宜探针） | `ppt-cli slide-count FILE.pptx` |
| 单页文本 | `ppt-cli extract FILE.pptx --slide N` |
| 全 deck 文本拍平（核对内容） | `ppt-cli extract FILE.pptx` |

---

## 编辑已有 .pptx —— 不要重建

用户给一个已有 deck 要改：先用 `info` 拿到页数和每页标题来定位目标页
（`--slide` 0 基），再用对应命令精确改：

| 要做的事 | 命令 | 关键参数 |
|----------|------|----------|
| 末尾追加一页（按页型） | `add-slide FILE.pptx --output NEW.pptx` | `--type cover\|toc\|section\|content\|summary`, `--title`, `--content`(同 spec 的 content 字段), `--theme`, `--style` |
| 改某页标题 | `set-title FILE.pptx --output NEW.pptx` | `--slide N --title TXT` |
| 某页加一个文本框 | `add-text FILE.pptx --output NEW.pptx` | `--slide N --text TXT [--position L,T[,W,H]] [--font-size 14] [--color HEX] [--bold]` |
| 某页插一张图 | `insert-image FILE.pptx --output NEW.pptx` | `--slide N --image IMG.png [--position L,T[,W,H]]` |
| 删某页 | `delete-slide FILE.pptx --output NEW.pptx` | `--slide N` |
| 转 PDF | `to-pdf FILE.pptx --output OUT.pdf` | — |

每个编辑命令都把改完的结果落到 `--output` 指定的新文件——**不会原地覆盖**。
后续操作要用新输出文件的路径，别一直用最初那个。所有编辑做完，
同样跑一遍 `thumbnails` 复检。

**何时该重建而非编辑**：要改的是版式选择本身（bullets → 想换成 icon_rows）、
要重排页序、要换 theme/style——这些单页命令做不了，改 spec 重 build 更快更稳。
单页命令适合：错字、补/删个别页、补一张图、改标题。

---

## 排错

| 现象 | 原因 / 处理 |
|------|-------------|
| `meta.layout_warnings` 非空 | 纯 bullets 太多/版式单一。按 `layouts.md` 换富版式重 build。 |
| `check-placeholders` 不干净 | spec 里留了 `xxxx`/占位。回填真实内容；起手就别用占位词。 |
| 图报 PayloadTooLarge | 单图 >12MB。压缩/裁剪后再传，或降分辨率。 |
| 引擎抱怨缺图字段 | spec 里 `image` 节点缺 `data_base64` 或文件路径。补上其一。 |
| 某页版式不是预期 | 字段优先级判错（见 `layouts.md` 判定顺序）。显式加 `"layout":"xxx"`，或清掉多余字段。 |
| 退出 2，错误信息 `unknown theme` | 传了未知 `--theme`。改用合法名/别名，或不确定就不传（回退 swiss_klein）。`ppt-cli list-themes` 看全表。 |
| 文字溢出/截断 | value/title 太长。stat 的 value 控制在 6–7 字符内；标题一行能读完。 |
| 字体相关告警 | 引擎会自动替换缺失字体，一般可忽略；如需指定在 spec 顶层加 `"fonts":{"header":"…","body":"…"}`。 |
| spec 报 "must be a non-empty list" | `spec.slides` 必须是非空数组，且每个元素是带 `type` 的对象。 |
| `pptxgenjs build failed` | 容器里 Node 或 pptxgenjs 没装。retry 时加 `--engine python-pptx` 强制降级。 |
| `build-js` 报 `FreeformBuildFailed` | 手写脚本里有 JS 错误。`error.message` 是完整 node 堆栈，按行号定位修。 |
| `build-js` 报 `FreeformNoOutput` | 脚本没写到 `PPT_OUT_PATH`。结尾必须 `await pres.writeFile({ fileName: process.env.PPT_OUT_PATH })`。 |

## 最终交付前

build → `layout_warnings` 空 → 看过 thumbnails → placeholders 干净 →
（要 PDF 才导）→ 把最终 .pptx 路径注册成 artifact 并通过
`pin_to_workspace(file_ids=[...])` 交付。一步都不能省。
