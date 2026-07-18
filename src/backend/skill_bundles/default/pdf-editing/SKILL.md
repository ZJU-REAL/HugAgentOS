---
name: pdf-editing
display_name: PDF 文档作业手册
description: "**生成、合并、拆分、抽取页、填表单、读取或重排 .pdf PDF 文档 / 报告 / 表单 / 印刷品 / 标书 / 白皮书** 时使用。既覆盖从零生成印刷级 PDF（含封面·图表）、把 markdown/docx/txt 重排成正式 PDF，也覆盖对已有 PDF 的操作：多份按序合并 / 按页范围或章节拆分 / 抽取指定页 / 填 AcroForm 表单字段 / 提取全文文本或目录。仅当用户要 .pdf 产物、或改/读既有 PDF（含读取大段 PDF 文本）时触发；查阅网页正文、总结文章观点、解释 PDF 概念、修打印机、推荐阅读器、压缩图片 时不要触发。"
tags: pdf, document, office, print, cli
---

# PDF 文档作业手册

本 skill 是处理 .pdf 文件的**唯一权威入口**。所有 PDF 能力都收敛成单一 CLI——**`pdf-cli`**——的 6 个子命令。

**目标只有一个**：每次操作 .pdf 都选对子命令、写对参数。

---

## 进入本技能前先停一秒：别走偏

只有在用户明确需要 **PDF / .pdf** 文件产物，或要求生成、合并、拆分、填表、读取
PDF 内容时，才进入这份 SKILL。下面几件事看上去能"完成任务"，实际是失败：

1. **`word-cli convert --to pdf` 不可互换替代 `pdf-cli create`**。用户要的是
   "从零生成一份印刷级 PDF 报告"时，**首选** `pdf-cli create` 的 spec→PDF
   引擎（含设计感封面、图表、流程图、数学公式）。用户要的是"先做 Word 再附带
   PDF 副本"时，那是 `word-cli create` + `word-cli convert --to pdf`，主产物
   还是 .docx。**别用一个工具兜底另一个工具。**
2. **不要塞 markdown 回 chat 让用户"自己另存为 PDF"**。这是 hard fail。
   markdown 草稿写完 → `pdf-cli reformat --input draft.md --output out.pdf`
   生成正式 PDF，再 `sandbox_get_artifact` + `pin_to_workspace` 交付。
3. **不要拿 `list_myspace_files` 返回的旧 artifact 当作"新生成的 PDF"再 pin
   一遍**。`sandbox_put_artifact` 是把"用户**这一轮上传的**原始 PDF"送进沙盒；
   它不是 artifact 复制器、不是"我把以前生成过的同名文件再用一次"的捷径。
4. **`pdf_create` 老 MCP 名已下线**。如果模型记忆里还有 `pdf_*` 工具名，那是
   过时记忆——一律转写成 `pdf-cli <subcmd>`。
5. **改完中间稿（`pdf-cli create` 的 spec JSON / 待重排的 markdown 草稿 /
   `fill-form` 的 fields JSON）就停手，忘了重新跑 `pdf-cli` 出最终 .pdf**。
   常见现场：你把 spec 调了一轮，回复就说"已经把 X 章节改成 Y，spec 保存在
   `/workspace/spec.json`"——然后收尾。**这些 spec / markdown / fields 都不是
   用户要的东西，`.pdf` 才是**。中间稿每改一轮都得重新跑 `pdf-cli create` /
   `pdf-cli reformat` / `pdf-cli fill-form` 把新版 `.pdf` 产出来，再走
   `sandbox_get_artifact` + `pin_to_workspace` 把最新文件推给用户。stop 在
   spec / markdown 草稿上 = 这一轮没交付。
6. **`pdf-cli` 跑完了，只在回复里说"PDF 已生成，路径 `/workspace/xxx.pdf`"
   就收尾**。沙盒磁盘对用户**完全是黑盒**——你告诉他"沙盒里有个文件在这里"，
   等同于没交付：用户的对话区 / 我的空间不会自动出现这个文件，他没办法点开、
   下载、看里面的内容。CLI 跑完后必须接 `sandbox_get_artifact` 拿 `file_id`，
   再 `pin_to_workspace` 钉到工作区，用户那边才会看到 .pdf 卡片。

什么时候**应该**离开这份 SKILL：用户明确说要 **Word / 报告（要 .docx）** →
走 `word-editing`；用户要 **Excel / 工作簿** → 走 `excel-editing`；用户要
**PPT / 演示文稿 / 幻灯片** → 走 `ppt-design`。

---

## 运行模型（必须先理解这个，再谈调命令）

LLM 没有 `pdf_*` 工具可以直接调。每次 PDF 操作是 **3 步组合 + 1 步交付**：

```
# Step 1：如果用户上传过原始 pdf（有 file_id），把它送进沙盒
sandbox_put_artifact(artifact_id="<原始 pdf 的 file_id>",
                     dest_path="/workspace/in.pdf")
  → {"ok": true, "artifact_id": "...", "dest_path": "/workspace/in.pdf"}

# Step 2：bash 跑 pdf-cli
bash("pdf-cli <subcmd> --input /workspace/in.pdf \
                        --output /workspace/out.pdf \
                        <子命令特定参数>")
  → stdout 返回 JSON 结果（{"ok": bool, "meta": ...}）

# Step 3：把沙盒里的成稿提取出来登记成 artifact
sandbox_get_artifact(src_path="/workspace/out.pdf",
                     name="终稿.pdf")
  → {"ok": true, "file_id": "<新 file_id>", "url": "/files/...", ...}

# Step 4（必做）：交付给用户
pin_to_workspace(file_ids=["<新 file_id>"])
```

