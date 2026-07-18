# word-cli 7 个子命令完整参考

本文件给 `word-cli` 7 个子命令各一张「卡片」：CLI 参数 / stdout JSON 结构 / 何时用 / 何时绝不用 / 典型误用。

每个子命令的通用约定：
- 输入 / 输出参数永远是**绝对路径**（CLI 不认 file_id）
- stdout 永远是单条 JSON（`{"ok": true|false, ...}`）
- exit 0 = 成功 / 1 = 业务失败（JSON 仍输出，含 `error` 字段）/ 2 = 参数错（argparse 抛错或字段校验失败）
- 调用形式：`word-cli <subcmd> --arg1 ... --arg2 ...`（运维 symlink 之后可简化为 `word-cli ...`）

总览：

```
word-cli help                  # 子命令清单
word-cli <subcmd> --help       # 单个子命令的详细参数
```

---

## word-cli read

**做什么**：只读检查一个 .docx——文字 / 标题树 / 占位符 / 完整盘点。不产生新文件。

**何时用**：编辑任何既有文档**之前**，作为第一步。也用于"用户问这文档讲了啥/有哪些占位符要填"。

**何时绝不用**：要改动文档时——这是只读工具。

```bash
word-cli read --mode <text|outline|placeholders|analyze> --input <abs-path-to-docx>
```

**4 个模式的取舍**：

| --mode | 干什么 | 开销 | 何时用 |
|---|---|---|---|
| `text` | 返回全文文字（可 `--paragraph-range 0,50` 切片） | 中 | 要看正文内容 / 引用原文 |
| `outline` | 返回标题树（`[{"level": N, "text": ..., "paragraph_index": N}, ...]`） | 低 | **编辑前的必选第一步**——拿锚点 |
| `placeholders` | 列出文档里所有 `{{xxx}}` 占位符 | 低 | 填模板之前列 key |
| `analyze` | 完整盘点（节数、表、图、自定义样式、字数、内部 XML 大小） | 较高（走 .NET CLI） | 套模板前侦察样式名能否对得上 |

**关键参数**：
- `--paragraph-range START,END` — text 模式专用，0-based 半开区间，避免拉回过长全文。
- `--pattern <regex>` — placeholders 模式，默认 `\{\{(\w+)\}\}`，可换 `<<(\w+)>>` 等。

**返回（成功）**：
```json
{"ok": true, "meta": {
  // text 模式：
  "paragraph_count": 123, "selected_range": [0, 50], "text": "全文..."
  // outline 模式：
  "heading_count": 7, "outline": [{"level": 1, "text": "...", "paragraph_index": 0}, ...]
  // placeholders 模式：
  "placeholders": ["name", "date"], "counts": {"name": 3, "date": 1}, "total_occurrences": 4
  // analyze 模式：
  "report": {"sections": [...], "headings": [...], "tables": [...], "images": [...], "wordCount": ...}
}}
```

**典型误用**：
- ❌ 编辑前用 `analyze`（重）只是为了拿 heading 锚点——改用 `outline`（轻）。
- ❌ 用 `text` 模式拉全文然后正则找占位符——改用 `placeholders` 模式（已内置正则）。
- ❌ 不指定 `--paragraph-range` 拉超大文档的全文——会产生 100KB+ stdout，浪费 token。

---

## word-cli create

**做什么**：从零生成 .docx。两种引擎：

- **markdown 模式**（python-docx，快，公文字体）：扁平 markdown → 单流文档。**这是默认路径**，覆盖 90% 的"写一份 X"需求。
- **content 模式**（.NET 结构化，功能全）：仅在用户**显式**要求 → 目录 / 封面 / 页眉 / 页脚 / 页码 / 多节布局 / 非默认页面尺寸或边距（A3、横排、narrow/wide）/ 非 report 版式（letter/memo/academic）。

