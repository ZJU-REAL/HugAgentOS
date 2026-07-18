# word-cli edit 的 15 个 op 详解

本文件给 `word-cli edit --ops` 数组里支持的 15 个 op 各一张「卡片」。每张卡片包含：用途 / 必填+可选 kwargs / 关键边界 / 正反例。

**通用约定**：
- 每个 op 是一个 dict `{"op": "<name>", **kwargs}`，按顺序放进 `--ops` 数组。
- ops 之间共享一份打开的 Document 实例——上一个 op 改完，下一个 op 看到的就是改后状态。
- 不要硬编码 paragraph_index（首选字符串 anchor）；连续 delete_paragraph 是个例外，整数 anchor 会自动降序排序，安全。
- 所有 op 的 `anchor` 字符串都是**子串前缀匹配**（大小写敏感，注意全角半角）。

## 速查表

| op | 一句话用途 | 典型 anchor 类型 |
|---|---|---|
| `replace` | 单条文本 find→replace | 不需要 anchor |
| `replace_many` | 多条 find→replace 一次跑完 | 不需要 anchor |
| `fill_placeholders` | `{{xxx}}` 占位符按 mapping 一次填光 | 不需要 anchor |
| `insert` | 在指定位置插入文字 / markdown 块 | 字符串 |
| `insert_image` | 在指定位置插入图片 | 字符串 |
| `format` | 改字体 / 字号 / 行距 / 缩进等 | 字符串 / style filter / paragraph_indexes |
| `replace_paragraph` | 把整段换成新文本 | 字符串 / int |
| `replace_section` | 整章重写（heading + body） | heading 文本 |
| `delete_paragraph` | 删一段 | 字符串 / int |
| `delete_range` | 删一段到另一段（原子） | 字符串 / int |
| `set_cell_text` | 表格单元格改文字 | table_index/row/col |
| `fill_table` | 给整张表填数据 | table_index |
| `add_table` | 插入新表格 | 字符串 + position |
| `move_table` | 把已有表移位 | table_index + position |
| `update_field` | 改文档元数据（标题/作者等 6 个字段） | 不需要 anchor |

---

## 文本类（3 个）

### `replace`

**用途**：单条 find→replace。

```json
{"op": "replace",
 "find": "示例科技",
 "replace": "示例科技有限公司",
 "scope": "all"     // "all" | "first" | <int N>，默认 "all"
 "regex": false,    // 默认 false
 "lenient": false}  // 默认 false；放松空白匹配（仍不归一引号）
```

**边界**：
- `scope` 取 `"all"` / `"first"` / 整数 N（≥1，**第 N 次出现**——不是第 N 行）。
- `regex=true` 时 `find` 是正则，`replace` 支持 `\1` `\2` 反向引用。
- `lenient=true` 在跨 run / 含空白时有用；副作用：匹中的 run 会被合并为第一个 run 的格式。

**正反例**：
```json
✅ {"op": "replace", "find": "甲方：", "replace": "甲方：示例科技"}
❌ {"op": "replace", "find": "公司名称", "replace": "示例科技", "scope": 1}
   // scope=1 是"第 1 次出现"，不是"第 1 段"
```

### `replace_many`

**用途**：N 条 find→replace 一次跑完。比写 N 个 `replace` op 略快（共享一次正则编译），语义等价。

```json
{"op": "replace_many",
 "replacements": [
   {"find": "甲方：", "replace": "甲方：示例科技"},
   {"find": "乙方：", "replace": "乙方：示例数字"},
   {"find": "日期：", "replace": "日期：2026-05-20"}
 ]}
```

每个 item 接收同 `replace` op 的所有 kwarg（`scope` / `regex` / `lenient`）。

### `fill_placeholders`

**用途**：填 `{{xxx}}` 占位符（或自定义模式）。

```json
{"op": "fill_placeholders",
 "mapping": {"name": "张三", "date": "2026-05-20", "amount": "¥12,500"},
 "pattern": "\\{\\{(\\w+)\\}\\}"}   // 可选，默认就是这个
```

**关键边界**：
- mapping 的 key 是正则**第一个捕获组**的内容，不是整个匹配。所以 `{{name}}` 在文档里时，mapping key 写 `name`，不是 `{{name}}`。
- 用 `<<name>>` 这种自定义模式时，pattern 写 `"<<(\\w+)>>"`，mapping key 还是 `name`。
- 返回的 meta 里有 `unfilled_keys` —— 文档里出现但 mapping 没给值的 key，便于二次填补。

---

## 插入类（3 个）

### `insert`

**用途**：插入一段或多段内容（纯文本或 markdown 块）。

