# Raw XML 兜底 —— 7 个脚本覆盖不到时

**先停一下，确认你真的需要走这条路**。

> 看完 `scripts-map.md`、`apply-edits-ops.md` 全部 7 个脚本 + 15 个 op，**确认没有任何组合能完成需求**，再继续读下面。

走 raw XML 的代价：
- 慢——LLM 要读完整 document.xml（可能数十 KB）+ 自己写 XML 字符串 + 用 bash 调 zip/unzip
- 易出错——任何 namespace / 元素顺序的小错都会让 Word 打开报警告
- 不可读——后续维护者看不懂为什么有这一段
- 浪费 token——XML 操作是高 token 的活儿

**90% 你以为需要 raw XML 的需求其实有 op 覆盖**。先回去翻 `apply-edits-ops.md` 一遍。

---

## 真正需要 raw XML 的场景

这些是我们**确认** 7 脚本不覆盖的：

- 旋转 / 透明度 / 自定义颜色：页眉里某张图旋转 30°、加阴影、改透明度
- 给图片加 / 改 alt text（用于翻译、可访问性）
- 修改 Content Controls（结构化文档标签）的绑定 / 占位文本
- 自定义 XML parts（CustomXml）添加 / 修改 / 删除
- Theme1.xml 里某个 accent color 改成特定值（apply_template 是整套换）
- 修订（Track Changes）的接受 / 拒绝——我们的 op 不支持，要走 XML
- 批注（Comment）的添加 / 删除 / 回复——我们的 op 不支持，要走 XML
- 域代码（Field codes）：定制 IF / ASK / MERGEFIELD 这类高级域
- 数学公式（OMML）的插入

如果你不知道你的需求落在哪里——**回去翻 `apply-edits-ops.md`**。`update_field` 是改 docProps；`add_table` 加普通表格；`insert_image` 加普通图片。这些都不是 raw XML 范畴。

---

## unpack → 编辑 → pack 工作流

### 三步走

```bash
# 步骤 1：解包 .docx 成目录
SANDBOX=/sandbox
ORIG="$SANDBOX/input.docx"
UNPACK="$SANDBOX/unpacked"
mkdir -p "$UNPACK"
cd "$UNPACK" && unzip -o "$ORIG"

# 步骤 2：编辑 word/document.xml（或其他 part）
# 用 Edit 工具直接改 XML 字符串。不要写 Python 脚本——一次性操作不值得。

# 步骤 3：重新打包成 .docx
# 关键：zip 必须保持原结构。先 [Content_Types].xml，再其他文件。
cd "$UNPACK"
zip -X -r "$SANDBOX/output.docx" "[Content_Types].xml" _rels word docProps customXml \
  -i '*' 2>/dev/null || true
# 上一行可能漏掉某些 zip 内部约定——如果 Word 打不开输出文件，改用 LibreOffice 重新打包：
# soffice --headless --convert-to docx "$SANDBOX/output.docx" --outdir "$SANDBOX/cleaned"
```

更推荐：用 LibreOffice 走一遍清洁化（自动修复 XSD 不合规）：

```bash
# 改完 XML 后，先粗暴 zip，再 LibreOffice 跑一遍当 "validator + repacker"
cd "$UNPACK" && zip -X -r "$SANDBOX/raw.docx" . -x ".*"
word-cli validate \
  --input "$SANDBOX/raw.docx" --repair --output "$SANDBOX/output.docx"
# word-cli validate 的 --repair 会跑 merge-runs + fix-order，修复多数 XSD 警告
```

### document.xml 必读约定

- **元素顺序**：在 `<w:pPr>` 里，必须 `<w:pStyle>` → `<w:numPr>` → `<w:spacing>` → `<w:ind>` → `<w:jc>` → `<w:rPr>`（最后）
- **空白保留**：`<w:t>` 包含前导/尾随空格时，**必须**加 `xml:space="preserve"`，否则 Word 渲染时空格会丢
- **RSID**：`w:rsidR` 等属性必须是 8 位 hex（`00AB1234`），不是任意字符串
- **runs vs paragraphs**：`<w:r>`（run）必须在 `<w:p>`（paragraph）内；不能直接放 body 里
- **namespace 不要乱删**：document.xml 头部的 `xmlns:w=...` 等声明动一个就废一片

