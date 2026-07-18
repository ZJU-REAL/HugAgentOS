# 端到端配方

5 个高频任务的完整调用序列。每个配方都把 4 步组合（**sandbox_put → bash → sandbox_get → pin_to_workspace**）显式写出来，照着抄就能跑。

约定：
- 占位符 `<FILE_ID>` 是用户上传 / 上一步产出的 artifact id
- `word-cli` 已经在 PATH 里（`/usr/local/bin/word-cli`），shim 自动找到 skill 实现
- 沙盒路径**统一用 `/workspace/`**（不是 `/sandbox/`）
- **方向记忆**：`sandbox_put_artifact` = put INTO sandbox（artifact_id → /workspace/路径）；`sandbox_get_artifact` = get OUT of sandbox（/workspace/路径 → 新 file_id）。**写反就报 unexpected keyword argument**。
- **交付铁律**：拿到新 file_id 必须 `pin_to_workspace(file_ids=[...])` 才会进我的空间/对话区；多个产物**一次性**塞进同一个列表，不要分次。

---

## 配方 1：合同 / 模板填值（最常见，~60% 流量）

**场景**：用户上传一份带 `{{NAME}}` `{{DATE}}` `{{AMOUNT}}` 等占位符的模板，要求填入具体值并产出最终合同。

```text
# Step 1: 把模板送进沙盒
sandbox_put_artifact(artifact_id="<合同模板的 FILE_ID>",
                     dest_path="/workspace/template.docx")
  → {"ok": true, "artifact_id": "...", "dest_path": "/workspace/template.docx"}

# Step 2: 列出占位符（避免漏填）
bash("word-cli read --mode placeholders --input /workspace/template.docx")
  → {"ok": true, "meta": {"placeholders": ["NAME","DATE","AMOUNT","ADDRESS"], "counts": {...}}}

# Step 3: 一次 apply_edits 全部填上
bash("""word-cli edit \
  --input /workspace/template.docx \
  --output /workspace/contract.docx \
  --ops '[
    {"op": "fill_placeholders", "mapping": {
      "NAME":    "示例科技有限公司",
      "DATE":    "2026 年 5 月 20 日",
      "AMOUNT":  "人民币壹拾贰万伍仟元整 (¥125,000)",
      "ADDRESS": "示例市示例区..."
    }}
  ]'""")
  → {"ok": true, "meta": {"ops_succeeded": 1, "results": [
       {"op": "fill_placeholders", "ok": true,
        "filled": {"NAME": 3, "DATE": 1, "AMOUNT": 2, "ADDRESS": 1},
        "unfilled_keys": []}]}}

# 留意：unfilled_keys 非空说明文档里有占位符没在 mapping 里给值；
# 把缺的 key 加进 mapping 再跑一次。

# Step 4: 把沙盒里的成稿登记成 artifact，拿到新 file_id
sandbox_get_artifact(src_path="/workspace/contract.docx", name="合同已填.docx")
  → {"ok": true, "file_id": "<file_id_A>", ...}

# Step 5（可选，用户要 PDF）
bash("word-cli convert --to pdf --input /workspace/contract.docx --output /workspace/contract.pdf")
sandbox_get_artifact(src_path="/workspace/contract.pdf", name="合同已填.pdf")
  → {"ok": true, "file_id": "<file_id_B>", ...}

# Step 6: 一次性把两个 file_id 都 pin（铁律）
pin_to_workspace(file_ids=["<file_id_A>", "<file_id_B>"])
```

**为什么不拆**：4 个字段如果各调一次 `word-cli edit`，要 4×（put+bash+get）= 12 个工具调用；这里 1 次 edit 一锅端，加上 PDF + pin 总共 7 个工具调用。

---

## 配方 2：政策文件改一节（"二、申报条件"整章重写）

**场景**：用户上传一份政策文件，要求"把申报条件那节按新口径重写一下"。

