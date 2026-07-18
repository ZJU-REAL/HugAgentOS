# pdf-cli 全子命令参考

> 每一个子命令都有 `--help`：`pdf-cli <subcmd> --help` 直接看 argparse 全量
> 参数。本文件是浓缩版，按子命令一卡片说清楚。

---

## 1. `pdf-cli read`

只读，不产新文件。所有 read 模式都不需要 `--output`。

### `--mode text`

提取全文文本（可选页号）。

```bash
pdf-cli read --mode text --input /workspace/doc.pdf
pdf-cli read --mode text --input /workspace/doc.pdf --pages 1,3,5
```

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--input` | ✅ | — | 源 .pdf 路径 |
| `--pages` | ❌ | — | 逗号分隔 1-based 页号；不传 = 全部 |

返回：`{ok, meta: {page_count, selected_pages, text, per_page: [{page, char_count}, ...]}}`

### `--mode outline`

返回书签 / 目录的扁平列表。

```bash
pdf-cli read --mode outline --input /workspace/doc.pdf
```

返回：`{ok, meta: {bookmark_count, outline: [{title, page (1-based), level}, ...]}}`。
没有书签时 outline 为空数组。

### `--mode metadata`

文档基础信息：页数 / 标题 / 作者 / 生成器 / 是否加密。

```bash
pdf-cli read --mode metadata --input /workspace/doc.pdf
```

返回：`{ok, meta: {page_count, title, author, subject, creator, producer, is_encrypted}}`

### `--mode overview`

"第一眼"模式：metadata + outline 合并到一份返回。等价于以前的
`pdf_open_document` MCP 工具。

```bash
pdf-cli read --mode overview --input /workspace/doc.pdf
```

### `--mode form-fields`

列出 AcroForm 表单字段。**填表前必先跑这一步**，拿字段名 / 类型 / 可选值。

```bash
pdf-cli read --mode form-fields --input /workspace/form.pdf
```

返回：`{ok, meta: {has_fields, field_count, fields: [{name, type, value?, page?, choices?, states?, checked_value?, radio_values?}, ...]}}`

各字段含义：

- `name` / `type`：永远有；type ∈ text / checkbox / dropdown / listbox / radio。
- `value`：字段当前值（若有）。
- `page`：1-based 字段所在页号（若 widget 能定位到页）。
- `choices`（dropdown / listbox）：**结构是 `[{"value": "<内部值>", "label": "<显示值>"}, ...]`，不是扁平字符串数组**。填表时传的是 `value` 字段，不是 `label`。
- `states`（checkbox）：所有 appearance state 名字（如 `["/Yes", "/Off"]`）；
  `checked_value` 是其中代表"勾上"的那一个（剩下的是 /Off）。填表时
  truthy 值（"yes"/"1"/"on"/"true"）会写入 `checked_value`。
- `radio_values`（radio）：组里所有可选值列表（已剥过 `/` 前缀）。

详细字段语义见 `references/form-fields.md`。

---

## 2. `pdf-cli merge`

合并多份 PDF（按顺序拼接），≥ 2 份输入。

```bash
pdf-cli merge --output /workspace/all.pdf \
    --inputs /workspace/a.pdf /workspace/b.pdf /workspace/c.pdf
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `--output` | ✅ | 输出 .pdf 路径（自动补 .pdf 后缀） |
| `--inputs` | ✅ | 空格分隔的源 .pdf 列表（至少 2 份） |

返回：`{ok, meta: {input_count, total_pages, per_input: [...]}}`

---

## 3. `pdf-cli split`

按页范围拆成 N 个 PDF。

```bash
pdf-cli split --input /workspace/doc.pdf --output-dir /workspace/parts \
    --ranges 1-3,4-6,7

# 指定文件名
pdf-cli split --input /workspace/doc.pdf --output-dir /workspace/parts \
    --ranges 1-3,4-6 --names chapter1.pdf chapter2.pdf
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `--input` | ✅ | 源 .pdf |
| `--output-dir` | ✅ | 拆分后多个文件写入的目录（自动创建） |
| `--ranges` | ✅ | 逗号分隔 1-based 页范围，如 `1-3,4-6,7` |
| `--names` | ❌ | 与 ranges 长度匹配的文件名列表；不传 = part_1.pdf … |

返回：`{ok, meta: {input_pages, output_count, outputs: [{filename, page_range, page_count, size_bytes, path}, ...]}}`

> 拆出来的多个文件需要**逐个**走 `sandbox_get_artifact`，然后一次性 pin 列表。

---

## 4. `pdf-cli fill-form`

向 AcroForm 字段写值。

```bash
pdf-cli fill-form --input /workspace/form.pdf --output /workspace/filled.pdf \
    --fields '{"Name":"张三","BirthDate":"1990-01-01","Newsletter":"yes"}'

