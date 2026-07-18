---
name: word-editing
display_name: Word 文档编辑作业手册
description: "**生成、编辑、修改、排版、套模板或导出 .docx Word 文档 / 公文 / 通知 / 批复 / 合同 / 方案 / 报告** 时使用。既覆盖从零起草，也覆盖对已有 .docx 的任何改动：替换文本 / 改甲乙方·落款·单位名 / 填 {{占位符}} / 套用模板的字体与样式 / 在文末或指定段插入·加粗·改格式 / 整章重写 / 加表格或图片。把 Word 转成 PDF 也走本技能（.docx 为主产物）。仅当用户要 .docx 文件产物、或改/转既有 Word 文件时触发；单纯阅读·总结·分析文档、口头改写一段话（不落文件）、问公文格式规范、修复打不开的文件、推荐模板网站 时不要触发。"
tags: docx, word, office, document, cli
---

# Word 文档编辑作业手册

本 skill 是处理 .docx 文件的**唯一权威入口**。所有 Word 能力都收敛成单一 CLI——**`word-cli`**——的 7 个子命令。

**目标只有一个**：每次操作 .docx 都选对子命令、写对参数。

---

## 进入本技能前先停一秒：别走偏

只有在用户明确需要 **Word / word / .docx** 文件产物，或要求生成、编辑、
排版、套模板、导出 Word 文档时，才进入这份 SKILL。用户只是说"总结这份报告"、
"分析这份材料"、"提炼规划要点"时，除非同时明确要求交付 Word/.docx 成稿，
否则应改用摘要、材料分析或其它更匹配的技能。

一旦确认这是 Word 产物任务，你的最终产物必须是 `.docx`：用 `word-cli create`
或 `word-cli edit` 产出，并 `pin_to_workspace` 交付。下面四件事看上去能"完成任务"，
实际是失败：

1. **`ppt-cli` 不可替代 `word-cli`**。哪怕用户要的是"产业链**分析报告**"、
   "**研究报告**"、"图文并茂的报告"——只要用户明确要 Word/.docx 成稿，
   **就必须出 .docx**。`ppt-cli` 出的是 `.pptx`，**输出格式不符合用户请求
   就是失败**，不接受"用户其实只是想看内容，PPT 也能看"这种推理。看到用户消息里
   明确要求 Word/.docx 文件——心里默念一句"产物必须是 .docx"，再开始动手。
2. **`pdf_create` 不可替代**。用户要 .docx 时不能直接出 .pdf 兜底。用户同时
   要 .docx + .pdf 时，**.docx 是主产物，.pdf 是由 word-cli convert --to pdf
   从 docx 转出来的副产物**，不能跳过 .docx 直接出 .pdf。
3. **不要把 markdown 文字塞回 chat 让用户"自己复制粘贴到 Word"**。这是 hard
   fail。如果你已经写好了 markdown 内容，**正确做法是**把它写进 `/workspace/draft.md`，
   然后 `word-cli create --markdown-file /workspace/draft.md --output /workspace/out.docx`，
   再 `sandbox_get_artifact` + `pin_to_workspace` 交付。从来不是"贴在回答里收工"。
   （`--markdown-file` 收文件路径、`--markdown` 收字面文本，别写反——详见铁律 #5。）
4. **不要拿 `list_myspace_files` 返回的旧报告 artifact 当作"这一轮新生成的成稿"直接再 pin 一遍。**
   每轮要交付的 .docx 必须是你本轮用 `word-cli edit`（改既有）或 `create`（造新）
   真正产出的那一份，不能把上一轮的旧产物当快捷方式重新 pin。
   注意区分：`sandbox_put_artifact` 本身**可以**把任意 artifact 送进沙盒供 CLI 使用——
   不只是"用户这一轮上传的原始 docx"，**也包括本轮其它工具产出的 artifact**，
   典型就是 `generate_chart_tool` 生成的图表（它返回一个 `file_id`，图在附件区、
   不在沙盒）。要把图表插进文档，正是 `sandbox_put_artifact(artifact_id=<图表 file_id>,
   dest_path="/workspace/chart1.png")` 把它拷进沙盒，再用 `insert_image` op 引用
   `/workspace/chart1.png`（完整步骤见 `references/workflows.md` 配方 6）。
