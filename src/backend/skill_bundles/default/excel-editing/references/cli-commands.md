# excel-cli 全子命令参考

> 每一个子命令都有 `--help`：`excel-cli <subcmd> --help` 可以直接看 argparse 全
> 量参数。本文件是浓缩版，把每个子命令的「典型用法 / 必填 / 可选 / 返回结构」一
> 张卡片说清楚。

---

## 1. `excel-cli read`

只读，不产新文件。所有 read 模式都不需要 `--output`。

### `--mode summary`

工作簿全局概览：sheet 列表、行/列数、表头、前 N 行样例。

```bash
excel-cli read --mode summary --input /workspace/wb.xlsx
excel-cli read --mode summary --input /workspace/wb.xlsx --sample-rows 10
```

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--input` | ✅ | — | 源 xlsx 路径 |
| `--sample-rows` | ❌ | 5 | 每个 sheet 抽样行数（max 50） |

返回：`{ok, meta: {sheet_names, sheets: [{name, max_row, max_column, headers, sample}, ...]}}`

### `--mode sheet`

读取单 sheet 的单元格值，可指定 range。

```bash
excel-cli read --mode sheet --input /workspace/wb.xlsx --sheet "Q3 Revenue"
excel-cli read --mode sheet --input /workspace/wb.xlsx --sheet "Q3" --range A1:D20 --max-rows 500
```

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--input` | ✅ | — | 源 xlsx 路径 |
| `--sheet` | ✅ | — | sheet 名（必须存在） |
| `--range` | ❌ | — | openpyxl-style `A1:D20`；不传 = 整个 sheet |
| `--max-rows` | ❌ | 1000 | 返回行数硬上限（防 LLM 上下文爆炸） |

返回：`{ok, meta: {sheet, range, row_count, column_count, rows: [[...], ...]}}`

### `--mode validate`

公式静态校验：检测 #REF!/#DIV/0! 等错误值、跨表引用断裂、命名区域断裂、共享公式
完整性。

```bash
excel-cli read --mode validate --input /workspace/wb.xlsx
excel-cli read --mode validate --input /workspace/wb.xlsx --sheet-filter "Model"
```

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--input` | ✅ | — | 源 xlsx 路径 |
| `--sheet-filter` | ❌ | — | 仅校验指定 sheet（不传 = 所有 sheet） |

返回：`{ok, meta: {file, sheets_checked, formula_count, shared_formula_ranges, error_count, errors: [...]}}`

---

## 2. `excel-cli create`

从零生成新 xlsx。需要 `--output`。

### `--mode workbook`

普通数据表：sheet 名 + 表头 + 行数据 + 可选列宽 + 可选冻结表头。

```bash
excel-cli create --mode workbook --output /workspace/out.xlsx \
  --sheets '[{"name":"Q3","headers":["地区","收入","增长"],"rows":[["华东",12500000,0.18],["华南",9800000,0.12]],"column_widths":[16,18,14],"freeze_header":true}]'

# 大 payload 走文件
excel-cli create --mode workbook --output /workspace/out.xlsx --sheets-file /workspace/sheets.json
```

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--output` | ✅ | — | 输出 .xlsx 路径 |
| `--sheets` | 二选一 | — | sheet 规格 JSON 数组（见上）；都不传 = 单个空 Sheet1 |
| `--sheets-file` | 二选一 | — | 同上，但从文件读 |

**sheet 规格**：

```json
{
  "name": "Q3 Revenue",
  "headers": ["地区", "收入", "增长"],
  "rows": [["华东", 12500000, 0.18]],
  "column_widths": [16, 18, 14],
  "freeze_header": true
}
```

返回：`{ok, meta: {output_filename, sheet_names, row_counts, size_bytes}}`

### `--mode model`

公式优先 + 角色样式（13 种）的财务/分析模型。每个 `{"formula":"..."}` 真的成为
live Excel 公式（不是硬编码值）。

```bash
excel-cli create --mode model --output /workspace/model.xlsx --spec-file /workspace/spec.json
```

| 参数 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `--output` | ✅ | — | 输出 .xlsx 路径 |
| `--spec` | 二选一 | — | 内联模型 spec JSON |
| `--spec-file` | 二选一 | — | 从文件读 spec |

**spec 结构**（精简）：

