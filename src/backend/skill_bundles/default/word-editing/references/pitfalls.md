# Top 21 反模式

每条都是「症状 → 根因 → 正确做法」。这些坑都来自真实使用——不是假想。

> 该文件目的是**当模型疑似要犯错时**给出明确的红绿对照。看到自己要走某条 ❌ 路径，立刻回到 ✅。

## 工具选型类

### 反模式 1：把一次编辑拆成多次 `word-cli edit` 调用
- **症状**：3 处不同改动跑了 3 次 `word-cli edit`，每次都伴随 `sandbox_put_artifact` + `sandbox_get_artifact`
- **根因**：以为每个 op 必须独立一次工具调用
- **✅ 做法**：所有改动放进**同一个** `--ops` 数组，一次跑完
- **代价**：3 × (put+bash+get) = 9 个工具调用 vs 1 × 3 = 3 个，多花约 6 次 LLM 轮次

### 反模式 2：用户上传 docx 后跑 `word-cli create` 重新生成
- **症状**：用户上传 `policy.docx` 说"改一改 XX 部分"，结果跑 `word-cli create --markdown ...` 生成新文档
- **根因**：把"改"误解为"重写"
- **✅ 做法**：先 `word-cli read --mode text` 看原文 → `word-cli edit` 改 / `word-cli template` 套样式 / `fill_placeholders` 填模板
- **代价**：用户的内容和格式全丢；输出的是工具默认公文字体，跟用户想要的完全不是一回事——**高优先级错误**

### 反模式 3：用 `word-cli template` 调字号
- **症状**：用户说"把正文字号统一改成小四"，跑 `word-cli template`
- **根因**：以为 template 是"统一格式"工具
- **✅ 做法**：`word-cli edit` + `format` op（`style_filter="!Heading"`, `font_size=12`）
- **代价**：template 是文档级换皮（拷 styles.xml + theme + numbering），改一个字号副作用过大，可能丢失现有特殊格式

### 反模式 4：删完整章用 delete_paragraph × N
- **症状**：outline 显示"附录 A"占第 80-95 段，跑 16 个 `delete_paragraph` op
- **根因**：没意识到有 `delete_range` 这个 op
- **✅ 做法**：`word-cli edit` + 一个 `delete_range`（start_anchor="附录 A", end_anchor="附录 B"）
- **代价**：连续 delete 容易在表格行上出问题；ops 数组冗长难读

### 反模式 5：`word-cli read --mode analyze 当 outline 用
- **症状**：每次编辑前都跑 `word-cli read --mode analyze`（重，走 .NET CLI）
- **根因**：以为 analyze 是"读结构"通用工具
- **✅ 做法**：日常编辑前用 `--mode outline`（轻，只返回标题树）；要 inspect 表格 / 图片 / 自定义样式时才用 `analyze`
- **代价**：浪费时间和资源；analyze 输出几千 token，污染上下文

### 反模式 6：`word-cli edit` 完了又跑 `word-cli convert --to docx`
- **症状**：编辑后输出 .docx，又跑 `word-cli convert --to docx --input out.docx`
- **根因**：以为 LibreOffice 转换能"修复"什么
- **✅ 做法**：`word-cli edit` 产出的就是合法 .docx，直接落 artifact；想清洁化用 `word-cli validate --repair`
- **代价**：浪费一次 LibreOffice subprocess（慢），还可能丢失某些格式细节

### 反模式 7：以为 `update_field` op 能改任意元数据
- **症状**：传 `{"op":"update_field", "field":"COMPANY", "value":"..."}`，报错
- **根因**：以为 update_field 支持所有 docProps
- **✅ 做法**：只支持 `TITLE / AUTHOR / SUBJECT / KEYWORDS / DESCRIPTION / CATEGORY` 6 个 core docprops。其他字段（Company / Manager / ContentStatus）目前不支持

---

## 参数错配类