5. **改完中间稿（markdown 草稿 / `--ops` JSON / 占位符 dict）就停手**——这些不是
   用户要的，`.docx` 才是。每改一轮中间稿都要重新跑 `word-cli` 出新版 .docx 再交付。
6. **只回一句"已生成，路径 `/workspace/xxx.docx`"就收尾**——沙盒对用户是黑盒，
   等同没交付。CLI 跑完必须 `sandbox_get_artifact` 拿 `file_id` → `pin_to_workspace`。

什么时候**应该**离开这份 SKILL：用户明确说要 **PPT / 演示文稿 / 幻灯片 / deck /
路演**，且没有明确要求 Word/.docx 成稿 → 那应该走 `ppt-design` 技能，
根本不该进 word-editing。如果用户明确要 **纯 PDF（不要 word）/ 海报 / 图片** →
那应该走 `pdf_create` 等其他技能。如果不小心被 load 进了这份 SKILL 但用户其实想
要 PPT / 纯 PDF，先承认走错了再换工具，不要勉强用 word-cli。

---

## 运行模型（必须先理解这个，再谈调命令）

LLM 没有 `word_*` 工具可以直接调。每次 Word 操作是 **3 步组合 + 1 步交付**：

```
# Step 1：如果用户上传过原始 docx（有 file_id），把它送进沙盒
sandbox_put_artifact(artifact_id="<原始 docx 的 file_id>",
                     dest_path="/workspace/in.docx")
  → {"ok": true, "artifact_id": "...", "dest_path": "/workspace/in.docx"}
# 注意：`put` = put INTO sandbox（沙盒是终点）。从零创建的场景跳过这一步。

# Step 2：bash 跑 word-cli
bash("word-cli <subcmd> --input /workspace/in.docx \
                        --output /workspace/out.docx \
                        <子命令特定参数>")
  → stdout 返回 JSON 结果（{"ok": bool, ...}）

# Step 3：把沙盒里的成稿提取出来登记成 artifact
sandbox_get_artifact(src_path="/workspace/out.docx",
                     name="终稿.docx")
  → {"ok": true, "file_id": "<新 file_id>", "url": "/files/...", ...}
# 注意：`get` = get OUT of sandbox（沙盒是源）。这一步返回新的 file_id。

# Step 4（必做）：交付给用户
pin_to_workspace(file_ids=["<新 file_id>"])
  → 文件出现在对话区/我的空间。没 pin = 用户看不到。
```

读取类子命令（`read` 的 text/outline/placeholders/analyze 模式）只有第 1、2 步，没有第 3、4 步（没有新文件交付）。

**关键约定**：
- 两个 sandbox 工具按 **「沙盒是名词」** 记忆方向：`sandbox_put_artifact` 把 artifact 放**进**沙盒；`sandbox_get_artifact` 把沙盒文件**取**出来登记成 artifact。**别搞反**，搞反了模型必然报 `unexpected keyword argument`。
- `sandbox_put_artifact(artifact_id, dest_path)`：**两个位置参数**，`dest_path` 必须以 `/workspace/` 开头。**不要**用 `file_id=` / `src_path=` / `name=`，那些都不是它的形参。
- `sandbox_get_artifact(src_path, name="")`：**`src_path` 必须以 `/workspace/` 开头**（不是 `/sandbox/`）。`name` 可选，给用户面前显示用。返回里 `file_id` 才是新登记的 artifact id。
- `pin_to_workspace(file_ids=[...])` 是**交付铁律**：拿到 `file_id` 不 pin，用户看不到。多个产物一次性塞进同一个列表，**别多次调用**。
- `word-cli` 只认**绝对路径**，不认 file_id。file_id↔路径的桥接由 LLM 显式做。
- stdout 永远是单条 JSON（`{"ok": true|false, ...}`），用 `jq` / `json.loads` 解析。
- exit code：0 成功 / 1 业务失败（JSON 仍然返回，有 error 字段）/ 2 参数错误。
- `word-cli` 已经装到 sandbox 容器的 `/usr/local/bin/` 下（同时也装到 mcp / script-runner 镜像作为调试用），直接 `word-cli <subcmd>` 即可。`bash` 工具在 sandbox 里执行，跟用户 artifact 流转配套。

