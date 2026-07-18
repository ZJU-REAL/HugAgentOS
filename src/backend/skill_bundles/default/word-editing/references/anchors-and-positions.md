# anchor / position / scope 系统深潜

模型最容易在这套坐标系上栽跟头：anchor 到底是数字还是字符串、position 5 个值的边界、scope 是行号还是次数、`_chars` vs `_pt` 谁优先。本文一次说清。

## anchor — 段落定位坐标

### 三种 anchor 类型

| 类型 | 怎么写 | 用什么解析 | 适用场景 |
|---|---|---|---|
| **字符串子串** | `"二、申报条件"` | `_resolve_paragraph_index` 找**第一个**段落文本包含该子串的段 | **首选**——抗删除/插入漂移 |
| **整数 paragraph_index** | `12`（0-based） | 直接用作 body 段落数组下标 | 仅在 ops 数组内无 paragraph-count 变化时安全 |
| **heading 锚点**（仅 replace_section / 某些 position） | `"二、申报条件"` | 同字符串子串，但要求命中段必须是 heading style | replace_section 必须 / `position=after_section` 推荐 |

### 字符串 anchor 的匹配规则

- 大小写敏感
- 全角半角不归一（中文 `，` ≠ 英文 `,`）
- 子串匹配——取段落 10-30 字前缀，唯一性自检后再用
- 只命中**第一个**包含该子串的段；不能同时改多段（用 `paragraph_indexes` 或 `style_filter`）
- 跨 run 时不影响匹配（引擎拼成整段 text 后再 search）

### 整数 anchor 的漂移问题

```
ops = [
  {"op": "delete_paragraph", "anchor": 10},     # 删原段 10，原段 11 上移成 10
  {"op": "delete_paragraph", "anchor": 11},     # 想删原段 11，但它已经是新的 10
  {"op": "insert", "anchor": 12, "position": "before", "text": "..."}
]
```

每个 op 看到的是"上一个 op 改过的文档状态"——int anchor 直接错位。

**例外**：`word-cli edit` 对**连续的 delete_paragraph int anchor 自动降序排序**（`editor.py::_normalize_int_anchor_deletes`）：

```
ops = [{"op":"delete_paragraph","anchor":10}, {"op":"delete_paragraph","anchor":11}]
        ↓ 自动重排为
ops = [{"op":"delete_paragraph","anchor":11}, {"op":"delete_paragraph","anchor":10}]
```

从后往前删，前面索引不动。**但只对 delete_paragraph 一种 op 自动护理**。其他 op 用 int anchor 没保护。

### 该用哪种？

- **想绝对安全 → 字符串 anchor**。每次取段落前缀 20 字左右，应付 99% 情况。
- **批量 delete_paragraph 且 outline 已读好 → int anchor 也行**（自动排序护着）。
- **同一段需要被多个 op 引用 → 字符串 anchor**。哪怕这段被前面 op 改过，只要锚定子串还在，就能继续命中。
- **outline 里发现锚点子串不唯一**（多段都包含"建议"两字） → 用更长的前缀（"五、政策建议"），或换 paragraph_index。

---

## position — 插入位置语义

**`insert` / `insert_image` / `add_table` / `move_table` 这 4 个 op 共用**这套 position 语义。但具体取值略有差异——`insert` 多一个 `"after_heading"`，`move_table` 不支持 `"after_section"`。

### 全部取值

| 值 | 含义 | 是否需要 anchor | 哪些 op 支持 |
|---|---|---|---|
| `"start"` | 文档**最前面**（第一段之前） | 否 | insert / add_table / move_table |
| `"end"` | 文档**最末尾**（sectPr 之前） | 否 | insert / add_table / move_table |
| `"before"` | anchor 段之前 | 是 | insert / add_table / move_table |
| `"after"` | anchor 段之后 | 是 | insert / add_table / move_table |
| `"before_paragraph"` | 同 "before"（向后兼容别名） | 是 | insert / add_table / move_table |
| `"after_paragraph"` | 同 "after"（向后兼容别名） | 是 | insert / add_table / move_table |
| `"after_heading"` | 紧贴 anchor heading 的下一行 | 是 | insert / add_table |
| `"after_section"` | anchor heading 所辖**整节末尾** | 是 | insert / add_table |

> 老 MCP 工具 `word_insert_text` 用过 `"after_paragraph"` 这种长名——脚本接受短的 `"before"` / `"after"`，长形式也兼容。两者等价。

### 5 个最常踩的 position 取舍

#### 1. `"after"` vs `"after_section"`

```
[H1] 二、申报条件          ← anchor 命中这里
[P]  申报单位需满足...
[P]  1. 注册地在浙江
[P]  2. 上年度营收 ≥ 5000 万
[P]  附：申报材料清单
[H1] 三、申报材料          ← 下一节开始
```