### 反模式 8：style 名漏空格
- **症状**：`{"op":"insert", "text":"...", "style":"Heading1"}` 跑完发现"看起来啥都没变"
- **根因**：python-docx 用的样式名是 Word 内置全名，带空格
- **✅ 做法**：`"Heading 1"`（带空格），不是 `"Heading1"` / `"H1"` / `"heading1"`
- **代价**：样式名匹不到时**静默回退到 Normal**——不报错，但效果是没改

### 反模式 9：用 paragraph_index 不读 outline
- **症状**：`{"op":"format", "paragraph_index": 12, "bold": true}`，命中错段
- **根因**：凭记忆/对话上下文猜段号
- **✅ 做法**：先 `word-cli read --mode outline`，从返回里**复制**段落文本作为字符串 anchor
- **代价**：误改其他段；用户得手动撤销

### 反模式 10：ops 数组里混用 int anchor delete + int anchor insert
- **症状**：`[{"op":"delete_paragraph","anchor":10}, {"op":"insert","anchor":11,"text":"..."}]`，发现 insert 进了错位置
- **根因**：delete 后段落数 -1，原 11 段已经变成 10 段
- **✅ 做法**：全用字符串 anchor。或：如果一定要用 int，确保 ops 之间不发生段落数变化
- **代价**：典型的"我以为在改 X，其实改的是 Y"

### 反模式 11：把 markdown 当 text 塞给 insert
- **症状**：`{"op":"insert", "text":"# 标题\n\n- 要点1\n- 要点2"}`，文档里看到字面的 `#` 和 `-`
- **根因**：默认 `format="text"` 不解析 Markdown
- **✅ 做法**：`{"op":"insert", "format":"markdown", "text":"..."}`；或用 `replace_section`（new_content 按 \n 分段但不解析 markdown 标记）
- **代价**：用户看到一堆 `#` 字符；要重做

### 反模式 12：用 `\n` 表示段落分隔（在 insert text 模式里）
- **症状**：`{"op":"insert", "text":"第一段\n第二段\n第三段"}`，PDF 里发现都挤在一段
- **根因**：text 模式的 `\n` 是软换行，不是段落分隔
- **✅ 做法**：
  - 拆 3 个 `insert` op
  - 或 `insert` 用 `format="markdown"` + 段间空行（`第一段\n\n第二段`）
  - 或用 `replace_section`（`new_content` 按 `\n` 自动分段）
- **代价**：PDF 渲染丑陋甚至乱码

### 反模式 13：scope=1 当"第一行"用
- **症状**：`{"op":"replace", "find":"公司", "replace":"示例科技", "scope": 1}`，发现改到了第 5 段的"公司"
- **根因**：scope 是"第 N 次出现"，不是"第 N 行/段"
- **✅ 做法**：要改特定段的特定字串，用 `replace_paragraph` + anchor 锁段；想换"第一次出现"用 `"first"`
- **代价**：改错位置

### 反模式 14：lenient=true 期望它处理弯引号 / 半角全角
- **症状**：`{"op":"replace", "find":"\"示例\"", "replace":"...", "lenient": true}`，0 命中
- **根因**：lenient 只做空白容忍，不归一化标点
- **✅ 做法**：先 `word-cli read --mode text` 看原文用的是直引号还是弯引号（`"` 或 `"`/`"`），find 字段精确照搬
- **代价**：替换 0 命中，浪费一次工具往返

### 反模式 15：`format` op 期望改文字内容
- **症状**：`{"op":"format", "anchor":"旧值", "color_hex":"C00000"}` 期望"把旧值改成红色的新值"
- **根因**：format 只改样式，不改文字
- **✅ 做法**：拆 2 个 op：先 `replace`（或 `replace_paragraph`）改文字，再 `format` 改样式
- **代价**：一次性想做两件事，结果只做了一件