```text
sandbox_put_artifact(artifact_id="<FILE_ID>",
                     dest_path="/workspace/policy.docx")

# Step 2: outline 必读（拿到 heading 文本作为锚点）
bash("word-cli read --mode outline --input /workspace/policy.docx")
  → {"ok": true, "meta": {"outline": [
       {"level": 1, "text": "一、总体要求", "paragraph_index": 0},
       {"level": 1, "text": "二、申报条件", "paragraph_index": 8},
       {"level": 1, "text": "三、申报材料", "paragraph_index": 23},
       ...
     ]}}

# Step 3: 一次 apply_edits + replace_section
bash("""word-cli edit \
  --input /workspace/policy.docx \
  --output /workspace/policy_v2.docx \
  --ops '[
    {"op": "replace_section",
     "heading_anchor": "二、申报条件",
     "preserve_heading": true,
     "new_content": "申报单位须同时符合以下条件：\\n\\n（一）依法在本省内注册的法人企业，注册时间不少于 2 年；\\n\\n（二）上年度营业收入不低于 5000 万元；\\n\\n（三）研发投入占营业收入比例不低于 3%；\\n\\n（四）未发生过重大违法违规行为。"}
  ]'""")
  → {"ok": true, "meta": {"results": [
       {"op": "replace_section", "ok": true,
        "heading_index": 8, "removed_paragraphs": 14, "new_paragraph_count": 5,
        "preserve_heading": true}]}}

# Step 4: 大改后强烈建议 validate
bash("""word-cli validate \
  --input /workspace/policy_v2.docx \
  --repair --output /workspace/policy_v2.fixed.docx""")
  → {"ok": true, "meta": {"is_valid": true, "repairs_applied": ["merge-runs","fix-order"], ...}}

# Step 5: 登记 + pin
sandbox_get_artifact(src_path="/workspace/policy_v2.fixed.docx", name="政策文件_修订.docx")
  → {"ok": true, "file_id": "<NEW>", ...}
pin_to_workspace(file_ids=["<NEW>"])
```

**关键决策**：
- **整章重写用 `replace_section`，不用** N 次 `delete_paragraph` + `insert` ——前者原子、保留样式、无 index 漂移。
- `preserve_heading=true` 让 heading 文本不变；想连标题一起改就传 `false`，`new_content` 第一行就成新标题。

---

## 配方 3：套模板出公文

**场景**：用户给了一份内容稿 + 一份样式模板，要求"把内容套上模板的格式"。

```text
sandbox_put_artifact(artifact_id="<内容稿 FILE_ID>", dest_path="/workspace/source.docx")
sandbox_put_artifact(artifact_id="<模板 FILE_ID>",   dest_path="/workspace/template.docx")

# Step 2: 侦察一下两份的样式名是否能对得上
bash("word-cli read --mode analyze --input /workspace/source.docx")
bash("word-cli read --mode analyze --input /workspace/template.docx")
# 看 customStyles 列表——如果源用了模板里没有的样式名，套完会有警告

# Step 3: 套模板
bash("""word-cli template \
  --source /workspace/source.docx \
  --template /workspace/template.docx \
  --output /workspace/applied.docx""")
  → {"ok": true, "meta": {"applied": ["styles","theme","numbering","sections"]}}

# Step 4: 强制 validate + repair（template 之后偶尔有 XSD 警告，**必做**）
bash("""word-cli validate \
  --input /workspace/applied.docx \
  --repair --output /workspace/final.docx""")
  → {"ok": true, "meta": {"is_valid": true, ...}}

# Step 5（可选）：源文档残留的直接格式可能覆盖了模板样式——一次 format op 清掉
bash("""word-cli edit \
  --input /workspace/final.docx --output /workspace/final2.docx \
  --ops '[
    {"op": "format", "style_filter": "!Heading",
     "font_name": "方正仿宋简体", "font_size": 12,
     "line_spacing": 1.5, "first_line_indent_chars": 2}
  ]'""")

# Step 6: 登记 + pin
sandbox_get_artifact(src_path="/workspace/final2.docx", name="终稿.docx")
  → {"ok": true, "file_id": "<NEW>", ...}
pin_to_workspace(file_ids=["<NEW>"])
```