## 7 个子命令 + 15 个 op（这是你能用的全部）

| 子命令 | 干什么 | 关键参数 |
|---|---|---|
| `word-cli read` | 读文档（4 种模式合一） | `--mode text\|outline\|placeholders\|analyze` |
| `word-cli create` | 从零生成 .docx | `--markdown-file <path>`（推荐）/ `--markdown <md>` / `--content <json>` |
| `word-cli edit` | **改既有 docx 的主入口**（15 个 op） | `--ops '[...]'` 或 `--ops-file ops.json` |
| `word-cli template` | 套另一份文档的样式 | `--source S --template T --output O` |
| `word-cli validate` | XSD + 业务规则校验，可自动修复 | `--repair` 修复并产出新文件 |
| `word-cli diff` | 比较两份 docx | `--before B --after A` |
| `word-cli convert` | .doc→.docx 或 .docx→PDF | `--to docx\|pdf` |

`word-cli edit` 支持的 15 个 op：`replace` / `replace_many` / `fill_placeholders` / `insert` / `insert_image` / `format` / `replace_paragraph` / `replace_section` / `delete_paragraph` / `delete_range` / `set_cell_text` / `fill_table` / `add_table` / `move_table` / `update_field`。详见 `references/apply-edits-ops.md`。

> ⚠️ 没有 `word-cli replace_text` / `word-cli insert_text` / `word-cli add_table` / `word-cli format_text` / `word-cli replace_many` / `word-cli fill_placeholders` 这种单步子命令——它们的功能**都被 `edit` 子命令吸收成 op**。这是有意收敛，避免你在多个入口里选错。一处替换也走 `word-cli edit` 加一个 `replace` op。

子命令各自 `--help` 都能查到详细参数：

```
word-cli help                    # 总览
word-cli <subcmd> --help         # 单个子命令的参数详解
```

## 铁律（先逐条确认，再往下做）

**1. 用户给了既有 .docx 要"修改 / 优化 / 编辑 / 改好 / 调整"，一律走 `word-cli edit` 增量改，绝不用 `word-cli create` 从头重生成。**
`create` = 从零造新文档，会丢掉原文的版式、表格、图片、分节、页眉页脚，以及任何你没誊进 markdown 草稿的内容。哪怕任务听起来像"把整篇没渲染好的 markdown 全改对"——正确做法仍是：先 `read --mode outline` / `--mode text` 定位正文里那些**字面 markdown** 段落（出现 `### 标题`、`**粗体**`、`- 列表` 这种没被渲染的），再用 `edit` 的 `replace_section` / `replace_paragraph` op 逐处改（这两个 op 的 `format` 默认 `auto`，会把 markdown 渲染成真正的 Word 标题/列表/加粗）。把既有文档"读出来 → 重写成一版 markdown → `create` 出一份新的"是**错误套路**：等于从头生成一遍、丢掉原结构。**只有用户手上没有任何既有文档、需要全新产出时才用 `create`。**

**2. 改既有文档之前必须先 `word-cli read --mode outline`。**
不要凭对话上下文猜段落 index 或 heading 文本。`outline` 给你文档当前真实的段落锚点和层级，是后续所有 `anchor` / `paragraph_index` / `heading_anchor` 参数的唯一可靠来源。

**3. 同一次编辑里所有改动一次性写进 `word-cli edit --ops`。**
不要先跑一次 `edit` 改一处，再起一次 `sandbox_get/put + bash` 改另一处。一次 ops 列表多 op，原子完成。每多一次 3 步往返就是 3 次 LLM 轮次开销。

**4. `\n` 不是新段落分隔符（在 `insert` op 里）。**
`insert` op 的 `text` 写 `"第一段\n第二段"` 不会变成两段——它会成为一段里的一个换行符（多数 Word 渲染成软回车甚至消失）。要么拆成多个 `insert` op，要么用 `replace_section`（`new_content` 按 `\n` 自动分段），要么 `insert` 用 `"format": "markdown"` 让 Markdown 引擎处理。