**默认就走 markdown**。markdown 模式自带公文版式（仿宋正文 + 小标宋标题 + 首行缩进 2 字 + 1.5 倍行距 + 两端对齐），还支持 `#`/`##`/`###` 标题、`-`/`1.` 列表、`| a | b |` 表格、行内 `**bold**`/`*italic*`/链接、` ``` ` 代码块和图片 `![](path)`。"图文并茂 / 含表格 / 字数多 / 公文风格"这类要求不构成升级到 content 的理由。只有用户原话出现"目录 / TOC / 封面 / 页眉 / 页脚 / 页码 / A3 / 横排 / 分节"等关键词时才升级。

**何时用**：用户**没有上传 docx**，只给文字/数据要求生成新文档（报告 / 通知 / 公文 / 方案 / 计划）。

**何时绝不用**：
- 用户**已经上传 docx**要修改 → 用 `word-cli edit`（直接 create 会丢失用户文档的全部内容和格式，**严重错误**）。
- 想把别人的格式套到自己内容上 → 先 create 出内容稿，再用 `word-cli template` 套样式。
- 模板里有 `{{占位符}}`要填 → 不需要 create，直接对模板用 `word-cli edit` 的 `fill_placeholders` op。

```bash
# markdown 模式（最常见）—— 推荐 --markdown-file 直接读文件
word-cli create --markdown-file /workspace/draft.md --output /workspace/out.docx \
    [--title "..."] [--language zh|en]

# 或者把 markdown 文本直接内联（注意必须真的传文本，不是路径）
word-cli create --markdown "$(cat /workspace/draft.md)" --output /workspace/out.docx

# content 模式（要封面/目录/多节）
word-cli create --content '{"sections":[{"heading":"一、概述","level":1,"paragraphs":["..."]}]}' \
    --output /workspace/out.docx \
    [--title "..."] [--author "..."] \
    [--type report|letter|memo|academic] \
    [--page-size a4|letter|legal|a3] [--margins standard|narrow|wide] \
    [--header "..."] [--footer "..."] [--page-numbers] [--toc]
```

**markdown 模式特性**：
- 自动应用公文版式：每段首行缩进 2 字符 + 1.5 倍行距 + 两端对齐
- 标题用方正小标宋简体，正文用方正仿宋简体（zh）；en 回退 Calibri
- 自动支持 `#`/`##`/`###` 标题、`-`/`1.` 列表、` ``` ` 代码块、`| a | b |` 表格、行内 `**bold**`/`*italic*`/` `code` `/`[文本](url)`
- 两种入口可选：`--markdown-file <path>`（脚本读 UTF-8 文件内容）/ `--markdown <text>`（字面文本，长文本建议 `$(cat ...)`）。**不能**把路径传给 `--markdown` —— 会被当成正文一行渲染进文档（脚本现在会 hard fail 拦截，提示改用 `--markdown-file`）

**markdown 模式的硬限制**：
- 不支持 `--toc` / `--header` / `--footer` / `--page-numbers` / `--author` / 非默认 `--page-size`/`--margins`/`--type` —— 传了直接报错引导改用 `--content`。
- 不支持多节布局。

**content 模式 sections 结构**：
```json
{"sections": [
  {"heading": "一、总体要求", "level": 1,
   "paragraphs": ["第一段...", "第二段..."],
   "items": ["要点 1", "要点 2"], "list_style": "bullet" | "numbered",
   "table": {"headers": ["项目","金额"], "rows": [["A","100"]]},
   "sections": [{"heading": "1.1 ...", "level": 2, "paragraphs": [...]}]
  }
]}
```

**`--toc` 行为**：注入的是**预填充的静态目录**（"目录" 这一节里写满所有 heading），用户打开 Word 即见，无需点"更新域"。代价：用户后续手改标题，TOC 不会自动跟更新。

**典型误用**：
- ❌ markdown 模式想加目录 → 报错。改用 content 模式 + `--toc`。
- ❌ content 模式同时手写一个 `heading: "目录"` 的 section 又传 `--toc` → 文档出现两个目录。`--toc` 时 sections 只放正文。
- ❌ 用户已上传 docx，但你跑 `word-cli create` 重新生成 → 用户内容全丢。**第一步永远是 `word-cli read`**。

---

## word-cli edit

**做什么**：在 1 次开关文档里跑 1..N 个编辑 op。这是改既有 docx 的**主入口**，吸收了过去的 `replace_text` / `insert_text` / `add_table` / `format_text` / `replace_many` / `fill_placeholders`。

**何时用**：用户上传的 docx 需要任何修改（替换、插入、删除、格式调整、套表、改元数据）。

**何时绝不用**：
- 想套别人模板的样式 → `word-cli template`
- 只想看内容 → `word-cli read`
- 大改完想校验 → `word-cli validate`

```bash
word-cli edit --input <in.docx> --output <out.docx> \
  --ops '[{"op":"replace","find":"X","replace":"Y"}, {"op":"format","anchor":"标题","bold":true}]' \
  [--ops-file ops.json] \
  [--stop-on-error] \
  [--image local_id=/abs/path.png ...]
```