```json
{"op": "insert",
 "text": "请见附表 1。",
 "position": "after",
 "anchor": "三、申报材料",
 "style": "Normal",        // 可选；段落样式名（注意带空格："Heading 1"）
 "style_for_all": false,   // 多行时是否每段都用此 style
 "format": "auto"}         // "auto"（默认）| "markdown" | "text"
```

**`position` 5 种取值**：见 `anchors-and-positions.md`。最常踩的：
- `"end"` / `"start"`：文档级位置，忽略 anchor
- `"before"` / `"after"`：紧邻 anchor 段的前/后
- `"after_section"`：anchor heading 所辖整节末尾——**新增章节时首选**

**`format` 取值**（`insert` / `replace_paragraph` / `replace_section` 三个写内容的 op 通用）：
- `"auto"`（**默认**）：内容里出现 markdown 记号（`###`、`-`/`1.`、`**bold**`、`| … |`）就按 markdown 渲染，否则当纯文本。**绝大多数情况无需显式传 format——直接写 markdown 草稿即可正常渲染成 Word 标题/列表/加粗。**
- `"markdown"`：强制按 markdown 解析，渲染 `###` 标题、`-`/`1.` 列表、行内 `**bold**`/`*italic*`/` `code` `。**不**解析表格——`| a | b |` 表格走 `add_table` op（markdown 里含表格会报错）。markdown 模式下 `style` 被忽略（块级样式由 markdown 结构决定）。
- `"text"`：强制纯文本，markdown 记号原样落字（`### 标题` 会在 docx 里显示成 `### 标题`）。仅在确实要保留字面 markdown 符号时才用。

**正反例**：
```json
✅ {"op": "insert", "text": "## 申报流程\n\n1. 准备材料\n2. 提交申请", "format": "markdown", "position": "after_section", "anchor": "三、申报条件"}
❌ {"op": "insert", "text": "第一段\n第二段\n第三段"}
   // 默认 format=text 时 \n 不是段落分隔；要么 format=markdown，要么拆 3 个 insert
```

### `insert_image`

**用途**：在指定位置插入图片。

**推荐写法（`image_path`，直收沙盒路径）**——先把图片 `sandbox_put_artifact` 进沙盒，再直接引用那个绝对路径，不需要 `--image` 标志、不需要别名：

```json
{"op": "insert_image",
 "image_path": "/workspace/chart1.png",   // 沙盒里的绝对路径（已 sandbox_put 进去）
 "position": "after",
 "anchor": "表2",
 "width_cm": 14,                           // width_cm 与 width_inches 二选一
 "alignment": "center"}                    // "left" | "center" | "right"
```

> 图表从哪来？`generate_chart_tool` 返回的是一个 **artifact `file_id`**（图在附件区、**不在沙盒**）。要插进文档先把它送进沙盒：
> `sandbox_put_artifact(artifact_id="<图表 file_id>", dest_path="/workspace/chart1.png")`，再用上面的 `image_path` 引用。**不要**把 `file_id` 直接当 `image_path` 传——CLI 在沙盒里跑，解析不了 artifact id，路径必须是沙盒里真实存在的文件。

**旧写法（`image_file_id` + `--image` 别名映射，仍支持）**：

```json
{"op": "insert_image", "image_file_id": "chart1", "position": "after", "anchor": "表2", "width_cm": 14}
```
启动时额外传 `--image chart1=/workspace/chart1.png`。`image_file_id` 是 `--image` 映射里的**本地别名**，不是 artifact file_id——容易搞混，优先用上面的 `image_path`。

**关键边界**：
- 二选一传图源：`image_path`（沙盒绝对路径，推荐）或 `image_file_id`（配 `--image` 映射）。都不传会报错。
- 不指定 width 时按图片原始像素插入，**经常超出页边距**——强烈建议给 `width_inches` 或 `width_cm`。
- **不支持插入到表格单元格**，只支持正文段落级插入。
- 图注：插完图再加一个 `insert` op（`position:"after"` 锚到图所在段，写 `图1 …`），或直接在 `image_path` 之后的 `insert` 里给 caption 文本。

### `add_table`

**用途**：插入新表格（带 caption / 自动样式）。

```json
{"op": "add_table",
 "rows": [["项目","金额"], ["A","100"], ["B","200"]],   // 二选一
 "markdown": "| 项目 | 金额 |\n|---|---|\n| A | 100 |", // 二选一
 "has_header": true,
 "caption": "表 3-1 申报金额明细",
 "position": "after_section",        // 必填，无默认
 "anchor": "三、申报金额",
 "auto_merge_empty": true}           // 自动 vMerge 连续空 cell（"类目一次写，下面留空"模式）
```