```json
{
  "sheets": [{
    "name": "Forecast",
    "freeze_header": true,
    "columns": [{"width": 16}, {"width": 14}],
    "rows": [
      {"role": "header", "cells": ["Item", "Q3"]},
      {"role": "input", "cells": [
        {"value": "Revenue", "role": "text"},
        {"value": 12500000, "role": "input_currency"}
      ]},
      {"role": "formula", "cells": [
        {"value": "Total", "role": "text"},
        {"formula": "SUM(B2:B9)", "role": "formula_currency"}
      ]}
    ]
  }]
}
```

**角色枚举**：`default / text / input / formula / xref / header / input_currency /
formula_currency / input_pct / formula_pct / input_int / formula_int / year /
highlight`

返回：`{ok, meta: {sheets, ...}}`

---

## 3. `excel-cli edit`

修改既有 xlsx。必须**且只能**选一个引擎：`--patches` / `--set-cells` /
`--add-sheet` / `--add-chart`。

### `--patches`（首选，字节保留）

参考 `references/apply-edits-ops.md` 看 7 个 op 的详细 spec。简版：

```bash
excel-cli edit --input /workspace/in.xlsx --output /workspace/out.xlsx \
  --patches '[{"op":"set_cell","sheet":"Q3","cell":"B3","value":1200},
              {"op":"add_column","col":"E","sheet":"Q3","header":"利润","formula":"=B{row}-D{row}","formula_rows":"2:9"}]'

excel-cli edit --input in.xlsx --output out.xlsx --patches-file /workspace/patches.json
```

✅ 保 VBA、数据透视、稀疏图（sparkline）、条件格式、命名区域。后续 op 看到前面 op
的结果（patch chain）。

### `--set-cells`（备选，openpyxl 回填）

```bash
excel-cli edit --input in.xlsx --output out.xlsx --sheet "Data" \
  --set-cells '[{"addr":"B2","value":100},{"addr":"C2","formula":"=SUM(B2:B10)"}]'
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `--input` / `--output` | ✅ | 源 + 目标路径 |
| `--sheet` | ✅ | 目标 sheet 名 |
| `--set-cells` / `--set-cells-file` | 二选一 | 单元格 JSON 数组 |

⚠️ **缺点**：openpyxl round-trip 会丢宏 (VBA)、丢数据透视、丢部分条件格式。
如果工作簿没这些就放心用。

### `--add-sheet`

```bash
excel-cli edit --input in.xlsx --output out.xlsx \
  --add-sheet '{"sheet_name":"Q4","after":"Q3","headers":["地区","收入"]}'
```

payload：

```json
{
  "sheet_name": "Q4",     // 必填，不能与已有 sheet 重名
  "after": "Q3",          // 可选，插在某个 sheet 之后；不传 = 追加到最后
  "headers": ["地区", "收入"]  // 可选，写在第 1 行并加表头样式
}
```

### `--add-chart`

```bash
excel-cli edit --input in.xlsx --output out.xlsx \
  --add-chart '{"sheet":"Data","chart_type":"bar","data_range":"B1:B10","categories_range":"A2:A10","title":"Q3 收入","anchor":"H2"}'
```

payload：

| 字段 | 必填 | 说明 |
|---|---|---|
| `sheet` | ✅ | 数据所在 sheet（图表也插在这里） |
| `chart_type` | ✅ | `bar` / `line` / `pie` |
| `data_range` | ✅ | 含表头的值范围，如 `B1:B10` |
| `categories_range` | ❌ | 类别轴范围，如 `A2:A10` |
| `title` / `x_title` / `y_title` | ❌ | 标签 |
| `anchor` | ❌ | 图表左上角单元格（默认 `H2`） |

⚠️ 与 `--set-cells` 同样会 openpyxl round-trip，可能丢宏。

---

## 4. `excel-cli save`

仅改交付名（等价于 `cp`），无业务逻辑。

```bash
excel-cli save --input /workspace/edit_v3.xlsx --output /workspace/年度财务报告.xlsx
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `--input` | ✅ | 源 .xlsx |
| `--output` | ✅ | 目标路径（必须 .xlsx 结尾） |

返回：`{ok, meta: {source, output, size_bytes}}`

---

## 5. `excel-cli convert --to pdf`

xlsx → pdf via LibreOffice headless。

```bash
excel-cli convert --to pdf --input /workspace/report.xlsx --output /workspace/report.pdf
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `--to` | ✅ | 只支持 `pdf` |
| `--input` | ✅ | 源 .xlsx |
| `--output` | ✅ | 目标 .pdf |

返回：`{ok, meta: {output_filename, size_bytes, pages}}`（pages best-effort）

⚠️ **顺序**：先把 xlsx 改满意，再 convert。PDF 是终点，PDF 出去后没办法再回头编辑。