**5. `create` 默认走 markdown 模式，只有用户**显式**要"目录 / 封面 / 页眉页脚 / 页码 / 多节布局 / 非默认页面尺寸/边距"才升级到 `--content`。**
用户只说"生成一份 X 报告"、"写一份 Y 通知"、"出一份 Z 方案"——不带额外排版要求——就用 `word-cli create --markdown-file /workspace/draft.md`（推荐，规避 argv 长度和 shell 转义的坑）或者退而用 `word-cli create --markdown "$(cat /workspace/draft.md)"`。**绝对不要**写 `--markdown /workspace/draft.md`——`--markdown` 收的是字面文本，不是路径，否则会把路径字符串本身渲染成 Word 正文（脚本已加 hard fail 拦截，但仍是常踩的坑）。markdown 模式已经自带公文版式（仿宋正文 + 小标宋标题 + 首行缩进 2 字 + 1.5 倍行距 + 两端对齐），还支持 `#`/`##` 标题、`-`/`1.` 列表、` ``` ` 代码块、`| a | b |` 表格、行内 `**bold**`/`*italic*`/链接，覆盖绝大多数公文/报告/通知/方案。要求"图文并茂 / 含表格"也走 markdown（表格用 `|...|`；图片先 `insert_image` op 或者在 markdown 里写 `![](path)`）。**只有**用户原话出现"目录 / TOC / 封面 / 页眉 / 页脚 / 页码 / A3 / 横排 / 分节"等才升级到 `--content` —— 见决策树的"造新文档"分支。

---

## 决策树（90% 场景看这一张图就够）

```
                    ┌─────────────────────────────┐
                    │ 用户的 .docx 任务是什么？     │
                    └──────────────┬──────────────┘
                                   │
        ┌──────────────────────────┼──────────────────────────┐
        │                          │                          │
   ┌────▼────┐               ┌─────▼─────┐               ┌────▼────┐
   │ 读 / 看 │               │ 改既有文档 │               │ 造新文档 │
   └────┬────┘               └─────┬─────┘               └────┬────┘
        │                          │                          │
  只要看文字？             先读 outline 拿锚点：           ▶ 默认走 markdown ◀
  word-cli read             word-cli read                  word-cli create
   --mode text               --mode outline                  --markdown-file draft.md

  看层级 / heading？                │                       何时升级到 --content？
  word-cli read              ┌──────▼─────────────┐         （用户**显式**要 ↓ 才升级）
   --mode outline            │ 改动数量与类型？     │         • 目录 / TOC
                             └──────┬─────────────┘         • 封面 / 副标题(author)
  找 {{占位符}}？                   │                        • 页眉 / 页脚 / 页码
  word-cli read              ┌──────┼──────┬──────┬──────┐  • A3 / Letter / 横排
   --mode placeholders       │      │      │      │      │  • narrow/wide 边距
                            纯文   占位   1 整   N 处   样式  • 多节布局
  完整盘点                   替换   符填   章重   混合   重排  • letter/memo/academic 版式
 （表/图/关系/命名）？        edit   edit   edit   edit   template
  word-cli read              op:    op:    op:    多个    --source X  否则 markdown 模式已涵盖：
   --mode analyze            replace fill_ replace op     --template Y  公文字体 + 首行缩进 +
                             (× N)   place- _section（混             1.5 倍行距 + 标题/列表/
                             或       holders 头部该       replace    表格/行内格式/图片
                             replace          段换标       insert
                             _many op         题用         format
                                              preserve_   delete_*
                                              heading=    等）
                                              True