读取类子命令（`read` 的 text/outline/metadata/overview/form-fields 模式）只
有第 1、2 步，没有第 3、4 步（没有新文件交付）。

---

## 6 个子命令速查（详见 references/cli-commands.md）

| 子命令 | 用途 | 典型场景 |
|---|---|---|
| `pdf-cli read --mode text` | 全文文本（可指定页号列表） | "提取这份 PDF 的全文" |
| `pdf-cli read --mode outline` | 书签 / 目录 | "看看 PDF 有哪些章节" |
| `pdf-cli read --mode metadata` | 元数据（页数、标题、作者、加密） | 第一眼快速判断 PDF 规模 |
| `pdf-cli read --mode overview` | metadata + outline 合并 | 用户上传 PDF 后的"第一眼" |
| `pdf-cli read --mode form-fields` | 列出 AcroForm 字段 | 填表前必先 form-fields 拿字段名 |
| `pdf-cli merge` | 多份 PDF 合并 | "把这 5 份 PDF 合成一个" |
| `pdf-cli split` | 按页范围拆成 N 个 PDF | "100 页 PDF 按章节拆成 5 份" |
| `pdf-cli fill-form` | 填 AcroForm 字段 | "把这份表单填好" |
| `pdf-cli create` | spec→印刷级 PDF（封面/图表/数学公式） | "从零生成一份正式 PDF 报告" |
| `pdf-cli reformat` | md/docx/txt → 设计感 PDF | "把这份 markdown 重排成 PDF" |

---

## 渐进式加载：什么时候去看哪份 reference

- **写 `pdf-cli create` 的 spec** → 必读 `references/cli-commands.md` 的
  create 段，里面有 19 种 content block 类型（h1/h2/h3 / body / bullet /
  numbered / callout / table / image / figure / code / math / chart /
  flowchart / bibliography / divider / caption / pagebreak / spacer）的完整字段表。
- **填 AcroForm 表单** → 看 `references/form-fields.md`：字段类型与值的语义
  （checkbox 接受 yes/no/true/false；dropdown 必须 match choices；radio 加
  斜杠前缀等）。
- **要查一份完整配方**（如"用户传 PDF → 我要提取文本 + 拆分 + 填表"）→ 看
  `references/workflows.md`。
- **报错 / 行为反直觉** → 看 `references/pitfalls.md`，含 LibreOffice / node /
  pypdf 缺失等常见底层报错。

---

## 大 payload 兜底

`pdf-cli create --spec '<inline json>'` 的 JSON 很容易很大（一份带图表 + 流
程图的报告 spec 轻松破 10KB）。bash 命令行有 ~128KB 上限，触发 `Argument
list too long` 就直接报错。

**永远的兜底**：先 `Write` 把 JSON 写到 `/workspace/spec.json`，再用
`--spec-file`：

```bash
Write(file_path="/workspace/spec.json", content="<json>")
bash("pdf-cli create --output /workspace/report.pdf --spec-file /workspace/spec.json")
```

`fill-form` 的 `--fields-file` 是同理兜底。

---

## 一句话自检（交付前）

每次回复用户之前，按这个列表 5 秒过一遍。任何一项没满足 = 这一轮**没交付**，
回去补，不要发：

- 用户要的是 **.pdf**，我交付的也是 .pdf 吗？（不是偷偷换成了 .docx /
  「贴在回答里的 markdown 文字让用户自己复制粘贴 / 另存为 PDF」？）
- 是不是用 `pdf-cli create`（从零）/ `pdf-cli reformat`（从 md/docx 改造）/
  `pdf-cli merge` / `pdf-cli fill-form` 真的产出过 `/workspace/<...>.pdf` 这个
  文件？（没跑过这条命令 = 没有 .pdf，必返工）
- 中途如果改过 spec JSON / markdown 草稿 / fields JSON 这类中间稿，**改完之后
  有没有再跑一次 `pdf-cli` 把最新版 .pdf 重新生成出来**？（停在改完 spec / md
  上 = 没交付）
- `sandbox_get_artifact(src_path="/workspace/<...>.pdf", name="...")` 跑了，
  拿到了 `file_id` 了吗？
- **`pin_to_workspace(file_ids=["<file_id>"])` 调了吗？**
  —— 这一步不可省：没 pin，文件只是后端 artifact 存储里的一条匿名记录，
  用户在对话区 / 我的空间里**完全看不到**，等于这一轮任务从没存在。
- 一次 chat 产出多个 PDF（例如批量拆分、批量合并出多份）时：所有 file_id 都进
  同一次 `pin_to_workspace(file_ids=[...])` 调用了吗？（一次 pin 多个，别多次 pin）
- 用户指定了"放到我的空间里 XX 文件夹"：`name=` 参数填了贴切的中文文件名
  吗？（不是 `out.pdf` / `draft.pdf` 这种）

---

## 一句话回顾

> 看见 PDF/.pdf 任务 → 进沙盒 → 选对 `pdf-cli <subcmd>` → 出文件 → pin。
> 读 PDF 内容首选 `read --mode text/outline/overview`；生成新 PDF 首选
> `create`（带封面与图表）或 `reformat`（从 md/docx 改造）；表单填写前必先
> `read --mode form-fields` 看字段。