**注意**：默认不 copy 模板的 headers/footers（多节模板复杂）。要 copy 加 `--apply-headers-footers`，但事先确认模板是单节。

---

## 配方 4：批量混合编辑

**场景**：用户要求"改 3 处人名，把'结论'段加粗变红，删除附录 B 那节，并把附录 A 后面加一节'参考文献'"。

```text
sandbox_put_artifact(artifact_id="<FILE_ID>", dest_path="/workspace/report.docx")

bash("word-cli read --mode outline --input /workspace/report.docx")
# 拿到附录 A / 附录 B / 结论 等 heading 的真实文本

# 一锅端：5 类改动 8 个 op
bash("""word-cli edit \
  --input /workspace/report.docx \
  --output /workspace/report_v2.docx \
  --ops '[
    {"op": "replace_many", "replacements": [
      {"find": "张三", "replace": "张明"},
      {"find": "李四", "replace": "李红"},
      {"find": "王五", "replace": "王伟"}
    ]},
    {"op": "format", "anchor": "结论", "bold": true, "color_hex": "C00000"},
    {"op": "delete_range",
     "start_anchor": "附录 B", "end_anchor": "参考文献", "include_end": false},
    {"op": "insert",
     "position": "after_section", "anchor": "附录 A",
     "format": "markdown",
     "text": "## 参考文献\\n\\n1. ...\\n2. ...\\n3. ..."}
  ]'""")

sandbox_get_artifact(src_path="/workspace/report_v2.docx", name="报告_修订.docx")
  → {"ok": true, "file_id": "<NEW>", ...}
pin_to_workspace(file_ids=["<NEW>"])
```

**为什么这样组织 ops 顺序**：
1. `replace_many` 先跑——纯文本改，不影响段落数
2. `format` 跑文本 anchor，受 replace_many 不影响
3. `delete_range` 删整段——会改段落数，但**后续 op 用的全是字符串 anchor**，不会错位
4. `insert` 在 delete 后跑——因为目标位置（附录 A 之后）此时已经没有附录 B 挡着

如果第 3 步用 `delete_paragraph` int anchor + 第 4 步 `insert` int anchor，就会乱套。

---

## 配方 5：从零起草一份公文

**场景**：用户口头描述要求"起草一份关于 XX 工作的通知"。

```text
# 没有 sandbox_put 步骤——用户没上传任何文件

# 生成（markdown 模式最常见）
bash("""word-cli create \
  --markdown '# 关于做好 XX 工作的通知

各有关单位：

为深入贯彻 XX 精神，根据 XX 工作部署，现就做好 XX 工作通知如下：

## 一、总体要求

...

## 二、重点任务

...

## 三、保障措施

...

特此通知。

XX 部门
2026 年 5 月 20 日' \
  --output /workspace/notice.docx \
  --title '关于做好 XX 工作的通知'""")
  → {"ok": true, "meta": {"engine": "markdown", "output": "/workspace/notice.docx", ...}}

# 如果需要封面 / 目录 / 多节，改 --content 模式
# bash("""word-cli create \
#   --content '{"sections":[{"heading":"一、总体要求","level":1,"paragraphs":["..."]}]}' \
#   --output /workspace/notice.docx \
#   --title '关于做好 XX 工作的通知' --toc --header '示例科技 内部资料'""")

# 用户后续要微调？继续 apply_edits
# bash("""word-cli edit \
#   --input /workspace/notice.docx --output /workspace/notice_v2.docx \
#   --ops '[{"op": "format", "style_filter": "!Heading", "first_line_indent_chars": 2}]'""")

sandbox_get_artifact(src_path="/workspace/notice.docx", name="通知.docx")
  → {"ok": true, "file_id": "<NEW>", ...}
pin_to_workspace(file_ids=["<NEW>"])
```

