# pdf-editing 端到端配方

完整的端到端工具调用序列。`pdf-cli` 已经在 PATH 里。

> 共同前置：所有路径假设落在 `/workspace/`；产物最终一定走
> `sandbox_get_artifact` + `pin_to_workspace` 交付。

---

## 配方 1：读用户上传 PDF 做总结

> "总结一下这份政策文件"

```python
sandbox_put_artifact(artifact_id="<上传 fid>", dest_path="/workspace/in.pdf")

# 第一眼：metadata + outline
bash("pdf-cli read --mode overview --input /workspace/in.pdf")
# → 拿到页数、有没有目录、章节结构

# 按章节抓正文（若 PDF 太长可只抓特定页号）
bash("pdf-cli read --mode text --input /workspace/in.pdf --pages 1,2,3")

# 回答用户。不产新文件，不需要 pin。
```

---

## 配方 2：合并多份 PDF

> "把这 5 份月报合成一个 PDF"

```python
# 把 5 份上传文件依次送进沙盒
for i, fid in enumerate(uploaded_fids):
    sandbox_put_artifact(artifact_id=fid, dest_path=f"/workspace/in_{i}.pdf")

# 一次合并
bash("""pdf-cli merge --output /workspace/all.pdf \
    --inputs /workspace/in_0.pdf /workspace/in_1.pdf /workspace/in_2.pdf /workspace/in_3.pdf /workspace/in_4.pdf""")

sandbox_get_artifact(src_path="/workspace/all.pdf", name="月报汇总.pdf")
pin_to_workspace(file_ids=["fid_new"])
```

---

## 配方 3：按章节拆分长 PDF

> "100 页的标书帮我按章节拆出来"

```python
sandbox_put_artifact(artifact_id="<上传 fid>", dest_path="/workspace/in.pdf")

# 先看目录确定页码
bash("pdf-cli read --mode outline --input /workspace/in.pdf")
# → 假设拿到 [{"title":"投标函","page":1}, {"title":"商务部分","page":15}, ...]

# 按页码切
bash("""pdf-cli split --input /workspace/in.pdf --output-dir /workspace/parts \
    --ranges 1-14,15-40,41-80,81-100 \
    --names 投标函.pdf 商务部分.pdf 技术部分.pdf 附件.pdf""")

# 4 份各自登记
new_fids = []
for name in ("投标函.pdf","商务部分.pdf","技术部分.pdf","附件.pdf"):
    r = sandbox_get_artifact(src_path=f"/workspace/parts/{name}", name=name)
    new_fids.append(r["file_id"])

# 一次 pin 全部
pin_to_workspace(file_ids=new_fids)
```

---

## 配方 4：填表

> "帮我填一下这份政府申请表"

```python
sandbox_put_artifact(artifact_id="<表单 fid>", dest_path="/workspace/form.pdf")

# Step 1：摸清字段
bash("pdf-cli read --mode form-fields --input /workspace/form.pdf")
# → fields: [{"name":"CompanyName","type":"text"},
#            {"name":"Province","type":"dropdown","choices":["浙江","江苏",...]},
#            {"name":"AcceptTerms","type":"checkbox"}, ...]

# Step 2：构造 fields.json（按用户提供的资料和字段约束）
Write(file_path="/workspace/fields.json", content='''{
  "CompanyName": "示例科技有限公司",
  "Province": "浙江",
  "AcceptTerms": "yes",
  "ContactPhone": "13800000000"
}''')

# Step 3：写入
bash("""pdf-cli fill-form --input /workspace/form.pdf --output /workspace/filled.pdf \
    --fields-file /workspace/fields.json""")

sandbox_get_artifact(src_path="/workspace/filled.pdf", name="填好的申请表.pdf")
pin_to_workspace(file_ids=["fid_new"])
```

---

## 配方 5：从零生成印刷级报告

> "做一份产业链分析的正式 PDF 报告"