**`position`** 必填——位置语义详见 `anchors-and-positions.md`。新增表格最常用：
- `"after_section"` + 节标题 anchor —— 把表放在该节末尾
- `"after_heading"` + 节标题 anchor —— 表紧贴 heading 之下（首段是表）
- `"after"` + 刚插入的 caption 文本 anchor —— 表紧跟 caption

**`auto_merge_empty=true`**（默认）：连续空 cell 自动 `<w:vMerge>` 与上方合并，让"类目只写一次、下面留空"的写法在 PDF 里渲染成视觉合并单元格。不想合并就传 false。

---

## 重写类（2 个）

### `replace_paragraph`

**用途**：把整段换成新文本。保留段落样式（除非 `style` 重指定）。

```json
{"op": "replace_paragraph",
 "anchor": "原段首字符前缀",  // 字符串子串或 int paragraph_index
 "new_text": "新内容\n第二段也可以", // \n 自动分段
 "style": "Normal",           // 可选
 "format": "auto"}            // 可选；"auto"（默认）| "markdown" | "text"，同 insert
```

`new_text` 里的 `\n` 会被自动拆成多段（不像 `insert` op 那样不分段）。`format` 语义同 `insert`：默认 `auto`，`new_text` 是 markdown 草稿时会渲染成真正的 Word 标题/列表/加粗（markdown 模式下 `style` 被忽略）。

### `replace_section`

**用途**：**整章重写**——heading + body 一并替换，到下一个同级或更高级 heading 为止。这是改一整章的**首选**，远比 N 个 `replace_paragraph` + `delete_paragraph` 安全。

```json
{"op": "replace_section",
 "heading_anchor": "二、申报条件",
 "new_content": "### 基本要求\n\n申报单位需满足：\n\n- 注册地在本省内\n- 上年度营收 ≥ 5000 万",
 "preserve_heading": true,    // true: 保留原 heading 不动；false: 用 new_content 替换整节(含 heading)
 "style": "Normal",           // 可选；纯文本模式下新增段落的样式
 "format": "auto"}            // 可选；"auto"（默认）| "markdown" | "text"
```

**边界**：
- **`format` 默认 `auto`**：`new_content` 是 markdown 草稿（整章重写最常见的写法）就自动渲染成 Word 标题/列表/加粗，不再原样落字。markdown 模式下 `style` 被忽略（块级样式由 markdown 结构决定）。
- `preserve_heading=true`（默认）时，markdown 的 `new_content` 只写**正文**，不要重复本节标题，否则会渲染出两个标题。
- markdown 里的 `| … |` 表格走 `add_table` op，不要塞进 `new_content`（会整条 op 报错；报错时本节内容**保持不动**，原子安全）。
- 纯文本模式：`new_content` 按 `\n` 分段；**空行只当段落分隔符，不会留下空段**（markdown 习惯的 `第一段\n\n第二段` 不会在两段之间多出一行空白）。新段落自动**继承被替换段落的首行缩进/行距**，保持与全文版式一致（不会丢"空2格"）。
- 章节边界：到下一个 ≥ 当前 heading level 的 heading（`Title` 或 `Heading N`）止。
- 单次原子操作——无 index 漂移。

**正反例**：
```json
✅ {"op": "replace_section", "heading_anchor": "二、申报条件",
    "new_content": "申报单位需满足以下条件：\n\n1. 注册地在本省内\n2. 上年度营收 ≥ 5000 万\n3. ..."}

❌  # 反例：写一堆 delete_paragraph + insert
[
  {"op": "delete_paragraph", "anchor": "申报单位需"},
  {"op": "delete_paragraph", "anchor": "1. 注册地"},
  {"op": "delete_paragraph", "anchor": "2. 上年度"},
  {"op": "insert", "anchor": "二、申报条件", "position": "after", "text": "..."}
]  # 索引漂移、丢段落样式、还容易漏
```

---

## 删除类（2 个）

### `delete_paragraph`

**用途**：删一段。

```json
{"op": "delete_paragraph", "anchor": "草稿水印"}
{"op": "delete_paragraph", "anchor": 12}              // int paragraph_index 也行
```

**关键边界**：**连续的整数 anchor delete_paragraph 会被 apply_edits 自动降序排序**（见 `editor.py::_normalize_int_anchor_deletes`），避免典型的 "删 10 → 原 11 变 10 → 删 11 实际删了原 12" 漂移。这是 delete_paragraph 独有的护理，其他 op 用 int anchor 没这个保护。

### `delete_range`

**用途**：从 start_anchor 删到 end_anchor，**单次原子操作**。

```json
{"op": "delete_range",
 "start_anchor": "附录 A",
 "end_anchor": "附录 B",
 "include_end": false}        // 默认 false：删到 end 之前；true：连 end 也删
```