```

收尾固定三步（按需）：
- 大改完 → `word-cli validate --repair`（XSD + 业务规则 + auto repair）
- 要 PDF → `word-cli convert --to pdf`
- 验前后变化 → `word-cli diff --before ... --after ...`

---

## 一次完整的编辑长什么样（端到端示例）

用户：把 `合同模板.docx` 里的甲方改成"甲方科技有限公司"，乙方改成"乙方实业有限公司"，"经办人"那段加粗，最后导出 PDF。

```
# 1) 把上传的原始 artifact 送进沙盒（put = put INTO sandbox）
sandbox_put_artifact(artifact_id="<合同模板的 file_id>",
                     dest_path="/workspace/合同模板.docx")
  → {"ok": true, "artifact_id": "...", "dest_path": "/workspace/合同模板.docx"}

# 2) （可选但强烈推荐）先 outline 一下，确认锚点真的存在
bash("word-cli read --mode outline --input /workspace/合同模板.docx")
  → {"ok": true, "meta": {"heading_count": ..., "outline": [...]}}

# 3) 一次 edit 跑完所有改动
bash("""word-cli edit \\
  --input /workspace/合同模板.docx \\
  --output /workspace/合同已填.docx \\
  --ops '[
    {"op": "replace", "find": "甲方：", "replace": "甲方：甲方科技有限公司"},
    {"op": "replace", "find": "乙方：", "replace": "乙方：乙方实业有限公司"},
    {"op": "format", "anchor": "经办人", "bold": true}
  ]'""")
  → {"ok": true, "meta": {"ops_total": 3, "ops_succeeded": 3, ...}}

# 4) 把沙盒里的成稿提取出来登记成 artifact（get = get OUT of sandbox）
sandbox_get_artifact(src_path="/workspace/合同已填.docx",
                     name="合同已填.docx")
  → {"ok": true, "file_id": "<file_id_A>", ...}

# 5) 转 PDF（如果用户要 PDF）
bash("word-cli convert --to pdf --input /workspace/合同已填.docx --output /workspace/合同已填.pdf")
  → {"ok": true, "meta": {"output_filename": ..., "pages": ...}}

sandbox_get_artifact(src_path="/workspace/合同已填.pdf",
                     name="合同已填.pdf")
  → {"ok": true, "file_id": "<file_id_B>", ...}

# 6) 交付：一次性 pin 两个 file_id（铁律：没 pin 就看不见）
pin_to_workspace(file_ids=["<file_id_A>", "<file_id_B>"])
```

这 6 步是稳定套路。把 2-3 处替换误拆成 3 次 `edit` 调用，就要多 6 次 sandbox_put/get + bash，纯属浪费。**Step 6 的 `pin_to_workspace` 不可省**（理由见末尾自检）。

---

## 子命令选型速查（Top 10 高频判断）

| 你想做的事 | 用这个 | 不要用（典型误选） | 为什么 |
|---|---|---|---|
| 替换文档里 3 处不同人名 | `word-cli edit` 一次 + 3 个 `replace` op；或 1 个 `replace_many` op | 跑 3 次 `word-cli edit` 各带 1 个 op | 每次跑命令 = 3 步往返；一次 ops 数组搞定 |
| "二、申报条件"整章内容重写 | `word-cli edit` + `replace_section` op（`heading_anchor`, `preserve_heading=true`） | 多个 `delete_paragraph` + 多个 `insert` | section op 原子完成，不会漏样式 / 索引漂移 |
| 给 `{{COMPANY_NAME}}` `{{DATE}}` 占位符填值 | `word-cli edit` + `fill_placeholders` op（mapping dict） | 多个 `replace` op | placeholders 走正则一次抓所有 key，能告诉你哪些 key 没填到 |
| 让某段加粗变红 | `word-cli edit` + `format` op（`anchor` 或 `style_filter`） | 误以为 `replace` 能改格式 | `replace` 只改文字，`format` 只改格式，是两件事 |
| 改某张表的某个单元格 | `word-cli edit` + `set_cell_text` op（`table_index/row/col`） | `replace` op（容易匹到表外的同名字串） | 走 table 坐标，定位准 |
| 用某模板的字体/编号/页面设置重排版 | `word-cli template` | 逐项 `format` op 模仿 | template 直接拷 styles.xml/theme/numbering.xml，一次到位 |
| 微调几个段落的字号 / 颜色 / 行距 | `word-cli edit` + `format` op | `word-cli template`（杀鸡用牛刀，会改全局） | template 是文档级换皮，不适合局部 |
| 改文档作者/标题/关键字等元数据 | `word-cli edit` + `update_field` op（`field`: TITLE/AUTHOR/SUBJECT/KEYWORDS/DESCRIPTION/CATEGORY） | 找不到工具？是错觉，就是 update_field op | core docprops 只能走这条路 |
| 删除连续多段（含中间表格） | `word-cli edit` + `delete_range` op（`start_anchor`/`end_anchor`） | 多个 `delete_paragraph` 逐段删 | delete_range 原子；连续 delete_paragraph 容易在表格行上出问题 |
| 一份 .doc（老格式）来了 | 先 `word-cli convert --to docx`，再操作 | 直接跑其他子命令 | 其他子命令都假设 .docx，遇 .doc 会报错 |

不要做的：
- 跑完一次 `word-cli edit` 立刻又跑一次 `word-cli convert --to docx` 重新归一化——`edit` 本身已经产出合法 .docx。

---

## 参数最容易写错的几个点

`style` 名要带空格（`"Heading 1"` 不是 `"Heading1"`）/ `paragraph_index` 会漂移（首选字符串 anchor）/ `position` 5 个值语义各异（`after` ≠ `after_section`）/ `scope=1` 是"第 1 次出现"不是"第一行" / `lenient=true` 只放空白不归一引号——这些参数误用都已在 `references/pitfalls.md`（反模式 8/9/10/13/14）和 `references/anchors-and-positions.md` 详细对照，写 ops 前**没把握就翻一下**。

---

## 另外两个稳定套路

> 编辑既有文档（最常见）的完整套路就是上面的端到端示例（put → read outline → edit →
> get → pin），不再重复。下面只列"造新"和"套模板"两条它没覆盖的流程。

### 套路 B：从零生成 + 后续微调

```
1. word-cli create --markdown-file /workspace/draft.md --output /workspace/draft.docx
   或 word-cli create --markdown "..." --output /workspace/draft.docx
   或 word-cli create --content '{"sections":[...]}' --output /workspace/draft.docx