### 反模式 16：`style_filter="Normal"` 期望覆盖所有正文
- **症状**：跑完发现部分正文段没被改到
- **根因**：真实文档常混用 Normal / Body Text / FirstParagraph 等多个 body style
- **✅ 做法**：用 `style_filter="!Heading"`（匹所有非标题段）
- **代价**：版式不统一；要再跑一次

### 反模式 17：add_table 漏写 position
- **症状**：`word-cli edit` 报 `ValueError: position is required`
- **根因**：add_table op 没有默认 position，必须显式传
- **✅ 做法**：永远显式写 `"position": "after_section"`（或其他合适值）+ `"anchor"`

### 反模式 21：把 markdown 文件路径直接传给 `--markdown`
- **症状**：`word-cli create --markdown /workspace/draft.md --output /workspace/out.docx` 返回 `{"ok": true, "size_bytes": ~9800}`，看着像成功；但用户打开 docx 只见一个标题"报告"和一行 `/workspace/draft.md` 的字面字符串
- **根因**：`--markdown` 收的是**字面 markdown 文本**，不是文件路径。把路径当文本传，整篇 docx 就是这一行
- **✅ 做法**：用新加的 `--markdown-file <path>`（脚本读 UTF-8 文件内容）。或者退而用 `--markdown "$(cat path.md)"`（shell 提前展开成文本）
  - ✅ `word-cli create --markdown-file /workspace/draft.md --output /workspace/out.docx`
  - ✅ `word-cli create --markdown "$(cat /workspace/draft.md)" --output /workspace/out.docx`
  - ❌ `word-cli create --markdown /workspace/draft.md --output /workspace/out.docx`（脚本现已 hard fail 拦截，错误类型 `MarkdownLooksLikePath`）
- **代价**：脚本以为完成、artifact pin 上去了，用户拿到的 .docx 只有"路径文本"一行，等于这一轮交付完全废掉

---

## 工作流类

### 反模式 18：忘了 outline → 跳过决策依据
- **症状**：`word-cli edit` 跑完，发现 anchor 不存在或匹错段
- **根因**：跳过 outline 直接写 ops
- **✅ 做法**：编辑既有文档的**第一步永远是** `word-cli read --mode outline`。outline 是 1-2KB 的 JSON，廉价，必做

### 反模式 19：`word-cli template` 后不 validate
- **症状**：用户打开输出文档报"文件中存在错误"或某些样式不渲染
- **根因**：template 之后偶尔残留 XSD 不合规（durableId 越界 / `xml:space` 漏标）
- **✅ 做法**：`word-cli template` 后**强制** `word-cli validate --repair` 一次
- **代价**：用户体验差，可能丢用户信任

### 反模式 20：PDF 出了之后又想改文字
- **症状**：跑了 `word-cli convert --to pdf` → 用户说"再加一段说明"，又想编辑 PDF
- **根因**：PDF 是终态格式
- **✅ 做法**：始终保留 .docx 为"可编辑产物"，PDF 只在交付时生成。要改回 docx 改，再转 PDF
- **代价**：要么放弃改动，要么找 OCR 工具反向，都不划算

---

## 何时**不要**用本 skill

记住：skill 只覆盖 Word/.docx 文件。以下场景请用其他工具：

| 场景 | 用什么 |
|---|---|
| .pptx 演示文稿 | `pptx` 或 `ppt-design` skill |
| .xlsx 电子表格 | `xlsx` skill |
| .pdf 文档（创建/编辑/拼合） | `pdf` skill |
| Markdown 文件操作 | 不需要 skill；直接编辑 |
| Google Docs | 飞书 / 谷歌的对应 skill |
| 飞书文档（docx token） | `lark-doc` skill |

误用本 skill 的最常见情形：用户说"这份 Word 转 PDF"，工具列表里同时有 `pdf` skill 和本 skill——选**本 skill** 的 `word-cli convert --to pdf`（因为源是 .docx）。但用户说"合并这两个 PDF"——选 `pdf` skill。