```python
# 大 payload → 先写 spec
Write(file_path="/workspace/spec.json", content='''{
  "title": "示例市智能制造产业链分析 Q3",
  "doc_type": "magazine",
  "author": "示例市工信局",
  "date": "2026-05",
  "subtitle": "面向智能装备 / 新能源汽车 / 集成电路 三大方向",
  "abstract": "本报告综合企业画像、链上协同与外部环境...",
  "accent": "#0a5",
  "content": [
    {"type": "h1", "text": "一、产业概况"},
    {"type": "body", "text": "全市规模以上工业总产值..."},
    {"type": "chart", "chart_type": "bar",
     "labels": ["智能装备","新能源车","集成电路"],
     "datasets": [{"label":"Q3收入(亿)","values":[125,98,156]}],
     "title": "Q3 三大方向收入"},
    {"type": "h2", "text": "链上协同"},
    {"type": "flowchart",
     "nodes": [{"id":"u","label":"上游元器件"},{"id":"m","label":"中游集成"},{"id":"d","label":"下游应用"}],
     "edges": [{"from":"u","to":"m"},{"from":"m","to":"d"}]},
    {"type": "pagebreak"},
    {"type": "h1", "text": "二、未来展望"},
    {"type": "body", "text": "..."}
  ]
}''')

bash("pdf-cli create --output /workspace/report.pdf --spec-file /workspace/spec.json")

sandbox_get_artifact(src_path="/workspace/report.pdf", name="智能制造产业链分析 Q3.pdf")
pin_to_workspace(file_ids=["fid_new"])
```

---

## 配方 6：markdown 草稿 → 印刷级 PDF

> "把我刚才写的这段 markdown 排版成正式 PDF"

```python
# 用户已经把 markdown 内容给到对话里 → 写到沙盒
Write(file_path="/workspace/draft.md", content="<markdown content here>")

# 一行 reformat
bash("""pdf-cli reformat --input /workspace/draft.md --output /workspace/final.pdf \
    --doc-type report --title "Q3 工作小结" --author "示例区工信局" --date "2026-05" --accent "#0a5" """)

sandbox_get_artifact(src_path="/workspace/final.pdf", name="Q3 工作小结.pdf")
pin_to_workspace(file_ids=["fid_new"])
```

---

## 配方 7：从已有 docx 重排成更好看的 PDF

> "把这份 Word 报告做成印刷级 PDF"

```python
sandbox_put_artifact(artifact_id="<docx fid>", dest_path="/workspace/in.docx")

bash("""pdf-cli reformat --input /workspace/in.docx --output /workspace/styled.pdf \
    --doc-type editorial""")

sandbox_get_artifact(src_path="/workspace/styled.pdf", name="样式 PDF.pdf")
pin_to_workspace(file_ids=["fid_new"])
```

> 注：用户如果要的是"Word 主产物，附带 PDF 副本"，那是 `word-cli convert
> --to pdf`，不是 reformat。reformat 是把 docx 当成"草稿"重新设计排版，
> 输出是**纯 PDF**（不一定与原 docx 视觉一致）。

---

## 通用反模式

1. **不读 outline / metadata 就硬切 / 硬填**：拆 PDF 前必先 outline，填表
   前必先 form-fields。
2. **拆出来的多份 PDF 一份一份 pin**：4 份各 pin 一次是 4 次工具调用；正
   确做法是收集 4 个 file_id 之后**一次性** `pin_to_workspace(file_ids=[
   ...4个...])`。
3. **`pdf-cli create` 用大 inline json**：超过几 KB 就 `Write` 文件 +
   `--spec-file`。
4. **从 markdown 直接 `Write` 出 .pdf 文件**：那是 `Write` 写了个文本文件，
   后缀 .pdf 不会让它变成真 PDF。要正式 PDF 必须走 `pdf-cli create` 或
   `pdf-cli reformat`。