**支持的 15 个 op**：见 `apply-edits-ops.md`。这里只列名字：
- 文本类：`replace` / `replace_many` / `fill_placeholders`
- 插入类：`insert` / `insert_image` / `add_table`
- 重写类：`replace_paragraph` / `replace_section`
- 删除类：`delete_paragraph` / `delete_range`
- 表格类：`set_cell_text` / `fill_table` / `move_table`
- 格式类：`format`
- 元数据：`update_field`

**关键参数**：
- `--ops '[...]'` 或 `--ops-file ops.json`：二选一，不能都不给也不能都给。
- `--stop-on-error`：默认 False（失败的 op 跳过继续跑，输出里 results[i].ok=false）；传 True 时遇到失败立刻停（前面成功的 op 仍保留在输出文件里）。
- `--image local_id=/abs/path`：仅 `insert_image` op 用。`local_id` 是你给的标识，在 op 的 `image_file_id` 字段里引用；脚本会把图片拷进 workdir。可重复传多次。

**返回**：
```json
{"ok": true, "meta": {
  "output": "/workspace/out.docx",
  "ops_total": 3, "ops_succeeded": 3, "ops_failed": 0,
  "results": [
    {"index": 0, "op": "replace", "ok": true, "replacements": 2, ...},
    ...
  ]
}}
```

**典型误用**：
- ❌ 把一组改动拆成多次 `word-cli edit` 调用 → 每次都要 sandbox_get+put，浪费往返。一次 ops 数组打包就完了。
- ❌ ops 数组里 `[delete_paragraph(int=10), insert(int=11)]` → delete 后段落数 -1，insert 锚错位。改用字符串 anchor。
- ❌ `insert` 的 text 写 `"第一段\n第二段"` → 不会分段。改用多个 insert op，或 `replace_section`，或 `insert` 加 `"format":"markdown"`。

---

## word-cli template

**做什么**：把模板 docx 的**格式**（styles.xml / theme / numbering.xml / 页面设置 / 可选页眉页脚）拷到源 docx 上，**保留**源的内容（文字、表格）。

**何时用**：用户说"按这个模板套样式"/"把这份内容用那份模板的格式重排"/"按 X 改写"。

**何时绝不用**：
- 只想调几个字号或字体 → `word-cli edit` + format op（template 是文档级换皮，副作用过大）
- 想填模板里 {{xxx}} 占位符 → `word-cli edit` + fill_placeholders op（template 是改格式，不动文字）
- 模板里有示例内容你想替换 → 先 `word-cli template` 套格式，再 `word-cli edit` 改文字

```bash
word-cli template --source <content donor.docx> --template <style donor.docx> --output <out.docx> \
  [--no-apply-styles] [--no-apply-theme] [--no-apply-numbering] [--no-apply-sections] \
  [--apply-headers-footers]
```

**默认行为**：copy styles + theme + numbering + sections。**不 copy headers/footers**——多节模板复杂时容易出意外，要求显式 opt-in。

**关键约定**：
- `--source` = 想**保留内容**的 docx
- `--template` = 想**复制格式**的 docx
- 套完之后强烈建议 `word-cli validate --repair`（XSD 偶尔报警）

**返回**：
```json
{"ok": true, "meta": {
  "output": "/workspace/out.docx",
  "applied": ["styles", "theme", "numbering", "sections"]
}}
```

**典型误用**：
- ❌ 想换字号跑 `word-cli template` → 副作用太大，会改全局。改用 `word-cli edit` format op。
- ❌ 套完没 validate → 可能 Word 打开报警告。
- ❌ source / template 颠倒 → 套完发现内容变成模板里的。

---

## word-cli validate

**做什么**：XSD 结构校验 + 业务规则校验 + 可选 gate-check（对照参考模板的结构）。加 `--repair` 时跑 merge-runs → fix-order → 再校验，产出修复后的文件。

**何时用**：
- 任何大改之后（特别是 `word-cli template` 之后）
- 用户说"打开 Word 报警告"
- 要交付前的最后一次 sanity check

**何时绝不用**：
- 小改一两处文本——开销不值。

```bash
word-cli validate --input <in.docx> [--no-xsd-check] [--no-business-rules] [--gate-check <ref.docx>]
word-cli validate --input <in.docx> --repair --output <fixed.docx>
```