2. word-cli edit --input /workspace/draft.docx --output /workspace/draft2.docx --ops '[...]'
3. word-cli validate --repair
4. sandbox_get_artifact(src_path="/workspace/draft2.docx", name="<合适的文件名>.docx")  → 新 file_id
5. pin_to_workspace(file_ids=["<新 file_id>"])
```

### 套路 C：套模板

```
1. sandbox_put_artifact(artifact_id="<源 file_id>",   dest_path="/workspace/source.docx")
   sandbox_put_artifact(artifact_id="<模板 file_id>", dest_path="/workspace/template.docx")
2. word-cli read --mode analyze --input /workspace/source.docx   # 看一下样式名能不能对上
   word-cli read --mode analyze --input /workspace/template.docx
3. word-cli template --source /workspace/source.docx --template /workspace/template.docx \\
                     --output /workspace/applied.docx
4. word-cli validate --repair --input /workspace/applied.docx --output /workspace/applied.fixed.docx
   # template 之后偶尔有 XSD 警告，必做
5. （可选）word-cli edit + format op 收拾源文档残留的直接格式
6. sandbox_get_artifact(src_path="/workspace/applied.fixed.docx", name="套版终稿.docx") → 新 file_id
7. pin_to_workspace(file_ids=["<新 file_id>"])
```

---

## 反模式速查

最常栽的坑——多次 `word-cli edit` 拆调、不读 outline 猜 paragraph_index、用 `\n` 表示段落、`word-cli template` 调字号、连续 `delete_paragraph` 删整章、ops 数组混用 int delete + int insert、markdown 当 text 塞给 `insert`、每次编辑都跑 `analyze` 而非 `outline`、`create --markdown` 收路径当正文——都在 `references/pitfalls.md` 整理成 21 条「症状 → 根因 → 正确做法」红绿对照。写 ops 卡住或行为反直觉时**先查它**。

---

## 一句话自检（交付前）

每次回复用户之前，按这个列表 5 秒过一遍。任何一项没满足 = 这一轮**没交付**，
回去补，不要发：

- 用户要的是 **.docx**，我交付的也是 .docx 吗？（不是偷偷换成了 .pptx / .pdf /
  「贴在回答里的 markdown 文字让用户自己复制」？）
- 是不是用 `word-cli create`（从零）或 `word-cli edit`（改既有）真的产出过
  `/workspace/<...>.docx` 这个文件？（没跑过这条命令 = 没有 .docx，必返工）
- 中途如果改过 markdown 草稿 / `--ops` / 填表 dict 这类中间稿，**改完之后有没有
  再跑一次 `word-cli` 把最新版 .docx 重新生成出来**？（停在改完中间稿 = 没交付）
- `sandbox_get_artifact(src_path="/workspace/<...>.docx", name="...")` 跑了，
  拿到了 `file_id` 了吗？
- **`pin_to_workspace(file_ids=["<file_id>"])` 调了吗？**
  —— 这一步不可省：没 pin，文件只是后端 artifact 存储里的一条匿名记录，
  用户在对话区 / 我的空间里**完全看不到**，等于这一轮任务从没存在。
- 用户同时要 .docx + .pdf 时：.docx 和 .pdf 两个 file_id 都进同一次
  `pin_to_workspace(file_ids=[...])` 调用了吗？（一次 pin 多个，别多次 pin）
- 用户指定了"放到我的空间里 XX 文件夹"：`name=` 参数填了贴切的中文文件名
  吗？（不是 `out.docx` / `draft.docx` 这种）

---

## 何时跳出本 skill 走 raw XML

只在以下情况：
1. 你已经看过 `references/cli-commands.md` 和 `references/apply-edits-ops.md`，确认 7 个子命令 + 15 个 op 都覆盖不了
2. 用户需求是真的奇葩：旋转页眉里某张图 30°、给图片加 alt text 翻译、修改某个 contentControl 的绑定……

那就用 `references/raw-xml-escape.md` 的 unpack/edit/pack 流程（bash + LibreOffice）。这条路**慢、token 重、易出错**，能走 op 就别走它。

---

## 深潜引用（按需读）

- `references/cli-commands.md` — 7 个子命令完整卡片：CLI 参数 / 返回 JSON / 何时用 / 何时绝不用 / 典型误用
- `references/apply-edits-ops.md` — `edit` 子命令 15 个 op 详解：参数清单 + 边界 + 正反例
- `references/anchors-and-positions.md` — anchor / position / scope 系统深潜
- `references/workflows.md` — 端到端配方（合同填模板、政策文重写一节、套模板出公文、审稿模式）
- `references/pitfalls.md` — Top 20 反模式扩展版（症状 → 根因 → 正确做法）
- `references/raw-xml-escape.md` — 7 子命令覆盖不到时的兜底（unpack/edit/pack）

## 依赖与运行环境

- **Python**: 通过 skill 自带的 `scripts/engine/` 引擎包调用（python-docx 等），CLI 自带 `sys.path` 注入
- **.NET 8 runtime**: `template` / `validate` / `diff` 子命令 + `read --mode analyze` 需要。**opensandbox 容器**（即 LLM `bash` 工具的执行环境）的 Dockerfile 已装；mcp / script-runner 容器同步装
- **LibreOffice (soffice)**: `convert` 子命令需要（.doc→.docx 和 .docx→PDF 都走 LibreOffice headless）
- **环境变量**:
  - `MINIMAX_DOCX_BIN`（默认 `/opt/minimax-docx/MiniMaxAIDocx.Cli.dll`）
  - `MINIMAX_DOCX_ASSETS`（默认 `/opt/minimax-docx/assets`，含 XSD schema）
- **运行时位置**: skill 目录由 `register_bash` 在 sandbox 启动时物化到 `/workspace/skills/word-editing/`；`word-cli` shim 通过 4 路径回退（先查 `/workspace`，再查 `$SKILL_DIR`，再查 `/app/src/backend/...`，最后查 shim 自身所在目录的兄弟文件）找到 `cli.py`，无需 LLM 关心 SKILL_DIR 是什么

