# excel-editing 端到端配方

完整的端到端工具调用序列。每个配方都假设 `excel-cli` 已经在 PATH 里
（沙盒/mcp 容器都装好了）。

> 共同前置：所有路径假设落在 `/workspace/`；产物最终一定走 `sandbox_get_artifact`
> + `pin_to_workspace` 交付，没 pin = 用户看不到。

---

## 配方 1：纯生成（用户没传文件，要新建一份）

> "整理一下示例市三季度产业收入数据成 Excel"

```python
# Step 1：直接 create（无需 sandbox_put_artifact，没有源文件）
bash("""excel-cli create --mode workbook --output /workspace/q3.xlsx \\
  --sheets '[{"name":"Q3 收入","headers":["地区","收入","同比"],"rows":[["城东区",125000000,0.18],["城西区",98000000,0.12],["城北区",156000000,0.21]],"column_widths":[14,18,12],"freeze_header":true}]'""")

# Step 2：登记成 artifact
sandbox_get_artifact(src_path="/workspace/q3.xlsx", name="示例Q3产业收入.xlsx")
# → file_id: fid_xyz

# Step 3：交付
pin_to_workspace(file_ids=["fid_xyz"])
```

---

## 配方 2：读一份用户上传的 xlsx 做总结

> "看看这份汇总表里有什么"

```python
# Step 1：送进沙盒
sandbox_put_artifact(artifact_id="<用户上传 file_id>", dest_path="/workspace/in.xlsx")

# Step 2：先 summary
bash("excel-cli read --mode summary --input /workspace/in.xlsx")
# → 拿到 sheet_names, 每个 sheet 的维度和表头

# Step 3（按需）：进入某个 sheet 取数据
bash("excel-cli read --mode sheet --input /workspace/in.xlsx --sheet 'Q3 收入' --range A1:E50")

# Step 4：回答用户。不产新文件，不需要 sandbox_get_artifact，不需要 pin。
```

---

## 配方 3：在既有 xlsx 上加一列计算列 + 总计行

> "在这份表里加一列利润 = 收入 - 成本，最后加一行总计"

```python
# Step 1
sandbox_put_artifact(artifact_id="<用户上传 file_id>", dest_path="/workspace/in.xlsx")

# Step 2（建议）：先 summary 摸清结构
bash("excel-cli read --mode summary --input /workspace/in.xlsx")
# 假设 sheet 名 "Data"，B 列是收入，D 列是成本，数据行 2-9

# Step 3：用 add_column 一锅端
bash("""excel-cli edit --input /workspace/in.xlsx --output /workspace/out.xlsx \\
  --patches '[
    {"op":"add_column","sheet":"Data","col":"E","header":"利润","formula":"=B{row}-D{row}","formula_rows":"2:9","total_row":10,"total_formula":"=SUM(E2:E9)","numfmt":"#,##0"}
  ]'""")

# Step 4：登记 + pin
sandbox_get_artifact(src_path="/workspace/out.xlsx", name="含利润列的汇总表.xlsx")
# → fid_new
pin_to_workspace(file_ids=["fid_new"])
```

**为什么 add_column 一次能搞定**：`add_column` op 内置 `formula_rows` 展开和
`total_row` 汇总，不需要再额外的 set_cell。如果手动 `[set_cell A, set_cell B, ...]` 
列出每个单元格，会非常冗长。

---

## 配方 4：批量改公式 + 插一行 + 改 sheet 名（多 op 一锅端）

> "把 Sheet1 改名为 Q3 实际，把 B5 的公式改成 SUM(B2:B4)，再在第 10 行插一个总计"

```python
sandbox_put_artifact(artifact_id="<上传 fid>", dest_path="/workspace/in.xlsx")

# 多 op 串成一个 patches 数组：1 次 unpack/pack
bash("""excel-cli edit --input /workspace/in.xlsx --output /workspace/out.xlsx \\
  --patches '[
    {"op":"rename_sheet","from":"Sheet1","to":"Q3 实际"},
    {"op":"fix_formula","sheet":"Q3 实际","cell":"B5","formula":"SUM(B2:B4)"},
    {"op":"insert_row","sheet":"Q3 实际","at":10,"text":{"A":"总计"},"formulas":{"B":"=SUM(B2:B9)"},"copy_style_from":9}
  ]'""")

sandbox_get_artifact(src_path="/workspace/out.xlsx", name="Q3 实际数.xlsx")
pin_to_workspace(file_ids=["fid_new"])
```

**为什么不拆**：如果分 3 次调 `excel-cli edit` → 3×（put+bash+get）= 9 个工具
调用；这里一次性 3 个 op 一锅端，加 pin 总共 4 个工具调用。

---

## 配方 5：建一个含公式的财务模型

> "做一份产业增长预测模型，三年滚动，含投入产出公式"