**模式**：
- 无 `--repair`：只校验，不产新文件。返回 `is_valid` / `errors[]` / `warnings[]`。
- 有 `--repair`：跑修复 pipeline（merge-runs 合并相邻同格式 run / fix-order 按 ISO 29500 重排子元素），产出新文件。

**`--gate-check`**：对比文档结构（样式名、节数、heading 数、页眉页脚有无）与参考模板的差异。返回 `gate_check.violations[]`。

**返回**（带 --repair 时）：
```json
{"ok": true, "meta": {
  "output": "/workspace/fixed.docx",
  "repairs_applied": ["merge-runs", "fix-order"],
  "is_valid": true,
  "errors": [], "warnings": [],
  "exit_code": 0
}}
```

**典型误用**：
- ❌ 每次小改都跑 validate → 浪费时间。攒一批改动后再跑。
- ❌ `--repair` 不给 `--output` → 报错（修复模式必须有输出路径）。
- ⚠️ `--xsd-check` 在某些 docx 上会撞到 .NET CLI 的 XSD schema namespace bug（`XmlSchemaValidationException`）。这是 minimax-docx 既有问题，不是 CLI 引入的。绕开：加 `--no-xsd-check`，只跑 business rules。

---

## word-cli diff

**做什么**：比较两个 .docx，返回段落级的 text / style / structure changes。

**何时用**：
- 验证 `word-cli template` / 批量改动**没有意外改写**正文
- 用户问"这两个版本差在哪"
- 法律 / 合规审计

**何时绝不用**：
- 要看正文细节 → 用 `word-cli read --mode text` 各跑一次再走外部 diff
- 想做字符级对比 → 这个工具只到段落级

```bash
word-cli diff --before <original.docx> --after <edited.docx>
```

**返回**：
```json
{"ok": true, "meta": {"diff": {
  "textChanges": [...],
  "styleChanges": {"added": [...], "removed": [...]},
  "structureChanges": [...],
  "summary": "..."
}}}
```

---

## word-cli convert

**做什么**：用 LibreOffice headless 跑格式转换。

**两个方向**：
- `--to docx`：把 .doc / .rtf / .odt 转成 .docx（用户传 .doc 时的第一步）
- `--to pdf`：把 .docx 渲染成 PDF（交付前的最后一步）

```bash
word-cli convert --to docx --input old.doc --output new.docx
word-cli convert --to pdf  --input draft.docx --output draft.pdf
```

**何时绝不用**：
- 其他 word-cli 子命令之间互转——`edit` 等产出的已经是合法 .docx，再 convert 一次是浪费

**返回**（pdf 模式还会返回 pages）：
```json
{"ok": true, "meta": {"output": "/workspace/draft.pdf", "format": "pdf", "pages": 12}}
```

**典型误用**：
- ❌ 把 .doc 直接交给 `word-cli edit` → python-docx 不认 .doc 老格式，要先 `word-cli convert --to docx`。
- ❌ pdf 模式之后还想继续编辑文字 → PDF 是终态，要回到 docx 改。

---

## 不在 7 个子命令里的功能

| 老 MCP 工具 | 现在的写法 |
|---|---|
| `word_replace_text` | `word-cli edit` + `{"op":"replace", ...}` |
| `word_insert_text` | `word-cli edit` + `{"op":"insert", ...}` |
| `word_add_table` | `word-cli edit` + `{"op":"add_table", ...}` |
| `word_format_text` | `word-cli edit` + `{"op":"format", ...}` |
| `word_replace_many` | `word-cli edit` + `{"op":"replace_many", ...}` 或多个 `{"op":"replace"}` |
| `word_fill_placeholders` | `word-cli edit` + `{"op":"fill_placeholders", ...}` |
| `word_save_document` | **删除**——所有 op 已自动落盘成新文件 |
| `word_export_pdf` | `word-cli convert --to pdf` |
| `word_doc_to_docx` | `word-cli convert --to docx` |
| `word_get_document_text` | `word-cli read --mode text` |
| `word_get_document_outline` | `word-cli read --mode outline` |
| `word_list_placeholders` | `word-cli read --mode placeholders` |
| `word_analyze` | `word-cli read --mode analyze` |
| `word_create` / `word_create_from_markdown` | `word-cli create --markdown ...` |
| `word_apply_template` | `word-cli template` |
| `word_validate` | `word-cli validate` |
| `word_diff` | `word-cli diff` |

如果你以前会调老 MCP 工具，现在都走 `word-cli` 的对应子命令。