### 修订（Track Changes）的 XML 模板

我们的 op 不支持 track changes，但 raw XML 可以加。author **统一用 "Claude"** 除非用户明说要别的：

```xml
<!-- 插入修订 -->
<w:ins w:id="1" w:author="Claude" w:date="2026-05-20T00:00:00Z">
  <w:r><w:t>新增的文字</w:t></w:r>
</w:ins>

<!-- 删除修订（注意 w:t → w:delText） -->
<w:del w:id="2" w:author="Claude" w:date="2026-05-20T00:00:00Z">
  <w:r><w:delText>被删除的文字</w:delText></w:r>
</w:del>

<!-- 把"30 天"改成"60 天"——minimal edit -->
<w:r><w:t>The term is </w:t></w:r>
<w:del w:id="1" w:author="Claude" w:date="...">
  <w:r><w:delText>30</w:delText></w:r>
</w:del>
<w:ins w:id="2" w:author="Claude" w:date="...">
  <w:r><w:t>60</w:t></w:r>
</w:ins>
<w:r><w:t> days.</w:t></w:r>
```

### 批注（Comments）的 XML 模板

需要同时改 5 个 part：`word/document.xml`（marker）、`word/comments.xml`、`word/commentsExtended.xml`、`word/commentsExtensible.xml`、`word/commentsIds.xml`、`word/people.xml`（author 注册）。

简化版（只加普通批注，不带回复）：

```xml
<!-- document.xml 里被批注的段落 -->
<w:p>
  <w:commentRangeStart w:id="0"/>
  <w:r><w:t>被批注的文字</w:t></w:r>
  <w:commentRangeEnd w:id="0"/>
  <w:r>
    <w:rPr><w:rStyle w:val="CommentReference"/></w:rPr>
    <w:commentReference w:id="0"/>
  </w:r>
</w:p>

<!-- word/comments.xml -->
<w:comments xmlns:w="...">
  <w:comment w:id="0" w:author="Claude" w:date="2026-05-20T00:00:00Z" w:initials="C">
    <w:p><w:r><w:t>这里需要补充数据来源</w:t></w:r></w:p>
  </w:comment>
</w:comments>
```

**`<w:commentRangeStart>` / `<w:commentRangeEnd>` 必须是 `<w:p>` 的直接子元素，不能放 `<w:r>` 里。** 这是 OOXML 强约束。

---

## 智能引号 / 中文标点

Word 自动把直引号 `"` 转成弯引号 `"` / `"`，apostrophe `'` 转成 `'`。XML 里建议用实体：

| 实体 | 字符 |
|---|---|
| `&#x2018;` | `'` |
| `&#x2019;` | `'` / 也是中文撇号 |
| `&#x201C;` | `"` |
| `&#x201D;` | `"` |

直接写字符也行，但实体形式更稳——某些 XML 处理库会把字符转回 `&#xNNNN;` 形式，写实体可以避免 diff 噪声。

---

## 何时把需求"提"成 op

如果你发现某个 raw XML 操作**反复出现**（比如修订模式相关的批量替换），考虑往 `word-cli edit` 加一个新 op：

1. 在 `scripts/engine/editor.py` 的 `_op_*` 系列里加新函数
2. 在 `_OP_REGISTRY` dict 里注册
3. 在 `apply_edits` 函数的 docstring + `apply-edits-ops.md` 里写说明

走 op 比走 raw XML：
- 用 Python AST 而不是字符串拼接，少出错
- LLM 调用时只写 op kwargs，token 省
- 可复用——下次有人需要相同操作直接调

近期可能值得加的 op：
- `add_tracked_insertion` / `add_tracked_deletion` / `accept_changes` / `reject_changes`
- `add_comment` / `delete_comment` / `reply_comment`
- `add_footnote` / `add_bookmark` / `add_internal_link`

.NET 层（`MiniMaxAIDocx.Core.OpenXml.TrackChangesHelper.cs` / `CommentSynchronizer.cs`）已经写了底层实现，只是 Python `apply_edits` 还没把它们暴露成 op。
