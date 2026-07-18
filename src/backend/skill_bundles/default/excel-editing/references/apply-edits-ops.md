# excel-cli edit --patches 的 7 个 op 详解

本文件给 `excel-cli edit --patches` 数组里支持的 7 个 op 各一张「卡片」。每张
卡片包含：用途 / 必填+可选 kwargs / 关键边界 / 正反例。

> ✨ `--patches` 是**字节保留**多 op 编辑器：unpack → 改 XML → repack，不走
> openpyxl，因此 VBA / 数据透视 / sparkline / 条件格式 / 命名区域全部保留。这是
> 编辑既有 .xlsx 的**首选**路径。
>
> 多个 op 按数组顺序串行执行，后续 op 看得到前面 op 的结果（例如先 insert_row
> 再 set_cell 到新行就能成功）。

---

## 1. `set_cell` — 写单个单元格的值或公式

### 必填

- `cell`：单元格地址，`"B3"` 这种。
- `value` 或 `formula` 二选一（同时给会报错）。

### 可选

- `sheet`：sheet 名；不传 = 第一个 sheet。
- `role`：单元格样式角色，影响字体/底色。枚举：`text / input / formula / xref /
  header / input_currency / formula_currency / input_pct / formula_pct /
  input_int / formula_int / year / highlight`。

### 例

```json
{"op": "set_cell", "sheet": "Q3", "cell": "B3", "value": 1250000}
{"op": "set_cell", "sheet": "Q3", "cell": "C3", "formula": "SUM(B2:B10)", "role": "formula_currency"}
```

### 边界

- `formula` 字符串前面的 `=` 可写可不写（自动剥）。
- 给 `value=null` 等价于清空单元格。
- 单元格在原 sheet 不存在会自动创建。

---

## 2. `fix_formula` — 修一个已有公式

行为完全等价于 `set_cell` + `formula`。语义上是"我知道这个单元格本来是公式，
我只是改公式表达式"，留 op 名是为了让 patch 数组读起来更自解释。

```json
{"op": "fix_formula", "sheet": "Q3", "cell": "C3", "formula": "SUM(B2:B11)"}
```

---

## 3. `replace_text` — 整库找替换字符串

在 `sharedStrings.xml` 里改所有匹配（这是 Excel 存储字符串的统一池子）。
还会改 inline-string 类型的 `<is><t>...</t></is>` 单元格。

### 必填

- `search`：非空字符串
- `replace`：替换串（可空字符串）

### 可选

- `sheet`：仅 inline string 受 sheet 限制；shared strings 永远是全局替换。

### 例

```json
{"op": "replace_text", "search": "甲方", "replace": "示例市工信局"}
```

### 边界

- 不支持正则，纯文本 substring 替换。
- 公式字符串里的 sheet 名引用**不会**被 replace_text 改——那是 `<f>` 里的内容，
  要改 sheet 名用 `rename_sheet`。

---

## 4. `rename_sheet` — 改 sheet 名

### 必填

- `from`：旧 sheet 名
- `to`：新 sheet 名（不能与现有 sheet 重名、不能含 `\ / ? * [ ] :`）

### 可选

- `update_formulas`：默认 `true`——把所有 `<f>` 里 `OldName!XX` 形式的引用同步改
  成 `NewName!XX`。**几乎永远不要把它设成 false**。

### 例

```json
{"op": "rename_sheet", "from": "Sheet1", "to": "Q3 收入"}
```

---

## 5. `insert_row` — 在指定行插入一行

### 必填

- `at`：1-based 行号。新行插在该位置，原 at 行及之后整体下移。

### 可选

- `sheet`：sheet 名；默认第一个 sheet。
- `text`：`{col_letter: str}` 文本类单元格（如 `{"A": "新分类"}`）。
- `values`：`{col_letter: number}` 数值类单元格。
- `formulas`：`{col_letter: formula_template}`，模板里 `{row}` 会替换成实际行号
  （如 `{"E": "=B{row}-C{row}"}`）。
- `copy_style_from`：1-based 行号，把那一行的样式克隆给新行。

### 例

```json
{
  "op": "insert_row",
  "sheet": "Forecast",
  "at": 5,
  "text": {"A": "新产品线"},
  "values": {"B": 0, "C": 0},
  "formulas": {"D": "=B{row}+C{row}"},
  "copy_style_from": 4
}
```

### 边界

- 插入后下方所有公式里指向 `at` 之后行的引用会**自动**调整（因为 Excel 用 row
  number 直接 encode 在 XML 里，插入后 row attribute 更新即可）。

---

## 6. `add_column` — 在某一列写整列公式 / 表头

### 必填

- `col`：单字母列名（`"G"`）。

### 可选

- `sheet`、`header`、`formula`、`formula_rows`、`total_row`、`total_formula`、
  `numfmt`、`border_row`、`border_style`。

### 例

```json
{
  "op": "add_column",
  "sheet": "Q3",
  "col": "E",
  "header": "利润",
  "formula": "=B{row}-D{row}",
  "formula_rows": "2:9",
  "total_row": 10,
  "total_formula": "=SUM(E2:E9)",
  "numfmt": "#,##0"
}
```

### 边界

- `formula` 中 `{row}` 是行号占位符，按 `formula_rows`（如 `"2:9"`）展开。
- `total_row` 单独写一行汇总。
- 如果列已有内容，会被**覆盖**——先 `read --mode summary` 确认这一列空。

---

## 7. `delete_row` — 删除指定行

### 必填

- `at`：1-based 行号。

### 可选

- `sheet`：sheet 名；默认第一个 sheet。

### 例

```json
{"op": "delete_row", "sheet": "Forecast", "at": 8}
```

### 边界

- 删除后下方行整体上移；所有公式里指向 `at` 之后的行号也会自动 -1。
- 行不存在会报错（避免静默失败）。

---

## ❌ 不在 --patches 里的能力（要换工具）

下面这些都**不是** `--patches` 的 op，遇到要换：

| 想干的事 | 换成 |
|---|---|
| 把 N 个值塞进一个 sheet 的零散单元格 | 仍走 `--patches` 多个 `set_cell` op，串成数组 |
| 插一张原生柱状/折线图 | `excel-cli edit --add-chart '{...}'` |
| 加一个新 sheet | `excel-cli edit --add-sheet '{...}'` |
| 删除一个 sheet | 暂不支持（issue#TBD；workaround：另存为时不复制） |
| 批量整理表格样式 / 列宽 | 暂不支持，建议 `create --mode workbook` 重建 |