```python
# 大 payload → 先写 spec.json 到 workspace
Write(file_path="/workspace/spec.json", content="""{
  "sheets": [{
    "name": "三年预测",
    "freeze_header": true,
    "columns": [{"width":20},{"width":14},{"width":14},{"width":14}],
    "rows": [
      {"role":"header","cells":["项目","2024","2025","2026"]},
      {"cells":["投入",{"value":1000,"role":"input_currency"},{"value":1200,"role":"input_currency"},{"value":1500,"role":"input_currency"}]},
      {"cells":["产出",{"value":1500,"role":"input_currency"},{"value":1900,"role":"input_currency"},{"value":2400,"role":"input_currency"}]},
      {"cells":["产出/投入",{"formula":"B3/B2","role":"formula_pct"},{"formula":"C3/C2","role":"formula_pct"},{"formula":"D3/D2","role":"formula_pct"}]},
      {"cells":["增长率",{"value":"","role":"text"},{"formula":"C3/B3-1","role":"formula_pct"},{"formula":"D3/C3-1","role":"formula_pct"}]}
    ]
  }]
}""")

bash("excel-cli create --mode model --output /workspace/model.xlsx --spec-file /workspace/spec.json")

sandbox_get_artifact(src_path="/workspace/model.xlsx", name="三年产业预测模型.xlsx")
pin_to_workspace(file_ids=["fid_new"])
```

---

## 配方 6：编辑完导出 PDF

> "改完表格再给我一份 PDF 版"

```python
sandbox_put_artifact(artifact_id="<原 xlsx fid>", dest_path="/workspace/in.xlsx")

# 改
bash("""excel-cli edit --input /workspace/in.xlsx --output /workspace/edited.xlsx \\
  --patches '[{"op":"set_cell","sheet":"Q3","cell":"B5","value":1500}]'""")

# 转 PDF（从已改好的 xlsx）
bash("excel-cli convert --to pdf --input /workspace/edited.xlsx --output /workspace/edited.pdf")

# 两份产物一起交付
sandbox_get_artifact(src_path="/workspace/edited.xlsx", name="Q3 调整版.xlsx")
# → fid_xlsx
sandbox_get_artifact(src_path="/workspace/edited.pdf", name="Q3 调整版.pdf")
# → fid_pdf
pin_to_workspace(file_ids=["fid_xlsx", "fid_pdf"])
```

**关键顺序**：先改 xlsx，再 convert PDF。PDF 是终点，不能反向编辑。

---

## 配方 7：在数据 sheet 旁加柱状图

> "给这份 Q3 收入表配一张柱状图"

```python
sandbox_put_artifact(artifact_id="<xlsx fid>", dest_path="/workspace/in.xlsx")

# 先看 summary 摸清 sheet 名和数据范围
bash("excel-cli read --mode summary --input /workspace/in.xlsx")

# 假设：sheet="Q3 收入"，A 列是地区名（A2:A5），B 列是收入数值（B1:B5，B1 是表头）
bash("""excel-cli edit --input /workspace/in.xlsx --output /workspace/with_chart.xlsx \\
  --add-chart '{"sheet":"Q3 收入","chart_type":"bar","data_range":"B1:B5","categories_range":"A2:A5","title":"Q3 各区收入","anchor":"D2"}'""")

sandbox_get_artifact(src_path="/workspace/with_chart.xlsx", name="Q3 收入含图表.xlsx")
pin_to_workspace(file_ids=["fid_new"])
```

⚠️ `--add-chart` 走 openpyxl roundtrip——如果源文件有 VBA / 数据透视，会丢。
没有的话放心用。

---

## 配方 8：校验公式 + 自动修

> "看看这份模型的公式有没有错"

```python
sandbox_put_artifact(artifact_id="<xlsx fid>", dest_path="/workspace/in.xlsx")

bash("excel-cli read --mode validate --input /workspace/in.xlsx")
# → errors 数组：[{"type":"REF_ERROR","cell":"B5","message":"#REF!"}, ...]

# 拿到 errors 列表，针对性 fix（每个 error 通常对应一个 set_cell / fix_formula）
# 然后：
bash("""excel-cli edit --input /workspace/in.xlsx --output /workspace/fixed.xlsx \\
  --patches '[{"op":"fix_formula","sheet":"Model","cell":"B5","formula":"=SUM(B2:B4)"}]'""")

# 再 validate 一遍确认无误
bash("excel-cli read --mode validate --input /workspace/fixed.xlsx")
```

---

## 通用反模式（碰到就停手）

1. **不读 summary 就 edit**：每个改既有 xlsx 的配方都先 `read --mode summary`，
   摸清 sheet 名、数据范围、表头位置。跳过等于"凭印象写参数"。
2. **`--patches` 能干的活非要拆**：所有 op 都串到一个数组里。每多调一次 bash 都
   是一轮 unpack/pack 往返。
3. **payload 大就硬塞内联**：超过几 KB 就 `Write` 写到文件，用 `-file` 变体。
   bash 命令行有 ~128KB 上限。
4. **忘了 PDF 是终点**：`convert --to pdf` 走最后一步。PDF 出去之后没法回头编辑。
5. **直接给用户 markdown 表格当答复**：用户要 Excel/.xlsx 文件，markdown 表格
   不算交付。一定要 create → get_artifact → pin。