---

## 配方 6：把图表 / 图片插进既有文档（"画张图写到表2下方"）

**场景**：用户上传了一份 docx，要求"画一张柱状图/折线图，插到某处，加图注"。关键点：图表工具产出的是 **artifact（在附件区，不在沙盒）**，要先 `sandbox_put` 进沙盒，再 `insert_image` 引用沙盒路径。**全程是 `edit`，不是 `create`**（用户给了既有文档）。

```text
# Step 1: 原文档送进沙盒
sandbox_put_artifact(artifact_id="<原 docx 的 FILE_ID>", dest_path="/workspace/in.docx")

# Step 2: outline 必读——拿到插入锚点（如 "表2" / 某段落文本）
bash("word-cli read --mode outline --input /workspace/in.docx")

# Step 3: 拿真实数据后画图（禁止凭空绘图）。图表工具返回 file_id
generate_chart_tool(data='{"年份":[2024,2025],"核心产业规模":[5800,7200]}',
                    query="画柱状图，标题『2024-2025 AI核心产业规模』，单位亿元")
  → {"ok": true, "file_id": "ch_abc123", "name": "chart_xxxx.png", ...}
  # ↑ 图在 artifact 存储里，不在沙盒

# Step 4: 把图表 artifact 拷进沙盒（这一步不能省——CLI 在沙盒里读不到 artifact）
sandbox_put_artifact(artifact_id="ch_abc123", dest_path="/workspace/chart1.png")

# Step 5: 一次 edit——插图 + 图注（用 image_path 直收沙盒路径，无需 --image）
bash("""word-cli edit \
  --input /workspace/in.docx --output /workspace/out.docx \
  --ops '[
    {"op":"insert_image","image_path":"/workspace/chart1.png",
     "position":"after","anchor":"表2","width_cm":14,"alignment":"center"},
    {"op":"insert","text":"图1 2024-2025 AI核心产业关键指标",
     "position":"after","anchor":"表2","style":"Caption"}
  ]'""")
  → results 里 insert_image 的 image_source = /workspace/chart1.png 即成功

# Step 6: 登记 + pin
sandbox_get_artifact(src_path="/workspace/out.docx", name="带图报告.docx")
  → {"ok": true, "file_id": "<NEW>", ...}
pin_to_workspace(file_ids=["<NEW>"])
```

> 踩坑：把 `generate_chart_tool` 返回的 `file_id` 直接当 `image_path` 传给 `insert_image` ——会报"image not found"。`file_id` 必须先经 Step 4 的 `sandbox_put_artifact` 变成 `/workspace/...` 真实路径。

---

## 各配方共同的反模式

1. **不读 outline 就编辑**：每个改既有文档的配方都从 `word-cli read --mode outline` 开始。跳过等于"凭印象写参数"。
2. **拆分 ops 数组**：上面任何配方里的 ops 都是一次性跑完。拆成 N 次调用就是 3N 次工具往返。
3. **忘了 validate**：apply_template 之后**必须**validate；replace_section / delete_range 等大改之后**推荐**validate。小改可以省。
4. **忘了 PDF 单向**：`word-cli convert --to pdf` 是终点，PDF 出去之后没法回头编辑。先把 docx 改满意再转 PDF。
5. **忘了 pin_to_workspace**：跑完 `sandbox_get_artifact` 拿到 file_id ≠ 已交付。**没 pin 就只是后端 artifact 记录，用户在对话区/我的空间都看不到**。多个产物一次性 pin。
6. **方向搞反**：`sandbox_put_artifact` 是「**写进**沙盒」（artifact_id → /workspace/...）；`sandbox_get_artifact` 是「**从沙盒读出**」（/workspace/... → 新 file_id）。写反 = `unexpected keyword argument` 错误，全链路返工。

更多反模式见 `pitfalls.md`。