- `position="after"`，anchor="二、申报条件"：插在 `[H1]` 和 `[P] 申报单位需满足` **之间**——通常**不是**你想要的。
- `position="after_heading"`，anchor="二、申报条件"：同上（紧贴 heading 后），但语义是"作为该节的第一段"——比 `"after"` 更明确。
- `position="after_section"`，anchor="二、申报条件"：插在 `[P] 附：申报材料清单` **之后**，`[H1] 三、申报材料` **之前**——**新增章节首选**。

**判断窍门**：要插一节作为下一节，用 `after_section`；要插内容作为该节的第一段，用 `after_heading`；要插内容跟在某具体段后面，用 `after` + 段落 anchor。

#### 2. `"after_heading"` 自动跳过 caption + 表格

`add_table` 在 `position="after_paragraph"` 时，如果 anchor 段刚好是一个 caption 紧跟着表格，会**自动跳过那个表格**插在表的后面（避免"caption + 新表 + 原表"的诡异叠加）。这意味着：

```json
# 文档结构：[P 表3-4 ...] [Table] ...
{"op": "add_table", "position": "after_paragraph",
 "anchor": "表3-4 ...", "rows": [...]}
# 实际插入位置在原 Table 之后，不是原 Table 之前
```

如果真要在 caption 和表之间塞内容（极少见），用 `paragraph_index` 显式定位 caption。

#### 3. `"start"` / `"end"` 忽略 anchor

```json
{"op": "insert", "position": "end", "anchor": "随便写", "text": "..."}
```

`anchor` 被忽略——但建议**不要乱填**，会让 LLM 阅读 ops 时困惑。直接省略 anchor 字段即可。

#### 4. `move_table` 不支持 `"after_section"`

要把表移到节末尾，目前只能用 `"after_paragraph"` + 该节最后一段的 anchor。或先 `delete_range` 该表所在范围 + 后续用 `add_table` 重新加。

#### 5. `position` 在 `add_table` 是**必填**的——没默认值

`apply_edits` 的 `add_table` op 没有默认 position，**必须显式传**。漏传会报 ValueError。

---

## scope — 替换范围（仅 replace / replace_many）

```json
{"op": "replace", "find": "公司", "replace": "...", "scope": "all"}
```

| 值 | 含义 |
|---|---|
| `"all"` | 全文所有出现位置都换（**默认**） |
| `"first"` | 只换第一次出现 |
| 整数 N ≥ 1 | 只换**第 N 次出现** |

**"第 N 次出现"不是"第 N 行"也不是"第 N 段"**。考虑文档：

```
段 1：本公司将...
段 5：分公司位于...
段 9：母公司股东...
```

`{"op":"replace","find":"公司","scope":2}` 改的是**第 5 段的"分公司"**那个"公司"，不是第 5 段或第 2 段。

---

## style_filter — `format` op 的特殊选择器

不属于 anchor/position/scope 体系，但很容易和它们混淆。

`format` op 的 `style_filter` 按段落样式名匹配——和 anchor 是平级互斥的选择器（四选一）。

| 值 | 匹配 |
|---|---|
| `"Heading"` | H1-H6 全部 |
| `"Heading 1"` | 只 H1 |
| `"Normal"` | 只 Normal |
| `["Normal", "Body Text", "FirstParagraph"]` | 多个 body 风格（OR） |
| `"!Heading"` | **所有非 Heading 段**（首选用于"全文正文统一格式"） |

**`"!Heading"` 比 `"Normal"` 更稳**——文档可能用了 Body Text / FirstParagraph 等其他 body style，`"Normal"` 会漏命中，`"!Heading"` 不会。

---

## 单位换算备忘

| 单位 | 1 个相当于 | 用在哪 |
|---|---|---|
| **EMU** (English Metric Unit) | 914,400 = 1 inch | 图片 extent（OOXML 底层） |
| **DXA** (Twip) | 1440 = 1 inch | 表格宽 / 页面尺寸 / 边距 |
| **Pt**（磅） | 12 = 1 pica = 1/6 inch | font_size, first_line_indent_pt, space_before_pt 等 |
| **字符宽**（CJK） | 跟随当前段字号 | first_line_indent_chars |

公文常用磅值速查：

| 字号 | 中文名 | 磅值 |
|---|---|---|
| 初号 | | 42 |
| 一号 | | 26 |
| 二号 | | 22 |
| 三号 | | 16 |
| 四号 | | 14 |
| 小四 | | 12 |
| 五号 | | 10.5 |

正文标准：仿宋小四（`font_size: 12`） + 1.5 倍行距（`line_spacing: 1.5`） + 首行缩进 2 字（`first_line_indent_chars: 2`）。

`first_line_indent_chars` 和 `first_line_indent_pt` 二选一，**两个都传时 _pt 胜出**。`_chars` 会按当前段字号换算成磅，所以"调整字号 + 重新缩进"一次性传两个字段也对齐。