# 大 payload
pdf-cli fill-form --input form.pdf --output filled.pdf --fields-file /workspace/fields.json
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `--input` / `--output` | ✅ | 源 + 目标 .pdf 路径 |
| `--fields` / `--fields-file` | 二选一 | 字段值 JSON（对象，键=字段名，值=字符串） |

字段值的语义参考 `references/form-fields.md`（checkbox / dropdown / radio
各有规则）。

返回：`{ok, meta: {filled_count, filled_fields, validation_errors?, not_found?}}`

---

## 5. `pdf-cli create`

从零生成印刷级设计感 PDF。

```bash
pdf-cli create --output /workspace/report.pdf --spec-file /workspace/spec.json

# 带图片资源（cover + 正文 image 块都通过 --image 引用本地 id）
pdf-cli create --output /workspace/report.pdf --spec-file /workspace/spec.json \
    --image chart1=/workspace/chart1.png --image cover=/workspace/cover.jpg
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `--output` | ✅ | 输出 .pdf 路径 |
| `--spec` / `--spec-file` | 二选一 | 文档 spec JSON |
| `--image` | ❌ | 可重复，`local_id=/abs/path.png`；在 spec 里用 `local_id` 引用 |

**spec 顶层字段**：

| 字段 | 必填 | 说明 |
|---|---|---|
| `title` | ✅ | 文档标题 |
| `content` | ✅ | content block 数组（见下） |
| `doc_type` | ❌ | 封面 / 排版风格（report / proposal / resume / portfolio / academic / general / minimal / stripe / diagonal / frame / editorial / magazine / darkroom / terminal / poster），默认 `report` |
| `author`, `date`, `subtitle`, `abstract` | ❌ | 封面 / 摘要文本 |
| `accent`, `cover_bg` | ❌ | 颜色覆盖；6 位 `#RRGGBB` 或 3 位 CSS 短形式 `#RGB`（自动展开成 `#RRGGBB`）都接受 |
| `cover_image` | ❌ | 封面图（local_id，需通过 `--image` 提供） |

**content block 类型**（按 type 字段区分）：

| type | 必填字段 | 说明 |
|---|---|---|
| `h1` / `h2` / `h3` | `text` | 标题（三级） |
| `body` | `text` | 段落正文（可含 `<b>` `<i>` 内联标签） |
| `bullet` / `numbered` | `text` | 项目符号 / 序号列表项 |
| `callout` / `caption` | `text` | 提示框 / 图说 |
| `table` | `headers`, `rows` | 可选 `col_widths`, `caption` |
| `image` / `figure` | `path` (= `--image` 里给的 local_id) | 可选 `caption` |
| `code` | `text` | 可选 `language` |
| `math` | `text` (LaTeX) | 可选 `label`, `caption` |
| `chart` | `chart_type`(bar/line/pie), `labels`, `datasets:[{label?,values}]` | 可选 `title`, `caption` |
| `flowchart` | `nodes:[{id,label,shape?}]`, `edges:[{from,to,label?}]` | 可选 `caption` |
| `bibliography` | `items:[{id,text}]` | 可选 `title` |
| `divider` / `pagebreak` | — | 分隔线 / 强制分页 |
| `spacer` | — | 可选 `pt` 高度 |

返回：`{ok, meta: {pages, cover_pattern, cover_mode, doc_type, warnings}}`

---

## 6. `pdf-cli reformat`

把 md / markdown / txt / docx / pdf / json 重排成同等设计感的 PDF（同
`create` 一个引擎，但内容从源文件解析）。

```bash
pdf-cli reformat --input /workspace/notes.md --output /workspace/notes.pdf
pdf-cli reformat --input draft.docx --output final.pdf \
    --doc-type magazine --title "年度报告" --author "工信局" --date "2026-05" --accent "#0a5"
```

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--input` | ✅ | — | 源文件（.md/.markdown/.txt/.docx/.pdf/.json） |
| `--output` | ✅ | — | 输出 .pdf |
| `--doc-type` | ❌ | `report` | 封面 / 风格（同 create） |
| `--title` / `--author` / `--date` | ❌ | — | 可选 metadata |
| `--accent` | ❌ | — | 主色覆盖；6 位 `#RRGGBB` 或 3 位 `#RGB`（自动展开）都接受 |

返回：`{ok, meta: {pages, cover_pattern, cover_mode, reformat_warnings}}`