**强烈优于**多个 `delete_paragraph`：原子完成，不会因中间有表格行/空段而漏删；不需要算 index 漂移。

---

## 表格类（3 个）

### `set_cell_text`

**用途**：改一个表格的一个单元格。

```json
{"op": "set_cell_text",
 "table_index": 0,           // 0-based；从 word-cli read --mode outline 的 tables[] 里查
 "row": 1,
 "col": 2,
 "text": "新内容\n第二行也行",
 "preserve_format": true}    // 默认 true，保留原 cell 格式
```

`text` 里的 `\n` 在 cell 内自动分段（不是软换行）。

### `fill_table`

**用途**：批量给整张表填数据。

```json
{"op": "fill_table",
 "table_index": 0,
 "rows": [["A1","B1","C1"], ["A2","B2","C2"]],
 "mode": "append",           // "append" | "overwrite"
 "has_header": true}         // append 时是否把表头算作已有第一行
```

### `add_table`

见上面「插入类」。

### `move_table`

**用途**：把文档里**已有**的表格移到另一个位置。比"复制 cells → 删旧表 → 加新表"安全得多——所有原始样式、合并单元格全部保留。

```json
{"op": "move_table",
 "table_index": 2,
 "position": "after_heading",
 "anchor": "三、申报金额"}
```

`table_index` 从 `word-cli read --mode outline` 或 `--mode analyze` 里查得到。

---

## 格式类（1 个，但参数最多）

### `format`

**用途**：改段落级 + run 级格式。

**选择器（必须且仅能选一个）**：
- `"paragraph_index": int` —— 单段
- `"paragraph_indexes": [int, ...]` —— 多段
- `"anchor": "<text>"` —— 段落文字子串匹配（多段命中）
- `"style_filter": "<value>"` —— 按段落样式名过滤：
  - `"Heading"` 匹 H1-H6
  - `"Heading 1"` 仅 H1
  - `"Normal"` 仅 Normal
  - `["Normal", "Body Text", "FirstParagraph"]` 多个 body 风格
  - `"!Heading"` —— **所有非标题段**（首选用于"全文正文统一格式"，覆盖 Normal/Body Text/FirstParagraph 等真实文档常见的多种正文 style）

**Run-level 字段（字体 / 字符）**：
- `bold` / `italic` / `underline`：true/false 或省略
- `font_size`: 整数磅（12=小四, 16=三号）
- `font_name`: 字体名（`"方正仿宋简体"` / `"黑体"`）。**会同时写入 ascii + eastAsia + hAnsi 三个字体槽**——这是 python-docx 裸写 `run.font.name=X` 做不到的（只设 ascii，中文字符不变）。
- `color_hex`: 6 字符 hex（无 `#`），如 `"C00000"`

**段落级字段（layout）**：
- `line_spacing`: 行距倍数（1.5 = 1.5 倍行距）
- `first_line_indent_chars`: 首行缩进 N 个字宽（公文标准段首空 2 格 → 传 2）。会按当前段落的字号换算成磅，所以 `font_size + first_line_indent_chars` 同改也对齐。
- `first_line_indent_pt`: 显式磅值缩进（和 `_chars` 二选一）
- `space_before_pt` / `space_after_pt`: 段前 / 段后磅

**公文版式典型 ops**：
```json
[
  {"op": "format", "style_filter": "!Heading",
   "font_name": "方正仿宋简体", "font_size": 12,
   "line_spacing": 1.5, "first_line_indent_chars": 2},
  {"op": "format", "style_filter": "Heading 1",
   "font_name": "黑体", "font_size": 16},
  {"op": "format", "style_filter": "Heading 2", "bold": true,
   "font_name": "黑体", "font_size": 14},
  {"op": "format", "style_filter": "Heading 3", "bold": true,
   "font_name": "黑体", "font_size": 12}
]
```

**典型误用**：
- ❌ `style_filter="Normal"` 只匹配 Normal 段——文档可能用了 Body Text / FirstParagraph 等其他 body style，会漏。改用 `"!Heading"`。
- ❌ 期望 `format` 改文字内容——它只改样式。要改文字+格式，拆成 `replace`/`replace_paragraph` + `format` 两个 op。

---

## 元数据类（1 个）

### `update_field`

**用途**：改文档的核心元数据（File → Info 里能看到的那些）。

```json
{"op": "update_field",
 "field": "TITLE",         // TITLE | AUTHOR | SUBJECT | KEYWORDS | DESCRIPTION | CATEGORY
 "value": "2026 年度工作总结"}
```

**唯一支持**这 6 个 core docprops。其他字段（Company / Manager 等）目前不支持。
